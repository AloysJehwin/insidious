"""Small NudgeBee GraphQL client for the CLI (shapes from CONTRACTS.md / nbctl source).

String values are inlined as JSON-escaped GraphQL literals because the server's input type names are not
public (a wrong variable type would fail the whole request). Errors arrive as HTTP 200 with `errors`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from .settings import is_placeholder

TOKEN_CHECK_QUERY = "{ accounts_list(limit: 1) { rows { id account_name } } }"


def gql_literal(value: Any) -> str:
    """Python value -> GraphQL input literal (objects use unquoted keys)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {gql_literal(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(gql_literal(v) for v in value) + "]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(str(value))


@dataclass
class GqlResult:
    ok: bool
    data: Any = None
    errors: Any = None
    detail: str = ""


class NudgeBeeClient:
    def __init__(self, url: str | None, token: str | None, account_id: str | None, dry_run: bool = False,
                 client: httpx.Client | None = None, timeout: float = 15.0):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.account_id = account_id or ""
        self.dry_run = dry_run
        self.client = client or httpx.Client(timeout=timeout)

    @property
    def endpoint(self) -> str:
        return f"{self.url or '<NUDGEBEE_URL>'}/api/graphql"

    @property
    def has_token(self) -> bool:
        return bool(self.url) and not is_placeholder(self.token)

    def gql(self, query: str, variables: dict | None = None) -> GqlResult:
        if self.dry_run:
            return GqlResult(False, detail="dry-run")
        if not self.has_token:
            return GqlResult(False, detail="NUDGEBEE_URL / NUDGEBEE_API_TOKEN not configured")
        try:
            r = self.client.post(self.endpoint, json={"query": query, "variables": variables or {}},
                                 headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as e:
            return GqlResult(False, detail=f"{type(e).__name__}: {e}")
        try:
            body = r.json()
        except ValueError:
            return GqlResult(False, detail=f"HTTP {r.status_code}: non-JSON response")
        if body.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in body["errors"] if isinstance(e, dict)) or str(body["errors"])
            return GqlResult(False, body.get("data"), body["errors"], msg[:300])
        if r.status_code >= 400:
            return GqlResult(False, detail=f"HTTP {r.status_code}")
        return GqlResult(True, body.get("data"))

    def check_token(self) -> GqlResult:
        res = self.gql(TOKEN_CHECK_QUERY)
        if res.ok:
            rows = ((res.data or {}).get("accounts_list") or {}).get("rows") or []
            res.detail = f"token ok ({len(rows)} account visible)" if rows else "token ok"
        return res

    def workflow_execute_query(self, workflow_id: str, inputs: dict[str, Any]) -> str:
        req = {"account_id": self.account_id, "id": workflow_id, "inputs": inputs}
        return "mutation { workflow_execute(request: %s) { id execution_id } }" % gql_literal(req)

    def workflow_execute(self, workflow_id: str, inputs: dict[str, Any]) -> GqlResult:
        res = self.gql(self.workflow_execute_query(workflow_id, inputs))
        if res.ok:
            ex = (res.data or {}).get("workflow_execute") or {}
            res.detail = f"execution {ex.get('execution_id') or ex.get('id') or '?'}"
        return res

    def close(self) -> None:
        self.client.close()
