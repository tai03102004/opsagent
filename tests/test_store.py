def test_seed_counts(store):
    assert len(store.customers()) == 4
    assert len(store.orders()) == 9
    assert len(store.subscriptions()) == 3


def test_customer_lookup_is_case_insensitive(store):
    assert store.customer_by_email("  ANNA@example.com ").id == "C001"
    assert store.customer_by_email("ghost@example.com") is None


def test_order_lookup_normalises_id(store):
    assert store.order("o123").id == "O123"
    assert store.order(None) is None
    assert store.order("O999") is None


def test_payments_for_order(store):
    assert [p.id for p in store.payments_for("O123")] == ["P1001", "P1002"]


def test_refund_totals_from_seed(store):
    assert store.paid_total("O123") == 68.0  # duplicate charge: two captured payments
    assert store.refundable("O457") == 18.0
    assert store.refundable("O321") == 0.0  # seeded as already refunded


def test_record_refund_of_a_duplicate_payment(store):
    store.record_refund("O123", 34.0, payment_id="P1002")
    assert store.payment("P1002").status == "refunded"
    assert store.order("O123").status == "shipped"  # the real purchase still stands
    assert store.refundable("O123") == 34.0


def test_partial_then_full_refund(store):
    store.record_refund("O457", 10.0)
    assert store.order("O457").status == "delivered" and store.refundable("O457") == 8.0
    store.record_refund("O457", 8.0)
    assert store.order("O457").status == "refunded" and store.refundable("O457") == 0.0


def test_mark_subscription_cancelled(store):
    store.mark_subscription_cancelled("S1")
    assert store.subscriptions_for("C001")[0].status == "cancelled"
