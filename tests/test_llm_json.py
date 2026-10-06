import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from opsagent.llm_json import LLMOutputError, extract_json, structured_call


class Out(BaseModel):
    intent: str
    confidence: float


GOOD = {"intent": "duplicate_charge", "confidence": 0.9}


@pytest.mark.parametrize(
    "text",
    [
        json.dumps(GOOD),
        "```json\n" + json.dumps(GOOD, indent=2) + "\n```\nThe order id is stated in the message.",
        "Here you go: " + json.dumps(GOOD) + " hope it helps",
    ],
)
def test_extract_json_tolerates_fences_and_prose(text):
    assert extract_json(text) == GOOD


@pytest.mark.parametrize("text", ["", "no json here", "{broken"])
def test_extract_json_rejects_garbage(text):
    with pytest.raises(LLMOutputError):
        extract_json(text)


class FakeMessages:
    def __init__(self, text, stop_reason="end_turn"):
        self.text, self.stop_reason, self.calls = text, stop_reason, []

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)],
                               stop_reason=self.stop_reason, model="served-model")


def test_structured_call_sends_schema_and_reports_strictness():
    client = SimpleNamespace(messages=FakeMessages(json.dumps(GOOD)))
    out, meta = structured_call(client, "m", "sys", "user", Out)
    assert out == Out(**GOOD) and meta["strict_json"] is True and meta["served_model"] == "served-model"
    fmt = client.messages.calls[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False


def test_structured_call_with_gateway_that_ignores_schema():
    client = SimpleNamespace(messages=FakeMessages("```json\n" + json.dumps(GOOD) + "\n```"))
    out, meta = structured_call(client, "m", "sys", "user", Out)
    assert out.intent == "duplicate_charge" and meta["strict_json"] is False


def test_structured_call_validates_schema():
    client = SimpleNamespace(messages=FakeMessages(json.dumps({"intent": "x"})))
    with pytest.raises(LLMOutputError):
        structured_call(client, "m", "sys", "user", Out)


def test_refusal_raises():
    client = SimpleNamespace(messages=FakeMessages("", stop_reason="refusal"))
    with pytest.raises(LLMOutputError):
        structured_call(client, "m", "sys", "user", Out)
