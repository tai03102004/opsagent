"""Deterministic verification: is the customer's claim true according to our data?

Nothing here trusts the message or the LLM. Every amount comes from the store.
"""

from __future__ import annotations

from datetime import timedelta

from .models import Customer, Extraction, Finding, Intent, Payment, Verification
from .store import Store

DUPLICATE_WINDOW = timedelta(hours=24)
UNSHIPPED_AFTER = timedelta(days=5)
DELAYED_AFTER = timedelta(days=7)


def _money(x: float) -> str:
    return f"${x:,.2f}"


def find_duplicate_payments(payments: list[Payment]) -> list[tuple[Payment, Payment]]:
    """Pairs of *succeeded* payments with the same amount less than 24h apart.

    A failed attempt followed by a successful retry is not a duplicate.
    """
    ok = sorted((p for p in payments if p.status == "succeeded"), key=lambda p: p.created_at)
    return [
        (a, b)
        for a, b in zip(ok, ok[1:])
        if a.amount == b.amount and b.created_at - a.created_at < DUPLICATE_WINDOW
    ]


def _block(reason, order_id=None, evidence=None) -> Verification:
    return Verification(confirmed=False, block_reason=reason, order_id=order_id, evidence=evidence or [])


def verify(ex: Extraction, customer: Customer, store: Store) -> Verification:
    if ex.intent == Intent.UNKNOWN:
        return Verification(confirmed=False, evidence=["intent unclear; needs human triage"])

    if ex.intent == Intent.CANCEL_SUBSCRIPTION:
        active = [s for s in store.subscriptions_for(customer.id) if s.status == "active"]
        if not active:
            return _block("no_active_subscription", evidence=["customer has no active subscription"])
        sub = active[0]
        return Verification(
            confirmed=True,
            subscription_id=sub.id,
            amount=sub.price,
            evidence=[f"{sub.id} '{sub.plan}' active, {_money(sub.price)}/month"],
        )

    order = store.order(ex.order_id)
    if order is None:
        return _block("order_not_found", evidence=[f"no order {ex.order_id}"])
    if order.customer_id != customer.id:
        # Do not record whose order it is in anything that may reach the customer.
        return _block("not_owner", order.id, [f"order {order.id} does not belong to {customer.id}"])

    payments = store.payments_for(order.id)

    if ex.intent == Intent.ORDER_STATUS:
        shipped = f", shipped {order.shipped_at:%Y-%m-%d}" if order.shipped_at else ""
        delivered = f", delivered {order.delivered_at:%Y-%m-%d}" if order.delivered_at else ""
        return Verification(confirmed=True, order_id=order.id, evidence=[f"status {order.status}{shipped}{delivered}"])

    if ex.intent == Intent.DUPLICATE_CHARGE:
        if order.status == "refunded" or any(p.status == "refunded" for p in payments):
            return _block("already_refunded", order.id, ["a refund was already issued for this order"])
        dups = find_duplicate_payments(payments)
        if not dups:
            n = sum(p.status == "succeeded" for p in payments)
            return Verification(confirmed=False, order_id=order.id, evidence=[f"{n} succeeded payment(s); no duplicate found"])
        first, extra = dups[0]
        return Verification(
            confirmed=True,
            order_id=order.id,
            payment_id=extra.id,
            amount=extra.amount,
            evidence=[f"{p.id} {_money(p.amount)} at {p.created_at:%Y-%m-%d %H:%M}" for p in (first, extra)],
        )

    if ex.intent == Intent.REFUND_REQUEST:
        if order.status == "refunded":
            return _block("already_refunded", order.id, [f"order {order.id} already refunded"])
        if ex.claimed_amount is not None and ex.claimed_amount > order.total:
            return _block(
                "amount_mismatch",
                order.id,
                [f"claimed {_money(ex.claimed_amount)} but order total is {_money(order.total)}"],
            )
        return Verification(
            confirmed=True,
            order_id=order.id,
            amount=order.total,
            evidence=[f"order {order.id} {order.status}, total {_money(order.total)}"],
        )

    if ex.intent == Intent.SHIPPING_ISSUE:
        if order.shipped_at and not order.delivered_at:
            days = (store.now - order.shipped_at).days
            if store.now - order.shipped_at > DELAYED_AFTER:
                return Verification(confirmed=True, order_id=order.id, evidence=[f"shipped {days} days ago, not delivered"])
            return Verification(confirmed=False, order_id=order.id, evidence=[f"shipped {days} days ago; within normal window"])
        return Verification(confirmed=False, order_id=order.id, evidence=[f"order status {order.status}"])

    raise ValueError(f"unhandled intent {ex.intent}")


def scan(store: Store) -> list[Finding]:
    """Proactive pass over operational data. Same Verification shape as request handling."""
    findings: list[Finding] = []

    for order in store.orders():
        payments = store.payments_for(order.id)
        dups = find_duplicate_payments(payments)
        if dups and not any(p.status == "refunded" for p in payments):
            first, extra = dups[0]
            ev = [f"{p.id} {_money(p.amount)} at {p.created_at:%Y-%m-%d %H:%M}" for p in (first, extra)]
            findings.append(Finding(
                kind="duplicate_charge", customer_id=order.customer_id, severity="high", evidence=ev,
                verification=Verification(confirmed=True, order_id=order.id, payment_id=extra.id,
                                          amount=extra.amount, evidence=ev),
            ))

        if order.status == "paid" and not order.shipped_at and store.now - order.created_at > UNSHIPPED_AFTER:
            ev = [f"order {order.id} paid {(store.now - order.created_at).days} days ago, not shipped"]
            findings.append(Finding(
                kind="unshipped_order", customer_id=order.customer_id, severity="medium", evidence=ev,
                verification=Verification(confirmed=True, order_id=order.id, evidence=ev),
            ))

        if order.shipped_at and not order.delivered_at and store.now - order.shipped_at > DELAYED_AFTER:
            ev = [f"order {order.id} shipped {(store.now - order.shipped_at).days} days ago, not delivered"]
            findings.append(Finding(
                kind="delayed_shipment", customer_id=order.customer_id, severity="medium", evidence=ev,
                verification=Verification(confirmed=True, order_id=order.id, evidence=ev),
            ))

    for sub in store.subscriptions():
        if sub.status == "active" and sub.last_renewal_status == "failed":
            ev = [f"{sub.id} renewal failed on {sub.last_renewal_at:%Y-%m-%d}"]
            findings.append(Finding(
                kind="failed_renewal", customer_id=sub.customer_id, severity="high", evidence=ev,
                verification=Verification(confirmed=True, subscription_id=sub.id, amount=sub.price, evidence=ev),
            ))

    return findings
