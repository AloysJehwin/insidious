"""Plan / request models (CONTRACTS.md v2)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

OptionKey = Literal["recommended", "alt1", "alt2", "reject"]
PlanStatus = Literal["planning", "pending_approval", "executing", "done", "rejected", "failed"]
MAX_OPTIONS = 3
TITLE_MAX = 40
SUMMARY_MAX = 300


def clip(text: Any, limit: int) -> str:
    """Trim to `limit` chars at a word boundary (soft fix for slightly long model output)."""
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut or s[:limit]


class Action(BaseModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class Option(BaseModel):
    key: OptionKey
    title_en: str
    impact_en: str = ""
    cost_en: str = ""
    actions: list[Action] = Field(default_factory=list)

    @field_validator("title_en", mode="before")
    @classmethod
    def _title(cls, v: Any) -> str:
        s = clip(v, TITLE_MAX)
        if not s:
            raise ValueError("title_en must not be empty")
        return s


class PlanDraft(BaseModel):
    """What a planner (Claude or rules) produces, before it becomes a stored Plan."""

    founder_summary_en: str
    technical_summary: str = ""
    options: list[Option]

    @field_validator("founder_summary_en", mode="before")
    @classmethod
    def _summary(cls, v: Any) -> str:
        s = clip(v, SUMMARY_MAX)
        if not s:
            raise ValueError("founder_summary_en must not be empty")
        return s

    @model_validator(mode="after")
    def _shape(self) -> "PlanDraft":
        opts = self.options
        if not opts or len(opts) > MAX_OPTIONS:
            raise ValueError(f"need 1..{MAX_OPTIONS} options, got {len(opts)}")
        if opts[-1].key != "reject":
            raise ValueError("the last option must have key 'reject'")
        if opts[-1].actions:
            raise ValueError("the 'reject' option must have no actions")
        keys = [o.key for o in opts]
        if len(set(keys)) != len(keys):
            raise ValueError(f"option keys must be unique, got {keys}")
        if "reject" in keys[:-1]:
            raise ValueError("'reject' may only be the last option")
        if len(opts) > 1 and opts[0].key != "recommended":
            raise ValueError("the first option must be 'recommended'")
        for o in opts[:-1]:
            if not o.actions:
                raise ValueError(f"option {o.key!r} has no actions")
        return self


class Plan(PlanDraft):
    id: str
    created_at: str
    updated_at: str = ""
    incident_id: str = ""
    status: PlanStatus = "pending_approval"
    planner: str = "rules"  # claude | rules | rules-fallback
    model: str | None = None
    incident_kind: str = ""  # cpu | db
    numbers: dict[str, Any] = Field(default_factory=dict)
    results: list[dict[str, Any]] = Field(default_factory=list)
    decision: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    error: str | None = None

    def option(self, key: str) -> Option | None:
        return next((o for o in self.options if o.key == key), None)


class IncidentRequest(BaseModel):
    source: str = "guardian"
    incident: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)


class DecisionRequest(BaseModel):
    option: OptionKey
    channel: str = "cli"
    actor: str = "founder"
    comments: str = ""


class AskRequest(BaseModel):
    question: str
    language: str = "en-IN"
