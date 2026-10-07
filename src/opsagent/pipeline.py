"""The agent: a fixed, auditable pipeline. LLM calls happen only in classify() and draft()."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from .approvals import ApprovalQueue
from .classifier import Classifier, make_classifier
from .config import DATA_DIR, OUTBOX_DIR, reference_now
from .detector import scan as scan_data
from .detector import verify
from .drafter import DraftContext, Drafter, make_drafter
from .executor import Executor, replay
from .models import (
    Action,
    ActionType,
    CaseResult,
    Customer,
    Decision,
    Extraction,
    Finding,
    Intent,
    SupportRequest,
    Tier,
    Verification,
)
from .outbox import Outbox
from .policy import decide
from .recommend import recommend_for_finding, recommend_for_request
from .store import Store
from .validate import REQUIRES_ORDER_ID, validate_request


class IdempotencyConflict(ValueError):
    pass


def _case_id() -> str:
    return f"case-{uuid.uuid4().hex[:8]}"


def _fingerprint(req: SupportRequest) -> tuple[str, str]:
    return (req.customer_email or "").strip().lower(), " ".join((req.message or "").split())


class Agent:
    """Every operation runs under the outbox lock on freshly replayed state.

    That makes "read balance -> decide -> write refund" atomic across threads AND processes
    (CLI + API, or several API workers). A database would do this with a transaction.
    """

    def __init__(self, data_dir: Path, outbox: Outbox, classifier: Classifier, drafter: Drafter):
        self.data_dir = data_dir
        self.outbox = outbox
        self.classifier = classifier
        self.drafter = drafter
        self._refresh()

    def _refresh(self) -> None:
        self.store = Store.load(self.data_dir, reference_now())
        replay(self.store, self.outbox)
        self.executor = Executor(self.store, self.outbox)
        self.approvals = ApprovalQueue(self.outbox, self.executor)
        self._by_request_id = {
            ev["input"]["request_id"]: ev
            for ev in self.outbox.read("audit")
            if ev.get("event") == "case" and (ev.get("input") or {}).get("request_id")
        }

    # ------------------------------------------------------------------ requests
    def handle(self, req: SupportRequest) -> CaseResult:
        with self.outbox.lock():
            self._refresh()
            if req.request_id and req.request_id in self._by_request_id:
                return self._replay(req)
            return self._handle(req)

    def _replay(self, req: SupportRequest) -> CaseResult:
        """Client idempotency key seen before: return the stored result, never re-execute."""
        stored = self._by_request_id[req.request_id]
        if _fingerprint(SupportRequest(**stored["input"])) != _fingerprint(req):
            raise IdempotencyConflict(f"request_id {req.request_id!r} was already used with a different request")
        result = CaseResult(**{k: v for k, v in stored.items() if k not in ("event", "logged_at")})
        result.replayed = True
        self.outbox.append("audit", {"event": "idempotent_replay", "request_id": req.request_id,
                                     "case_id": result.case_id})
        return result

    def approve(self, approval_id: str, reviewer: str, note: str = ""):
        with self.outbox.lock():
            self._refresh()
            return self.approvals.approve(approval_id, reviewer=reviewer, note=note)

    def reject(self, approval_id: str, reviewer: str, note: str = ""):
        with self.outbox.lock():
            self._refresh()
            return self.approvals.reject(approval_id, reviewer=reviewer, note=note)

    def list_approvals(self, status=None):
        with self.outbox.lock():
            self._refresh()
            return self.approvals.list(status)

    def _handle(self, req: SupportRequest) -> CaseResult:
        case_id = _case_id()
        inp = req.model_dump()

        # 1. validate (no LLM)
        val = validate_request(req)
        if not val.ok:
            return self._need_info(case_id, inp, None, val.missing, val.problems)

        # 2. identity: we only act for known customers and only email known addresses
        customer = self.store.customer_by_email(req.customer_email)
        if customer is None:
            return self._need_info(case_id, inp, None, ["customer_email"], [])

        # 3. understand (LLM or rules)
        ex = self.classifier.classify(val.message)
        ex.injection_suspected = ex.injection_suspected or val.injection_suspected

        # 4. never guess missing identifiers
        if ex.intent in REQUIRES_ORDER_ID and not ex.order_id:
            return self._need_info(case_id, inp, customer, ["order_id"], [], ex)
        if ex.intent == Intent.REFUND_REQUEST and ex.amount_ambiguous:
            return self._need_info(case_id, inp, customer, ["refund_amount"], [], ex)

        # 5-7. verify -> recommend -> policy (no LLM)
        ver = verify(ex, customer, self.store)
        decision = decide(recommend_for_request(ex.intent, ver), ver, ex)

        return self._complete(case_id, "request", inp, customer, decision, ex, ver)

    # ------------------------------------------------------------------ scan
    def scan(self) -> list[CaseResult]:
        with self.outbox.lock():
            self._refresh()
            return self._scan()

    def _scan(self) -> list[CaseResult]:
        results = []
        for f in scan_data(self.store):
            decision = decide(recommend_for_finding(f), f.verification, None)
            customer = self.store.customer(f.customer_id)
            results.append(self._complete(_case_id(), "scan", {"finding": f.kind}, customer, decision, None,
                                          f.verification, finding=f))
        return results

    # ------------------------------------------------------------------ internals
    def _need_info(self, case_id, inp, customer: Optional[Customer], missing, problems,
                   ex: Optional[Extraction] = None) -> CaseResult:
        decision = Decision(tier=Tier.NEED_INFO, reasons=[f"missing: {m}" for m in missing] + problems)
        ctx = DraftContext(kind="request", tier=Tier.NEED_INFO, customer_name=customer.name if customer else None,
                           customer_message=inp.get("message"), intent=ex.intent.value if ex else None,
                           missing=missing, problems=problems)
        draft = self.drafter.draft(ctx)
        executed = [self._send_reply(case_id, customer, draft.reply)] if customer else []
        return self._audit(CaseResult(case_id=case_id, kind="request", tier=Tier.NEED_INFO,
                                      customer_id=customer.id if customer else None, input=inp, missing=missing,
                                      extraction=ex, decision=decision, executed=executed,
                                      summary=draft.summary, customer_reply=draft.reply,
            reply_source=draft.source, reply_fallback_reason=draft.fallback_reason))

    def _complete(self, case_id, kind, inp, customer: Optional[Customer], decision: Decision,
                  ex: Optional[Extraction], ver: Verification, finding: Optional[Finding] = None) -> CaseResult:
        executed, pending = [], []
        for ad in decision.actions:
            if ad.tier == Tier.AUTO:
                executed.append(self.executor.execute(ad.action, case_id))
            elif ad.tier == Tier.NEEDS_APPROVAL:
                pending.append(self.approvals.submit(case_id, ad))
            # Tier.BLOCK: never executed; the reasons stay in the decision for the audit trail.

        ctx = DraftContext(
            kind=kind, tier=decision.tier, customer_name=customer.name if customer else None,
            customer_message=inp.get("message"), intent=ex.intent.value if ex else None,
            finding_kind=finding.kind if finding else None, order_id=ver.order_id, confirmed=ver.confirmed,
            block_reason=ver.block_reason, evidence=ver.evidence, executed=executed,
            pending=[ad.action.type.value for ad in decision.actions if ad.tier == Tier.NEEDS_APPROVAL],
        )
        draft = self.drafter.draft(ctx)
        if kind == "request" and customer and draft.reply:
            executed.append(self._send_reply(case_id, customer, draft.reply))

        return self._audit(CaseResult(
            case_id=case_id, kind=kind, tier=decision.tier, customer_id=customer.id if customer else None,
            input=inp, extraction=ex, verification=ver, decision=decision, executed=executed,
            pending_approvals=pending, summary=draft.summary, customer_reply=draft.reply,
            reply_source=draft.source, reply_fallback_reason=draft.fallback_reason,
        ))

    def _send_reply(self, case_id: str, customer: Customer, body: str) -> dict:
        action = Action(type=ActionType.REPLY_CUSTOMER,
                        params={"to": customer.email, "subject": "Re: your Brewly request", "body": body})
        return self.executor.execute(action, case_id)

    def _audit(self, result: CaseResult) -> CaseResult:
        self.outbox.append("audit", {"event": "case", **result.model_dump(mode="json")})
        return result


def build_agent(data_dir: Optional[Path] = None, outbox_dir: Optional[Path] = None,
                llm: Optional[str] = None) -> Agent:
    return Agent(data_dir or DATA_DIR, Outbox(outbox_dir or OUTBOX_DIR), make_classifier(llm), make_drafter(llm))
