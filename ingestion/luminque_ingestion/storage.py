"""Thin S3 wrapper — the only module in the codebase that imports boto3.

Locally this talks to MinIO via S3_ENDPOINT_URL; in prod the endpoint is
unset (boto3 defaults to real S3) and auth is the task's IAM role. Same
code path, different env.
"""

import boto3

from luminque_ingestion.config import get_settings


def _client():
    return boto3.client("s3", endpoint_url=get_settings().s3_endpoint_url)


def put(key: str, body: bytes, content_type: str = "image/png") -> None:
    _client().put_object(
        Bucket=get_settings().s3_bucket, Key=key, Body=body, ContentType=content_type
    )


def get(key: str) -> bytes:
    return _client().get_object(Bucket=get_settings().s3_bucket, Key=key)["Body"].read()


def list(prefix: str) -> list[str]:
    paginator = _client().get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=get_settings().s3_bucket, Prefix=prefix):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    return keys
