# Ops Automation Agent — Implementation Plan

> **For agentic workers:** executed inline in the authoring session (superpowers:executing-plans style),
> TDD per task, commit per task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable CLI + FastAPI demo of an ops/support agent that classifies requests, verifies them
against mock data, and executes or gates actions through a deterministic guardrail policy.

**Architecture:** Fixed pipeline (validate → classify → missing-info → verify → recommend → policy →
execute/queue → draft → audit). LLM only behind two interfaces (`Classifier`, `Drafter`) with
rule/template fallbacks. State = seed JSON + replayed outbox JSONL events.

**Tech Stack:** Python 3.12, uv, Pydantic v2, anthropic SDK 1.x (`messages.parse`), FastAPI + uvicorn,
PyYAML, pytest.

**Spec:** `docs/spec.md`

## Global Constraints

- `AUTO_REFUND_LIMIT = 50.00`, `MIN_CONFIDENCE = 0.7`.
- Reference clock `2026-10-06T12:00:00+00:00`, override `OPSAGENT_NOW`.
- Model env `OPSAGENT_MODEL`, default `claude-opus-5-5`. `OPSAGENT_LLM=off` forces rules/templates.
- Tests never call the network (`OPSAGENT_LLM=off` in `conftest.py`); live tests marked `live`, excluded by default.
- Money amounts come from the store only. Order IDs from the LLM are kept only if they appear in the message text.
- Customer replies never reveal whether another customer's order exists (`not_owner` reads like `order_not_found`).

## File Structure

```
pyproject.toml, .python-version, .gitignore
data/{customers,orders,payments,subscriptions}.json
src/opsagent/
  config.py       env + paths + reference clock
  models.py       Pydantic models & enums (shared vocabulary)
  store.py        in-memory data access, loaded from data/, mutated only via executor
  validate.py     input validation, injection heuristics, required fields per intent
  classifier.py   Classifier protocol, RuleClassifier, ClaudeClassifier, FallbackClassifier, make_classifier
  detector.py     verify() per intent, find_duplicate_payments(), scan()
  recommend.py    intent/finding + verification → list[Action]
  policy.py       decide() → Decision (per-action tiers + overall tier)
  outbox.py       JSONL event log (tickets, slack, emails, refunds, subscriptions, approvals, audit)
  executor.py     simulated actions, idempotent refunds, ticket dedupe
  approvals.py    ApprovalQueue (submit/list/approve/reject), persisted as events
  drafter.py      TemplateDrafter, ClaudeDrafter, FallbackDrafter, make_drafter
  pipeline.py     Agent.handle(), Agent.scan(), build_agent()
  evals.py        load scenarios, run offline/live, report
  api.py          FastAPI app
  cli.py          argparse CLI
evals/scenarios.yaml
tests/ conftest.py + one test file per module + test_scenarios.py + test_api.py
```

## Tasks

