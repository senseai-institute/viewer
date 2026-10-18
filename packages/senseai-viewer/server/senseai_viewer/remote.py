# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Runs kept in S3 (``SENSEAI_RUNS_S3``, e.g. ``s3://senseai-data/runs/``).

S3 is where runs live; the server's disk is only a cache. Every ``<prefix><run>/viewer.json``
(written by ``senseai-viewer push``) lists that run's RunLogs with their run-list rows, so
the run list needs no download. A RunLog is fetched the first time someone opens it; the
least recently opened ones are evicted beyond ``SENSEAI_CACHE_GB``. A redeploy with an empty
disk therefore loses nothing.

Credentials come from the standard AWS chain: on Railway, ``AWS_ACCESS_KEY_ID``,
``AWS_SECRET_ACCESS_KEY`` and ``AWS_DEFAULT_REGION`` variables of a read-only IAM user.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from senseai_viewer.bundle import FORMAT, MANIFEST
from senseai_viewer.runner import s3 as s3mod

log = logging.getLogger("senseai_viewer.remote")


@dataclass(frozen=True)
class RemoteRun:
    """One RunLog listed in a manifest."""

    run_id: str
    bucket: str
    key: str  # the RunLog
    size: int
    summary: dict[str, Any]
    report_md: str | None
    report_json: str | None
    training: str | None


class RemoteRuns:
    """The manifests under one S3 prefix, refreshed at most every ``refresh_s`` seconds."""

    def __init__(
        self,
        uri: str,
        client: Callable[[], Any] = s3mod.client,
        refresh_s: float = 60.0,
    ) -> None:
        self.uri = uri
        self.bucket, self.prefix = s3mod.parse_uri(uri)
        self._client_factory = client
        self._client: Any = None
        self.refresh_s = refresh_s
        self._runs: dict[str, RemoteRun] = {}
        self._t = -1e9
        self._lock = threading.Lock()
        self.error: str | None = None

    def client(self) -> Any:
        """Return the S3 client (created on first use)."""
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def runs(self, force: bool = False) -> dict[str, RemoteRun]:
        """Every RunLog in S3 by run id; a failed listing keeps the previous one."""
        if not force and time.monotonic() - self._t < self.refresh_s:
            return self._runs
        with self._lock:
            if not force and time.monotonic() - self._t < self.refresh_s:
                return self._runs
            try:
                found = self._list()
                self._runs, self.error = found, None
            except Exception as e:  # no credentials, no network: keep serving the cache
                self.error = s3mod._explain(e, f"list {self.uri}")
                log.warning("runs in S3 unavailable: %s", self.error)
            self._t = time.monotonic()
            return self._runs

    def _list(self) -> dict[str, RemoteRun]:
        c = self.client()
        out: dict[str, RemoteRun] = {}
        for o in s3mod.list_objects(c, self.bucket, self.prefix):
            if not o.key.endswith("/" + MANIFEST):
                continue
            try:
                doc = json.loads(c.get_object(Bucket=self.bucket, Key=o.key)["Body"].read())
            except (ValueError, KeyError) as e:
                log.warning("skipping %s: %s", o.key, e)
                continue
            if doc.get("format") != FORMAT:
                continue
            base = o.key[: -len(MANIFEST)]

            def k(rel: str | None, base: str = base) -> str | None:
                return base + rel if rel else None

            for r in doc.get("runlogs", []):
                rid = str(r["run_id"])
                out[rid] = RemoteRun(
                    run_id=rid,
                    bucket=self.bucket,
                    key=base + r["key"],
                    size=int(r.get("size", 0)),
                    summary=dict(r.get("summary") or {}) | {"run_id": rid},
                    report_md=k(doc.get("report_md")),
                    report_json=k(doc.get("report_json")),
                    training=k(doc.get("training")),
                )
        return out

    def download(self, run: RemoteRun, dest_h5: Path) -> dict[str, Path]:
        """Download the RunLog to ``dest_h5`` and its attachments beside it."""
        c = self.client()
        tmp = dest_h5.with_name(f".{dest_h5.name}.part")
        c.download_file(run.bucket, run.key, str(tmp))
        tmp.replace(dest_h5)
        got = {}
        for kind, key in (
            ("report.md", run.report_md),
            ("report.json", run.report_json),
            ("training.txt", run.training),
        ):
            if key:
                p = dest_h5.with_name(f"{dest_h5.stem}.{kind}")
                try:
                    c.download_file(run.bucket, key, str(p))
                    got[kind] = p
                except Exception as e:  # an attachment is optional
                    log.warning("attachment %s: %s", key, e)
        return got
