import threading

import pytest

from opsagent.models import SupportRequest, Tier
from opsagent.pipeline import IdempotencyConflict, build_agent


def req(message, request_id=None, email="ben@example.com"):
    return SupportRequest(customer_email=email, message=message, request_id=request_id)


def refunds(agent):
    return agent.outbox.read("refunds")


# ------------------------------------------------------------- client idempotency key

def test_same_request_id_is_replayed_not_reprocessed(outbox_dir):
    agent = build_agent(outbox_dir=outbox_dir, llm="off")
    first = agent.handle(req("Please refund $5 for order O457", "req-1"))
    second = agent.handle(req("Please refund $5 for order O457", "req-1"))
    assert first.tier == Tier.AUTO and not first.replayed
    assert second.replayed and second.case_id == first.case_id
    assert len(refunds(agent)) == 1


def test_without_request_id_two_submissions_are_two_requests(outbox_dir):
    agent = build_agent(outbox_dir=outbox_dir, llm="off")
    agent.handle(req("Please refund $5 for order O457"))
    agent.handle(req("Please refund $5 for order O457"))
    assert len(refunds(agent)) == 2  # documented: the client must send a key to get dedupe


def test_same_request_id_with_different_payload_is_a_conflict(outbox_dir):
    agent = build_agent(outbox_dir=outbox_dir, llm="off")
    agent.handle(req("Please refund $5 for order O457", "req-1"))
    with pytest.raises(IdempotencyConflict):
        agent.handle(req("Please refund $15 for order O457", "req-1"))


def test_request_ids_survive_a_restart(outbox_dir):
    build_agent(outbox_dir=outbox_dir, llm="off").handle(req("Please refund $5 for order O457", "req-1"))
    again = build_agent(outbox_dir=outbox_dir, llm="off").handle(req("Please refund $5 for order O457", "req-1"))
    assert again.replayed


# ------------------------------------------------------------- two processes, one outbox

def test_second_process_sees_the_first_process_refund(outbox_dir):
    a = build_agent(outbox_dir=outbox_dir, llm="off")
    b = build_agent(outbox_dir=outbox_dir, llm="off")  # loaded BEFORE a's refund: its memory is stale
    a.handle(req("Please refund $10 for order O457"))
    r = b.handle(req("Please refund $10 for order O457, another bag"))
    assert r.tier == Tier.BLOCK and r.verification.block_reason == "amount_mismatch"
    assert len(refunds(a)) == 1


def test_concurrent_refunds_cannot_exceed_what_was_paid(outbox_dir):
    agents = [build_agent(outbox_dir=outbox_dir, llm="off") for _ in range(2)]
    start = threading.Barrier(2)
    results = []

    def run(agent, n):
        start.wait()
        results.append(agent.handle(req(f"Please refund $10 for order O457 (attempt {n})")))

    threads = [threading.Thread(target=run, args=(a, i)) for i, a in enumerate(agents)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.tier.value for r in results) == ["AUTO", "BLOCK"]
    assert sum(r["amount"] for r in refunds(agents[0])) == 10.0
