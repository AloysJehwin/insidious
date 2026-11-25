"""Client for the Claude ops agent (service `agent`, port 8004). See docs/CONTRACTS.md "v2 contracts".

- POST /api/plans/{id}/decision {option, channel, actor, comments} -> {status: done|failed|rejected, results: [...]}
- POST /api/ask {question, language} -> {answer_en}
Every POST carries X-Insidious-Token (and the legacy X-FestiveKit-Token).
"""
from __future__ import annotations

from urllib.parse import quote

import httpx

from .config import Settings
from .log import log

CHANNELS = ("whatsapp-button", "whatsapp-voice", "cli")


class AgentError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, timeout: bool = False):
        super().__init__(message)
        self.status = status
        self.timeout = timeout

    @property
    def retryable(self) -> bool:
        """Nothing reached the agent (or it had a server error): safe to let the founder try again."""
        return self.status is None or self.status >= 500 or self.status in (408, 429)


class AgentClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.calls: list[dict] = []  # journal for debugging / tests

    @property
    def configured(self) -> bool:
        return bool(self.s.agent_url)

    def _headers(self) -> dict:
        # Both headers while receivers migrate from X-FestiveKit-Token to X-Insidious-Token.
        tok = self.s.internal_token
        return {"X-Insidious-Token": tok, "X-FestiveKit-Token": tok} if tok else {}

    async def _post(self, path: str, body: dict, timeout: float) -> dict:
        if not self.configured:
            raise AgentError("AGENT_URL not set")
        self.calls.append({"path": path, "body": body})
        url = f"{self.s.agent_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(url, json=body, headers=self._headers())
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            log("agent_unreachable", path=path, error=str(e)[:200])
            raise AgentError(f"unreachable: {e}") from e
        except httpx.TimeoutException as e:
            # The request reached the agent; it may still be executing.
            log("agent_timeout", path=path, timeout_s=timeout)
            raise AgentError(f"timeout after {timeout}s", timeout=True) from e
        except Exception as e:  # noqa: BLE001
            log("agent_request_failed", path=path, error=str(e)[:200])
            raise AgentError(f"request failed: {e}") from e
        if r.status_code >= 400:
            log("agent_http_error", path=path, status=r.status_code, response=r.text[:300])
            raise AgentError(f"HTTP {r.status_code}: {r.text[:200]}", status=r.status_code)
        try:
            data = r.json()
        except ValueError as e:
            raise AgentError("agent returned non-JSON", status=r.status_code) from e
        return data if isinstance(data, dict) else {"data": data}

    async def incident(self, incident: dict, source: str = "founder") -> dict:
        """Ask the agent to plan something the founder asked for (e.g. go back to normal mode)."""
        return await self._post("/api/incident", {"source": source, "incident": incident}, 15)

    async def decision(self, plan_id: str, option: str, channel: str, comments: str) -> dict:
        body = {"option": option, "channel": channel, "actor": "founder", "comments": comments[:1000]}
        path = f"/api/plans/{quote(plan_id, safe='')}/decision"
        try:
            res = await self._post(path, body, self.s.agent_timeout_s)
        except AgentError as e:
            # "whatsapp-text" is not in the contract's channel list; if the agent validates strictly, resend.
            if e.status == 422 and channel not in CHANNELS:
                res = await self._post(path, {**body, "channel": "whatsapp-voice"}, self.s.agent_timeout_s)
            else:
                raise
        log("agent_decision", plan_id=plan_id, option=option, channel=channel, status=res.get("status"))
        return res

    async def ask(self, question: str, language: str) -> str:
        res = await self._post("/api/ask", {"question": question[:2000], "language": language},
                               self.s.agent_ask_timeout_s)
        answer = str(res.get("answer_en") or res.get("answer") or "").strip()
        if not answer:
            raise AgentError("empty answer")
        log("agent_answer", chars=len(answer))
        return answer
