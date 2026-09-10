"""Inbound supplier replies. Every message is untrusted until it passes explicit checks."""

import email
import json
from email.utils import getaddresses, parseaddr

import boto3

from unblock import mail, service, storage
from unblock.config import settings
from unblock.domain import now
from unblock.presentation import sample_case
from unblock.repository import Missing, repository

MAX_ATTACHMENTS = 5


def raw_message(message_id: str) -> bytes:
    """Read the message the SES S3 action stored before this function was invoked."""
    return (
        boto3.client("s3", region_name=settings().aws_default_region)
        .get_object(Bucket=settings().unblock_bucket, Key=f"inbound/{message_id}")["Body"]
        .read(12 * 1024 * 1024)
    )


def attachments(parsed):
    """Yield only the parts that are already an accepted evidence type and size."""
    found = []
    for part in parsed.walk():
        if part.is_multipart() or part.get_content_disposition() not in ("attachment", "inline"):
            continue
        content_type = (part.get_content_type() or "").casefold()
        if content_type not in storage.CONTENT_TYPES:
            continue
        try:
            data = part.get_payload(decode=True)
        except Exception:
            continue
        if not data:
            continue
        try:
            storage.validate_file(data, content_type)
        except (ValueError, UnicodeDecodeError):
            continue
        name = (part.get_filename() or "attachment")[:160]
        found.append((name, content_type, data))
        if len(found) >= MAX_ATTACHMENTS:
            break
    return found


def resolve(recipients):
    for address in recipients:
        token = mail.token_from(address)
        if not token:
            continue
        try:
            return repository().resolve_reply_token(token)
        except Missing:
            continue
    return None


def handle(record):
    ses = record["ses"]
    receipt, envelope = ses["receipt"], ses["mail"]
    message_id = envelope["messageId"]
    target = resolve(receipt.get("recipients", []))
    if not target:
        # Unroutable mail is dropped rather than stored against a guessed case.
        return {"result": "unrouted", "message_id": message_id}

    repo = repository()
    tenant, case_id = target["tenant"], target["case_id"]
    case = repo.get(tenant, case_id)
    # A stale or previously exposed routing pointer must not mutate the public sample,
    # including its audit/quarantine records, or launch model work.
    if sample_case(case):
        return {"result": "ignored", "reason": "read_only_sample"}
    headers = envelope.get("commonHeaders", {})
    sender = parseaddr(((headers.get("from") or [""])[0]))[1].casefold()
    subject = headers.get("subject") or "(no subject)"

    verdicts = {
        k: (receipt.get(f"{k}Verdict", {}) or {}).get("status", "UNKNOWN")
        for k in ("spam", "virus", "spf", "dkim", "dmarc")
    }
    detail = " ".join(f"{k}={v}" for k, v in verdicts.items())

    if verdicts["spam"] != "PASS" or verdicts["virus"] != "PASS":
        service.quarantine(repo, tenant, case_id, "failed_security_checks", sender, subject, detail)
        return {"result": "quarantined", "reason": "failed_security_checks"}

    # DMARC confirms alignment to the visible From domain; an isolated SPF or DKIM
    # PASS does not. Missing/GRAY/error verdicts are held for review, not accepted.
    # Either aligned method may pass: forwarded mail can legitimately fail SPF.
    if verdicts["dmarc"] != "PASS" or not any(verdicts[k] == "PASS" for k in ("spf", "dkim")):
        service.quarantine(
            repo, tenant, case_id, "email_authentication_failed", sender, subject, detail
        )
        return {"result": "quarantined", "reason": "email_authentication_failed"}

    from_addresses = headers.get("from") or []
    if len(getaddresses(from_addresses)) != 1:
        service.quarantine(repo, tenant, case_id, "ambiguous_sender", sender, subject, detail)
        return {"result": "quarantined", "reason": "ambiguous_sender"}
    if sender != (case["contact_email"] or "").casefold():
        service.quarantine(
            repo,
            tenant,
            case_id,
            "unexpected_sender",
            sender,
            subject,
            f"Case contact is {case['contact_email']}. {detail}",
        )
        return {"result": "quarantined", "reason": "unexpected_sender"}

    if case.get("review"):
        service.quarantine(repo, tenant, case_id, "case_already_reviewed", sender, subject, detail)
        return {"result": "quarantined", "reason": "case_already_reviewed"}

    found = attachments(email.message_from_bytes(raw_message(message_id)))
    if not found:
        service.quarantine(repo, tenant, case_id, "no_usable_attachment", sender, subject, detail)
        return {"result": "quarantined", "reason": "no_usable_attachment"}

    attached = 0
    for name, content_type, data in found:
        digest, key = storage.put(tenant, case_id, data, content_type)
        case = repo.get(tenant, case_id)
        before = len(case["documents"])
        case = service.attach(repo, case, digest, key, name, content_type, "supplier")
        attached += len(case["documents"]) - before

    case = repo.get(tenant, case_id)
    case["last_reply_at"] = now()
    case = repo.save(case, f"Supplier replied with {attached} document(s)", "supplier")
    if attached:
        service.enqueue(repo, case, "supplier")
    return {"result": "accepted", "attached": attached}


def handler(event, context):
    results = []
    for record in event.get("Records", []):
        try:
            results.append(handle(record))
        except Exception as error:
            # Never log message bodies, addresses, or attachment content.
            print(json.dumps({"event": "inbound_failed", "error_type": type(error).__name__}))
            raise
    return {"results": results}
