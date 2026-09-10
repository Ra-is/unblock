"""Create the shared read-only demo account and seed one finished case for visitors.

The demo account is in its own tenant, is never a reviewer, and the API refuses every
write it attempts. Re-running refreshes the seeded case without touching real tenants.
"""

import json
import os
from pathlib import Path
import secrets
import sys
from datetime import datetime, timezone

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from unblock.domain import ExtractedEvidence, NewCase  # noqa: E402

TENANT = "demo"
CONTACT = "ama@amacatering.example"


def evidence(**changes):
    return (
        ExtractedEvidence(
            document_type="invoice",
            supplier="Ama Catering Ltd",
            invoice_ref="INV-2041",
            order_ref="PO-1042",
            amount_minor=480000,
            currency="GHS",
            signed=True,
            source_quote="Order PO-1042 · GHS 4,800.00 · received and signed by A. Mensah",
        ).model_dump()
        | changes
    )


def reset(repo):
    """Remove the demo tenant's seeded records so the case can be rebuilt."""
    from boto3.dynamodb.conditions import Key

    cases = repo.list(TENANT)
    backup = (
        ROOT
        / ".local"
        / ("demo-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json")
    )
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(
            [{"case": c, "audit": repo.audit(TENANT, c["id"])} for c in cases], file, indent=2
        )
    for case in cases:
        for pk in (f"AUDIT#{TENANT}#{case['id']}", f"REPLY#{case.get('reply_token', '')}"):
            params = {"KeyConditionExpression": Key("pk").eq(pk)}
            while True:
                result = repo.table.query(**params)
                for item in result["Items"]:
                    repo.table.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                if not result.get("LastEvaluatedKey"):
                    break
                params["ExclusiveStartKey"] = result["LastEvaluatedKey"]
        repo.table.delete_item(Key={"pk": f"TENANT#{TENANT}", "sk": f"CASE#{case['id']}"})
        print(f"Removed seeded demo case {case['id']}.")


def seed(repo):
    from unblock import service

    case = service.create_case(
        repo,
        TENANT,
        NewCase(
            supplier="Ama Catering Ltd",
            invoice_ref="INV-2041",
            order_ref="PO-1042",
            amount_minor=480000,
            currency="GHS",
            contact_name="Ama Mensah",
            contact_email=CONTACT,
        ),
        "demo-case-inv-2041",
        "demo-seed",
    )
    if case["documents"]:
        print("Demo case already seeded; leaving it as it is.")
        return case

    def attach(name, kind, **changes):
        from unblock import storage

        current = repo.get(TENANT, case["id"])
        raw = (ROOT / "examples" / name).read_bytes()
        digest, key = storage.put(TENANT, case["id"], raw, "text/plain")
        service.attach(repo, current, digest, key, name, "text/plain", "sample-seed")
        service.record_extraction(
            repo, TENANT, case["id"], digest, evidence(document_type=kind, **changes)
        )

    attach("invoice.txt", "invoice")
    attach("purchase-order.txt", "purchase_order")
    attach(
        "wrong-receipt.txt",
        "delivery_receipt",
        order_ref="PO-9999",
        source_quote="Order reference: PO-9999",
    )
    # The request must exist before the correction arrives, or the requirement is already met.
    service.request_document(
        repo,
        TENANT,
        case["id"],
        "delivery_receipt",
        "The delivery confirmation references order PO-9999, but this invoice is for order "
        "PO-1042. Please send the signed delivery confirmation for PO-1042.",
    )
    case = repo.get(TENANT, case["id"])
    for request in case["requests"]:
        request.update(
            status="sent",
            sent_at=case["created_at"],
            blocked_reason=None,
            delivery_mode="simulated",
        )
    repo.save(
        case,
        "Sample: simulated a request for the correct delivery receipt; no email sent",
        "sample-seed",
    )
    attach("correct-receipt.txt", "delivery_receipt")
    case = repo.get(TENANT, case["id"])
    case["agent_summary"] = (
        "Sample walkthrough: the invoice and purchase order match. The first receipt "
        "references PO-9999. A simulated request and corrected receipt illustrate how "
        "the case becomes ready for review. No email or live agent run occurred in this sample."
    )
    repo.save(case, "Agent finished evidence review", "worker")

    service.quarantine(
        repo,
        TENANT,
        case["id"],
        "unexpected_sender",
        "accounts@unknown-supplier.example",
        "Re: Evidence needed for invoice INV-2041",
        f"Case contact is {CONTACT}. spam=PASS virus=PASS spf=FAIL dkim=FAIL dmarc=FAIL",
    )
    print("Seeded the demo case, its email request and one held message.")
    return repo.get(TENANT, case["id"])


def main():
    config = json.loads((ROOT / ".local/deployment.json").read_text())
    os.environ.setdefault("UNBLOCK_TABLE", config["TableName"])
    os.environ.setdefault("UNBLOCK_BUCKET", config["DocumentBucket"])
    os.environ.setdefault("AWS_PROFILE", config["Profile"])
    os.environ.setdefault("AWS_DEFAULT_REGION", config["Region"])
    os.environ.setdefault("APP_ENV", "dev")

    session = boto3.Session(profile_name=config["Profile"], region_name=config["Region"])
    cognito = session.client("cognito-idp")
    path = ROOT / ".local/demo-access.json"

    if path.exists():
        payload = json.loads(path.read_text())
        print("Demo credentials already exist; reusing them.")
    else:
        payload = {
            "url": config["AppUrl"],
            "username": "demo",
            "password": secrets.token_urlsafe(24) + "Aa1!",
            "tenant": TENANT,
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(payload, file, indent=2)
        cognito.admin_create_user(
            UserPoolId=config["UserPoolId"],
            Username=payload["username"],
            TemporaryPassword=payload["password"],
            MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "custom:tenant", "Value": TENANT}],
        )
        print("Demo account created.")

    cognito.admin_set_user_password(
        UserPoolId=config["UserPoolId"],
        Username=payload["username"],
        Password=payload["password"],
        Permanent=True,
    )
    cognito.admin_add_user_to_group(
        UserPoolId=config["UserPoolId"], Username=payload["username"], GroupName="demo"
    )

    from unblock.repository import DynamoRepository

    repo = DynamoRepository(config["TableName"])
    if "--reset" in sys.argv:
        reset(repo)
    seed(repo)
    print("Redeploy so the API picks up the demo credentials from .local/demo-access.json.")


if __name__ == "__main__":
    main()
