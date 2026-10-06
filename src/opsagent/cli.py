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
    print(f"{TIER_ICON[r.tier.value]} {r.tier.value}  ({r.case_id}, {r.kind})")
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="opsagent", description="Brewly operations automation agent")
    p.add_argument("--llm", choices=["auto", "claude", "off"], help="override OPSAGENT_LLM")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("handle", help="process one customer request")
    h.add_argument("--email", required=True)
    h.add_argument("--message", required=True)
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

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR, format="%(levelname)s %(message)s")

    if args.cmd == "eval":
        from .evals import format_report, run_all

        results = run_all(llm="claude" if args.live else "off")
        print(format_report(results))
        return 0 if all(r.passed for r in results) else 1

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
        _print_case(agent.handle(SupportRequest(customer_email=args.email, message=args.message)), args.json)
    elif args.cmd == "scan":
        for r in agent.scan():
            _print_case(r, args.json)
            print()
    elif args.cmd == "approvals":
        q = agent.approvals
        if args.action == "list":
            for rec in q.list(None if args.all else "pending"):
                print(f"{rec.id} [{rec.status}] {rec.action.type.value} {json.dumps(rec.action.params)} "
                      f"case={rec.case_id} reasons={rec.reasons}")
        else:
            if not args.id:
                p.error("approval id required")
            try:
                fn = q.approve if args.action == "approve" else q.reject
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
