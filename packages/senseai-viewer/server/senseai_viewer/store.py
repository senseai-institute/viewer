# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Run store: RunLog files in ``DATA_DIR/runs``, their indexes, and a window cache.

There is no database yet: a run is its file, its id is the file's ``run_id`` attribute, and
the index is cached as JSON beside it. Open files are kept per run and reopened if the file
changes.
"""

from __future__ import annotations

import re
import shutil
import threading
from collections import OrderedDict
from collections.abc import Hashable
from pathlib import Path
from typing import Any

from senseai_viewer import index as index_mod
from senseai_viewer.readers.runlog import RunLog, RunLogError

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ByteLRU:
    """An LRU of ``bytes`` values bounded by total size."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._d: OrderedDict[Hashable, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable) -> bytes | None:
        """Return the cached value and mark it recent, or ``None``."""
        with self._lock:
            v = self._d.get(key)
            if v is None:
                self.misses += 1
                return None
            self._d.move_to_end(key)
            self.hits += 1
            return v

    def put(self, key: Hashable, value: bytes) -> None:
        """Insert, evicting the oldest entries beyond the budget."""
        if len(value) > self.max_bytes:
            return
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self._size -= len(old)
            self._d[key] = value
            self._size += len(value)
            while self._size > self.max_bytes:
                _, ev = self._d.popitem(last=False)
                self._size -= len(ev)


class Store:
    """All runs under ``root/runs``."""

    def __init__(self, root: Path, cache_mb: int = 64) -> None:
        self.runs_dir = root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.cache = ByteLRU(cache_mb * 1024 * 1024)
        self._lock = threading.Lock()
        self._paths: dict[str, Path] = {}
        self._open: dict[str, tuple[int, RunLog]] = {}
        self._index: dict[str, tuple[int, dict[str, Any]]] = {}
        self.rescan()

    def rescan(self) -> None:
        """Rediscover run files; unreadable files are skipped."""
        paths: dict[str, Path] = {}
        for p in sorted(self.runs_dir.glob("*.h5")):
            try:
                with RunLog(p) as log:
                    paths[log.run_id] = p
            except RunLogError:
                continue
        with self._lock:
            self._paths = paths

    def ids(self) -> list[str]:
        """Run ids, sorted."""
        return sorted(self._paths)

    def path(self, run_id: str) -> Path:
        """File of ``run_id``; ``KeyError`` if unknown."""
        return self._paths[run_id]

    def version(self, run_id: str) -> int:
        """Return a value that changes whenever the run's file changes (cache key part)."""
        st = self.path(run_id).stat()
        return hash((st.st_size, st.st_mtime_ns))

    def log(self, run_id: str) -> RunLog:
        """Return the open RunLog of ``run_id`` (shared; do not close)."""
        ver = self.version(run_id)
        with self._lock:
            cur = self._open.get(run_id)
            if cur and cur[0] == ver:
                return cur[1]
            if cur:
                cur[1].close()
            log = RunLog(self._paths[run_id])
            self._open[run_id] = (ver, log)
            return log

    def index(self, run_id: str) -> dict[str, Any]:
        """Return the run's index (computed once per file version)."""
        ver = self.version(run_id)
        cur = self._index.get(run_id)
        if cur and cur[0] == ver:
            return cur[1]
        idx = index_mod.load_or_build(self.path(run_id))
        self._index[run_id] = (ver, idx)
        return idx

    def add(self, tmp: Path) -> str:
        """Validate an uploaded file, move it into the store, index it; return its run id."""
        with RunLog(tmp) as log:
            run_id = log.run_id
        if not RUN_ID.match(run_id):
            raise RunLogError(f"run_id {run_id!r} must match {RUN_ID.pattern}")
        dest = self.runs_dir / f"{run_id}.h5"
        with self._lock:
            cur = self._open.pop(run_id, None)
            if cur:
                cur[1].close()
        shutil.move(str(tmp), dest)
        self.rescan()
        self.index(run_id)
        return run_id

    def delete(self, run_id: str) -> None:
        """Remove a run and its cached index."""
        p = self.path(run_id)
        with self._lock:
            cur = self._open.pop(run_id, None)
            if cur:
                cur[1].close()
            self._index.pop(run_id, None)
        p.unlink(missing_ok=True)
        p.with_suffix(p.suffix + ".index.json").unlink(missing_ok=True)
        self.rescan()
