"""One structured Claude call, robust to gateways that drop `output_config.format`.

We always request JSON-schema structured output. The direct Anthropic API enforces it; some
Anthropic-compatible gateways silently ignore it, and the model then answers with fenced JSON
plus prose. We parse tolerantly, but the result must still validate against the Pydantic model.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMOutputError(RuntimeError):
    pass


def extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = _FENCE_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    if start == -1:
        raise LLMOutputError("no JSON object in model output")
    try:
        obj, _ = json.JSONDecoder().raw_decode(candidate[start:])
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"invalid JSON in model output: {exc}") from exc
    if not isinstance(obj, dict):
        raise LLMOutputError("model output is not a JSON object")
    return obj


def structured_call(client, model: str, system: str, user: str, schema: type[T],
                    max_tokens: int = 4096) -> tuple[T, dict[str, Any]]:
    import anthropic

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}},
    )
    if response.stop_reason == "refusal":
        raise LLMOutputError("model refused")
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")

    strict = True
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        strict = False  # structured output was not enforced upstream
        data = extract_json(text)
    try:
        parsed = schema.model_validate(data)
    except ValidationError as exc:
        raise LLMOutputError(f"output does not match schema: {exc.errors()[:2]}") from exc
    return parsed, {"strict_json": strict, "served_model": getattr(response, "model", None),
                    "stop_reason": response.stop_reason}
