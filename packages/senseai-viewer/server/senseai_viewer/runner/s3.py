# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""The recordings bucket: list DSEC archives, download them resumably, check permissions.

Credentials come from the standard AWS chain; on EC2 that is the instance role, so nothing
is configured by hand. ``boto3`` is imported lazily (extra ``s3``), so the viewer still runs
without it and the Setup panel says what is missing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: DSEC sequence names: zurich_city_04_a, interlaken_00_b, thun_01_a, …
SEQ_RE = re.compile(r"(?<![a-z0-9])([a-z]+(?:_[a-z]+)*_\d{2}_[a-z])(?![a-z0-9])")
CHUNK = 8 * 1024 * 1024
#: Archives shared by many sequences that are small enough to fetch for one of them.
SHARED_SMALL = re.compile(r"calib|timestamp|detection|label|track", re.IGNORECASE)
SHARED_MAX_BYTES = 2 * 1024**3


class S3Unavailable(RuntimeError):
    """boto3 is missing or the bucket cannot be reached; the message says which."""


@dataclass(frozen=True)
class S3Object:
    """One object in the bucket."""

    key: str
    size: int
    etag: str


def parse_uri(uri: str) -> tuple[str, str]:
    """``s3://bucket/prefix/`` -> ``(bucket, "prefix/")``."""
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// URI: {uri!r}")
    bucket, _, prefix = uri[5:].partition("/")
    if not bucket:
        raise ValueError(f"no bucket in {uri!r}")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return bucket, prefix


def client(region: str | None = None) -> Any:
    """Return an S3 client from the default credential chain (instance role on EC2)."""
    try:
        import boto3
    except ImportError as e:
        raise S3Unavailable("the boto3 library is not installed (Setup: Install / repair)") from e
    return boto3.client("s3", region_name=region) if region else boto3.client("s3")


def list_objects(s3: Any, bucket: str, prefix: str) -> list[S3Object]:
    """Every object under ``prefix``."""
    out: list[S3Object] = []
    token: str | None = None
    while True:
        kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            if not o["Key"].endswith("/"):
                out.append(S3Object(o["Key"], int(o["Size"]), str(o.get("ETag", "")).strip('"')))
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")


def sequence_of(key: str, prefix: str) -> str | None:
    """Return the DSEC sequence an object belongs to, from its path or file name."""
    rel = key[len(prefix) :] if key.startswith(prefix) else key
    m = SEQ_RE.search(rel.lower())
    return m.group(1) if m else None


def group(objs: list[S3Object], prefix: str) -> tuple[dict[str, list[S3Object]], list[S3Object]]:
    """Objects per sequence, and the shared ones that name no sequence."""
    per: dict[str, list[S3Object]] = {}
    shared: list[S3Object] = []
    for o in objs:
        seq = sequence_of(o.key, prefix)
        if seq:
            per.setdefault(seq, []).append(o)
        else:
            shared.append(o)
    return per, shared


def useful_shared(shared: list[S3Object]) -> list[S3Object]:
    """Shared archives worth fetching for one sequence (calibration, timestamps, labels)."""
    return [
        o
        for o in shared
        if SHARED_SMALL.search(o.key.rsplit("/", 1)[-1]) and o.size <= SHARED_MAX_BYTES
    ]


def download(
    s3: Any,
    bucket: str,
    obj: S3Object,
    dest: Path,
    on_bytes: Callable[[int], None] = lambda _n: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> Path:
    """Download ``obj`` to ``dest``, resuming a previous partial download of the same version.

    Returns ``dest``; does nothing if it already has the right size. ``on_bytes`` gets the
    bytes on disk so far. Raises ``InterruptedError`` if ``cancelled()`` turns true.
    """
    if dest.exists() and dest.stat().st_size == obj.size:
        on_bytes(obj.size)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    tag = dest.with_name(dest.name + ".etag")
    if part.exists() and (not tag.exists() or tag.read_text() != obj.etag):
        part.unlink()  # the object changed since the partial download
    tag.write_text(obj.etag)
    have = part.stat().st_size if part.exists() else 0
    on_bytes(have)
    if have < obj.size:
        resp = s3.get_object(Bucket=bucket, Key=obj.key, Range=f"bytes={have}-")
        body = resp["Body"]
        with part.open("ab") as f:
            for chunk in iter(lambda: body.read(CHUNK), b""):
                f.write(chunk)
                have += len(chunk)
                on_bytes(have)
                if cancelled():
                    raise InterruptedError("download cancelled")
    if part.stat().st_size != obj.size:
        raise OSError(f"{obj.key}: got {part.stat().st_size} bytes, expected {obj.size}")
    part.replace(dest)
    tag.unlink(missing_ok=True)
    return dest


def check_access(s3: Any, bucket: str, prefix: str) -> tuple[bool, str]:
    """Can this machine list and read the recordings? ``(ok, plain message)``."""
    try:
        page = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=5)
    except Exception as e:  # botocore ClientError / NoCredentialsError / EndpointConnectionError
        return False, _explain(e, f"list s3://{bucket}/{prefix}")
    objs = [o for o in page.get("Contents", []) if not o["Key"].endswith("/")]
    if not objs:
        return True, f"s3://{bucket}/{prefix} is reachable but empty"
    try:
        s3.head_object(Bucket=bucket, Key=objs[0]["Key"])
    except Exception as e:
        return False, _explain(e, f"read s3://{bucket}/{objs[0]['Key']}")
    return True, f"s3://{bucket}/{prefix} is reachable"


def _explain(e: Exception, what: str) -> str:
    code = ""
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        code = str(resp.get("Error", {}).get("Code", ""))
    name = type(e).__name__
    if code in ("AccessDenied", "403", "AllAccessDisabled"):
        return f"this machine's role is not allowed to {what}"
    if name in ("NoCredentialsError", "PartialCredentialsError"):
        return f"no AWS credentials on this machine, so it cannot {what} (attach an instance role)"
    if code in ("NoSuchBucket", "404"):
        return f"the bucket does not exist ({what})"
    return f"could not {what}: {code or name}: {e}"


def policy_for(bucket: str, prefix: str) -> str:
    """Return the IAM policy the instance role needs, ready to paste."""
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ListSenseAIRecordings",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {"StringLike": {"s3:prefix": [f"{prefix}*"]}},
            },
            {
                "Sid": "ReadSenseAIRecordings",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": f"arn:aws:s3:::{bucket}/{prefix}*",
            },
        ],
    }
    return json.dumps(doc, indent=2)
