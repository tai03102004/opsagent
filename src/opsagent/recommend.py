"""Map a verified situation to actions from a fixed catalog. Proposes only; policy decides."""

from __future__ import annotations

from .models import Action, ActionType, Finding, Intent, Verification


def _ticket(queue: str, priority: str, title: str, key: str, evidence: list[str]) -> Action:
    return Action(
        type=ActionType.CREATE_TICKET,
        params={"queue": queue, "priority": priority, "title": title, "body": "; ".join(evidence), "dedupe_key": key},
    )


def _slack(channel: str, text: str, key: str) -> Action:
    return Action(type=ActionType.NOTIFY_SLACK, params={"channel": channel, "text": text, "dedupe_key": key})


def _refund(v: Verification, reason: str) -> Action:
    return Action(
        type=ActionType.ISSUE_REFUND,
        params={"order_id": v.order_id, "payment_id": v.payment_id, "amount": v.amount, "reason": reason},
    )


def recommend_for_request(intent: Intent, v: Verification) -> list[Action]:
    if v.block_reason:
        return []
    oid = v.order_id

    if intent == Intent.DUPLICATE_CHARGE:
        if v.confirmed:
            return [
                _refund(v, "duplicate charge"),
                _ticket("billing", "high", f"Duplicate charge on {oid}", f"duplicate_charge:{oid}", v.evidence),
                _slack("#billing", f"Duplicate charge on {oid} (${v.amount:.2f}) - auto-refund proposed",
                       f"duplicate_charge:{oid}"),
            ]
        return [_ticket("billing", "medium", f"Investigate duplicate-charge claim on {oid}",
                        f"duplicate_claim:{oid}", v.evidence)]

    if intent == Intent.REFUND_REQUEST:
        return [
            _refund(v, "customer refund request"),
            _ticket("billing", "medium", f"Refund request for {oid}", f"refund_request:{oid}", v.evidence),
        ]

    if intent == Intent.CANCEL_SUBSCRIPTION:
        return [Action(type=ActionType.CANCEL_SUBSCRIPTION, params={"subscription_id": v.subscription_id})]

    if intent == Intent.SHIPPING_ISSUE and v.confirmed:
        return [
            _ticket("fulfillment", "medium", f"Delayed shipment {oid}", f"delayed_shipment:{oid}", v.evidence),
            _slack("#fulfillment", f"Customer reports {oid} not delivered: {'; '.join(v.evidence)}",
                   f"delayed_shipment:{oid}"),
        ]

    if intent == Intent.UNKNOWN:
        return [Action(type=ActionType.ESCALATE_HUMAN, params={"reason": "intent unclear"})]

    return []  # order_status, unconfirmed shipping issue: a reply is enough


def recommend_for_finding(f: Finding) -> list[Action]:
    v = f.verification
    ref = v.order_id or v.subscription_id
    if f.kind == "duplicate_charge":
        return [
            _refund(v, "duplicate charge (detected by scan)"),
            _ticket("billing", "high", f"Duplicate charge on {ref}", f"duplicate_charge:{ref}", f.evidence),
            _slack("#billing", f"Scan: duplicate charge on {ref} (${v.amount:.2f})", f"duplicate_charge:{ref}"),
        ]
    if f.kind == "failed_renewal":
        return [
            _ticket("billing", "high", f"Subscription {ref} renewal failed", f"failed_renewal:{ref}", f.evidence),
            _slack("#billing", f"Scan: renewal failed for {ref}; customer should update payment method",
               f"failed_renewal:{ref}"),
        ]
    queue_title = {"unshipped_order": "Paid but not shipped", "delayed_shipment": "Delayed shipment"}[f.kind]
    return [
        _ticket("fulfillment", "medium", f"{queue_title}: {ref}", f"{f.kind}:{ref}", f.evidence),
        _slack("#fulfillment", f"Scan: {queue_title.lower()} {ref}", f"{f.kind}:{ref}"),
    ]
