"""Command-line interface: opsagent handle | scan | approvals | eval | serve | reset."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .models import CaseResult, SupportRequest

TIER_ICON = {"AUTO": "🟢", "NEEDS_APPROVAL": "🟡", "BLOCK": "🔴", "NEED_INFO": "❓"}


def _print_case(r: CaseResult, as_json: bool) -> None:
    if as_json:
        print(r.model_dump_json(indent=2))
        return
    replayed = "  [replayed: same request_id, nothing re-executed]" if r.replayed else ""
    print(f"{TIER_ICON[r.tier.value]} {r.tier.value}  ({r.case_id}, {r.kind}){replayed}")
    if r.extraction:
        e = r.extraction
        flags = " ⚠️ injection suspected" if e.injection_suspected else ""
        print(f"  understood : {e.intent.value} order={e.order_id} conf={e.confidence:.2f} via {e.source}{flags}")
    if r.verification:
        v = r.verification
        print(f"  verified   : confirmed={v.confirmed} block={v.block_reason} evidence={v.evidence}")
    for d in r.decision.actions:
        print(f"  decision   : {d.action.type.value:20} -> {d.tier.value:15} {'; '.join(d.reasons)}")
    if r.missing:
        print(f"  missing    : {', '.join(r.missing)}")
    for e in r.executed:
        print(f"  executed   : {e['type']} {e['id']} [{e['status']}]")
    for a in r.pending_approvals:
        print(f"  pending    : approval {a}")
    print(f"  summary    : {r.summary}")
    if r.customer_reply:
        print("  reply      :\n    " + r.customer_reply.replace("\n", "\n    "))


def _check_llm() -> int:
    """Diagnostics for a new key or gateway (e.g. ANTHROPIC_BASE_URL pointing at a proxy)."""
    import os
    import time

    import anthropic

    from .classifier import SYSTEM_PROMPT, _LLMExtraction
    from .config import model_name
    from .llm_json import structured_call

    print(f"base_url : {os.getenv('ANTHROPIC_BASE_URL', 'https://api.anthropic.com (default)')}")
    print(f"model    : {model_name()} (requested)")
    started = time.time()
    try:
        out, meta = structured_call(
            anthropic.Anthropic(max_retries=0, timeout=60.0), model_name(), SYSTEM_PROMPT,
            "<customer_message>\nTôi bị trừ tiền 2 lần cho đơn hàng O123\n</customer_message>", _LLMExtraction,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED   : {type(exc).__name__}: {str(exc)[:300]}")
        return 1
    print(f"served   : {meta['served_model']}  stop={meta['stop_reason']}  {time.time() - started:.1f}s")
    print(f"parsed   : {out}")
    print("schema   : " + ("enforced (strict JSON)" if meta["strict_json"]
                           else "NOT enforced by this endpoint -> tolerant parsing + Pydantic validation used"))
    ok = out.intent.value == "duplicate_charge" and out.order_id == "O123"
    print("OK: classification works" if ok else "WARNING: unexpected classification")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="opsagent", description="Brewly operations automation agent")
    p.add_argument("--llm", choices=["auto", "claude", "off"], help="override OPSAGENT_LLM")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("handle", help="process one customer request")
    h.add_argument("--email", required=True)
    h.add_argument("--message", required=True)
    h.add_argument("--request-id", help="client idempotency key: resending the same id never re-processes")
    h.add_argument("--json", action="store_true")

    s = sub.add_parser("scan", help="scan operational data for issues")
    s.add_argument("--json", action="store_true")

    a = sub.add_parser("approvals", help="list / approve / reject queued actions")
    a.add_argument("action", choices=["list", "approve", "reject"])
    a.add_argument("id", nargs="?")
    a.add_argument("--reviewer", default="ops-lead@brewly.com")
    a.add_argument("--note", default="")
    a.add_argument("--all", action="store_true", help="list all, not only pending")

    e = sub.add_parser("eval", help="run scenario evaluation")
    e.add_argument("--live", action="store_true", help="use Claude (needs ANTHROPIC_API_KEY; costs tokens)")

    sv = sub.add_parser("serve", help="start the API (Swagger UI at /docs)")
    sv.add_argument("--port", type=int, default=8000)

    sub.add_parser("reset", help="clear the outbox (restore seed state)")
    sub.add_parser("check-llm", help="one Claude call: is the key/base URL/model/structured output working?")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR, format="%(levelname)s %(message)s")

    if args.cmd == "eval":
        from .evals import format_report, run_all

        results = run_all(llm="claude" if args.live else "off")
        print(format_report(results))
        return 0 if all(r.passed for r in results) else 1

    if args.cmd == "check-llm":
        return _check_llm()

    if args.cmd == "serve":
        import os

        import uvicorn

        if args.llm:
            os.environ["OPSAGENT_LLM"] = args.llm
        uvicorn.run("opsagent.api:create_app", factory=True, port=args.port)
        return 0

    from .pipeline import build_agent

    agent = build_agent(llm=args.llm)

    if args.cmd == "reset":
        agent.outbox.clear()
        print("outbox cleared; seed state restored")
    elif args.cmd == "handle":
        from .pipeline import IdempotencyConflict

        try:
            result = agent.handle(SupportRequest(customer_email=args.email, message=args.message,
                                                 request_id=args.request_id))
        except IdempotencyConflict as exc:
            print(str(exc), file=sys.stderr)
            return 1
        _print_case(result, args.json)
    elif args.cmd == "scan":
        for r in agent.scan():
            _print_case(r, args.json)
            print()
    elif args.cmd == "approvals":
        if args.action == "list":
            for rec in agent.list_approvals(None if args.all else "pending"):
                print(f"{rec.id} [{rec.status}] {rec.action.type.value} {json.dumps(rec.action.params)} "
                      f"case={rec.case_id} reasons={rec.reasons}")
        else:
            if not args.id:
                p.error("approval id required")
            try:
                fn = agent.approve if args.action == "approve" else agent.reject
                rec = fn(args.id, reviewer=args.reviewer, note=args.note)
            except KeyError:
                print(f"approval {args.id} not found", file=sys.stderr)
                return 1
            except Exception as exc:  # ApprovalError
                print(str(exc), file=sys.stderr)
                return 1
            print(f"{rec.id} -> {rec.status} by {rec.reviewer}; result={rec.result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
