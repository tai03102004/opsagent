import pytest

from opsagent.models import Action, ActionType, Extraction, Intent, Tier, Verification
from opsagent.policy import AUTO_REFUND_LIMIT, decide
from opsagent.recommend import recommend_for_request

OK = Verification(confirmed=True, order_id="O1", amount=10)


def refund(amount):
    return Action(type=ActionType.ISSUE_REFUND, params={"order_id": "O1", "amount": amount})


def ex(intent=Intent.REFUND_REQUEST, confidence=0.9, injection=False):
    return Extraction(intent=intent, confidence=confidence, injection_suspected=injection)


def tiers(decision):
    return {d.action.type: d.tier for d in decision.actions}


def test_limit_value():
    assert AUTO_REFUND_LIMIT == 50.0


@pytest.mark.parametrize("amount, tier", [(18, Tier.AUTO), (50, Tier.AUTO), (50.01, Tier.NEEDS_APPROVAL), (120, Tier.NEEDS_APPROVAL)])
def test_refund_threshold(amount, tier):
    d = decide([refund(amount)], OK, ex())
    assert d.tier == tier and d.actions[0].reasons


def test_unverified_refund_is_blocked():
    d = decide([refund(10)], Verification(confirmed=False), ex())
    assert d.tier == Tier.BLOCK


def test_cancel_subscription_needs_approval():
    d = decide([Action(type=ActionType.CANCEL_SUBSCRIPTION)], OK, ex(Intent.CANCEL_SUBSCRIPTION))
    assert d.tier == Tier.NEEDS_APPROVAL


def test_block_reason_blocks_case_even_without_actions():
    d = decide([], Verification(confirmed=False, block_reason="not_owner"), ex())
    assert d.tier == Tier.BLOCK and any("not_owner" in r for r in d.reasons)


def test_block_reason_blocks_every_side_effect():
    d = decide([refund(5), Action(type=ActionType.CREATE_TICKET)],
               Verification(confirmed=False, block_reason="amount_mismatch"), ex())
    assert set(tiers(d).values()) == {Tier.BLOCK}


def test_injection_sends_everything_to_human():
    d = decide([refund(5), Action(type=ActionType.CREATE_TICKET)], OK, ex(injection=True))
    assert set(tiers(d).values()) == {Tier.NEEDS_APPROVAL}
    assert d.tier == Tier.NEEDS_APPROVAL


@pytest.mark.parametrize("e", [ex(Intent.UNKNOWN), ex(confidence=0.5)])
def test_unclear_cases_need_human(e):
    d = decide([refund(5)], OK, e)
    assert d.tier == Tier.NEEDS_APPROVAL


def test_low_risk_actions_are_auto_and_overall_is_most_restrictive():
    d = decide([Action(type=ActionType.CREATE_TICKET), Action(type=ActionType.NOTIFY_SLACK), refund(120)], OK, ex())
    t = tiers(d)
    assert t[ActionType.CREATE_TICKET] == Tier.AUTO and t[ActionType.NOTIFY_SLACK] == Tier.AUTO
    assert d.tier == Tier.NEEDS_APPROVAL


def test_no_actions_is_auto():
    assert decide([], OK, ex(Intent.ORDER_STATUS)).tier == Tier.AUTO


def test_recommend_duplicate_confirmed():
    v = Verification(confirmed=True, order_id="O123", payment_id="P1002", amount=34.0)
    types = [a.type for a in recommend_for_request(Intent.DUPLICATE_CHARGE, v)]
    assert types == [ActionType.ISSUE_REFUND, ActionType.CREATE_TICKET, ActionType.NOTIFY_SLACK]


def test_recommend_refund_uses_store_amount():
    v = Verification(confirmed=True, order_id="O457", amount=18.0)
    a = recommend_for_request(Intent.REFUND_REQUEST, v)[0]
    assert a.params["amount"] == 18.0 and a.params["order_id"] == "O457"


def test_recommend_nothing_when_blocked():
    assert recommend_for_request(Intent.REFUND_REQUEST, Verification(confirmed=False, block_reason="not_owner")) == []
