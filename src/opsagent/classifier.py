"""Intent classification + entity extraction.

Two interchangeable implementations behind one interface:
- RuleClassifier: keyword/regex, EN + VI, zero cost, always available.
- ClaudeClassifier: Claude structured output; used when credentials exist.
FallbackClassifier wraps the two so an LLM failure never breaks a request.
"""

from __future__ import annotations

import re
from typing import Optional, Protocol

from .models import Extraction, Intent
from .validate import detect_injection, normalise

ORDER_ID_RE = re.compile(r"\b(O\d{3,})\b", re.IGNORECASE)
AMOUNT_RE = re.compile(
    r"\$\s?(\d+(?:[.,]\d{1,2})?)|(\d+(?:[.,]\d{1,2})?)\s?(?:usd|dollars?|đô)\b", re.IGNORECASE
)


class Classifier(Protocol):
    def classify(self, message: str) -> Extraction: ...


def extract_order_id(text: str) -> Optional[str]:
    m = ORDER_ID_RE.search(text)
    return m.group(1).upper() if m else None


def extract_amount(text: str) -> Optional[float]:
    m = AMOUNT_RE.search(text)
    if not m:
        return None
    return float((m.group(1) or m.group(2)).replace(",", "."))


# Checked in order: the first intent with a matching pattern wins.
# duplicate_charge precedes refund_request ("charged twice, please refund").
RULES: list[tuple[Intent, list[str]]] = [
    (Intent.DUPLICATE_CHARGE, [
        r"charged (me )?(twice|two times|2 times|double)",
        r"double[- ]charged?", r"duplicate (charge|payment)", r"charged .* twice",
        r"trừ tiền (2|hai) lần", r"bị trừ .*(2|hai) lần", r"thanh toán (2|hai) lần",
    ]),
    (Intent.CANCEL_SUBSCRIPTION, [
        r"cancel (my |the )?(subscription|plan|membership|box)", r"unsubscribe",
        r"(hủy|huỷ) (gói|đăng ký|subscription)",
    ]),
    (Intent.REFUND_REQUEST, [r"\brefund", r"money back", r"hoàn tiền", r"trả lại tiền"]),
    (Intent.SHIPPING_ISSUE, [
        r"(haven't|have not|hasn't|has not|didn't|did not|never|not yet) "
        r"(received|arrived|arrive|come|got|been delivered)",
        r"not (arrived|delivered)", r"where is my (package|parcel)", r"lost (package|parcel)",
        r"chưa (nhận được|nhận|giao|tới|đến)",
    ]),
    (Intent.ORDER_STATUS, [
        r"where is (my )?order", r"\bstatus\b", r"\btrack", r"đơn .*(ở đâu|tới đâu|thế nào)",
        r"trạng thái",
    ]),
]
_COMPILED = [(intent, re.compile("|".join(pats), re.IGNORECASE)) for intent, pats in RULES]


class RuleClassifier:
    def classify(self, message: str) -> Extraction:
        text = normalise(message)
        intent = next((i for i, rx in _COMPILED if rx.search(text)), Intent.UNKNOWN)
        return Extraction(
            intent=intent,
            order_id=extract_order_id(text),
            claimed_amount=extract_amount(text),
            confidence=0.3 if intent == Intent.UNKNOWN else 0.8,
            injection_suspected=detect_injection(text),
            source="rules",
        )
