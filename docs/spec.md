# Ops Automation Agent — Design Spec

Status: approved design (2026-10-06). Source of truth for the implementation plan and README.

## 1. Problem

A DTC brand's support/ops team receives free-text customer requests and has operational data
(orders, payments, subscriptions). We want an AI agent that:

1. understands a request (or scans data) and detects issues that need attention,
2. verifies the issue against real data instead of trusting the customer's claim,
3. summarises it and recommends a next action,
4. executes (simulated) safe actions automatically,
5. routes risky actions to a human approver, and blocks invalid ones,
6. handles invalid/incomplete input without guessing.

## 2. Core principle

**LLM proposes, code disposes.** The LLM is used only for language work: classifying intent,
extracting entities, and drafting text. Every decision with consequences (verifying facts,
choosing whether an action may run) is deterministic Python that can be unit-tested.

Consequences:
- Safety properties are tested deterministically, offline, for free.
- Money amounts always come from the data store, never from the message or the LLM.
- Customer text is treated as data; it cannot change policy.

## 3. Fictional domain: "Brewly"

DTC coffee brand selling one-off orders and monthly subscriptions.
Mock data lives in `data/*.json`. A fixed reference clock (`NOW = 2026-10-06T12:00:00Z`,
overridable via `OPSAGENT_NOW`) makes time-based rules deterministic.

| Entity | Fields |
|---|---|
| customer | id, email, name |
| order | id, customer_id, items, total, status (`paid`/`shipped`/`delivered`/`refunded`), created_at, shipped_at, delivered_at |
| payment | id, order_id, amount, status (`succeeded`/`failed`), created_at |
| subscription | id, customer_id, plan, price, status (`active`/`cancelled`), last_renewal_status (`ok`/`failed`), last_renewal_at |

Seeded situations:

| Record | Situation | Used by |
|---|---|---|
| C001 anna@example.com | — | |
| O123 (C001, $34.00, shipped) | two succeeded $34.00 payments 3 min apart | duplicate charge, scan |
| O124 (C001, $28.00, shipped 2026-10-03) | single payment | order status, false duplicate claim |
| S1 (C001, active) | | cancel subscription |
| C002 ben@example.com | — | |
| O456 (C002, $45.00, delivered) | | amount-mismatch claim |
| O457 (C002, $18.00, delivered) | | small refund (auto) |
| S2 (C002, active, last renewal failed) | | scan |
| C003 chloe@example.com | — | |
| O789 (C003, $120.00, delivered) | | large refund (approval) |
| O321 (C003, $30.00, refunded) | | already refunded |
| C004 david@example.com | — | |
| O555 (C004, $52.00, paid 2026-09-28, not shipped) | paid > 5 days, not shipped | scan |
| O654 (C004, $40.00, shipped 2026-09-25, not delivered) | shipped > 7 days | shipping issue, scan |
| O658 (C004, $26.00, delivered) | failed payment + successful retry 4 min later (decoy) | scan must NOT flag it |

## 4. Inputs

- **handle** — one request `{customer_email, message}`. Email stands in for an authenticated identity.
- **scan** — proactive pass over the data store. Detectors:
  - duplicate charge: ≥2 succeeded payments, same order, same amount, < 24h apart
  - failed subscription renewal
  - order paid > 5 days and not shipped
  - order shipped > 7 days and not delivered

  Each finding goes through the same policy engine as a request.

## 5. Pipeline (handle)

```
validate → classify+extract → missing-info check → verify against data
        → recommend actions → POLICY → execute AUTO actions / queue approvals
        → draft summary + customer reply → audit
```

1. **validate** — empty/oversized message or malformed email → `NEED_INFO`. Unknown customer email →
   `NEED_INFO` ("we couldn't find an account for this email"). Regex prompt-injection heuristics set
   `injection_suspected`.
2. **classify+extract** — `Classifier` interface returns
   `Extraction{intent, order_id?, claimed_amount?, confidence, injection_suspected, source}`.
   Implementations: `ClaudeClassifier` (structured output) and `RuleClassifier` (keywords/regex, EN+VI).
   Claude is used when credentials exist; on missing key or API error we fall back to rules and record
   `source="rules"`.
3. **missing info** — intents `order_status`, `duplicate_charge`, `refund_request`, `shipping_issue`
   require `order_id`. If absent → `NEED_INFO` naming the missing field. Never inferred, even if the
   customer has a single order.
