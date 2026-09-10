"""Tenant-scoped aggregates, optimistic concurrency, immutable audit and transactional jobs."""

import copy
import json
import threading
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from unblock.config import settings
from unblock.domain import identifier, now


class Conflict(Exception):
    pass


class Missing(Exception):
    pass


def plain(value):
    return json.loads(
        json.dumps(value, default=lambda x: int(x) if isinstance(x, Decimal) else str(x))
    )


class MemoryRepository:
    def __init__(self):
        self.cases, self.events, self.jobs, self.replies = {}, {}, {}, {}
        self.lock = threading.RLock()

    def resolve_reply_token(self, token):
        with self.lock:
            if token not in self.replies:
                raise Missing()
            return dict(self.replies[token])

    def get(self, tenant, case_id):
        with self.lock:
            if (tenant, case_id) not in self.cases:
                raise Missing()
            return copy.deepcopy(self.cases[tenant, case_id])

    def list(self, tenant):
        with self.lock:
            return sorted(
                [copy.deepcopy(v) for (t, _), v in self.cases.items() if t == tenant],
                key=lambda c: c["created_at"],
                reverse=True,
            )[:100]

    def save(self, case, event, actor, job=None):
        with self.lock:
            key = (case["tenant"], case["id"])
            current = self.cases.get(key)
            if (current and current["version"] != case["version"]) or (
                not current and case["version"] != 0
            ):
                raise Conflict()
            record, audit = prepare(case, event, actor)
            if not current and record.get("reply_token"):
                self.replies[record["reply_token"]] = {
                    "tenant": record["tenant"],
                    "case_id": record["id"],
                }
            self.cases[key] = record
            self.events.setdefault(key, []).append(audit)
            if job:
                self.jobs[job["id"]] = copy.deepcopy(job)
            return copy.deepcopy(record)

    def audit(self, tenant, case_id):
        self.get(tenant, case_id)
        return copy.deepcopy(self.events.get((tenant, case_id), []))

    def job_get(self, tenant, job_id):
        job = self.jobs.get(job_id)
        if not job or job["tenant"] != tenant:
            raise Missing()
        return copy.deepcopy(job)

    def job_complete(self, tenant, job_id):
        with self.lock:
            self.job_get(tenant, job_id)
            self.jobs[job_id]["status"] = "complete"


def prepare(case, event, actor):
    record = copy.deepcopy(case)
    record["version"] += 1
    record["updated_at"] = now()
    # Keep aggregate well below DynamoDB's 400 KB limit. Audit is stored separately.
    if len(json.dumps(record).encode()) > 280_000:
        raise ValueError("Case capacity reached. Start a separate case for additional evidence.")
    audit = {
        "id": identifier(),
        "at": now(),
        "actor": actor,
        "message": event,
        "version": record["version"],
    }
    return record, audit


class DynamoRepository:
    def __init__(self, table_name=None):
        self.table_name = table_name or settings().unblock_table
        resource = boto3.resource("dynamodb", region_name=settings().aws_default_region)
        self.table = resource.Table(self.table_name)
        self.client = boto3.client("dynamodb", region_name=settings().aws_default_region)

    @staticmethod
    def pk(tenant):
        return f"TENANT#{tenant}"

    def get(self, tenant, case_id):
        item = self.table.get_item(
            Key={"pk": self.pk(tenant), "sk": f"CASE#{case_id}"}, ConsistentRead=True
        ).get("Item")
        if not item:
            raise Missing()
        return plain(item["data"])

    def list(self, tenant):
        items, cursor = [], None
        while len(items) < 100:
            params = {
                "KeyConditionExpression": Key("pk").eq(self.pk(tenant))
                & Key("sk").begins_with("CASE#"),
                "Limit": 100 - len(items),
            }
            if cursor:
                params["ExclusiveStartKey"] = cursor
            result = self.table.query(**params)
            items.extend(plain(i["data"]) for i in result["Items"])
            cursor = result.get("LastEvaluatedKey")
            if not cursor:
                break
        return sorted(items, key=lambda c: c["created_at"], reverse=True)

    def save(self, case, event, actor, job=None):
        record, audit = prepare(case, event, actor)

        def serialize(obj):
            return {k: TypeSerializer().serialize(v) for k, v in obj.items()}

        aggregate = {
            "pk": self.pk(case["tenant"]),
            "sk": f"CASE#{case['id']}",
            "kind": "CASE",
            "version": record["version"],
            "data": record,
        }
        put = {"TableName": self.table_name, "Item": serialize(aggregate)}
        pointer = None
        if case["version"] == 0:
            put["ConditionExpression"] = "attribute_not_exists(pk)"
            if record.get("reply_token"):
                pointer = {
                    "pk": f"REPLY#{record['reply_token']}",
                    "sk": "POINTER",
                    "kind": "REPLY",
                    "data": {"tenant": record["tenant"], "case_id": record["id"]},
                }
        else:
            put.update(
                ConditionExpression="version = :v",
                ExpressionAttributeValues={":v": {"N": str(case["version"])}},
            )
        audit_item = {
            "pk": f"AUDIT#{case['tenant']}#{case['id']}",
            "sk": f"{record['version']:010}#{audit['id']}",
            "kind": "AUDIT",
            "data": audit,
        }
        transaction = [
            {"Put": put},
            {
                "Put": {
                    "TableName": self.table_name,
                    "Item": serialize(audit_item),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
        ]
        if pointer:
            transaction.append(
                {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": serialize(pointer),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                }
            )
        if job:
            transaction.append(
                {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": serialize(
                            {
                                "pk": self.pk(case["tenant"]),
                                "sk": f"JOB#{job['id']}",
                                "kind": "JOB",
                                "data": job,
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                }
            )
        try:
            self.client.transact_write_items(TransactItems=transaction)
        except ClientError as error:
            if error.response["Error"]["Code"] == "TransactionCanceledException":
                raise Conflict() from error
            raise
        return record

    def audit(self, tenant, case_id):
        self.get(tenant, case_id)
        items, cursor = [], None
        while True:
            params = {
                "KeyConditionExpression": Key("pk").eq(f"AUDIT#{tenant}#{case_id}"),
                "ConsistentRead": True,
            }
            if cursor:
                params["ExclusiveStartKey"] = cursor
            result = self.table.query(**params)
            items.extend(plain(i["data"]) for i in result["Items"])
            cursor = result.get("LastEvaluatedKey")
            if not cursor:
                return items

    def resolve_reply_token(self, token):
        result = self.table.get_item(
            Key={"pk": f"REPLY#{token}", "sk": "POINTER"}, ConsistentRead=True
        )
        if "Item" not in result:
            raise Missing()
        return plain(result["Item"]["data"])

    def job_get(self, tenant, job_id):
        result = self.table.get_item(
            Key={"pk": self.pk(tenant), "sk": f"JOB#{job_id}"}, ConsistentRead=True
        )
        if "Item" not in result:
            raise Missing()
        return plain(result["Item"]["data"])

    def job_complete(self, tenant, job_id):
        self.table.update_item(
            Key={"pk": self.pk(tenant), "sk": f"JOB#{job_id}"},
            UpdateExpression="SET #d.#s = :s",
            ExpressionAttributeNames={"#d": "data", "#s": "status"},
            ExpressionAttributeValues={":s": "complete"},
            ConditionExpression="attribute_exists(pk)",
        )


_memory = MemoryRepository()


def repository():
    if settings().unblock_table:
        return DynamoRepository()
    if settings().app_env != "local":
        raise RuntimeError("A durable repository is required outside local development")
    return _memory
