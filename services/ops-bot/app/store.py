"""Pending approvals + founder state, in memory and mirrored to a JSON file under DATA_DIR."""
from __future__ import annotations

import json
import os
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from .log import log


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Store:
    def __init__(self, data_dir: str, default_language: str):
        self.path = os.path.join(data_dir, "ops-bot-state.json")
        self.lock = threading.Lock()
        self.approvals: dict[str, dict] = {}
        # style: None (use Flagsmith/env), "native" or "tanglish". switched: set by an explicit switch message
        # ("tanglish", "hindi", ...); while True, detection no longer changes language/style.
        self.founder: dict = {"language": default_language, "language_probability": None, "style": None,
                              "switched": False}
        self.seen: OrderedDict[str, None] = OrderedDict()
        os.makedirs(data_dir, exist_ok=True)
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.approvals = data.get("approvals", {}) or {}
            self.founder.update(data.get("founder", {}) or {})
            for mid in data.get("seen", [])[-500:]:
                self.seen[mid] = None
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log("state_load_failed", error=str(e))

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"approvals": self.approvals, "founder": self.founder, "seen": list(self.seen)[-500:]},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            log("state_save_failed", error=str(e))

    # ----- dedupe -----
    def seen_before(self, msg_id: str) -> bool:
        """True if this message id was already processed; otherwise records it (keeps the last 500)."""
        if not msg_id:
            return False
        with self.lock:
            if msg_id in self.seen:
                return True
            self.seen[msg_id] = None
            while len(self.seen) > 500:
                self.seen.popitem(last=False)
            self._save()
            return False

    # ----- founder language -----
    @property
    def language(self) -> str:
        return self.founder.get("language") or "ta-IN"

    @property
    def style(self) -> str | None:
        return self.founder.get("style")

    def set_language(self, code: str | None, prob: float | None = None, style: str | None = None) -> bool:
        """Detected language (and style). Ignored after an explicit switch message. Any detected language other
        than Tamil resets the style to native; a Tamil one keeps the current style unless `style` is given."""
        if not code or code == "unknown":
            return False
        with self.lock:
            if self.founder.get("switched"):
                return False
            self.founder["language"] = code
            self.founder["language_probability"] = prob
            if style:
                self.founder["style"] = style
            elif code != "ta-IN":
                self.founder["style"] = "native"
            self._save()
            return True

    def switch(self, code: str, style: str) -> None:
        """The founder asked for a language/style ("tanglish", "hindi", ...): pin it until the next switch."""
        with self.lock:
            self.founder.update(language=code, style=style, switched=True, language_probability=None)
            self._save()

    # ----- approvals -----
    def add_approval(self, req: dict) -> dict:
        pid = uuid.uuid4().hex[:8]
        rec = {"pending_id": pid, "status": "pending", "created_at": now_iso(), **req}
        with self.lock:
            self.approvals[pid] = rec
            self._save()
        return rec

    def update_approval(self, pid: str, **fields) -> dict | None:
        with self.lock:
            rec = self.approvals.get(pid)
            if rec is None:
                return None
            rec.update(fields)
            self._save()
            return rec

    def get(self, pid: str) -> dict | None:
        return self.approvals.get(pid)

    def pending(self) -> list[dict]:
        return sorted((a for a in self.approvals.values() if a.get("status") == "pending"),
                      key=lambda a: a.get("created_at", ""))

    def all(self) -> list[dict]:
        return sorted(self.approvals.values(), key=lambda a: a.get("created_at", ""), reverse=True)