4. **verify** — per intent, deterministic checks producing `Verification{confirmed, evidence[], block_reason?}`.
   Block reasons: `order_not_found`, `not_owner`, `amount_mismatch` (claimed > paid), `already_refunded`.
5. **recommend** — map intent + verification to a list of actions from a fixed catalog:
   `reply_customer`, `request_info`, `create_ticket`, `notify_slack`, `issue_refund`,
   `cancel_subscription`, `escalate_human`.
6. **policy** — see §6. Produces a per-action decision plus an overall tier.
7. **execute** — AUTO actions run via `Executor` (simulated, append to `outbox/*.jsonl`).
   APPROVAL actions go to the approval queue and run only after `approve`.
8. **draft** — `Drafter` writes an internal summary and a customer reply from verified facts and the
   decision (Claude, with template fallback). The reply must not promise anything not decided.
9. **audit** — every request/finding writes one audit record with the full trace.

## 6. Guardrail policy (`policy.py`)

Tiers: `AUTO`, `NEEDS_APPROVAL`, `BLOCK`, `NEED_INFO`. Overall tier = most restrictive action tier.

| Rule | Result |
|---|---|
| verification has `block_reason` | `BLOCK` all side-effecting actions; safe reply only |
| `injection_suspected` | `NEEDS_APPROVAL` for the whole case |
| `confidence < 0.7` or intent `unknown` | `NEEDS_APPROVAL` (human triage) |
| `reply_customer`, `request_info`, `create_ticket`, `notify_slack` | `AUTO` |
| `issue_refund` ≤ $50 and verified | `AUTO` |
| `issue_refund` > $50 | `NEEDS_APPROVAL` |
| `cancel_subscription` | `NEEDS_APPROVAL` |

Constants: `AUTO_REFUND_LIMIT = 50.00`, `MIN_CONFIDENCE = 0.7`. Each decision carries human-readable
`reasons`.

Intent → verification → actions:

| Intent | Verified when | Actions |
|---|---|---|
| order_status | order exists & owned | reply |
| duplicate_charge | duplicate payments found | refund(dup amount) + ticket + slack #billing + reply |
| duplicate_charge | not found | ticket (investigate) + reply |
| refund_request | order owned, not refunded, claim ≤ paid | refund(order total from DB) + ticket + reply |
| cancel_subscription | active subscription exists | cancel_subscription + reply |
| shipping_issue | shipped > 7 days, not delivered | ticket + slack #fulfillment + reply |
| unknown | — | escalate_human + reply |

## 7. Reliability details

- **Idempotency**: refunds keyed `refund:{order_id}`; executor refuses a second refund, and a refunded
  order is marked so later requests hit `already_refunded`.
- **Approvals**: `pending → approved|rejected`; approve executes the stored action exactly once.
- **LLM failure**: timeout/API error/invalid output → rules fallback; never crash the request.
- **Determinism**: injected clock; LLM output validated by Pydantic before use.

## 8. Interfaces

API (FastAPI, Swagger at `/docs`): `POST /requests`, `POST /scan`, `GET /approvals`,
`POST /approvals/{id}/approve`, `POST /approvals/{id}/reject`, `GET /audit`, `POST /reset`.

CLI: `opsagent handle --email --message`, `opsagent scan`, `opsagent approvals [list|approve|reject]`,
`opsagent eval [--live]`, `opsagent serve`.

Model: env `OPSAGENT_MODEL`, default `claude-opus-5-5` (effort `low`); `claude-haiku-4-5` is a cheaper option.

## 9. Testing

1. Unit tests — validate, rules classifier, detector, policy, executor idempotency, approvals.
2. Offline scenario eval — `evals/scenarios.yaml`, full pipeline with `RuleClassifier`; 0 tokens; runs in `pytest`.
3. Live eval — `opsagent eval --live` runs the same scenarios with Claude; opt-in, costs tokens.

Scenarios: order status; real duplicate ($34 auto refund); false duplicate claim; small refund ($18 auto);
large refund ($120 approval); missing order id; empty message; unknown email; other customer's order;
non-existent order; claimed $500 on $45 order; already refunded; cancel subscription; prompt injection;
vague message; Vietnamese duplicate-charge message; repeated duplicate request (no double refund);
shipping issue; scan finds 4 seeded issues.

## 10. Out of scope / known limitations

Real auth, real integrations (Stripe/Slack/Zendesk), persistence beyond JSONL, multi-turn conversation,
refund windows and partial refunds, multi-order requests, rate limiting, UI beyond Swagger.
