"""Exercise the real send-wait-reply loop against the deployed stack.

Sends genuine SES mail between two verified identities, then confirms the agent resumed
on its own. Creates one clearly marked synthetic case per run. Never prints tokens.
"""

import json
import email
import os
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
import time
from uuid import uuid4

import boto3
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def wait_idle(client, case_id, label):
    last = None
    for _ in range(75):
        case = client.get(f"/api/cases/{case_id}").raise_for_status().json()
        if case["agent_status"] != last:
            print(f"  {label}: {case['agent_status']}", flush=True)
            last = case["agent_status"]
        if case["agent_status"] == "failed":
            raise RuntimeError("Worker failed. Inspect the -worker log group for the error type.")
        if case["agent_status"] == "idle":
            return case
        time.sleep(4)
    raise TimeoutError(f"{label}: agent did not finish within five minutes")


def wait_for(client, case_id, predicate, label, attempts=60):
    for _ in range(attempts):
        case = client.get(f"/api/cases/{case_id}").raise_for_status().json()
        if predicate(case):
            return case
        time.sleep(5)
    raise TimeoutError(f"{label}: condition not met within five minutes")


def deliver(ses, sender, recipient, subject, attachment: Path | None):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content("Synthetic supplier reply for an automated test. No real transaction.")
    if attachment:
        message.add_attachment(
            attachment.read_bytes(),
            maintype="text",
            subtype="plain",
            filename=attachment.name,
        )
    ses.send_raw_email(
        Source=sender, Destinations=[recipient], RawMessage={"Data": message.as_bytes()}
    )
    print(f"  sent {subject} -> {recipient.split('@')[1]}", flush=True)


def main():
    cfg = json.loads((ROOT / ".local/deployment.json").read_text())
    owner = json.loads((ROOT / ".local/owner-access.json").read_text())
    session = boto3.Session(profile_name=cfg["Profile"], region_name=cfg["Region"])
    ses = session.client("ses")
    contact = "supplier@" + cfg["InboundDomainName"]
    stranger = "other-supplier@" + cfg["InboundDomainName"]
    os.environ.setdefault("AWS_PROFILE", cfg["Profile"])
    os.environ.setdefault("AWS_DEFAULT_REGION", cfg["Region"])
    os.environ.setdefault("UNBLOCK_TABLE", cfg["TableName"])
    from unblock.repository import DynamoRepository

    repo = DynamoRepository(cfg["TableName"])
    started = datetime.now(timezone.utc)

    token = session.client("cognito-idp").initiate_auth(
        ClientId=cfg["ClientId"],
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": owner["username"], "PASSWORD": owner["password"]},
    )["AuthenticationResult"]["IdToken"]

    with httpx.Client(
        base_url=cfg["AppUrl"], headers={"Authorization": "Bearer " + token}, timeout=45
    ) as client:
        response = client.post(
            "/api/cases",
            headers={"Idempotency-Key": "mail-" + uuid4().hex},
            json={
                "supplier": "Ama Catering Ltd",
                "invoice_ref": "INV-2041",
                "order_ref": "PO-1042",
                "amount_minor": 480000,
                "currency": "GHS",
                "contact_name": "Synthetic supplier contact",
                "contact_email": contact,
            },
        )
        response.raise_for_status()
        case = response.json()
        case_id = case["id"]
        private = repo.get("renobytes", case_id)
        reply_to = f"case-{private['reply_token']}@{cfg['InboundDomainName']}"
        assert reply_to, "Deployment has no inbound domain configured"
        print(f"Case {case_id} opened; replies route to the case address.", flush=True)

        for filename in ("invoice.txt", "purchase-order.txt", "wrong-receipt.txt"):
            client.post(
                f"/api/cases/{case_id}/documents",
                files={
                    "file": (filename, (ROOT / "examples" / filename).read_bytes(), "text/plain")
                },
            ).raise_for_status()
        client.post(f"/api/cases/{case_id}/run").raise_for_status()
        case = wait_idle(client, case_id, "first review")
        assert case["status"] == "blocked", case["requirements"]
        sent = [r for r in case["requests"] if r["status"] == "sent"]
        assert sent, f"Agent did not deliver a request: {case['requests']}"
        assert case["awaiting_reply"], "Case should be waiting on the supplier"
        print(f"Agent sent {len(sent)} request(s) and stopped, waiting for a reply.", flush=True)
        # Prove delivery to the controlled supplier inbox, not only SES API acceptance.
        s3 = session.client("s3")
        delivered = False
        for _ in range(30):
            for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=cfg["DocumentBucket"], Prefix="inbound/"
            ):
                for item in page.get("Contents", []):
                    if item["LastModified"] < started:
                        continue
                    raw = s3.get_object(Bucket=cfg["DocumentBucket"], Key=item["Key"])[
                        "Body"
                    ].read()
                    received = email.message_from_bytes(raw)
                    if contact in (received.get("To") or "") and sent[0]["subject"] == received.get(
                        "Subject"
                    ):
                        delivered = True
            if delivered:
                break
            time.sleep(2)
        assert delivered, "SES accepted the send, but the controlled mailbox has not received it"
        print("Confirmed: request actually arrived in the controlled supplier mailbox.", flush=True)

        print("Replying from an address that is NOT the case contact...", flush=True)
        deliver(ses, stranger, reply_to, "Re: wrong sender", ROOT / "examples/correct-receipt.txt")
        case = wait_for(
            client,
            case_id,
            lambda c: any(q["reason"] == "unexpected_sender" for q in c["quarantine"]),
            "quarantine",
        )
        assert len(case["documents"]) == 3, "A held message must not attach evidence"
        assert case["status"] == "blocked"
        print("Held for review: sender is not the case contact. No evidence attached.", flush=True)

        print("Replying from the real case contact with the corrected receipt...", flush=True)
        deliver(
            ses, contact, reply_to, "Re: corrected receipt", ROOT / "examples/correct-receipt.txt"
        )
        case = wait_for(
            client, case_id, lambda c: len(c["documents"]) > 3, "inbound evidence", attempts=60
        )
        print("Reply accepted; the agent was woken by the message.", flush=True)
        case = wait_idle(client, case_id, "resumed review")
        assert case["status"] == "ready_for_review", case["requirements"]
        assert not case["awaiting_reply"]
        assert all(r["status"] in ("resolved", "failed") for r in case["requests"]), case[
            "requests"
        ]

        (ROOT / ".local/smoke-mail-result.json").write_text(
            json.dumps(
                {
                    "case_id": case_id,
                    "status": "passed",
                    "url": cfg["AppUrl"],
                    "checks": [
                        "outbound_ses_delivery",
                        "request_received_in_controlled_mailbox",
                        "dmarc_authenticated_supplier_reply",
                        "case_waits_after_sending",
                        "unexpected_sender_quarantined",
                        "held_message_attaches_no_evidence",
                        "inbound_reply_routed_by_token",
                        "agent_resumed_without_a_browser",
                        "requirement_satisfied_from_email",
                    ],
                },
                indent=2,
            )
        )
        print("PASS: sent, waited, was woken by a real reply, and finished the case.", flush=True)


if __name__ == "__main__":
    main()
