"""store-api replica count: in-cluster Kubernetes API first, Prometheus `up` fallback, else None."""
from __future__ import annotations

import os
import ssl
from pathlib import Path

import httpx

from .log import log
from .prom import REPLICAS_QUERY, PromClient, PromError

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class ReplicaReader:
    def __init__(self, namespace: str, deployment: str, prom: PromClient, k8s_enabled: bool = True,
                 sa_dir: Path = SA_DIR, timeout: float = 3.0):
        self.namespace = namespace
        self.deployment = deployment
        self.prom = prom
        self.sa_dir = sa_dir
        self.timeout = timeout
        self._k8s_client: httpx.AsyncClient | None = None
        self.in_cluster = (
            k8s_enabled and bool(os.getenv("KUBERNETES_SERVICE_HOST")) and (sa_dir / "token").is_file()
        )
        self._warned = False

    def _client(self) -> httpx.AsyncClient:
        if self._k8s_client is None:
            ca = self.sa_dir / "ca.crt"
            verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=str(ca)) if ca.is_file() else True
            self._k8s_client = httpx.AsyncClient(timeout=self.timeout, verify=verify)
        return self._k8s_client

    async def _from_k8s(self) -> int | None:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        token = (self.sa_dir / "token").read_text().strip()
        url = f"https://{host}:{port}/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.deployment}"
        r = await self._client().get(url, headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            raise RuntimeError(f"k8s HTTP {r.status_code}")
        status = (r.json() or {}).get("status") or {}
        return int(status.get("availableReplicas") or 0)

    async def read(self) -> int | None:
        if self.in_cluster:
            try:
                return await self._from_k8s()
            except Exception as e:  # noqa: BLE001
                if not self._warned:
                    log("replicas_k8s_failed", error=str(e)[:200])
                    self._warned = True
        try:
            v = await self.prom.scalar(REPLICAS_QUERY)
            return int(v) if v is not None else None
        except PromError:
            return None

    async def aclose(self) -> None:
        if self._k8s_client is not None:
            await self._k8s_client.aclose()
