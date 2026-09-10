"""Private recording harness: genuine SES messages, never simulated product state.

Only setup returns public metadata; credentials stay in an owner-only local file.
"""

import email
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

import boto3
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PRIVATE = ROOT / ".local/recording/session.json"


def main():
    cfg = json.loads((ROOT / ".local/deployment.json").read_text())
    os.environ.setdefault("AWS_PROFILE", cfg["Profile"])
    os.environ.setdefault("AWS_DEFAULT_REGION", cfg["Region"])
    os.environ.setdefault("UNBLOCK_TABLE", cfg["TableName"])
    session = boto3.Session(profile_name=cfg["Profile"], region_name=cfg["Region"])
    action = sys.argv[1]
    if action == "setup":
        owner = json.loads((ROOT / ".local/owner-access.json").read_text())
        token = session.client("cognito-idp").initiate_auth(
            ClientId=cfg["ClientId"],
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": owner["username"], "PASSWORD": owner["password"]},
        )["AuthenticationResult"]["IdToken"]
        data = {
            "token": token,
            "url": cfg["AppUrl"],
            "contact": "supplier@" + cfg["InboundDomainName"],
            "started": datetime.now(timezone.utc).isoformat(),
        }
        PRIVATE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(PRIVATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(data, file)
        print(json.dumps({"url": data["url"], "contact": data["contact"]}))
        return
    data = json.loads(PRIVATE.read_text())
    if action == "bind":
        from unblock.repository import DynamoRepository

        case = DynamoRepository(cfg["TableName"]).get("renobytes", sys.argv[2])
        assert case["contact_email"] == data["contact"]
        data["case_id"] = case["id"]
        data["reply_to"] = f"case-{case['reply_token']}@{cfg['InboundDomainName']}"
        PRIVATE.write_text(json.dumps(data))
        print(json.dumps({"bound": True}))
        return
    if action == "mail":
        client = session.client("s3")
        started = datetime.fromisoformat(data["started"])
        for _ in range(30):
            for page in client.get_paginator("list_objects_v2").paginate(
                Bucket=cfg["DocumentBucket"], Prefix="inbound/"
            ):
                for item in page.get("Contents", []):
                    if item["LastModified"] < started:
                        continue
                    raw = client.get_object(Bucket=cfg["DocumentBucket"], Key=item["Key"])[
                        "Body"
                    ].read()
                    message = email.message_from_bytes(raw)
                    if (
                        data["contact"] not in (message.get("To") or "")
                        or message.get("Subject") != "Evidence needed for invoice INV-2041"
                    ):
                        continue
                    parts = [p for p in message.walk() if p.get_content_type() == "text/plain"]
                    body = parts[0].get_payload(decode=True).decode("utf-8", errors="replace")
                    from unblock.presentation import redact

                    print(
                        json.dumps(
                            redact(
                                {
                                    "from": message.get("From"),
                                    "to": message.get("To"),
                                    "subject": message.get("Subject"),
                                    "body": body,
                                    "received_at": item["LastModified"].isoformat(),
                                    "proof": "Received from SES-backed S3 inbox",
                                }
                            )
                        )
                    )
                    return
            time.sleep(2)
        raise TimeoutError("Mail has not arrived in controlled inbox")
    if action == "reply":
        from smoke_mail import deliver

        sender = (
            data["contact"]
            if sys.argv[2] == "correct"
            else "other-supplier@" + cfg["InboundDomainName"]
        )
        deliver(
            session.client("ses"),
            sender,
            data["reply_to"],
            "Re: Evidence needed for invoice INV-2041",
            ROOT / "examples/correct-receipt.txt",
        )
        return
    if action == "proof":
        response = httpx.get(
            data["url"] + f"/api/cases/{data['case_id']}/packet",
            headers={"Authorization": "Bearer " + data["token"]},
        )
        response.raise_for_status()
        payload = response.json()
        assert payload["case"]["status"] == "reviewed"
        (PRIVATE.parent / "live-proof.json").write_text(json.dumps(payload, indent=2))
        print(json.dumps({"status": "reviewed", "audit_events": len(payload["audit"])}))


if __name__ == "__main__":
    main()
