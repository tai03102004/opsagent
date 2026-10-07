"""Shared vocabulary of the agent: domain records, pipeline artefacts, decisions."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- domain data


class Customer(BaseModel):
    id: str
    email: str
    name: str


class Order(BaseModel):
    id: str
    customer_id: str
    items: list[str]
    total: float
    status: Literal["paid", "shipped", "delivered", "refunded"]
    created_at: datetime
    shipped_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None


class Payment(BaseModel):
    id: str
    order_id: str
    amount: float
    status: Literal["succeeded", "failed", "refunded"]
    created_at: datetime


class Subscription(BaseModel):
    id: str
    customer_id: str
    plan: str
    price: float
    status: Literal["active", "cancelled"]
    last_renewal_status: Literal["ok", "failed"]
    last_renewal_at: datetime


# ---------------------------------------------------------------- pipeline


class Intent(str, Enum):
    ORDER_STATUS = "order_status"
    DUPLICATE_CHARGE = "duplicate_charge"
    REFUND_REQUEST = "refund_request"
    CANCEL_SUBSCRIPTION = "cancel_subscription"
    SHIPPING_ISSUE = "shipping_issue"
    UNKNOWN = "unknown"


class Tier(str, Enum):
    AUTO = "AUTO"
    NEED_INFO = "NEED_INFO"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    BLOCK = "BLOCK"


# Higher = more restrictive. The overall tier of a case is the max over its actions.
TIER_RANK = {Tier.AUTO: 0, Tier.NEED_INFO: 1, Tier.NEEDS_APPROVAL: 2, Tier.BLOCK: 3}

BlockReason = Literal[
    "order_not_found", "not_owner", "amount_mismatch", "already_refunded", "no_active_subscription"
]


class SupportRequest(BaseModel):
    # Optional on purpose: malformed input must produce NEED_INFO, not a crash.
    customer_email: Optional[str] = None
    message: Optional[str] = None
    request_id: Optional[str] = None


class Extraction(BaseModel):
    intent: Intent
    order_id: Optional[str] = None
    claimed_amount: Optional[float] = None
    confidence: float = Field(ge=0.0, le=1.0)
    injection_suspected: bool = False
    amount_ambiguous: bool = False  # several amounts in the text, or an LLM amount we couldn't find in it
    source: Literal["claude", "rules"] = "rules"
    fallback_reason: Optional[str] = None


class Verification(BaseModel):
    confirmed: bool
    evidence: list[str] = []
    block_reason: Optional[BlockReason] = None
    order_id: Optional[str] = None
    payment_id: Optional[str] = None
    subscription_id: Optional[str] = None
    amount: Optional[float] = None  # never more than what our records say is still refundable
    refunded_so_far: float = 0.0  # already refunded on this order (policy caps the cumulative total)


class ActionType(str, Enum):
    CREATE_TICKET = "create_ticket"
    NOTIFY_SLACK = "notify_slack"
    ISSUE_REFUND = "issue_refund"
    CANCEL_SUBSCRIPTION = "cancel_subscription"
    ESCALATE_HUMAN = "escalate_human"
    REPLY_CUSTOMER = "reply_customer"


class Action(BaseModel):
    type: ActionType
    params: dict[str, Any] = {}


class ActionDecision(BaseModel):
    action: Action
    tier: Tier
    reasons: list[str]


class Decision(BaseModel):
    tier: Tier
    actions: list[ActionDecision] = []
    reasons: list[str] = []


FindingKind = Literal["duplicate_charge", "failed_renewal", "unshipped_order", "delayed_shipment"]


class Finding(BaseModel):
    kind: FindingKind
    customer_id: str
    severity: Literal["high", "medium", "low"]
    evidence: list[str]
    verification: Verification


class CaseResult(BaseModel):
    case_id: str
    kind: Literal["request", "scan"]
    replayed: bool = False  # true when returned from the idempotency index instead of being re-processed
    tier: Tier
    customer_id: Optional[str] = None
    input: dict[str, Any] = {}
    missing: list[str] = []
    extraction: Optional[Extraction] = None
    verification: Optional[Verification] = None
    decision: Decision
    executed: list[dict[str, Any]] = []
    pending_approvals: list[str] = []
    summary: str = ""
    customer_reply: Optional[str] = None
    reply_source: Optional[str] = None  # "claude" or "templates"
    reply_fallback_reason: Optional[str] = None  # e.g. a fact-guard violation
