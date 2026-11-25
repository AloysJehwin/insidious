"""Tiny in-cluster Kubernetes API client over plain httpx (service account token + CA)."""
from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import Any

import httpx

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class K8sError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class K8sClient:
    def __init__(self, enabled: bool = True, sa_dir: Path = SA_DIR, timeout: float = 5.0,
                 base_url: str | None = None, token: str | None = None):
        self.sa_dir = sa_dir
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._token = token
        if base_url:  # tests / explicit config
            self.base_url = base_url.rstrip("/")
            self.available = enabled
        else:
            host = os.getenv("KUBERNETES_SERVICE_HOST", "")
            port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            self.base_url = f"https://{host}:{port}" if host else "https://kubernetes.default.svc"
            self.available = enabled and bool(host) and (sa_dir / "token").is_file()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            ca = self.sa_dir / "ca.crt"
            verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=str(ca)) if ca.is_file() else True
            self._client = httpx.AsyncClient(timeout=self.timeout, verify=verify)
        return self._client

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        token = self._token if self._token is not None else (self.sa_dir / "token").read_text().strip()
        h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if content_type:
            h["Content-Type"] = content_type
        return h

    async def request(self, method: str, path: str, *, params: dict | None = None, json_body: Any = None,
                      content_type: str | None = None, text: bool = False) -> Any:
        if not self.available:
            raise K8sError("not running in a Kubernetes cluster")
        r = await self._http().request(method, f"{self.base_url}{path}", params=params, json=json_body,
                                       headers=self._headers(content_type))
        if r.status_code >= 400:
            msg = r.text[:300]
            try:
                msg = (r.json() or {}).get("message") or msg
            except ValueError:
                pass
            raise K8sError(f"{method} {path} -> HTTP {r.status_code}: {msg}", r.status_code)
        if text:
            return r.text
        return r.json() if r.content else {}

    # ------------------------------------------------------------ reads
    async def list_deployments(self, ns: str) -> list[dict]:
        return (await self.request("GET", f"/apis/apps/v1/namespaces/{ns}/deployments")).get("items") or []

    async def get_deployment(self, ns: str, name: str) -> dict:
        return await self.request("GET", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")

    async def list_pods(self, ns: str, label_selector: str | None = None) -> list[dict]:
        params = {"labelSelector": label_selector} if label_selector else None
        return (await self.request("GET", f"/api/v1/namespaces/{ns}/pods", params=params)).get("items") or []

    async def pod_metrics(self, ns: str) -> list[dict]:
        return (await self.request("GET", f"/apis/metrics.k8s.io/v1beta1/namespaces/{ns}/pods")).get("items") or []

    async def pod_log(self, ns: str, pod: str, container: str | None = None, tail_lines: int = 200) -> str:
        params: dict[str, Any] = {"tailLines": tail_lines}
        if container:
            params["container"] = container
        return await self.request("GET", f"/api/v1/namespaces/{ns}/pods/{pod}/log", params=params, text=True)

    # ------------------------------------------------------------ writes
    async def scale(self, ns: str, name: str, replicas: int) -> dict:
        return await self.request("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}/scale",
                                  json_body={"spec": {"replicas": int(replicas)}},
                                  content_type="application/merge-patch+json")

    async def set_resources(self, ns: str, name: str, container: str, limits: dict, requests: dict | None = None) -> dict:
        resources: dict[str, Any] = {"limits": limits}
        if requests:
            resources["requests"] = requests
        body = {"spec": {"template": {"spec": {"containers": [{"name": container, "resources": resources}]}}}}
        return await self.request("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
                                  json_body=body, content_type="application/strategic-merge-patch+json")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
