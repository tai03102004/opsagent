import pytest
from fastapi.testclient import TestClient

from opsagent.api import create_app


@pytest.fixture
def client(outbox_dir):
    return TestClient(create_app(outbox_dir=outbox_dir, llm="off"))


def post_request(client, email, message):
    return client.post("/requests", json={"customer_email": email, "message": message})


def test_duplicate_charge_auto(client):
    r = post_request(client, "anna@example.com", "I was charged twice for order O123!")
    assert r.status_code == 200 and r.json()["tier"] == "AUTO"


def test_approval_flow(client):
    r = post_request(client, "chloe@example.com", "Refund order O789 please").json()
    assert r["tier"] == "NEEDS_APPROVAL"
    aid = r["pending_approvals"][0]

    pending = client.get("/approvals", params={"status": "pending"}).json()
    assert [a["id"] for a in pending] == [aid]

    ok = client.post(f"/approvals/{aid}/approve", json={"reviewer": "lead@brewly.com", "note": "ok"})
    assert ok.status_code == 200 and ok.json()["result"]["status"] == "done"

    again = client.post(f"/approvals/{aid}/approve", json={"reviewer": "lead@brewly.com"})
    assert again.status_code == 409


def test_reject(client):
    aid = post_request(client, "anna@example.com", "Please cancel my subscription.").json()["pending_approvals"][0]
    r = client.post(f"/approvals/{aid}/reject", json={"reviewer": "lead@brewly.com", "note": "retention call first"})
    assert r.json()["status"] == "rejected" and r.json()["result"] is None


def test_unknown_approval_404(client):
    assert client.post("/approvals/A-9999/approve", json={"reviewer": "x"}).status_code == 404


def test_reviewer_required(client):
    assert client.post("/approvals/A-0001/approve", json={}).status_code == 422


@pytest.mark.parametrize("body", [{}, {"message": "hi"}, {"customer_email": 123, "message": None}])
def test_malformed_body_is_handled(client, body):
    r = client.post("/requests", json=body)
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        assert r.json()["tier"] == "NEED_INFO"


def test_scan_audit_reset(client):
    assert len(client.post("/scan").json()) == 4
    assert len(client.get("/audit").json()) >= 4
    client.post("/reset")
    assert client.get("/audit").json() == []
    assert len(client.post("/scan").json()) == 4  # seed state restored


def test_idempotency_key_header(client):
    body = {"customer_email": "ben@example.com", "message": "Please refund $5 for order O457"}
    first = client.post("/requests", json=body, headers={"Idempotency-Key": "k-1"}).json()
    second = client.post("/requests", json=body, headers={"Idempotency-Key": "k-1"}).json()
    assert not first["replayed"] and second["replayed"] and second["case_id"] == first["case_id"]
    assert len(client.get("/outbox/refunds").json()) == 1


def test_idempotency_key_reused_with_other_body_is_409(client):
    client.post("/requests", json={"customer_email": "ben@example.com", "message": "Please refund $5 for order O457"},
                headers={"Idempotency-Key": "k-1"})
    r = client.post("/requests", json={"customer_email": "ben@example.com", "message": "Please refund $9 for order O457"},
                    headers={"Idempotency-Key": "k-1"})
    assert r.status_code == 409
