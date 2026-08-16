"""Local/S3 backends for discovery frames and artifacts.

Two abstractions, each with a local and an S3 implementation:

  FrameSource  read-only screenshots. Keys are layout-root-relative
               ({tenant}/{agent}/{YYYY-MM-DD}/{captured_at_iso}.png), so the
               same keys work against the local mirror and the bucket.
               Listing carries sizes, so chunk budgeting needs no reads;
               bytes are fetched per frame on demand and never touch disk.
  Store        read/write artifacts (segment notes, run reports/workflows).

`make_frames(x)` / `make_store(x)` accept a local path or an `s3://bucket[/prefix]`
URI; with a prefix, keys stay relative to that prefix (it is the layout root).
No model calls, no writes to frame sources.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

os.environ.setdefault("AWS_PROFILE", "kangaroo")

import workflow

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".md": "text/markdown; charset=utf-8",
    ".mmd": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
}


def content_type(name: str) -> str:
    return _CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def is_s3(uri) -> bool:
    return str(uri).startswith("s3://")


def parse_s3_uri(uri) -> tuple[str, str]:
    bucket, _, prefix = str(uri)[5:].partition("/")
    if not bucket:
        raise ValueError(f"invalid S3 URI: {uri}")
    return bucket, prefix.strip("/")


def s3_client():
    import boto3
    from botocore.config import Config
    return boto3.client("s3", config=Config(retries={"max_attempts": 10, "mode": "adaptive"}))


def selection(tenant: str | None, agent: str | None, date: str | None):
    """(prefix, match) narrowing a frame listing to a tenant/agent/date.

    The prefix covers the leading contiguous selectors (cheap: one prefix
    listing); match re-checks all of them against key segments (correct even
    for gappy selections like --date without --tenant).
    """
    parts = []
    for v in (tenant, agent, date):
        if not v:
            break
        parts.append(v)
    prefix = "/".join(parts) + "/" if parts else ""

    def match(key: str) -> bool:
        kp = key.split("/")
        return ((not tenant or (len(kp) > 0 and kp[0] == tenant))
                and (not agent or (len(kp) > 1 and kp[1] == agent))
                and (not date or (len(kp) > 2 and kp[2] == date)))

    return prefix, match


# --- Frame sources -----------------------------------------------------------

@dataclass(frozen=True)
class Frame:
    key: str
    ts: datetime
    size: int
    source: object = field(compare=False, repr=False)

    def read(self) -> bytes:
        return self.source.read(self.key)


class LocalFrames:
    def __init__(self, root) -> None:
        self.root = Path(root)

    def __str__(self) -> str:
        return str(self.root)

    def list(self, prefix: str = "", start: datetime | None = None,
             end: datetime | None = None) -> list[Frame]:
        base = self.root / prefix if prefix else self.root
        if not base.is_dir():
            return []
        frames = []
        for p in base.rglob("*.png"):
            key = p.relative_to(self.root).as_posix()
            ts = workflow.frame_time(key)
            if ts is None:
                continue
            if start and ts < start:
                continue
            if end and ts > end:
                continue
            frames.append(Frame(key, ts, p.stat().st_size, self))
        return frames

    def read(self, key: str) -> bytes:
        return (self.root / key).read_bytes()


class S3Frames:
    def __init__(self, bucket: str, prefix: str = "", client=None) -> None:
        self.bucket = bucket
        self.base = f"{prefix}/" if prefix else ""
        self.client = client or s3_client()

    def __str__(self) -> str:
        return f"s3://{self.bucket}/{self.base}".rstrip("/")

    def list(self, prefix: str = "", start: datetime | None = None,
             end: datetime | None = None) -> list[Frame]:
        frames = []
        token = None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self.base + prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kw)
            for obj in resp.get("Contents", []):
                key = obj["Key"][len(self.base):]
                if not key.endswith(".png"):
                    continue
                ts = workflow.frame_time(key)
                if ts is None:
                    continue
                if start and ts < start:
                    continue
                if end and ts > end:
                    continue
                frames.append(Frame(key, ts, obj["Size"], self))
            token = resp.get("NextContinuationToken")
            if not token:
                break
        return frames

    def read(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self.base + key)["Body"].read()


def make_frames(uri) -> LocalFrames | S3Frames:
    return S3Frames(*parse_s3_uri(uri)) if is_s3(uri) else LocalFrames(uri)


# --- Artifact stores ---------------------------------------------------------

class LocalStore:
    def __init__(self, root) -> None:
        self.root = Path(root)

    def list(self, suffix: str = "") -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.relative_to(self.root).as_posix()
                      for p in self.root.rglob(f"*{suffix}") if p.is_file())

    def read_text(self, rel: str) -> str:
        return (self.root / rel).read_text()

    def write_text(self, rel: str, content: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def url(self, rel: str = "") -> str:
        return str(self.root / rel if rel else self.root)


class S3Store:
    def __init__(self, bucket: str, prefix: str = "", client=None) -> None:
        self.bucket = bucket
        self.base = f"{prefix}/" if prefix else ""
        self.client = client or s3_client()

    def list(self, suffix: str = "") -> list[str]:
        keys = []
        token = None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": self.base}
            if token:
                kw["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kw)
            keys += [obj["Key"][len(self.base):] for obj in resp.get("Contents", [])
                     if obj["Key"].endswith(suffix)]
            token = resp.get("NextContinuationToken")
            if not token:
                break
        return sorted(keys)

    def read_text(self, rel: str) -> str:
        return self.client.get_object(Bucket=self.bucket, Key=self.base + rel)["Body"].read().decode()

    def write_text(self, rel: str, content: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.base + rel,
                               Body=content.encode(), ContentType=content_type(rel))

    def url(self, rel: str = "") -> str:
        return f"s3://{self.bucket}/{self.base}{rel}"


def make_store(uri) -> LocalStore | S3Store:
    return S3Store(*parse_s3_uri(uri)) if is_s3(uri) else LocalStore(uri)
