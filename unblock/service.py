import hashlib
from datetime import datetime, timedelta, timezone

from unblock import mail
from unblock.domain import NewCase, assess, identifier, make_case, now, recompute
from unblock.repository import Conflict
from unblock.presentation import sample_case


def lease(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def is_busy(case):
    return (
        case["agent_status"] in ("queued", "running") and case.get("agent_lease_until", "") > now()
    )


def create_case(repo, tenant, data: NewCase, key: str, actor):
    case_id = hashlib.sha256(f"{tenant}:{key}".encode()).hexdigest()[:24]
    from unblock.repository import Missing

    try:
        existing = repo.get(tenant, case_id)
        if any(existing[k] != v for k, v in data.model_dump().items()):
            raise Conflict("Idempotency key was already used for another request")
        return existing
    except Missing:
        return repo.save(make_case(data, tenant, case_id), "Case opened", actor)


def enqueue(repo, case, actor):
    if sample_case(case):
        raise ValueError("Sample cases cannot launch agent jobs.")
    if is_busy(case):
        return case, None
    if case.get("review"):
        raise ValueError("This case has already been reviewed.")
    job = {
        "id": identifier(),
        "tenant": case["tenant"],
        "case_id": case["id"],
        "status": "pending",
        "created_at": now(),
    }
    case["agent_status"] = "queued"
    case["agent_lease_until"] = lease(900)
    case["active_job_id"] = job["id"]
    return repo.save(case, "Evidence review queued", actor, job), job


def attach(repo, case, digest, key, filename, content_type, actor):
    if case.get("review"):
        raise ValueError("Reviewed cases cannot receive new evidence. Open a new case.")
    if any(d["id"] == digest for d in case["documents"]):
        return case
    if len(case["documents"]) >= 30:
        raise ValueError("This case has reached its 30-document limit.")
    case["documents"].append(
        {
            "id": digest,
            "key": key,
            "filename": filename,
            "content_type": content_type,
            "status": "pending",
            "uploaded_at": now(),
            "issues": [],
        }
    )
    return repo.save(case, f"Document received: {filename}", actor)


def record_extraction(repo, tenant, case_id, document_id, extraction):
    for _ in range(5):
        case = repo.get(tenant, case_id)
        document = next(d for d in case["documents"] if d["id"] == document_id)
        if document["status"] != "pending":
            return document
        document["extraction"] = extraction
        document["issues"] = assess(case, extraction)
        document["status"] = "needs_correction" if document["issues"] else "accepted"
        recompute(case)
        try:
            repo.save(
                case,
                f"Checked {document['filename']}: {document['status'].replace('_', ' ')}",
                "agent",
            )
            return document
        except Conflict:
            continue
    raise Conflict()


def request_document(repo, tenant, case_id, requirement, explanation):
    """Record a request, then attempt delivery. A recorded draft is never silently sent."""
    if requirement not in ("invoice", "purchase_order", "delivery_receipt"):
        raise ValueError("Unknown requirement")
    request = _record_request(repo, tenant, case_id, requirement, explanation)
    if request.get("status") != "draft":
        return request
    return _deliver(repo, tenant, case_id, request["id"])


def _record_request(repo, tenant, case_id, requirement, explanation):
    for _ in range(5):
        case = repo.get(tenant, case_id)
        state = next(r for r in case["requirements"] if r["kind"] == requirement)
        if state["status"] == "satisfied":
            return {"result": "Already satisfied; no request needed"}
        fingerprint = hashlib.sha256(
            (requirement + ":" + ",".join(sorted(d["id"] for d in case["documents"]))).encode()
        ).hexdigest()[:20]
        existing = next((r for r in case["requests"] if r["id"] == fingerprint), None)
        if existing:
            return existing
        if len(case["requests"]) >= 40:
            raise ValueError("Request limit reached; reviewer attention required")
        reply_to = mail.reply_address(case.get("reply_token", ""))
        request = {
            "id": fingerprint,
            "requirement": requirement,
            "recipient": case["contact_email"],
            "reply_to": reply_to,
            "subject": f"Evidence needed for invoice {case['invoice_ref']}",
            "body": (
                f"Hello {case['contact_name']},\n\n{explanation[:1500]}\n\n"
                f"Please reply to this message with the document attached, keeping order "
                f"reference {case['order_ref']} in the conversation.\n\nThank you."
            ),
            "status": "draft",
            "created_at": now(),
        }
        case["requests"].append(request)
        try:
            repo.save(case, f"Recorded request for {requirement.replace('_', ' ')}", "agent")
            return request
        except Conflict:
            continue
    raise Conflict()


def _deliver(repo, tenant, case_id, request_id):
    """Send an already-recorded request. Delivery is at least once; the record is authoritative."""
    case = repo.get(tenant, case_id)
    request = next(r for r in case["requests"] if r["id"] == request_id)
    if sample_case(case):
        return {**request, "status": "draft", "blocked_reason": "Sample cases cannot send mail."}
    try:
        message_id = mail.send(
            request["recipient"], request["reply_to"], request["subject"], request["body"]
        )
        outcome, event = (
            {"status": "sent", "sent_at": now(), "message_id": message_id},
            f"Sent request for {request['requirement'].replace('_', ' ')} to the supplier contact",
        )
    except mail.Blocked as blocked:
        outcome, event = (
            {"status": "draft", "blocked_reason": str(blocked)},
            f"Request for {request['requirement'].replace('_', ' ')} was not sent: {blocked}",
        )
    except Exception as error:
        outcome, event = (
            {"status": "failed", "blocked_reason": type(error).__name__},
            f"Delivery failed for {request['requirement'].replace('_', ' ')}; retry required",
        )
    for _ in range(5):
        case = repo.get(tenant, case_id)
        current = next(r for r in case["requests"] if r["id"] == request_id)
        if current["status"] == "sent":
            return current
        current.update(outcome)
        try:
            repo.save(case, event, "agent")
            return current
        except Conflict:
            continue
    raise Conflict()


def quarantine(repo, tenant, case_id, reason, sender, subject, detail=""):
    """Hold an inbound message for a reviewer instead of treating it as evidence."""
    for _ in range(5):
        case = repo.get(tenant, case_id)
        entry = {
            "id": identifier(),
            "reason": reason,
            "sender": sender[:254],
            "subject": subject[:200],
            "detail": detail[:400],
            "at": now(),
            "status": "held",
        }
        if len(case.get("quarantine", [])) >= 40:
            raise ValueError("Quarantine limit reached; reviewer attention required")
        case.setdefault("quarantine", []).append(entry)
        try:
            repo.save(case, f"Inbound message held for review: {reason}", "inbound")
            return entry
        except Conflict:
            continue
    raise Conflict()


def release_quarantine(repo, case, entry_id, actor, expected_version):
    if case["version"] != expected_version:
        raise Conflict("Case changed; refresh before acting")
    entry = next((q for q in case.get("quarantine", []) if q["id"] == entry_id), None)
    if not entry:
        raise ValueError("Quarantined message not found")
    if entry["status"] != "held":
        return case
    entry["status"] = "dismissed"
    entry["decided_by"] = actor
    entry["decided_at"] = now()
    return repo.save(case, "Reviewer dismissed a held inbound message", actor)


def review(repo, case, actor, expected_version, note):
    if case["version"] != expected_version:
        raise Conflict("Case changed; refresh before reviewing")
    if is_busy(case):
        raise ValueError("Wait for the evidence review to finish.")
    recompute(case)
    if case["status"] != "ready_for_review":
        raise ValueError("All three evidence requirements must be satisfied first.")
    case["review"] = {"by": actor, "at": now(), "note": note, "decision": "packet_accepted"}
    recompute(case)
    return repo.save(
        case, "Reviewer accepted the evidence packet. No payment was authorized.", actor
    )
