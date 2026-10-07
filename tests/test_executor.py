import pytest

from opsagent.approvals import ApprovalError, ApprovalQueue
from opsagent.config import DATA_DIR, reference_now
from opsagent.executor import Executor, replay
from opsagent.models import Action, ActionDecision, ActionType, Tier
from opsagent.outbox import Outbox
from opsagent.store import Store


def refund(order_id="O123", payment_id="P1002", amount=34.0):
    return Action(type=ActionType.ISSUE_REFUND,
                  params={"order_id": order_id, "payment_id": payment_id, "amount": amount, "reason": "t"})


def ticket(key="k1"):
    return Action(type=ActionType.CREATE_TICKET,
                  params={"queue": "billing", "priority": "high", "title": "t", "body": "b", "dedupe_key": key})


@pytest.fixture
def ex(store, outbox_dir):
    return Executor(store, Outbox(outbox_dir))


def test_refund_is_idempotent(ex, store):
    first = ex.execute(refund(), "case-1")
    second = ex.execute(refund(), "case-2")
    assert first["status"] == "done" and first["id"] == "R-0001"
    assert second["status"] == "duplicate_ignored" and second["id"] == "R-0001"
    assert len(ex.outbox.read("refunds")) == 1
    assert store.payment("P1002").status == "refunded"


def test_partial_refunds_on_one_order_in_separate_cases(ex, store):
    a = ex.execute(refund("O457", None, 10.0), "case-1")
    b = ex.execute(refund("O457", None, 5.0), "case-2")
    again = ex.execute(refund("O457", None, 10.0), "case-1")
    assert a["status"] == b["status"] == "done" and again["status"] == "duplicate_ignored"
    assert store.refundable("O457") == 3.0


def test_refund_is_rechecked_at_execution_time(ex):
    ex.execute(refund("O457", None, 10.0), "case-1")
    late = ex.execute(refund("O457", None, 18.0), "case-2")  # e.g. an approval decided earlier
    assert late["status"] == "rejected_exceeds_refundable"
    assert len(ex.outbox.read("refunds")) == 1


def test_ticket_dedupe(ex):
    a = ex.execute(ticket("same"), "c1")
    b = ex.execute(ticket("same"), "c2")
    c = ex.execute(ticket("other"), "c3")
    assert a["id"] == b["id"] != c["id"]
    assert b["status"] == "duplicate_ignored"


def test_cancel_subscription(ex, store):
    r = ex.execute(Action(type=ActionType.CANCEL_SUBSCRIPTION, params={"subscription_id": "S1"}), "c")
    assert r["status"] == "done" and store.subscriptions_for("C001")[0].status == "cancelled"


def test_state_survives_restart(outbox_dir):
    s1 = Store.load(DATA_DIR, reference_now())
    Executor(s1, Outbox(outbox_dir)).execute(refund(), "c")
    s2 = Store.load(DATA_DIR, reference_now())
    replay(s2, Outbox(outbox_dir))
    assert s2.payment("P1002").status == "refunded"
    again = Executor(s2, Outbox(outbox_dir)).execute(refund(), "c2")
    assert again["status"] == "duplicate_ignored"


def pending(action):
    return ActionDecision(action=action, tier=Tier.NEEDS_APPROVAL, reasons=["r"])


def test_approve_executes_exactly_once(ex):
    q = ApprovalQueue(ex.outbox, ex)
    aid = q.submit("case-1", pending(refund("O789", None, 120.0)))
    assert [a.id for a in q.list("pending")] == [aid]
    rec = q.approve(aid, reviewer="lead@brewly", note="ok")
    assert rec.status == "approved" and rec.result["status"] == "done"
    with pytest.raises(ApprovalError):
        q.approve(aid, reviewer="lead@brewly")
    assert len(ex.outbox.read("refunds")) == 1


def test_reject_never_executes(ex):
    q = ApprovalQueue(ex.outbox, ex)
    aid = q.submit("case-1", pending(refund("O789", None, 120.0)))
    rec = q.reject(aid, reviewer="lead@brewly", note="fraud risk")
    assert rec.status == "rejected" and rec.result is None
    assert ex.outbox.read("refunds") == []


def test_unknown_approval_id(ex):
    with pytest.raises(KeyError):
        ApprovalQueue(ex.outbox, ex).approve("A-9999", reviewer="x")


def test_approvals_survive_restart(ex, outbox_dir):
    aid = ApprovalQueue(ex.outbox, ex).submit("case-1", pending(refund("O789", None, 120.0)))
    q2 = ApprovalQueue(Outbox(outbox_dir), ex)
    assert q2.get(aid).status == "pending"