### Task 1: Scaffold, seed data, models, store
**Files:** pyproject, data/*.json, config.py, models.py, store.py, tests/test_store.py
**Produces:** `Store.load(data_dir, now)`, `customer_by_email`, `order`, `payments_for`,
`subscriptions_for`, `orders()`, `subscriptions()`, `mark_refunded(order_id, payment_id=None)`,
`mark_subscription_cancelled(sub_id)`; enums `Intent`, `Tier`, `ActionType`; models `Customer`, `Order`,
`Payment`, `Subscription`, `SupportRequest`, `Extraction`, `Verification`, `Action`, `ActionDecision`,
`Decision`, `Finding`, `CaseResult`.
- [ ] Tests: email lookup is case-insensitive; order lookup normalises `o123`→`O123`; O123 has 2 payments;
      mark_refunded on payment flips payment status; seed contains 4 customers / 9 orders / 3 subs.
- [ ] Implement, run `uv run pytest`, commit.

### Task 2: Validation + rule classifier
**Files:** validate.py, classifier.py (RuleClassifier part), tests/test_validate.py, tests/test_rules.py
**Produces:** `validate_request(req) -> ValidationResult{ok, missing, problems, injection_suspected, message}`,
`detect_injection(text) -> bool`, `REQUIRES_ORDER_ID: set[Intent]`, `RuleClassifier().classify(text) -> Extraction`.
- [ ] Tests: empty message → missing `message`; bad email → missing `customer_email`; >2000 chars → problem;
      injection EN+VI detected, normal text not; classify table (EN + VI) for all 6 intents, order id
      and `$500` extraction, unknown → confidence < 0.7.
- [ ] Implement, run, commit.

### Task 3: Claude classifier + fallback
**Files:** classifier.py (ClaudeClassifier, FallbackClassifier, make_classifier), tests/test_classifier_llm.py
**Produces:** `ClaudeClassifier(client, model).classify(text)` using `client.messages.parse(output_format=_LLMExtraction)`;
`FallbackClassifier(primary, fallback)` catches any exception → fallback result with `fallback_reason`;
`make_classifier(mode)`.
- [ ] Tests with a fake client object: parsed output mapped to `Extraction(source="claude")`; hallucinated
      order id (not in text) dropped; confidence clamped; refusal/None parsed → fallback; exception → fallback.
- [ ] Live test (`@pytest.mark.live`) for one message.
- [ ] Implement, run, commit.

### Task 4: Detector (verify + scan)
**Files:** detector.py, tests/test_detector.py
**Produces:** `verify(extraction, customer, store) -> Verification`, `find_duplicate_payments(payments) -> list[tuple]`,
`scan(store) -> list[Finding]`.
- [ ] Tests: O123 duplicate confirmed amount 34; O124 duplicate not confirmed; O999 → order_not_found;
      ben+O123 → not_owner; O456 claim 500 → amount_mismatch; O321 → already_refunded;
      cancel for anna → S1; cancel for david → no_active_subscription; O654 shipping confirmed;
      O124 shipping not confirmed; scan → 4 findings of the 4 kinds.
- [ ] Implement, run, commit.

### Task 5: Recommend + policy
**Files:** recommend.py, policy.py, tests/test_policy.py
**Produces:** `recommend_for_request(intent, verification) -> list[Action]`, `recommend_for_finding(finding) -> list[Action]`,
`decide(actions, verification, extraction=None) -> Decision`.
- [ ] Tests: refund 34 verified → AUTO; refund 120 → NEEDS_APPROVAL; refund unverified → BLOCK;
      cancel → NEEDS_APPROVAL; block_reason → BLOCK overall even with no actions; injection → all
      side effects NEEDS_APPROVAL; unknown/low confidence → NEEDS_APPROVAL; ticket/slack → AUTO;
      every decision has non-empty reasons; overall = most restrictive.
- [ ] Implement, run, commit.

### Task 6: Outbox, executor, approvals
**Files:** outbox.py, executor.py, approvals.py, tests/test_executor.py
**Produces:** `Outbox(dir).append(stream, record)`, `.read(stream)`, `.clear()`;
`Executor(store, outbox).execute(action, case_id) -> dict`; `replay(store, outbox)`;
`ApprovalQueue(outbox, executor)`: `submit`, `list(status)`, `get`, `approve(id, reviewer, note)`, `reject(...)`.
- [ ] Tests: second refund with same key returns `status="duplicate_ignored"` and writes nothing;
      ticket dedupe by key; approve executes exactly once, approving again raises; reject never executes;
      state survives a new Executor/Queue on the same outbox dir (replay).
- [ ] Implement, run, commit.

### Task 7: Drafter + pipeline
**Files:** drafter.py, pipeline.py, tests/test_pipeline.py
**Produces:** `Agent.handle(req) -> CaseResult`, `Agent.scan() -> list[CaseResult]`, `build_agent(data_dir, outbox_dir, llm)`.
- [ ] Tests: duplicate O123 end-to-end → refund executed, reply mentions $34.00; unknown email → NEED_INFO,
      nothing executed; refund O789 → pending approval id returned, ticket executed; not_owner reply does
      not contain "another customer"; audit has one record per case; Claude drafter failure → template.
- [ ] Implement, run, commit.

### Task 8: Scenario eval
**Files:** evals/scenarios.yaml, evals.py, tests/test_scenarios.py
**Produces:** `load_scenarios(path)`, `run_scenario(s, llm) -> ScenarioResult`, `run_all(llm) -> list[ScenarioResult]`.
- [ ] All 20 scenarios from spec §9 (incl. invalid email, repeated duplicate, scan) pass offline.
- [ ] Commit.

### Task 9: API + CLI
**Files:** api.py, cli.py, tests/test_api.py
- [ ] TestClient: POST /requests duplicate → AUTO; refund O789 → pending; GET /approvals shows it;
      approve → executed; approve again → 409; unknown id → 404; malformed body handled; /reset clears.
- [ ] CLI smoke: `uv run opsagent handle ...`, `scan`, `approvals list`, `eval`.
- [ ] Commit.

### Task 10: README + Loom script
**Files:** README.md, docs/loom-script.md
- [ ] Architecture diagram, design decisions, how to run, testing, limitations, production improvements.
- [ ] Loom script (3–5 min) with timestamps.
- [ ] Final full test run, commit.
