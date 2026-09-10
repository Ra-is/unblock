import hashlib
from pathlib import Path

import boto3

from unblock.config import settings

MAX_BYTES = 4 * 1024 * 1024
CONTENT_TYPES = {"application/pdf", "text/plain", "image/png", "image/jpeg"}


def validate_file(data: bytes, content_type: str):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Upload a non-empty document smaller than 4 MB.")
    if content_type not in CONTENT_TYPES:
        raise ValueError("Supported documents: PDF, TXT, PNG and JPEG.")
    if content_type == "application/pdf" and not data.startswith(b"%PDF-"):
        raise ValueError("The file is not a PDF.")
    if content_type == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("The file is not a PNG.")
    if content_type == "image/jpeg" and not data.startswith(b"\xff\xd8\xff"):
        raise ValueError("The file is not a JPEG.")
    if content_type == "text/plain":
        data.decode("utf-8")


def put(tenant: str, case_id: str, data: bytes, content_type: str):
    digest = hashlib.sha256(data).hexdigest()
    key = f"{tenant}/{case_id}/{digest}"
    if settings().unblock_bucket:
        boto3.client("s3", region_name=settings().aws_default_region).put_object(
            Bucket=settings().unblock_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )
    elif settings().app_env == "local":
        path = Path(".local/documents") / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    else:
        raise RuntimeError("Private document storage is required")
    return digest, key


def get(key: str) -> bytes:
    if settings().unblock_bucket:
        return (
            boto3.client("s3", region_name=settings().aws_default_region)
            .get_object(Bucket=settings().unblock_bucket, Key=key)["Body"]
            .read(MAX_BYTES + 1)
        )
    return (Path(".local/documents") / key).read_bytes()
