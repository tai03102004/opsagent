"""Guardrail policy. Pure functions over (actions, verification, extraction) -> Decision.

This is the only place that decides whether something may run automatically.
The LLM has no input here other than the intent/confidence/injection flags it produced.
"""

from __future__ import annotations

from typing import Optional

from .models import (
    TIER_RANK,
    Action,
    ActionDecision,
    ActionType,
    Decision,
    Extraction,
    Intent,
    Tier,
    Verification,
)

AUTO_REFUND_LIMIT = 50.00
MIN_CONFIDENCE = 0.7

LOW_RISK = {ActionType.CREATE_TICKET, ActionType.NOTIFY_SLACK, ActionType.REPLY_CUSTOMER}


def _case_gate(v: Optional[Verification], ex: Optional[Extraction]) -> tuple[Optional[Tier], list[str]]:
    """Conditions that apply to the whole case, before looking at individual actions."""
    if v and v.block_reason:
        return Tier.BLOCK, [f"verification failed: {v.block_reason}"]
    if ex and ex.injection_suspected:
        return Tier.NEEDS_APPROVAL, ["message looks like a prompt-injection attempt"]
    if ex and ex.intent == Intent.UNKNOWN:
        return Tier.NEEDS_APPROVAL, ["intent unclear"]
    if ex and ex.confidence < MIN_CONFIDENCE:
        return Tier.NEEDS_APPROVAL, [f"classifier confidence {ex.confidence:.2f} < {MIN_CONFIDENCE}"]
    return None, []


def _action_tier(a: Action, v: Optional[Verification]) -> tuple[Tier, str]:
    if a.type in LOW_RISK:
        return Tier.AUTO, "low-risk, reversible action"
    if a.type == ActionType.ISSUE_REFUND:
        amount = float(a.params.get("amount") or 0)
        if not (v and v.confirmed) or amount <= 0:
            return Tier.BLOCK, "refund requires a verified issue and a positive amount from our records"
        if amount <= AUTO_REFUND_LIMIT:
            return Tier.AUTO, f"verified refund ${amount:.2f} <= ${AUTO_REFUND_LIMIT:.2f} auto limit"
        return Tier.NEEDS_APPROVAL, f"refund ${amount:.2f} > ${AUTO_REFUND_LIMIT:.2f} auto limit"
    if a.type == ActionType.CANCEL_SUBSCRIPTION:
        return Tier.NEEDS_APPROVAL, "subscription cancellation is hard to reverse (retention review)"
    if a.type == ActionType.ESCALATE_HUMAN:
        return Tier.NEEDS_APPROVAL, "handed to a human by design"
    return Tier.NEEDS_APPROVAL, f"no rule for {a.type.value}; default deny-to-human"


def decide(actions: list[Action], v: Optional[Verification], ex: Optional[Extraction] = None) -> Decision:
    gate, case_reasons = _case_gate(v, ex)
    decisions: list[ActionDecision] = []
    for a in actions:
        tier, reason = _action_tier(a, v)
        reasons = [reason]
        if gate is not None and TIER_RANK[gate] > TIER_RANK[tier]:
            tier, reasons = gate, case_reasons + reasons
        decisions.append(ActionDecision(action=a, tier=tier, reasons=reasons))

    overall = max([d.tier for d in decisions] + ([gate] if gate else [Tier.AUTO]), key=TIER_RANK.__getitem__)
    if not case_reasons:
        case_reasons = sorted({r for d in decisions for r in d.reasons}) or ["no side-effecting actions"]
    return Decision(tier=overall, actions=decisions, reasons=case_reasons)
