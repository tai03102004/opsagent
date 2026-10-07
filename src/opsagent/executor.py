"""Executes (simulated) actions. Every side effect is idempotent and written to the outbox."""

from __future__ import annotations

from typing import Any, Callable

from .models import Action, ActionType
from .outbox import Outbox
from .store import Store

Record = dict[str, Any]


class Executor:
    def __init__(self, store: Store, outbox: Outbox):
        self.store = store
        self.outbox = outbox
        self._handlers: dict[ActionType, Callable[[dict, str], Record]] = {
            ActionType.ISSUE_REFUND: self._refund,
            ActionType.CREATE_TICKET: self._ticket,
            ActionType.NOTIFY_SLACK: self._slack,
            ActionType.CANCEL_SUBSCRIPTION: self._cancel,
            ActionType.ESCALATE_HUMAN: self._escalate,
            ActionType.REPLY_CUSTOMER: self._email,
        }

    def execute(self, action: Action, case_id: str) -> Record:
        record = self._handlers[action.type](dict(action.params), case_id)
        return {"type": action.type.value, **record}

    # ----------------------------------------------------------------- handlers
    def _refund(self, p: dict, case_id: str) -> Record:
        # A payment can be refunded once; an order can get several partial refunds, one per case.
        key = (f"refund:{p['order_id']}:{p['payment_id']}" if p.get("payment_id")
               else f"refund:{p['order_id']}:{case_id}")
        existing = next((r for r in self.outbox.read("refunds") if r["idempotency_key"] == key), None)
        if existing:
            return {"id": existing["id"], "status": "duplicate_ignored", "idempotency_key": key}
        # Re-check at execution time: an approval may run long after the decision was made.
        if not p.get("payment_id") and p["amount"] > self.store.refundable(p["order_id"]):
            return {"id": None, "status": "rejected_exceeds_refundable", "idempotency_key": key,
                    "refundable": self.store.refundable(p["order_id"])}
        rec = {"id": self.outbox.next_id("refunds", "R"), "status": "done", "case_id": case_id,
               "idempotency_key": key, **p}
        self.outbox.append("refunds", rec)
        self.store.record_refund(p["order_id"], p["amount"], p.get("payment_id"))
        return rec

    def _ticket(self, p: dict, case_id: str) -> Record:
        key = p.get("dedupe_key")
        if key:
            existing = next((t for t in self.outbox.read("tickets") if t.get("dedupe_key") == key), None)
            if existing:
                return {"id": existing["id"], "status": "duplicate_ignored", "dedupe_key": key}
        rec = {"id": self.outbox.next_id("tickets", "T"), "status": "done", "case_id": case_id, **p}
        self.outbox.append("tickets", rec)
        return rec

    def _slack(self, p: dict, case_id: str) -> Record:
        key = p.get("dedupe_key")
        if key:
            existing = next((m for m in self.outbox.read("slack") if m.get("dedupe_key") == key), None)
            if existing:
                return {"id": existing["id"], "status": "duplicate_ignored", "dedupe_key": key}
        rec = {"id": self.outbox.next_id("slack", "M"), "status": "done", "case_id": case_id, **p}
        self.outbox.append("slack", rec)
        return rec

    def _cancel(self, p: dict, case_id: str) -> Record:
        sub_id = p["subscription_id"]
        if any(r["subscription_id"] == sub_id for r in self.outbox.read("subscriptions")):
            return {"id": sub_id, "status": "duplicate_ignored"}
        rec = {"id": sub_id, "status": "done", "case_id": case_id, "subscription_id": sub_id, "change": "cancelled"}
        self.outbox.append("subscriptions", rec)
        self.store.mark_subscription_cancelled(sub_id)
        return rec

    def _escalate(self, p: dict, case_id: str) -> Record:
        return self._ticket({"queue": "human-triage", "priority": "high",
                             "title": f"Human triage needed ({p.get('reason', 'unspecified')})",
                             "body": p.get("message", "")}, case_id)

    def _email(self, p: dict, case_id: str) -> Record:
        rec = {"id": self.outbox.next_id("emails", "E"), "status": "done", "case_id": case_id, **p}
        self.outbox.append("emails", rec)
        return rec


def replay(store: Store, outbox: Outbox) -> None:
    """Re-apply persisted side effects to a freshly loaded store."""
    for r in outbox.read("refunds"):
        store.record_refund(r["order_id"], r["amount"], r.get("payment_id"))
    for r in outbox.read("subscriptions"):
        store.mark_subscription_cancelled(r["subscription_id"])
