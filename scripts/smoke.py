"""Exercise real auth, upload, queue, Strands/Bedrock, correction and reviewer acceptance.

Creates one clearly marked synthetic case per run. Leaves the result for inspection.
Never sends email and never prints access tokens or passwords.
"""

import json
from pathlib import Path
import time
from uuid import uuid4

import boto3
import httpx

ROOT = Path(__file__).resolve().parents[1]


def wait_review(client, case_id):
    last = None
    for _ in range(75):
        response = client.get(f"/api/cases/{case_id}")
        response.raise_for_status()
        case = response.json()
        if case["agent_status"] != last:
            print("Agent: " + case["agent_status"], flush=True)
            last = case["agent_status"]
        if case["agent_status"] == "failed":
            raise RuntimeError("Worker failed. Inspect CloudWatch logs for the error type.")
        if case["agent_status"] == "idle":
            return case
        time.sleep(4)
    raise TimeoutError("Agent did not complete within five minutes")


def main():
    cfg = json.loads((ROOT / ".local/deployment.json").read_text())
    for asset in ("index.html", "app.js", "style.css"):
        route = "/" if asset == "index.html" else f"/assets/{asset}"
        response = httpx.get(cfg["AppUrl"] + route)
        response.raise_for_status()
        assert response.content == (ROOT / "unblock/web" / asset).read_bytes(), (
            f"Deployed {asset} differs from source"
        )
    owner = json.loads((ROOT / ".local/owner-access.json").read_text())
    cognito = boto3.Session(profile_name=cfg["Profile"], region_name=cfg["Region"]).client(
        "cognito-idp"
    )
    token = cognito.initiate_auth(
        ClientId=cfg["ClientId"],
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": owner["username"], "PASSWORD": owner["password"]},
    )["AuthenticationResult"]["IdToken"]
    assert httpx.get(cfg["AppUrl"] + "/api/cases").status_code == 401
    print("Anonymous access rejected; owner authentication succeeded.", flush=True)
    with httpx.Client(
        base_url=cfg["AppUrl"], headers={"Authorization": "Bearer " + token}, timeout=45
    ) as client:
        key = "smoke-" + uuid4().hex
        payload = {
            "supplier": "Ama Catering Ltd",
            "invoice_ref": "INV-2041",
            "order_ref": "PO-1042",
            "amount_minor": 480000,
            "currency": "GHS",
            "contact_name": "Synthetic test contact",
            "contact_email": "nobody@example.com",
        }
        response = client.post("/api/cases", headers={"Idempotency-Key": key}, json=payload)
        response.raise_for_status()
        case_id = response.json()["id"]
        repeat = client.post("/api/cases", headers={"Idempotency-Key": key}, json=payload)
        assert repeat.json()["id"] == case_id
        print("Synthetic case created; repeated create is idempotent.", flush=True)
        for filename in ("invoice.txt", "purchase-order.txt", "wrong-receipt.txt"):
            response = client.post(
                f"/api/cases/{case_id}/documents",
                files={
                    "file": (filename, (ROOT / "examples" / filename).read_bytes(), "text/plain")
                },
            )
            response.raise_for_status()
        response = client.post(f"/api/cases/{case_id}/run")
        response.raise_for_status()
        case = wait_review(client, case_id)
        assert case["status"] == "blocked", case["requirements"]
        assert len([r for r in case["requirements"] if r["status"] == "satisfied"]) == 2, case[
            "documents"
        ]
        assert case["requests"], "Agent did not prepare a request"
        print(
            "Wrong-order receipt rejected; invoice and purchase order matched; request drafted.",
            flush=True,
        )
        response = client.post(
            f"/api/cases/{case_id}/review",
            json={"version": case["version"], "note": "Must reject premature review"},
        )
        assert response.status_code == 422
        filename = "correct-receipt.txt"
        response = client.post(
            f"/api/cases/{case_id}/documents",
            files={"file": (filename, (ROOT / "examples" / filename).read_bytes(), "text/plain")},
        )
        response.raise_for_status()
        response = client.post(f"/api/cases/{case_id}/run")
        response.raise_for_status()
        case = wait_review(client, case_id)
        assert case["status"] == "ready_for_review", case["documents"]
        response = client.post(
            f"/api/cases/{case_id}/review",
            json={
                "version": case["version"],
                "note": "Synthetic acceptance test: compared all three evidence types. No real transaction.",
            },
        )
        response.raise_for_status()
        assert response.json()["status"] == "reviewed"
        packet = client.get(f"/api/cases/{case_id}/packet")
        packet.raise_for_status()
        assert len(packet.json()["audit"]) > 8
        (ROOT / ".local/smoke-result.json").write_text(
            json.dumps(
                {
                    "case_id": case_id,
                    "status": "passed",
                    "url": cfg["AppUrl"],
                    "checks": [
                        "authentication",
                        "idempotent_create",
                        "s3_upload",
                        "transactional_outbox",
                        "sqs_worker",
                        "strands_bedrock_extraction",
                        "wrong_order_rejection",
                        "request_draft",
                        "premature_review_rejected",
                        "correction",
                        "human_review",
                        "packet_export",
                    ],
                },
                indent=2,
            )
        )
        print(
            "PASS: correction accepted, human review recorded, audit packet exported.", flush=True
        )


if __name__ == "__main__":
    main()
