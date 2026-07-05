"""All config comes from env vars — no config files, no environment branches."""

import os
from dataclasses import dataclass

DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    database_url: str
    s3_endpoint_url: str | None  # unset in prod → boto3 defaults to real S3
    s3_bucket: str
    max_upload_bytes: int


def get_settings() -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
        s3_bucket=os.environ["S3_BUCKET"],
        max_upload_bytes=int(
            os.environ.get("MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)
        ),
    )
