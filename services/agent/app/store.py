"""Plans persisted as one JSON file each under DATA_DIR/plans/ (survives restarts)."""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .log import log, utcnow_iso
from .models import Plan

ACTIVE = ("planning", "pending_approval", "executing")


def new_plan_id() -> str:
    return f"plan-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class PlanStore:
    def __init__(self, data_dir: Path | str):
        self.dir = Path(data_dir) / "plans"
        self.plans: dict[str, Plan] = {}
        self.persist = True
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log("plan_store_memory_only", error=str(e)[:200])
            self.persist = False
        self._load()

    def _load(self) -> None:
        if not self.persist:
            return
        for p in sorted(self.dir.glob("plan-*.json")):
            try:
                plan = Plan.model_validate(json.loads(p.read_text()))
            except Exception as e:  # noqa: BLE001
                log("plan_load_failed", file=p.name, error=str(e)[:200])
                continue
            if plan.status in ("planning", "executing"):  # the background task died with the old process
                plan = plan.model_copy(update={"status": "failed", "error": "agent restarted while " + plan.status})
            self.plans[plan.id] = plan
        if self.plans:
            log("plans_loaded", count=len(self.plans))

    def save(self, plan: Plan) -> Plan:
        plan = plan.model_copy(update={"updated_at": utcnow_iso()})
        self.plans[plan.id] = plan
        if self.persist:
            try:
                tmp = self.dir / f".{plan.id}.json.tmp"
                tmp.write_text(json.dumps(plan.model_dump(), ensure_ascii=False, indent=1, default=str))
                os.replace(tmp, self.dir / f"{plan.id}.json")
            except OSError as e:
                log("plan_save_failed", plan_id=plan.id, error=str(e)[:200])
        return plan

    def get(self, plan_id: str) -> Plan | None:
        return self.plans.get(plan_id)

    def all(self) -> list[Plan]:
        return sorted(self.plans.values(), key=lambda p: (p.created_at, p.id), reverse=True)

    def active_for(self, incident_id: str) -> Plan | None:
        for p in self.all():
            if p.incident_id == incident_id and p.status in ACTIVE:
                return p
        return None

    def any_active(self) -> Plan | None:
        """One surge usually breaches several SLOs at once (checkout, home, db). The founder should get ONE
        plan for it, so any plan still being planned / awaiting approval / executing absorbs new incidents."""
        for p in self.all():
            if p.status in ACTIVE:
                return p
        return None

    def latest(self) -> Plan | None:
        plans = self.all()
        return plans[0] if plans else None
