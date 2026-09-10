"""Inbound replies are untrusted: routing, sender identity and verdicts are checked explicitly."""

from email.message import EmailMessage

import pytest

from unblock import inbound, mail, service
from unblock.config import settings
from unblock.domain import NewCase
from unblock.repository import MemoryRepository, Missing


@pytest.fixture
def configured(monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "unblock_inbound_domain", "inbound.example.com")
    monkeypatch.setattr(cfg, "unblock_mail_from", "unblock@example.com")
    monkeypatch.setattr(cfg, "unblock_send_enabled", True)
    monkeypatch.setattr(cfg, "unblock_allowed_recipients", "example.com")
    return cfg


@pytest.fixture
def case(monkeypatch):
    repo = MemoryRepository()
    monkeypatch.setattr(inbound, "repository", lambda: repo)
    record = service.create_case(
        repo,
        "org-a",
        NewCase(
            supplier="Ama Catering Ltd",
            invoice_ref="INV-2041",
            order_ref="PO-1042",
            amount_minor=480000,
            currency="GHS",
            contact_name="Ama",
            contact_email="ama@example.com",
        ),
        "create-001",
        "member",
    )
    return repo, record


def message(attachment: bytes | None = b"Delivery receipt PO-1042", subtype="plain"):
    mail_message = EmailMessage()
    mail_message.set_content("Attached.")
    if attachment is not None:
        mail_message.add_attachment(
            attachment, maintype="text", subtype=subtype, filename="receipt.txt"
        )
    return mail_message.as_bytes()


def event(case, sender="ama@example.com", spam="PASS", virus="PASS", recipient=None):
    address = recipient or f"case-{case['reply_token']}@inbound.example.com"
    return {
        "ses": {
            "receipt": {
                "recipients": [address],
                "spamVerdict": {"status": spam},
                "virusVerdict": {"status": virus},
                "spfVerdict": {"status": "PASS"},
                "dkimVerdict": {"status": "PASS"},
                "dmarcVerdict": {"status": "PASS"},
            },
            "mail": {
                "messageId": "msg-1",
                "commonHeaders": {"from": [sender], "subject": "Re: evidence"},
            },
        }
    }


def test_token_round_trip(configured, case):
    _, record = case
    address = mail.reply_address(record["reply_token"])
    assert address.endswith("@inbound.example.com")
    assert mail.token_from(address) == record["reply_token"]
    assert mail.token_from(f"case-{record['reply_token']}+tag@inbound.example.com")


@pytest.mark.parametrize(
    "address",
    [
        "case-deadbeef@inbound.example.com",
        "case-" + "0" * 32 + "@elsewhere.example.com",
        "postmaster@inbound.example.com",
        "case-not-hex-value-here-000000000@inbound.example.com",
    ],
)
def test_rejects_unroutable_addresses(configured, address):
    assert mail.token_from(address) is None


def test_allowlist(configured):
    assert mail.permitted("ama@example.com")
    assert not mail.permitted("stranger@other.test")
    assert not mail.permitted("not-an-address")


def test_sending_disabled_blocks_delivery(configured, monkeypatch):
    monkeypatch.setattr(settings(), "unblock_send_enabled", False)
    with pytest.raises(mail.Blocked):
        mail.check("ama@example.com")


def test_unknown_token_is_not_routed(configured, case):
    _, record = case
    result = inbound.handle(event(record, recipient="case-" + "a" * 32 + "@inbound.example.com"))
    assert result["result"] == "unrouted"


def test_unexpected_sender_is_quarantined(configured, case, monkeypatch):
    repo, record = case
    monkeypatch.setattr(inbound, "raw_message", lambda _: message())
    result = inbound.handle(event(record, sender="stranger@evil.test"))
    assert result == {"result": "quarantined", "reason": "unexpected_sender"}
    held = repo.get("org-a", record["id"])["quarantine"]
    assert held[0]["reason"] == "unexpected_sender" and held[0]["status"] == "held"
    assert not repo.get("org-a", record["id"])["documents"]


def test_virus_verdict_is_quarantined_before_reading_the_body(configured, case, monkeypatch):
    repo, record = case

    def explode(_):
        raise AssertionError("message body must not be read after a failed verdict")

    monkeypatch.setattr(inbound, "raw_message", explode)
    result = inbound.handle(event(record, virus="FAIL"))
    assert result == {"result": "quarantined", "reason": "failed_security_checks"}
    assert not repo.get("org-a", record["id"])["documents"]


