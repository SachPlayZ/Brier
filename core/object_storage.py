"""Private S3-compatible object storage for receipt evidence.

The adapter intentionally speaks the S3 API so it works with AWS S3, Cloudflare
R2, Supabase Storage S3, MinIO, and other compatible providers. Objects remain
private; the application downloads them server-side and can issue short-lived
presigned URLs when a caller needs one.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
from uuid import uuid4


@dataclass(frozen=True)
class StorageConfig:
    bucket: str
    region: str
    endpoint_url: str | None
    access_key_id: str | None
    secret_access_key: str | None
    prefix: str


def config_from_env() -> StorageConfig | None:
    """Return storage settings when a bucket is configured, otherwise ``None``."""
    bucket = os.getenv("BRIER_OBJECT_STORAGE_BUCKET", "").strip()
    if not bucket:
        return None
    return StorageConfig(
        bucket=bucket,
        region=os.getenv("BRIER_OBJECT_STORAGE_REGION", "us-east-1").strip()
        or "us-east-1",
        endpoint_url=os.getenv("BRIER_OBJECT_STORAGE_ENDPOINT_URL", "").strip() or None,
        access_key_id=os.getenv("BRIER_OBJECT_STORAGE_ACCESS_KEY_ID", "").strip()
        or None,
        secret_access_key=os.getenv(
            "BRIER_OBJECT_STORAGE_SECRET_ACCESS_KEY", ""
        ).strip()
        or None,
        prefix=_normalise_prefix(os.getenv("BRIER_OBJECT_STORAGE_PREFIX", "receipts")),
    )


def enabled() -> bool:
    return config_from_env() is not None


def key_for(receipt_id: str, filename: str) -> str:
    """Create a non-guessable, path-safe key for one receipt upload."""
    config = config_from_env()
    if config is None:
        raise RuntimeError("Object storage is not configured.")
    suffix = PurePosixPath(filename or "receipt.png").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".bin"
    return f"{config.prefix}/{receipt_id}/{uuid4().hex}{suffix}"


def put_bytes(data: bytes, *, key: str, content_type: str) -> str:
    """Upload private bytes and return the provider ETag."""
    config = _required_config()
    params = {
        "Bucket": config.bucket,
        "Key": key,
        "Body": data,
        "ContentType": content_type or "application/octet-stream",
    }
    if not config.endpoint_url:
        params["ServerSideEncryption"] = "AES256"
    response = _client(config).put_object(
        **params,
    )
    return str(response.get("ETag", "")).strip('"')


def get_bytes(key: str, *, bucket: str | None = None) -> bytes:
    config = _required_config()
    response = _client(config).get_object(Bucket=bucket or config.bucket, Key=key)
    body = response["Body"]
    try:
        return body.read()
    finally:
        close = getattr(body, "close", None)
        if close:
            close()


def delete(key: str, *, bucket: str | None = None) -> None:
    config = _required_config()
    _client(config).delete_object(Bucket=bucket or config.bucket, Key=key)


def presigned_get_url(
    key: str, *, expires_in: int = 900, bucket: str | None = None
) -> str:
    """Create a short-lived GET URL for a private object."""
    if expires_in < 1 or expires_in > 86_400:
        raise ValueError("expires_in must be between 1 second and 24 hours")
    config = _required_config()
    return _client(config).generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket or config.bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def _required_config() -> StorageConfig:
    config = config_from_env()
    if config is None:
        raise RuntimeError("BRIER_OBJECT_STORAGE_BUCKET is not configured.")
    return config


def _normalise_prefix(prefix: str) -> str:
    value = "/".join(
        part for part in prefix.strip().split("/") if part not in ("", ".", "..")
    )
    return value or "receipts"


@lru_cache(maxsize=4)
def _client(config: StorageConfig):
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except (
        ImportError
    ) as exc:  # pragma: no cover - dependency is installed in deployment
        raise RuntimeError("boto3 is required for object storage uploads.") from exc

    kwargs = {
        "region_name": config.region,
        "endpoint_url": config.endpoint_url,
        "aws_access_key_id": config.access_key_id,
        "aws_secret_access_key": config.secret_access_key,
        "config": BotoConfig(s3={"addressing_style": "path"}),
    }
    return boto3.client(
        "s3", **{key: value for key, value in kwargs.items() if value is not None}
    )
