"""NudgeBee GraphQL client. Shapes follow NudgeBee's nbctl CLI source (not public docs).

Errors come back as HTTP 200 with an `errors` array, so every call checks for it.
NUDGEBEE_DRY_RUN=1 logs the call and returns fake success.
"""
from __future__ import annotations

import uuid

import httpx

from .config import Settings
from .log import log


class NudgeBeeError(RuntimeError):
    pass


Q_COMPLETE_APPROVAL = """
mutation CompleteWorkflowApproval($request: WorkflowCompleteApprovalRequest!) {
  workflow_complete_approval(request: $request) { status message }
}"""

Q_GET_EXECUTION = """
query getWorkflowExecution($request: WorkflowExecutionGetRequest!) {
  workflow_get_execution(request: $request) {
    id status start_time close_time inputs workflow_result error
    tasks { id type status input output error attempt children }
  }
}"""

Q_LIST_EXECUTIONS = """
query listWorkflowExecutions($accountId: String!, $id: String!, $limit: Int, $status: String) {
  workflow_list_executions(request: {account_id: $accountId, id: $id, limit: $limit, status: $status}) {
    next_page_token
    executions { id workflow_id status trigger_type triggered_by start_time close_time }
  }
}"""

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


class NudgeBeeClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.calls: list[dict] = []  # dry-run journal
        self._dry_polls: dict[str, int] = {}

    @property
    def dry(self) -> bool:
        return self.s.nudgebee_dry_run

    async def gql(self, op: str, query: str, variables: dict) -> dict:
        if self.dry:
            self.calls.append({"op": op, "variables": variables})
            log("nb_dry_run", op=op, variables=variables)
            return {}
        url = f"{self.s.nudgebee_url}/api/graphql"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(url, json={"query": query, "variables": variables},
                             headers={"Authorization": f"Bearer {self.s.nudgebee_api_token}"})
        if r.status_code >= 400:
            log("nb_http_error", op=op, status=r.status_code, response=r.text[:500])
            raise NudgeBeeError(f"{op}: HTTP {r.status_code}")
        body = r.json()
        if body.get("errors"):
            log("nb_graphql_errors", op=op, errors=body["errors"])
            raise NudgeBeeError(f"{op}: {body['errors']}")
        return body.get("data") or {}

    async def workflow_complete_approval(self, execution_id: str, task_id: str, status: str, comments: str) -> dict:
        req = {"execution_id": execution_id, "task_id": task_id, "status": status}
        if comments:
            req["comments"] = comments[:1000]
        data = await self.gql("workflow_complete_approval", Q_COMPLETE_APPROVAL, {"request": req})
        if self.dry:
            return {"status": "success", "message": f"dry-run {status}"}
        return data.get("workflow_complete_approval") or {}

    async def workflow_get_execution(self, execution_id: str, workflow_id: str | None = None) -> dict:
        req = {"account_id": self.s.nudgebee_account_id, "id": execution_id}
        if workflow_id:
            req["workflow_id"] = workflow_id
        data = await self.gql("workflow_get_execution", Q_GET_EXECUTION, {"request": req})
        if self.dry:
            return {"id": execution_id, "status": "RUNNING",
                    "tasks": [{"id": "dry-approval-task", "type": "core.approval", "status": "WAITING"}]}
        return data.get("workflow_get_execution") or {}

    async def workflow_list_executions(self, workflow_id: str, limit: int = 5, status: str | None = None) -> list[dict]:
        """Defensive: the response shape is from nbctl and unverified against the live API."""
        variables = {"accountId": self.s.nudgebee_account_id, "id": workflow_id, "limit": limit}
        if status:
            variables["status"] = status
        data = await self.gql("workflow_list_executions", Q_LIST_EXECUTIONS, variables)
        if self.dry:
            return [{"id": "dry-execution", "workflow_id": workflow_id, "status": "RUNNING"}]
        res = data.get("workflow_list_executions")
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            for key in ("executions", "rows", "items", "data"):
                if isinstance(res.get(key), list):
                    return res[key]
        return []

    async def workflow_execute(self, workflow_id: str, inputs: dict | None = None) -> dict:
        req = {"account_id": self.s.nudgebee_account_id, "id": workflow_id, "inputs": inputs or {}}
        data = await self.gql("workflow_execute", Q_EXECUTE, {"request": req})
        if self.dry:
            return {"id": workflow_id, "execution_id": f"dry-{uuid.uuid4().hex[:8]}"}
        return data.get("workflow_execute") or {}

    async def ai_execute_investigation(self, query: str, session_id: str | None = None) -> str:
        session_id = session_id or str(uuid.uuid4())
        await self.gql("ai_execute_investigation", Q_AI_INVESTIGATE,
                       {"accountId": self.s.nudgebee_account_id, "query": query[:2000], "sessionId": session_id})
        return session_id

    async def ai_get_conversation_v3(self, session_id: str) -> dict:
        data = await self.gql("ai_get_conversation_v3", Q_AI_CONVERSATION,
                              {"accountId": self.s.nudgebee_account_id, "sessionId": session_id})
        if self.dry:
            n = self._dry_polls[session_id] = self._dry_polls.get(session_id, 0) + 1
            tools = [{"tool_name": "k8s_get_pods", "parameters": {"namespace": "store"}},
                     {"tool_name": "prometheus_query", "parameters": {"query": "p95 /checkout"}}][: min(n, 2)]
            done = n >= 3
            return {
                "conversation": {"id": "dry-conv", "status": "COMPLETED" if done else "IN_PROGRESS"},
                "messages": [{"message_type": "generation", "status": "COMPLETED" if done else "IN_PROGRESS",
                              "response": ("Checkout is slow because the recommendations feature is using most of "
                                           "the CPU on the single store-api pod." if done else "")}],
                "agents": [], "tool_calls": tools,
            }
        return data.get("ai_get_conversation_v3") or {}


def find_waiting_approval(execution: dict) -> str | None:
    """Return the id of a core.approval task that is waiting for a decision, if any."""
    tasks = [t for t in execution.get("tasks") or [] if "approval" in str(t.get("type", "")).lower()]
    for wanted in (("WAITING", "WAITING_FOR_APPROVAL", "PENDING"), ("RUNNING", "IN_PROGRESS", "SCHEDULED")):
        for t in tasks:
            if str(t.get("status", "")).upper() in wanted:
                return t.get("id")
    return None


def conversation_answer(conv: dict) -> tuple[bool, str]:
    """(done, answer_text) from an ai_get_conversation_v3 payload."""
    status = str((conv.get("conversation") or {}).get("status", "")).upper()
    msgs = [m for m in conv.get("messages") or [] if m.get("message_type") in ("generation", "followup", None)]
    answer = ""
    for m in reversed(msgs):
        if m.get("response"):
            answer = str(m["response"])
            break
    if not answer:
        for a in reversed(conv.get("agents") or []):
            if a.get("response"):
                answer = str(a["response"])
                break
    in_progress = status in ("", "IN_PROGRESS", "RUNNING", "PENDING", "QUEUED", "STARTED")
    failed = status in ("FAILED", "ERROR", "CANCELLED", "CANCELED", "TIMEOUT")
    return (failed or (not in_progress and bool(answer)) or (status == "WAITING" and bool(answer))), answer
