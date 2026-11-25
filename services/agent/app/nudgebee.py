"""Minimal NudgeBee GraphQL client (shapes from nbctl, see CONTRACTS.md). Errors arrive as HTTP 200 + `errors`."""
from __future__ import annotations

import asyncio
import time
import uuid

import httpx

from .config import Settings
from .log import log

Q_EXECUTE = """
mutation triggerWorkflow($request: WorkflowTriggerRequest!) {
  workflow_execute(request: $request) { id execution_id }
}"""

Q_AI_INVESTIGATE = """
mutation AiTriggerInvestigateResponse($accountId: String!, $query: String!, $sessionId: String!) {
  ai_execute_investigation(request: {account_id: $accountId, query: $query, session_id: $sessionId, async: true}) {
    data { agent_step_response response query chain_name }
  }
}"""

Q_AI_CONVERSATION = """
query GetLlmConversation($accountId: String!, $sessionId: String) {
  ai_get_conversation_v3(request: {account_id: $accountId, session_id: $sessionId}) {
    conversation { id status }
    messages { id status response message_type }
    agents { id message_id agent_name status response }
    tool_calls { agent_id tool_name parameters }
  }
}"""


class NudgeBeeError(RuntimeError):
    pass


class NudgeBeeClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.s = settings
        self.client = client

    @property
    def configured(self) -> bool:
        return bool(self.s.nudgebee_url and self.s.nudgebee_token and self.s.nudgebee_account_id)

    @property
    def dry(self) -> bool:
        return self.s.nudgebee_dry_run or self.s.dry_run or not self.configured

    async def gql(self, op: str, query: str, variables: dict, timeout: float = 30.0) -> dict:
        r = await self.client.post(f"{self.s.nudgebee_url}/api/graphql", json={"query": query, "variables": variables},
                                   headers={"Authorization": f"Bearer {self.s.nudgebee_token}"}, timeout=timeout)
        if r.status_code >= 400:
            log("nb_http_error", op=op, status=r.status_code, response=r.text[:300])
            raise NudgeBeeError(f"{op}: HTTP {r.status_code}")
        body = r.json()
        if body.get("errors"):
            log("nb_graphql_errors", op=op, errors=body["errors"])
            raise NudgeBeeError(f"{op}: {str(body['errors'])[:300]}")
        return body.get("data") or {}

    async def workflow_execute(self, workflow_id: str, inputs: dict | None = None) -> dict:
        req = {"account_id": self.s.nudgebee_account_id, "id": workflow_id, "inputs": inputs or {}}
        data = await self.gql("workflow_execute", Q_EXECUTE, {"request": req})
        return data.get("workflow_execute") or {}

    async def investigate(self, query: str, timeout_s: float = 60.0, poll_s: float = 5.0) -> dict:
        """Start an async AI investigation and poll for the answer (bounded). Raises on error."""
        session_id = str(uuid.uuid4())
        await self.gql("ai_execute_investigation", Q_AI_INVESTIGATE,
                       {"accountId": self.s.nudgebee_account_id, "query": query[:2000], "sessionId": session_id})
        deadline = time.monotonic() + timeout_s
        answer, status = "", ""
        while time.monotonic() < deadline:
            await asyncio.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
            data = await self.gql("ai_get_conversation_v3", Q_AI_CONVERSATION,
                                  {"accountId": self.s.nudgebee_account_id, "sessionId": session_id}, timeout=15)
            conv = data.get("ai_get_conversation_v3") or {}
            status = str((conv.get("conversation") or {}).get("status", "")).upper()
            for m in reversed(conv.get("messages") or []):
                if m.get("response"):
                    answer = str(m["response"])
                    break
            if status in ("COMPLETED", "DONE", "SUCCESS", "FAILED", "ERROR", "CANCELLED", "WAITING") and answer:
                break
            if status in ("FAILED", "ERROR", "CANCELLED"):
                break
        return {"session_id": session_id, "status": status or "TIMEOUT", "answer": answer[:3000]}
