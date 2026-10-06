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


def test_mark_refunded_payment_and_order(store):
    store.mark_refunded("O123", payment_id="P1002")
    assert store.payment("P1002").status == "refunded"
    assert store.order("O123").status == "shipped"
    store.mark_refunded("O457")
    assert store.order("O457").status == "refunded"


def test_mark_subscription_cancelled(store):
    store.mark_subscription_cancelled("S1")
    assert store.subscriptions_for("C001")[0].status == "cancelled"
