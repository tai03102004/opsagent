"""Scenario evaluation: run every scenario on a fresh world and compare with expectations.

Offline (default) uses rules/templates -> deterministic, free. --live uses Claude, no fallback.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel

from .config import SCENARIOS_FILE
from .models import CaseResult, SupportRequest
from .pipeline import build_agent


class ScenarioResult(BaseModel):
    id: str
    passed: bool
    failures: list[str] = []
    source: Optional[str] = None
    tier: Optional[str] = None


def load_scenarios(path: Path = SCENARIOS_FILE) -> list[dict[str, Any]]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _side_effects(records: list[dict]) -> list[str]:
    return [r["type"] for r in records if r["type"] != "reply_customer" and r.get("status") == "done"]


def check(result: CaseResult, expect: dict[str, Any], pending_types: list[str]) -> list[str]:
    failures = []

    def cmp(name, got, want):
        if got != want:
            failures.append(f"{name}: expected {want!r}, got {got!r}")

    cmp("tier", result.tier.value, expect["tier"])
    if "intent" in expect:
        cmp("intent", result.extraction.intent.value if result.extraction else None, expect["intent"])
    if "executed" in expect:
        cmp("executed", _side_effects(result.executed), expect["executed"])
    if "pending" in expect:
        cmp("pending", pending_types, expect["pending"])
    if "block_reason" in expect:
        cmp("block_reason", result.verification.block_reason if result.verification else None, expect["block_reason"])
    if "missing" in expect:
        cmp("missing", result.missing, expect["missing"])
    if "injection" in expect:
        cmp("injection", bool(result.extraction and result.extraction.injection_suspected), expect["injection"])
    return failures


def run_scenario(s: dict[str, Any], llm: str = "off") -> ScenarioResult:
    with tempfile.TemporaryDirectory() as tmp:
        agent = build_agent(outbox_dir=Path(tmp), llm=llm)
        if s.get("scan"):
            kinds = sorted(r.input["finding"] for r in agent.scan())
            want = sorted(s["expect"]["findings"])
            fails = [] if kinds == want else [f"findings: expected {want}, got {kinds}"]
            return ScenarioResult(id=s["id"], passed=not fails, failures=fails)

        for msg in s.get("setup", []):
            agent.handle(SupportRequest(customer_email=s["email"], message=msg))
        result = agent.handle(SupportRequest(customer_email=s["email"], message=s["message"]))
        pending = [agent.approvals.get(a).action.type.value for a in result.pending_approvals]
        fails = check(result, s["expect"], pending)
        if llm == "claude" and result.extraction and result.extraction.source != "claude":
            fails.append(f"expected Claude, got fallback: {result.extraction.fallback_reason}")
        return ScenarioResult(id=s["id"], passed=not fails, failures=fails, tier=result.tier.value,
                              source=result.extraction.source if result.extraction else None)


def run_all(llm: str = "off", path: Path = SCENARIOS_FILE) -> list[ScenarioResult]:
    return [run_scenario(s, llm) for s in load_scenarios(path)]


def format_report(results: list[ScenarioResult]) -> str:
    lines = [f"{'scenario':40} {'result':6} {'tier':15} source"]
    for r in results:
        lines.append(f"{r.id:40} {'PASS' if r.passed else 'FAIL':6} {r.tier or '-':15} {r.source or '-'}")
        lines += [f"    - {f}" for f in r.failures]
    passed = sum(r.passed for r in results)
    lines.append(f"\n{passed}/{len(results)} scenarios passed")
    return "\n".join(lines)
