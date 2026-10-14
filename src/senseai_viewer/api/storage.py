# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Where summaries live: an S3 prefix, or a local folder (``LOCAL_SUMMARIES``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from senseai_viewer.export.format import is_valid_recording_id

__all__ = ["LocalStorage", "S3Storage", "Storage"]


class Storage(Protocol):
    """Read-only access to ``<root>/<recording_id>/<relative path>``."""

    def list_ids(self) -> list[str]:
        """Return the ids of all recording directories, sorted."""
        ...

    def read(self, recording_id: str, rel: str) -> bytes | None:
        """Return the bytes of one summary file, or ``None`` if it does not exist."""
        ...


class LocalStorage:
    """Summaries in a local folder, as written by ``senseai-viewer export``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def list_ids(self) -> list[str]:
        """Return the ids of all sub-folders with a valid recording id, sorted."""
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if p.is_dir() and is_valid_recording_id(p.name)
        )

    def read(self, recording_id: str, rel: str) -> bytes | None:
        """Read ``<root>/<recording_id>/<rel>``; paths escaping the root read as missing."""
        path = (self.root / recording_id / rel).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return None
        return path.read_bytes()


class S3Storage:
    """Summaries under ``s3://<bucket>/<prefix>/``."""

    def __init__(
        self, bucket: str, prefix: str = "viewer", client: Any = None, region: str | None = None
    ) -> None:
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=region)
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _base(self) -> str:
        return f"{self.prefix}/" if self.prefix else ""

    def list_ids(self) -> list[str]:
        """List the "directories" directly under the prefix."""
        base = self._base()
        ids: set[str] = set()
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=base, Delimiter="/"
        )
        for page in pages:
            for cp in page.get("CommonPrefixes", []):
                rid = cp["Prefix"][len(base) :].rstrip("/")
                if is_valid_recording_id(rid):
                    ids.add(rid)
        return sorted(ids)

    def read(self, recording_id: str, rel: str) -> bytes | None:
        """Fetch one object; a missing key reads as ``None``."""
        from botocore.exceptions import ClientError

        key = f"{self._base()}{recording_id}/{rel}"
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        data: bytes = obj["Body"].read()
        return data
