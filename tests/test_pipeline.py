import pytest

from opsagent.drafter import ClaudeDrafter, DraftContext, FallbackDrafter, TemplateDrafter
from opsagent.models import SupportRequest, Tier
from opsagent.pipeline import build_agent


@pytest.fixture
def agent(outbox_dir):
    return build_agent(outbox_dir=outbox_dir, llm="off")


def req(email, message):
    return SupportRequest(customer_email=email, message=message)


def types(records):
    return [r["type"] for r in records]


def test_duplicate_charge_end_to_end(agent):
    r = agent.handle(req("anna@example.com", "I was charged twice for order O123!"))
    assert r.tier == Tier.AUTO
    assert types(r.executed) == ["issue_refund", "create_ticket", "notify_slack", "reply_customer"]
    assert "$34.00" in r.customer_reply and "R-0001" in r.customer_reply
    assert r.extraction.source == "rules"


def test_unknown_email_needs_info_and_sends_nothing(agent):
    r = agent.handle(req("ghost@example.com", "Where is my order O124?"))
    assert r.tier == Tier.NEED_INFO and r.missing == ["customer_email"]
    assert r.executed == [] and agent.outbox.read("emails") == []
    assert r.customer_reply


def test_missing_order_id_asks_for_it(agent):
    r = agent.handle(req("ben@example.com", "Please refund my order."))
    assert r.tier == Tier.NEED_INFO and r.missing == ["order_id"]
    assert "order ID" in r.customer_reply
    assert types(r.executed) == ["reply_customer"]


def test_large_refund_goes_to_approval(agent):
    r = agent.handle(req("chloe@example.com", "I want a refund for order O789."))
    assert r.tier == Tier.NEEDS_APPROVAL
    assert types(r.executed) == ["create_ticket", "reply_customer"]
    assert len(r.pending_approvals) == 1
    assert agent.approvals.get(r.pending_approvals[0]).action.params["amount"] == 120.0
    assert "review" in r.customer_reply


def test_not_owner_reply_does_not_leak(agent):
    r = agent.handle(req("ben@example.com", "Where is my order O123?"))
    assert r.tier == Tier.BLOCK
    assert "couldn't find order O123" in r.customer_reply
    assert "Anna" not in r.customer_reply and "C001" not in r.customer_reply


def test_audit_one_record_per_case(agent):
    agent.handle(req("anna@example.com", "Where is my order O124?"))
    agent.handle(req("anna@example.com", ""))
    assert [a["event"] for a in agent.outbox.read("audit")] == ["case", "case"]


def test_scan_runs_findings_through_policy(agent):
    results = agent.scan()
    assert len(results) == 4
    dup = next(r for r in results if r.verification.order_id == "O123")
    assert dup.tier == Tier.AUTO and "issue_refund" in types(dup.executed)
    # second scan: refund already done, tickets/slack deduplicated
    again = agent.scan()
    assert len(again) == 3
    assert all(e["status"] == "duplicate_ignored" for r in again for e in r.executed)


class Boom:
    class messages:
        @staticmethod
        def create(**_):
            raise RuntimeError("api down")


def test_drafter_falls_back_to_templates():
    ctx = DraftContext(kind="request", tier=Tier.NEED_INFO, missing=["order_id"], customer_name="Ben Tran")
    d = FallbackDrafter(ClaudeDrafter(client=Boom(), model="m"), TemplateDrafter()).draft(ctx)
    assert "order ID" in d.reply and d.source == "templates"
