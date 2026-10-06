"""Human-in-the-loop queue. A queued action runs only after an explicit approve()."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel

from .executor import Executor
from .models import Action, ActionDecision
from .outbox import Outbox

Status = Literal["pending", "approved", "rejected"]


class ApprovalError(RuntimeError):
    pass


class ApprovalRecord(BaseModel):
    id: str
    case_id: str
    action: Action
    reasons: list[str]
    status: Status = "pending"
    created_at: str
    reviewer: Optional[str] = None
    note: Optional[str] = None
    decided_at: Optional[str] = None
    result: Optional[dict[str, Any]] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApprovalQueue:
    def __init__(self, outbox: Outbox, executor: Executor):
        self.outbox = outbox
        self.executor = executor
        self._records: dict[str, ApprovalRecord] = {}
        for ev in outbox.read("approvals"):  # fold the event log
            if ev["event"] == "created":
                self._records[ev["record"]["id"]] = ApprovalRecord(**ev["record"])
            else:
                self._records[ev["id"]] = self._records[ev["id"]].model_copy(update=ev["update"])

    def submit(self, case_id: str, decision: ActionDecision) -> str:
        rec = ApprovalRecord(id=f"A-{len(self._records) + 1:04d}", case_id=case_id,
                             action=decision.action, reasons=decision.reasons, created_at=_now())
        self._records[rec.id] = rec
        self.outbox.append("approvals", {"event": "created", "record": rec.model_dump(mode="json")})
        return rec.id

    def list(self, status: Optional[Status] = None) -> list[ApprovalRecord]:
        return [r for r in self._records.values() if status is None or r.status == status]

    def get(self, approval_id: str) -> ApprovalRecord:
        if approval_id not in self._records:
            raise KeyError(approval_id)
        return self._records[approval_id]

    def approve(self, approval_id: str, reviewer: str, note: str = "") -> ApprovalRecord:
        rec = self._pending(approval_id)
        result = self.executor.execute(rec.action, rec.case_id)
        return self._decide(rec, "approved", reviewer, note, result)

    def reject(self, approval_id: str, reviewer: str, note: str = "") -> ApprovalRecord:
        return self._decide(self._pending(approval_id), "rejected", reviewer, note, None)

    def _pending(self, approval_id: str) -> ApprovalRecord:
        rec = self.get(approval_id)
        if rec.status != "pending":
            raise ApprovalError(f"{approval_id} is already {rec.status}")
        return rec

    def _decide(self, rec, status, reviewer, note, result) -> ApprovalRecord:
        update = {"status": status, "reviewer": reviewer, "note": note, "decided_at": _now(), "result": result}
        self._records[rec.id] = rec.model_copy(update=update)
        self.outbox.append("approvals", {"event": "decided", "id": rec.id, "update": update})
        self.outbox.append("audit", {"event": "approval_decided", "approval_id": rec.id, "case_id": rec.case_id,
                                     "action": rec.action.type.value, **update})
        return self._records[rec.id]
