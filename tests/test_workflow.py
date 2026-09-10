import copy

import pytest
from fastapi.testclient import TestClient

from unblock import service
from unblock.api import app
from unblock.auth import Identity, identity
from unblock.domain import ExtractedEvidence, NewCase, assess
from unblock.repository import Conflict, MemoryRepository, Missing


@pytest.fixture
def data():
    return NewCase(
        supplier="Ama Catering Ltd",
        invoice_ref="INV-2041",
        order_ref="PO-1042",
        amount_minor=480000,
        currency="GHS",
        contact_name="Ama",
        contact_email="ama@example.com",
    )


@pytest.fixture
def repo():
    return MemoryRepository()


def evidence(kind, **changes):
    return (
        ExtractedEvidence(
            document_type=kind,
            supplier="Ama Catering Ltd",
            invoice_ref="INV-2041",
            order_ref="PO-1042",
            amount_minor=480000,
            currency="GHS",
            signed=True,
            source_quote="Order PO-1042, signed by Ama",
        ).model_dump()
        | changes
    )


def add(repo, case, kind, **changes):
    doc_id = kind + str(len(case["documents"]))
    case = service.attach(repo, case, doc_id, "test/key", kind + ".txt", "text/plain", "member")
    service.record_extraction(repo, case["tenant"], case["id"], doc_id, evidence(kind, **changes))
    return repo.get(case["tenant"], case["id"])


def test_wrong_receipt_then_correction_and_review(repo, data):
    case = service.create_case(repo, "org-a", data, "create-001", "member")
    case = add(repo, case, "invoice")
    case = add(repo, case, "purchase_order")
    case = add(repo, case, "delivery_receipt", order_ref="PO-WRONG")
    assert case["status"] == "blocked"
    assert case["documents"][-1]["status"] == "needs_correction"
    request = service.request_document(
        repo, "org-a", case["id"], "delivery_receipt", "Please correct the order reference"
    )
    # Sending is disabled by default, so the request is recorded but explicitly not delivered.
    assert request["status"] == "draft" and request["blocked_reason"]
    case = repo.get("org-a", case["id"])
    with pytest.raises(ValueError):
        service.review(repo, case, "reviewer", case["version"], "Checked")
    case = add(repo, case, "delivery_receipt")
    assert case["status"] == "ready_for_review"
    assert case["requests"][0]["status"] == "resolved"
    reviewed = service.review(
        repo, case, "reviewer", case["version"], "Compared all source documents"
    )
    assert reviewed["status"] == "reviewed"
    assert reviewed["review"]["decision"] == "packet_accepted"
    with pytest.raises(ValueError):
        service.attach(repo, reviewed, "new", "key", "new.txt", "text/plain", "member")
    assert len(repo.audit("org-a", case["id"])) == reviewed["version"]


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_minor": 480001},
        {"currency": "USD"},
        {"supplier": None},
        {"order_ref": None},
        {"invoice_ref": "OTHER"},
        {"uncertainty": "Text is ambiguous"},
    ],
)
def test_extraction_cannot_override_checks(data, changes):
    assert assess(data.model_dump(), evidence("invoice", **changes))


def test_unsigned_receipt_is_rejected(data):
    assert assess(data.model_dump(), evidence("delivery_receipt", signed=False))


def test_tenant_isolation_and_stale_writes(repo, data):
    case = service.create_case(repo, "org-a", data, "create-001", "member")
    with pytest.raises(Missing):
        repo.get("org-b", case["id"])
    assert repo.list("org-b") == []
    stale = copy.deepcopy(case)
    repo.save(case, "First writer", "member")
    with pytest.raises(Conflict):
        repo.save(stale, "Stale writer", "member")


def test_idempotent_create_upload_request_and_queue(repo, data):
    case = service.create_case(repo, "org-a", data, "create-001", "member")
    assert service.create_case(repo, "org-a", data, "create-001", "member") == case
    with pytest.raises(Conflict):
        service.create_case(
            repo, "org-a", data.model_copy(update={"amount_minor": 1}), "create-001", "member"
        )
    case = service.attach(repo, case, "hash", "key", "invoice.txt", "text/plain", "member")
    assert (
        service.attach(repo, case, "hash", "key", "duplicate.txt", "text/plain", "member") == case
    )
    request = service.request_document(
        repo, "org-a", case["id"], "invoice", "Please send the invoice"
    )
    assert (
        service.request_document(repo, "org-a", case["id"], "invoice", "Please resend") == request
    )
    case = repo.get("org-a", case["id"])
    case, job = service.enqueue(repo, case, "member")
    assert repo.job_get("org-a", job["id"])["status"] == "pending"
    assert service.enqueue(repo, case, "member")[1] is None


def test_api_reviewer_permission_and_cross_tenant(repo, data, monkeypatch):
    monkeypatch.setattr("unblock.api.repository", lambda: repo)
    app.dependency_overrides[identity] = lambda: Identity(tenant="org-a", actor="member")
    try:
        client = TestClient(app)
        response = client.post(
            "/api/cases", json=data.model_dump(), headers={"Idempotency-Key": "new-case-001"}
        )
        assert response.status_code == 201
        case_id = response.json()["id"]
        assert (
            client.post(
                f"/api/cases/{case_id}/review", json={"version": 1, "note": "All checked"}
            ).status_code
            == 403
        )
        app.dependency_overrides[identity] = lambda: Identity(tenant="org-b", actor="other")
        assert client.get(f"/api/cases/{case_id}").status_code == 404
        assert client.get(f"/api/cases/{case_id}/packet").status_code == 404
        assert client.get("/api/cases").json() == []
    finally:
        app.dependency_overrides.clear()


def test_deployed_mode_never_allows_anonymous(monkeypatch):
    from unblock.config import Settings

    monkeypatch.setattr("unblock.auth.settings", lambda: Settings(app_env="dev"))
    assert TestClient(app).get("/api/cases").status_code == 401


def test_completed_job_is_not_rerun(repo, data, monkeypatch):
    from unblock.agent import process_job

    case = service.create_case(repo, "org-a", data, "new-case-001", "member")
    case, job = service.enqueue(repo, case, "member")
    calls = []
    monkeypatch.setattr(
        "unblock.agent.run_agent", lambda *args: calls.append(1) or "No evidence yet"
    )
    process_job(repo, job)
    process_job(repo, job)
    assert calls == [1]


def test_expired_worker_lease_can_be_replaced(repo, data, monkeypatch):
    from unblock.agent import process_job

    case = service.create_case(repo, "org-a", data, "lease-case-001", "member")
    case, old_job = service.enqueue(repo, case, "member")
    case["agent_status"] = "running"
    case["agent_lease_until"] = "2000-01-01T00:00:00+00:00"
    case = repo.save(case, "Simulated terminated worker", "test")
    case, replacement = service.enqueue(repo, case, "member")
    assert replacement and replacement["id"] != old_job["id"]
    calls = []
    monkeypatch.setattr("unblock.agent.run_agent", lambda *args: calls.append(1) or "Done")
    process_job(repo, old_job)
    assert calls == []
    assert repo.job_get("org-a", old_job["id"])["status"] == "complete"
    process_job(repo, replacement)
    assert calls == [1]


def test_summary_strips_model_reasoning_scaffolding():
    from unblock.agent import summarize

    assert summarize("<thinking>internal</thinking>\n\n\n**Summary:** done") == "**Summary:** done"
    assert summarize("plain text") == "plain text"
    assert "internal" not in summarize("<THINKING>internal</THINKING>ok")
