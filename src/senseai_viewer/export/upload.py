# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Upload a summary directory to ``s3://<bucket>/<prefix>/<recording_id>/``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["CONTENT_TYPES", "upload_summary"]

CONTENT_TYPES = {
    ".json": "application/json",
    ".parquet": "application/vnd.apache.parquet",
    ".jpg": "image/jpeg",
}


def upload_summary(
    summary_dir: str | Path,
    *,
    bucket: str,
    prefix: str = "viewer",
    client: Any = None,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Upload every file of ``summary_dir`` and return the uploaded keys.

    ``manifest.json`` is uploaded **last**, so the viewer never lists a half-uploaded
    recording. Objects left over from an earlier export of the same id are deleted before
    the manifest goes up.
    """
    if client is None:
        import boto3

        client = boto3.client("s3")
    root = Path(summary_dir)
    base = f"{prefix.strip('/')}/{root.name}/" if prefix.strip("/") else f"{root.name}/"
    files = sorted(p for p in root.rglob("*") if p.is_file())
    rels = [p.relative_to(root).as_posix() for p in files]
    ordered = [r for r in rels if r != "manifest.json"] + (
        ["manifest.json"] if "manifest.json" in rels else []
    )
    stale = sorted(_existing_keys(client, bucket, base) - {base + r for r in rels})
    keys: list[str] = []
    for rel in ordered:
        if rel == "manifest.json" and stale:
            for i in range(0, len(stale), 1000):
                batch = stale[i : i + 1000]
                client.delete_objects(
                    Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch]}
                )
        key = base + rel
        extra = {"ContentType": CONTENT_TYPES.get(Path(rel).suffix, "application/octet-stream")}
        client.upload_file(str(root / rel), bucket, key, ExtraArgs=extra)
        keys.append(key)
        if log is not None:
            log(f"uploaded s3://{bucket}/{key}")
    return keys


def _existing_keys(client: Any, bucket: str, base: str) -> set[str]:
    keys: set[str] = set()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=base):
        keys.update(obj["Key"] for obj in page.get("Contents", []))
    return keys
