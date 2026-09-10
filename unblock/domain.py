"""Deterministic business rules. Models cannot waive these checks."""

import re
import secrets
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier() -> str:
    return uuid4().hex


def reply_token() -> str:
    """Unguessable capability embedded in the case's reply address."""
    return secrets.token_hex(16)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewCase(StrictModel):
    supplier: str = Field(min_length=2, max_length=160)
    invoice_ref: str = Field(min_length=1, max_length=80)
    order_ref: str = Field(min_length=1, max_length=80)
    amount_minor: int = Field(gt=0, le=100_000_000_000)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    contact_name: str = Field(min_length=1, max_length=100)
    contact_email: str = Field(max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class ExtractedEvidence(StrictModel):
    document_type: Literal["invoice", "purchase_order", "delivery_receipt", "other"]
    supplier: str | None = Field(default=None, max_length=160)
    invoice_ref: str | None = Field(default=None, max_length=80)
    order_ref: str | None = Field(default=None, max_length=80)
    amount_minor: int | None = Field(default=None, ge=0, le=100_000_000_000)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    signed: bool | None = None
    source_quote: str = Field(max_length=600)
    uncertainty: str | None = Field(default=None, max_length=400)


REQUIREMENTS = ("invoice", "purchase_order", "delivery_receipt")


def normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def assess(case: dict, evidence: dict) -> list[str]:
    """Missing fields fail closed. Extraction is not proof of document authenticity."""
    issues = []
    kind = evidence["document_type"]
    if kind not in REQUIREMENTS:
        return ["This document does not satisfy an invoice, order, or delivery requirement."]
    if normalized(evidence.get("supplier")) != normalized(case["supplier"]):
        issues.append("Supplier does not match the case.")
    if normalized(evidence.get("order_ref")) != normalized(case["order_ref"]):
        issues.append("Order reference is missing or belongs to another order.")
    if kind == "invoice" and normalized(evidence.get("invoice_ref")) != normalized(
        case["invoice_ref"]
    ):
        issues.append("Invoice reference does not match the case.")
    if kind in ("invoice", "purchase_order"):
        if evidence.get("amount_minor") != case["amount_minor"]:
            issues.append("Amount is missing or differs from the expected invoice amount.")
        if evidence.get("currency") != case["currency"]:
            issues.append("Currency is missing or does not match.")
    if kind == "delivery_receipt" and evidence.get("signed") is not True:
        issues.append("Delivery confirmation has no identified signature or explicit acceptance.")
    if evidence.get("uncertainty"):
        issues.append("Document extraction needs clarification: " + evidence["uncertainty"])
    return issues


def awaiting_reply(case: dict) -> bool:
    unresolved = {r["kind"] for r in case["requirements"] if r["status"] != "satisfied"}
    return any(r["status"] == "sent" and r["requirement"] in unresolved for r in case["requests"])


def recompute(case: dict) -> dict:
    requirements = []
    for kind in REQUIREMENTS:
        documents = [
            d for d in case["documents"] if d.get("extraction", {}).get("document_type") == kind
        ]
        valid = [d for d in documents if d.get("status") == "accepted"]
        requirements.append(
            {
                "kind": kind,
                "status": "satisfied"
                if valid
                else ("needs_correction" if documents else "missing"),
                "document_id": valid[-1]["id"] if valid else None,
            }
        )
    case["requirements"] = requirements
    satisfied = {r["kind"] for r in requirements if r["status"] == "satisfied"}
    for request in case["requests"]:
        if request["requirement"] in satisfied and request["status"] != "failed":
            request["status"] = "resolved"
    if case.get("review"):
        case["status"] = "reviewed"
    elif all(r["status"] == "satisfied" for r in requirements):
        case["status"] = "ready_for_review"
    else:
        case["status"] = "blocked"
    return case


def make_case(data: NewCase, tenant: str, case_id: str) -> dict:
    return recompute(
        {
            **data.model_dump(),
            "id": case_id,
            "tenant": tenant,
            "version": 0,
            "created_at": now(),
            "updated_at": now(),
            "documents": [],
            "requests": [],
            "requirements": [],
            "quarantine": [],
            "review": None,
            "reply_token": None if tenant == "demo" else reply_token(),
            "agent_status": "idle",
            "agent_summary": None,
        }
    )
