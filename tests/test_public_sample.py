import pytest
from fastapi.testclient import TestClient

from unblock import service
from unblock.api import app
from unblock.auth import Identity, identity
from unblock.domain import NewCase
from unblock.presentation import present
from unblock.repository import MemoryRepository


@pytest.fixture
def sample(monkeypatch):
    repo = MemoryRepository()
    case = service.create_case(
        repo,
        "demo",
        NewCase(
            supplier="Sample Ltd",
            invoice_ref="I1",
            order_ref="P1",
            amount_minor=100,
            currency="GHS",
            contact_name="Sample",
            contact_email="supplier@example.com",
        ),
        "sample-key",
        "sample-seed",
    )
    # Simulate legacy sample records created before reply routing was disabled.
    token = "a" * 32
    case["reply_token"] = token
    case["requests"] = [
        {
            "status": "sent",
            "requirement": "delivery_receipt",
            "reply_to": f"case-{token}@inbound.example.com",
            "body": f"Legacy routing {token}",
        }
    ]
    case = repo.save(case, f"Legacy address case-{token}@inbound.example.com", "sample-seed")
    monkeypatch.setattr("unblock.api.repository", lambda: repo)
    app.dependency_overrides[identity] = lambda: Identity(tenant="demo", actor="guest", demo=True)
    yield repo, case, token
    app.dependency_overrides.clear()


def test_every_json_projection_removes_nested_capabilities(sample):
    repo, case, token = sample
    client = TestClient(app)
    for route in (
        "/api/cases",
        f"/api/cases/{case['id']}",
        f"/api/cases/{case['id']}/packet",
        f"/api/cases/{case['id']}/audit",
    ):
        response = client.get(route)
        assert response.status_code == 200
        assert token not in response.text
        assert '"reply_token"' not in response.text
        assert '"reply_to"' not in response.text
        assert '"reply_address"' not in response.text
    assert repo.get("demo", case["id"])["reply_token"] == token
    assert present(case)["sample"] is True
    assert "Simulated" in present(case)["provenance"]


def test_demo_write_paths_reject_at_api(sample):
    _, case, _ = sample
    client = TestClient(app)
    for route, payload in (
        ("/api/cases", {}),
        (f"/api/cases/{case['id']}/run", None),
        (f"/api/cases/{case['id']}/review", {"version": case["version"], "note": "Cannot approve"}),
        (f"/api/cases/{case['id']}/quarantine/x/dismiss", {"version": case["version"]}),
    ):
        assert (
            client.post(
                route, json=payload, headers={"Idempotency-Key": "guest-attempt"}
            ).status_code
            == 403
        )
    assert (
        client.post(
            f"/api/cases/{case['id']}/documents",
            files={"file": ("evidence.txt", b"test", "text/plain")},
        ).status_code
        == 403
    )


def test_sample_cannot_launch_jobs_or_send_mail(sample, monkeypatch):
    repo, case, _ = sample
    with pytest.raises(ValueError):
        service.enqueue(repo, case, "worker")
    case["requests"][0].update(id="request-id", status="draft")
    repo.save(case, "Draft", "sample-seed")

    def forbidden(*args):
        pytest.fail("Sample must never send mail")

    monkeypatch.setattr("unblock.mail.send", forbidden)
    assert service._deliver(repo, "demo", case["id"], "request-id")["status"] == "draft"


def test_new_sample_has_no_routing_pointer():
    repo = MemoryRepository()
    case = service.create_case(
        repo,
        "demo",
        NewCase(
            supplier="Sample Ltd",
            invoice_ref="I1",
            order_ref="P1",
            amount_minor=100,
            currency="GHS",
            contact_name="Sample",
            contact_email="supplier@example.com",
        ),
        "sample-key",
        "seed",
    )
    assert case["reply_token"] is None
    assert repo.replies == {}


def test_summary_uses_case_state_not_model_text():
    from unblock.agent import case_summary

    summary = case_summary(
        {"status": "ready_for_review", "agent_summary": "<thinking>send another request</thinking>"}
    )
    assert "accept the evidence packet" in summary
    assert "thinking" not in summary
