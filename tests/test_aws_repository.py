import boto3
import pytest
from moto import mock_aws

from unblock.domain import NewCase
from unblock.repository import Conflict, DynamoRepository, Missing
from unblock.service import create_case, enqueue


@mock_aws
def test_transactional_job_and_audit_with_optimistic_lock():
    boto3.client("dynamodb", region_name="eu-west-2").create_table(
        TableName="unblock-test",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
    )
    repo = DynamoRepository("unblock-test")
    data = NewCase(
        supplier="Test Ltd",
        invoice_ref="I1",
        order_ref="P1",
        amount_minor=100,
        currency="GHS",
        contact_name="Test",
        contact_email="test@example.com",
    )
    case = create_case(repo, "tenant-one", data, "new-case-key", "reviewer")
    case, job = enqueue(repo, case, "reviewer")
    assert repo.get("tenant-one", case["id"])["agent_status"] == "queued"
    assert repo.job_get("tenant-one", job["id"]) == job
    assert len(repo.audit("tenant-one", case["id"])) == 2
    with pytest.raises(Missing):
        repo.job_get("tenant-two", job["id"])
    stale = dict(case)
    repo.save(case, "Version incremented", "worker")
    with pytest.raises(Conflict):
        repo.save(stale, "Must not appear in audit", "worker")
    assert len(repo.audit("tenant-one", case["id"])) == 3
    repo.job_complete("tenant-one", job["id"])
    assert repo.job_get("tenant-one", job["id"])["status"] == "complete"
