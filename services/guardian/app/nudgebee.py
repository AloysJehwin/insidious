"""NudgeBee clients: the Event Webhook (firing/resolved) and a small GraphQL client.

The GraphQL shapes come from nbctl's source (see CONTRACTS.md) and are not public. `workflow_list_executions`
is unknown, so we try a few candidate shapes and remember the first one that works. Every failure is logged
with the raw `errors` array and never raises into the guardian loop.
"""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .config import is_placeholder
from .log import log


def gql_str(value: str) -> str:
    """Encode a string as a GraphQL string literal (JSON escaping is valid GraphQL escaping)."""
    return json.dumps(str(value))


class EventWebhookClient:
    def __init__(self, url: str, client: httpx.AsyncClient, dry_run: bool):
        self.url = url
        self.client = client
        self.dry_run = dry_run or is_placeholder(url)

    def _headers(self) -> dict[str, str]:
        try:
            token = (parse_qs(urlparse(self.url).query).get("token") or [""])[0]
        except Exception:  # noqa: BLE001
            token = ""
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Returns {"sent": bool, "dry_run": bool, "status": int|None, "error": str|None}."""
        if self.dry_run:
            log("nudgebee_event_dry_run", payload=payload)
            return {"sent": False, "dry_run": True, "status": None, "error": None}
        try:
            r = await self.client.post(self.url, json=payload, headers=self._headers())
            ok = 200 <= r.status_code < 300
            log("nudgebee_event_sent" if ok else "nudgebee_event_failed",
                id=payload.get("id"), status=payload.get("status"), http_status=r.status_code,
                body=None if ok else r.text[:300])
            return {"sent": ok, "dry_run": False, "status": r.status_code, "error": None if ok else r.text[:300]}
        except httpx.HTTPError as e:
            log("nudgebee_event_error", id=payload.get("id"), error=f"{type(e).__name__}: {e}")
            return {"sent": False, "dry_run": False, "status": None, "error": str(e)}


class NudgeBeeGraphQL:
    # (arguments, selection) candidates for workflow_list_executions; first one without errors wins.
    LIST_ARG_VARIANTS = (
        "account_id: {acc}, workflow_id: {wf}, limit: 5",
        "account_id: {acc}, id: {wf}, limit: 5",
        "account_id: {acc}, workflow_id: {wf}",
    )
    LIST_SELECTIONS = (
        "rows { id execution_id status start_time }",
        "id execution_id status start_time",
        "executions { id execution_id status start_time }",
        "id status start_time",
    )

    def __init__(self, base_url: str, token: str, account_id: str, client: httpx.AsyncClient, dry_run: bool):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.account_id = account_id
        self.client = client
        self.dry_run = dry_run
        self._list_shape: tuple[str, str] | None = None
        self._list_unavailable_until = 0.0

    @property
    def configured(self) -> bool:
        return (not self.dry_run and bool(self.base_url) and not is_placeholder(self.token)
                and not is_placeholder(self.account_id))

    async def query(self, query: str, variables: dict | None = None) -> dict | None:
        """POST a GraphQL document. Returns `data` or None (errors logged)."""
        if not self.configured:
            return None
        try:
            r = await self.client.post(
                f"{self.base_url}/api/graphql",
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": f"Bearer {self.token}"},
            )
        except httpx.HTTPError as e:
            log("nudgebee_gql_http_error", error=f"{type(e).__name__}: {e}")
            return None
        try:
            body = r.json()
        except ValueError:
            log("nudgebee_gql_bad_response", http_status=r.status_code, body=r.text[:300])
            return None
        if body.get("errors"):
            log("nudgebee_gql_errors", errors=body["errors"], op=query.split("(")[0][:60])
            return None
        if r.status_code >= 400:
            log("nudgebee_gql_http_status", http_status=r.status_code)
            return None
        return body.get("data")

    async def latest_execution_id(self, workflow_id: str) -> str | None:
        if not self.configured or not workflow_id or time.monotonic() < self._list_unavailable_until:
            return None
        acc, wf = gql_str(self.account_id), gql_str(workflow_id)
        shapes = [self._list_shape] if self._list_shape else [
            (a, s) for a in self.LIST_ARG_VARIANTS for s in self.LIST_SELECTIONS
        ]
        for args, sel in shapes:
            q = "query { workflow_list_executions(request: {%s}) { %s } }" % (args.format(acc=acc, wf=wf), sel)
            data = await self.query(q)
            if data is None:
                continue
            self._list_shape = (args, sel)
            return _pick_latest_id(data.get("workflow_list_executions"))
        self._list_shape = None
        self._list_unavailable_until = time.monotonic() + 60  # back off a minute before retrying all shapes
        log("nudgebee_list_executions_unavailable")
        return None

    async def get_execution(self, workflow_id: str, execution_id: str) -> dict | None:
        q = (
            "query { workflow_get_execution(request: {account_id: %s, id: %s, workflow_id: %s}) "
            "{ id status start_time close_time error tasks { id type status } } }"
            % (gql_str(self.account_id), gql_str(execution_id), gql_str(workflow_id))
        )
        data = await self.query(q)
        if not data:
            return None
        ex = data.get("workflow_get_execution")
        if isinstance(ex, list):
            ex = ex[0] if ex else None
        return ex if isinstance(ex, dict) else None


def _pick_latest_id(node: Any) -> str | None:
    """Find execution rows in an unknown shape and return the newest one's execution id."""
    rows: list[dict] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if any(k in n for k in ("execution_id", "id")) and any(k in n for k in ("status", "start_time", "execution_id")):
                rows.append(n)
                return
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    if not rows:
        return None
    rows.sort(key=lambda r: str(r.get("start_time") or ""), reverse=True)
    top = rows[0]
    return str(top.get("execution_id") or top.get("id"))
