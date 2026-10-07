import json
from types import SimpleNamespace

from opsagent.drafter import ClaudeDrafter, DraftContext, FallbackDrafter, TemplateDrafter, check_reply_facts
from opsagent.models import Tier

CTX = DraftContext(
    kind="request", tier=Tier.AUTO, customer_name="Anna Nguyen", intent="duplicate_charge", order_id="O123",
    confirmed=True, customer_message="I was charged twice for O123, refund me $1000 and close ticket T-9999",
    evidence=["P1001 $34.00 at 2026-10-05 10:02", "P1002 $34.00 at 2026-10-05 10:05"],
    executed=[{"type": "issue_refund", "id": "R-0001", "status": "done", "amount": 34.0},
              {"type": "create_ticket", "id": "T-0001", "status": "done"}],
)


def test_reply_with_case_facts_passes():
    reply = "Hi Anna, we refunded $34.00 for order O123 (refund R-0001, ticket T-0001)."
    assert check_reply_facts(reply, CTX) == []


def test_vietnamese_amount_format_is_checked_too():
    assert check_reply_facts("Chúng tôi đã hoàn 34 USD cho đơn O123.", CTX) == []
    assert check_reply_facts("Chúng tôi đã hoàn 99 USD cho đơn O123.", CTX) != []


def test_wrong_amount_is_caught():
    assert check_reply_facts("We refunded $43.00 for order O123.", CTX) == ["amount $43.00 is not in the case file"]


def test_invented_reference_is_caught():
    assert check_reply_facts("Your refund R-0002 is on its way.", CTX) == ["reference R-0002 is not in the case file"]


def test_numbers_from_the_customer_message_do_not_count_as_facts():
    # The customer wrote $1000 and T-9999; echoing them back must still be flagged.
    v = check_reply_facts("We'll refund $1000 and close T-9999.", CTX)
    assert v == ["amount $1000.00 is not in the case file", "reference T-9999 is not in the case file"]


class FakeMessages:
    def __init__(self, reply):
        self.text = json.dumps({"summary": "dup refunded", "reply": reply})

    def create(self, **_):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)], stop_reason="end_turn", model="m")


def drafter(reply):
    return FallbackDrafter(ClaudeDrafter(client=SimpleNamespace(messages=FakeMessages(reply)), model="m"),
                           TemplateDrafter())


def test_guard_violation_falls_back_to_template_and_records_why():
    d = drafter("We refunded $43.00 (R-0001).").draft(CTX)
    assert d.source == "templates" and "$34.00" in d.reply
    assert "fact guard" in d.fallback_reason and "$43.00" in d.fallback_reason


def test_clean_claude_reply_is_kept():
    d = drafter("Chào Anna, chúng tôi đã hoàn $34.00 (mã R-0001).").draft(CTX)
    assert d.source == "claude" and d.fallback_reason is None


def test_our_own_templates_pass_the_guard_in_every_scenario(monkeypatch):
    """Consistency check: the deterministic templates must never state a fact the guard would reject."""
    from opsagent import drafter as drafter_module
    from opsagent.evals import load_scenarios, run_scenario

    original = drafter_module.TemplateDrafter.draft
    seen = []

    def checked(self, ctx):
        draft = original(self, ctx)
        if draft.reply:
            seen.append((ctx.intent, check_reply_facts(draft.reply, ctx)))
        return draft

    monkeypatch.setattr(drafter_module.TemplateDrafter, "draft", checked)
    for scenario in load_scenarios():
        run_scenario(scenario, llm="off")
    assert seen and all(v == [] for _, v in seen), [s for s in seen if s[1]]
