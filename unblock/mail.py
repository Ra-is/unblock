"""Outbound evidence requests. Recipients are fixed by the case and screened by an allowlist."""

import re

import boto3

from unblock.config import settings

ADDRESS = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class Blocked(Exception):
    """Sending was refused before any message left the system."""


def reply_address(token: str) -> str:
    domain = settings().unblock_inbound_domain
    return f"case-{token}@{domain}" if domain and token else ""


def token_from(address: str) -> str | None:
    """Recover a case reply token from an inbound envelope recipient."""
    local, _, domain = address.strip().casefold().partition("@")
    if domain != settings().unblock_inbound_domain.casefold():
        return None
    # SES may append a +suffix; the token is the first labelled segment.
    local = local.split("+", 1)[0]
    if not local.startswith("case-"):
        return None
    token = local[5:]
    return token if re.fullmatch(r"[0-9a-f]{32}", token) else None


def permitted(recipient: str) -> bool:
    """An address passes only if it matches an allowlist entry or its domain."""
    recipient = recipient.strip().casefold()
    if not ADDRESS.match(recipient):
        return False
    domain = recipient.rpartition("@")[2]
    return any(entry in (recipient, domain) for entry in settings().allowed_recipients)


def check(recipient: str):
    cfg = settings()
    if not cfg.unblock_send_enabled:
        raise Blocked("Outbound mail is disabled for this deployment.")
    if not cfg.unblock_mail_from or not cfg.unblock_inbound_domain:
        raise Blocked("Outbound mail is not configured.")
    if not permitted(recipient):
        raise Blocked("Recipient is outside this deployment's allowlist.")


def send(recipient: str, reply_to: str, subject: str, body: str) -> str:
    check(recipient)
    client = boto3.client("sesv2", region_name=settings().aws_default_region)
    result = client.send_email(
        FromEmailAddress=settings().unblock_mail_from,
        Destination={"ToAddresses": [recipient]},
        ReplyToAddresses=[reply_to],
        Content={
            "Simple": {
                "Subject": {"Data": subject[:200], "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body[:20_000], "Charset": "UTF-8"}},
            }
        },
    )
    return result["MessageId"]