def test_reply_without_usable_attachment_is_quarantined(configured, case, monkeypatch):
    repo, record = case
    monkeypatch.setattr(inbound, "raw_message", lambda _: message(attachment=None))
    result = inbound.handle(event(record))
    assert result == {"result": "quarantined", "reason": "no_usable_attachment"}
    assert repo.get("org-a", record["id"])["quarantine"][0]["reason"] == "no_usable_attachment"


def test_valid_reply_attaches_evidence_and_queues_the_agent(configured, case, monkeypatch):
    repo, record = case
    monkeypatch.setattr(inbound, "raw_message", lambda _: message())
    monkeypatch.setattr(inbound.storage, "put", lambda *a: ("digest-1", "org-a/key"))
    result = inbound.handle(event(record))
    assert result == {"result": "accepted", "attached": 1}
    updated = repo.get("org-a", record["id"])
    assert updated["documents"][0]["status"] == "pending"
    assert updated["agent_status"] == "queued"
    assert updated["last_reply_at"]


def test_reviewed_case_rejects_further_inbound_evidence(configured, case, monkeypatch):
    repo, record = case
    monkeypatch.setattr(inbound, "raw_message", lambda _: message())
    record["review"] = {
        "by": "reviewer",
        "at": "now",
        "note": "done",
        "decision": "packet_accepted",
    }
    repo.save(record, "Reviewed", "reviewer")
    result = inbound.handle(event(record))
    assert result == {"result": "quarantined", "reason": "case_already_reviewed"}


def test_reply_token_is_tenant_scoped(configured, case):
    repo, record = case
    assert repo.resolve_reply_token(record["reply_token"]) == {
        "tenant": "org-a",
        "case_id": record["id"],
    }
    with pytest.raises(Missing):
        repo.resolve_reply_token("b" * 32)


def test_reviewer_can_dismiss_a_held_message(configured, case, monkeypatch):
    repo, record = case
    monkeypatch.setattr(inbound, "raw_message", lambda _: message())
    inbound.handle(event(record, sender="stranger@evil.test"))
    current = repo.get("org-a", record["id"])
    entry = current["quarantine"][0]
    updated = service.release_quarantine(repo, current, entry["id"], "reviewer", current["version"])
    assert updated["quarantine"][0]["status"] == "dismissed"


@pytest.mark.parametrize("dmarc", ["FAIL", "GRAY", "UNKNOWN", "PROCESSING_FAILED", None])
def test_matching_from_without_alignment_is_held(configured, case, monkeypatch, dmarc):
    repo, record = case
    envelope = event(record)
    if dmarc is None:
        del envelope["ses"]["receipt"]["dmarcVerdict"]
    else:
        envelope["ses"]["receipt"]["dmarcVerdict"]["status"] = dmarc
    monkeypatch.setattr(
        inbound, "raw_message", lambda _: pytest.fail("Must not read unauthenticated body")
    )
    assert inbound.handle(envelope)["reason"] == "email_authentication_failed"
    assert repo.get("org-a", record["id"])["documents"] == []


def test_aligned_dkim_can_pass_when_forwarding_breaks_spf(configured, case, monkeypatch):
    _, record = case
    envelope = event(record)
    envelope["ses"]["receipt"]["spfVerdict"]["status"] = "FAIL"
    monkeypatch.setattr(inbound, "raw_message", lambda _: message())
    monkeypatch.setattr(inbound.storage, "put", lambda *a: ("digest-1", "key"))
    assert inbound.handle(envelope)["result"] == "accepted"


@pytest.mark.parametrize("check", ["spamVerdict", "virusVerdict"])
def test_missing_scan_verdicts_fail_closed(configured, case, check):
    _, record = case
    envelope = event(record)
    del envelope["ses"]["receipt"][check]
    assert inbound.handle(envelope)["reason"] == "failed_security_checks"


def test_legacy_demo_reply_is_ignored_without_even_quarantine(configured, case, monkeypatch):
    repo, record = case
    record["read_only"] = True
    record = repo.save(record, "Sample fixture", "test")
    before = repo.get("org-a", record["id"])
    monkeypatch.setattr(inbound, "raw_message", lambda _: pytest.fail("Never read sample mail"))
    assert inbound.handle(event(record)) == {"result": "ignored", "reason": "read_only_sample"}
    assert repo.get("org-a", record["id"]) == before
    assert repo.jobs == {}


def test_ambiguous_from_is_held(configured, case):
    _, record = case
    envelope = event(record, sender="ama@example.com, someone@example.com")
    assert inbound.handle(envelope)["reason"] == "ambiguous_sender"
