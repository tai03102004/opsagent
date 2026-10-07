from opsagent.detector import find_duplicate_payments, scan, verify
from opsagent.models import Extraction, Intent


def ex(intent, order_id=None, amount=None):
    return Extraction(intent=intent, order_id=order_id, claimed_amount=amount, confidence=0.9)


def cust(store, email):
    return store.customer_by_email(email)


def test_real_duplicate_is_confirmed(store):
    v = verify(ex(Intent.DUPLICATE_CHARGE, "O123"), cust(store, "anna@example.com"), store)
    assert v.confirmed and v.amount == 34.0 and v.payment_id == "P1002" and v.block_reason is None
    assert len(v.evidence) == 2


def test_claimed_duplicate_without_evidence_is_not_confirmed(store):
    v = verify(ex(Intent.DUPLICATE_CHARGE, "O124"), cust(store, "anna@example.com"), store)
    assert not v.confirmed and v.block_reason is None


def test_failed_then_retried_payment_is_not_a_duplicate(store):
    assert find_duplicate_payments(store.payments_for("O658")) == []


def test_unknown_order(store):
    v = verify(ex(Intent.REFUND_REQUEST, "O999"), cust(store, "anna@example.com"), store)
    assert v.block_reason == "order_not_found"


def test_order_of_another_customer(store):
    v = verify(ex(Intent.ORDER_STATUS, "O123"), cust(store, "ben@example.com"), store)
    assert v.block_reason == "not_owner"
    assert all("C001" not in e for e in v.evidence)


def test_claim_above_paid_amount(store):
    v = verify(ex(Intent.REFUND_REQUEST, "O456", 500), cust(store, "ben@example.com"), store)
    assert v.block_reason == "amount_mismatch"


def test_partial_refund_uses_the_requested_amount(store):
    v = verify(ex(Intent.REFUND_REQUEST, "O457", 10), cust(store, "ben@example.com"), store)
    assert v.confirmed and v.amount == 10.0 and "partial" in v.evidence[0]


def test_refund_without_amount_is_whatever_is_left(store):
    store.record_refund("O457", 10.0)
    v = verify(ex(Intent.REFUND_REQUEST, "O457"), cust(store, "ben@example.com"), store)
    assert v.amount == 8.0 and v.refunded_so_far == 10.0


def test_second_partial_cannot_exceed_what_is_left(store):
    store.record_refund("O457", 10.0)
    v = verify(ex(Intent.REFUND_REQUEST, "O457", 10), cust(store, "ben@example.com"), store)
    assert v.block_reason == "amount_mismatch"


def test_already_refunded_order(store):
    v = verify(ex(Intent.REFUND_REQUEST, "O321"), cust(store, "chloe@example.com"), store)
    assert v.block_reason == "already_refunded"


def test_duplicate_already_refunded(store):
    store.record_refund("O123", 34.0, payment_id="P1002")
    v = verify(ex(Intent.DUPLICATE_CHARGE, "O123"), cust(store, "anna@example.com"), store)
    assert v.block_reason == "already_refunded"


def test_cancel_subscription(store):
    v = verify(ex(Intent.CANCEL_SUBSCRIPTION), cust(store, "anna@example.com"), store)
    assert v.confirmed and v.subscription_id == "S1"
    v = verify(ex(Intent.CANCEL_SUBSCRIPTION), cust(store, "david@example.com"), store)
    assert v.block_reason == "no_active_subscription"


def test_shipping_delay(store):
    v = verify(ex(Intent.SHIPPING_ISSUE, "O654"), cust(store, "david@example.com"), store)
    assert v.confirmed
    v = verify(ex(Intent.SHIPPING_ISSUE, "O124"), cust(store, "anna@example.com"), store)
    assert not v.confirmed and v.block_reason is None


def test_order_status(store):
    v = verify(ex(Intent.ORDER_STATUS, "O124"), cust(store, "anna@example.com"), store)
    assert v.confirmed and any("shipped" in e for e in v.evidence)


def test_scan_finds_the_four_seeded_issues(store):
    findings = scan(store)
    got = sorted((f.kind, f.verification.order_id or f.verification.subscription_id) for f in findings)
    assert got == [
        ("delayed_shipment", "O654"),
        ("duplicate_charge", "O123"),
        ("failed_renewal", "S2"),
        ("unshipped_order", "O555"),
    ]
