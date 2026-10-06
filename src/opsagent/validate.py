"""Input validation that runs before any LLM call. Cheap, deterministic, testable."""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel

from .models import Intent, SupportRequest

MAX_MESSAGE_CHARS = 2000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Intents that cannot be acted on without an order id. We never guess it.
REQUIRES_ORDER_ID = {
    Intent.ORDER_STATUS,
    Intent.DUPLICATE_CHARGE,
    Intent.REFUND_REQUEST,
    Intent.SHIPPING_ISSUE,
}

# Heuristics, not a security boundary: the real defence is that the LLM cannot take actions.
# A hit only forces the case to human review.
INJECTION_PATTERNS = [
    r"ignore (all |any |your |previous |prior |the |of |these )*(rules|instructions|policies|guidelines)",
    r"(system|developer) prompt",
    r"you are now",
    r"(admin|developer|god) mode",
    r"override (the |your )?(rules|policy|limits?)",
    r"bỏ qua (mọi |tất cả |các )?(quy tắc|hướng dẫn|chỉ dẫn)",
]
_INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)


class ValidationResult(BaseModel):
    ok: bool
    missing: list[str] = []
    problems: list[str] = []
    injection_suspected: bool = False
    message: str = ""


def normalise(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()


def detect_injection(text: str) -> bool:
    return bool(text) and bool(_INJECTION_RE.search(normalise(text)))


def validate_request(req: SupportRequest) -> ValidationResult:
    missing: list[str] = []
    problems: list[str] = []

    email = (req.customer_email or "").strip()
    if not EMAIL_RE.match(email):
        missing.append("customer_email")

    message = normalise(req.message or "")
    if not message:
        missing.append("message")
    elif len(message) > MAX_MESSAGE_CHARS:
        problems.append(f"message longer than {MAX_MESSAGE_CHARS} characters")

    return ValidationResult(
        ok=not missing and not problems,
        missing=missing,
        problems=problems,
        injection_suspected=detect_injection(message),
        message=message,
    )
