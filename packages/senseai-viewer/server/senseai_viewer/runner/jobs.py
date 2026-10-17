# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Background jobs: a SQLite job table and a supervised worker process per job.

A job never runs inside a web request. The server writes the job row and a snapshot of its
settings, then starts ``python -m senseai_viewer.runner.jobs work <id>`` in its own process
group, so closing the browser, losing SSH or restarting the viewer leaves it running. The
worker records stage progress in the table; the page polls it.

Job states::

    queued -> running -> succeeded
                  |----> failed      --Resume--> queued   (finished stages are skipped)
                  '----> cancelling -> cancelled --Resume--> queued
    viewer restarts: a running job whose worker is alive is left alone; a dead one -> failed
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from senseai_viewer.settings import Settings

ACTIVE = ("queued", "running", "cancelling")
FINISHED = ("succeeded", "failed", "cancelled")
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    params TEXT NOT NULL,
    stages TEXT NOT NULL,
    created TEXT NOT NULL,
    started TEXT,
    ended TEXT,
    pid INTEGER,
    error TEXT,
    result TEXT,
    cancel INTEGER NOT NULL DEFAULT 0
)
"""


def now() -> str:
    """UTC timestamp, second resolution."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Job:
    """One row of the job table."""

    id: str
    kind: str
    status: str
    params: dict[str, Any]
    stages: list[dict[str, Any]]
    created: str
    started: str | None
    ended: str | None
    pid: int | None
    error: str | None
    result: dict[str, Any] | None
    cancel: bool

    def to_json(self) -> dict[str, Any]:
        """Plain dict for the API."""
        return self.__dict__ | {"active": self.status in ACTIVE}


class JobStore:
    """The job table (``DATA_DIR/jobs/jobs.sqlite``) and the per-job folders beside it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = root / "jobs.sqlite"
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute(SCHEMA)

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.db, timeout=30)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def dir(self, job_id: str) -> Path:
        """Folder of a job (logs, outputs, settings snapshot)."""
        return self.root / job_id

    def create(
        self, kind: str, params: dict[str, Any], stages: list[tuple[str, str]], cfg: Settings
    ) -> Job:
        """Insert a queued job and write its settings snapshot."""
        job_id = datetime.now(UTC).strftime(f"{kind[0]}%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        d = self.dir(job_id)
        d.mkdir(parents=True)
        (d / "settings.json").write_text(cfg.to_json())
        st = [
            {
                "name": n,
                "label": label,
                "status": "pending",
                "frac": None,
                "msg": "",
                "started": None,
                "ended": None,
            }
            for n, label in stages
        ]
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO jobs (id, kind, status, params, stages, created) "
                "VALUES (?, ?, 'queued', ?, ?, ?)",
                (job_id, kind, json.dumps(params), json.dumps(st), now()),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> Job:
        """One job; ``KeyError`` if unknown."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job(row)

    def recent(self, kind: str | None = None, limit: int = 50) -> list[Job]:
        """Newest first."""
        with self._conn() as c:
            if kind:
                rows = c.execute(
                    "SELECT * FROM jobs WHERE kind = ? ORDER BY created DESC, id DESC LIMIT ?",
                    (kind, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM jobs ORDER BY created DESC, id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._job(r) for r in rows]

    def active(self, kind: str | None = None) -> list[Job]:
        """Jobs that are queued, running or cancelling."""
        return [j for j in self.recent(kind, 500) if j.status in ACTIVE]

    def update(self, job_id: str, **fields: Any) -> None:
        """Set columns (dicts and lists are stored as JSON)."""
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k} = ?")
            vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id = ?", (*vals, job_id))

    def stage(self, job_id: str, name: str, **fields: Any) -> None:
        """Update one stage's status, fraction or message."""
        with self._lock, self._conn() as c:
            row = c.execute("SELECT stages FROM jobs WHERE id = ?", (job_id,)).fetchone()
            stages = json.loads(row[0])
            for s in stages:
                if s["name"] == name:
                    s.update(fields)
            c.execute("UPDATE jobs SET stages = ? WHERE id = ?", (json.dumps(stages), job_id))

    @staticmethod
    def _job(row: tuple[Any, ...]) -> Job:
        (id_, kind, status, params, stages, created, started, ended, pid, error, result, cancel) = (
            row
        )
        return Job(
            id_,
            kind,
            status,
            json.loads(params),
            json.loads(stages),
            created,
            started,
            ended,
            pid,
            error,
            json.loads(result) if result else None,
            bool(cancel),
        )


# --------------------------------------------------------------------------- supervisor


def pid_alive(pid: int | None, job_id: str) -> bool:
    """Whether ``pid`` is still this job's worker (guards against reused pids)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        cmdline = (
            Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        )
    except OSError:
        return True  # no /proc (macOS): trust the pid
    if "<defunct>" in cmdline or not cmdline:
        return False
    return job_id in cmdline


class Supervisor:
    """Starts, cancels, resumes and recovers job workers."""

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self.recover()

    def start(self, job_id: str) -> Job:
        """Launch the worker for a queued job."""
        d = self.store.dir(job_id)
        log = (d / "worker.log").open("ab")
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "senseai_viewer.runner.jobs",
                "work",
                "--jobs",
                str(self.store.root),
                job_id,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            cwd=d,
        )
        self._procs[job_id] = proc
        self.store.update(
            job_id, pid=proc.pid, status="running", started=now(), ended=None, error=None, cancel=0
        )
        threading.Thread(target=self._reap, args=(job_id, proc), daemon=True).start()
        return self.store.get(job_id)

    def _reap(self, job_id: str, proc: subprocess.Popen[bytes]) -> None:
        code = proc.wait()
        self._procs.pop(job_id, None)
        job = self.store.get(job_id)
        if job.status in ACTIVE:  # the worker died without recording an outcome
            status = "cancelled" if job.cancel else "failed"
            self.store.update(
                job_id,
                status=status,
                ended=now(),
                error=job.error
                or (
                    None
                    if job.cancel
                    else f"the worker stopped unexpectedly (exit code {code}); see worker.log"
                ),
            )

    def cancel(self, job_id: str) -> Job:
        """Ask the worker to stop; it ends the current step's processes and records 'cancelled'."""
        job = self.store.get(job_id)
        if job.status not in ACTIVE:
            return job
        self.store.update(job_id, cancel=1, status="cancelling")
        if job.pid and pid_alive(job.pid, job_id):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(job.pid, signal.SIGTERM)

            def kill_later(pid: int = job.pid) -> None:
                for _ in range(100):
                    if not pid_alive(pid, job_id):
                        return
                    time.sleep(0.1)
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, signal.SIGKILL)

            threading.Thread(target=kill_later, daemon=True).start()
        else:
            self.store.update(job_id, status="cancelled", ended=now())
        return self.store.get(job_id)

    def resume(self, job_id: str) -> Job:
        """Run a failed or cancelled job again; finished stages are skipped."""
        job = self.store.get(job_id)
        if job.status not in ("failed", "cancelled"):
            raise ValueError(f"job {job_id} is {job.status}; only failed or cancelled jobs resume")
        self.store.update(job_id, status="queued", cancel=0, error=None)
        return self.start(job_id)

    def wait(self, job_id: str, timeout: float = 60) -> Job:
        """Block until the job is finished (tests)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            job = self.store.get(job_id)
            if job.status in FINISHED and job_id not in self._procs:
                return job
            time.sleep(0.05)
        raise TimeoutError(f"job {job_id} still {self.store.get(job_id).status}")

    def recover(self) -> None:
        """After a viewer restart: keep live workers, mark dead ones failed (they can Resume)."""
        for job in self.store.active():
            if job.pid and pid_alive(job.pid, job.id):
                continue
            status = "cancelled" if job.cancel else "failed"
            self.store.update(
                job.id,
                status=status,
                ended=now(),
                error=None
                if job.cancel
                else "the worker stopped while the viewer was down; press Resume",
            )
            for s in job.stages:
                if s["status"] == "running":
                    self.store.stage(
                        job.id, s["name"], status="failed" if status == "failed" else "cancelled"
                    )


# --------------------------------------------------------------------------- worker


class Cancelled(Exception):
    """The user pressed Cancel."""


def work(jobs_root: Path, job_id: str) -> int:
    """Worker entry point: run one job to the end, recording progress in the table."""
    from senseai_viewer.runner import setup, stages

    store = JobStore(jobs_root)
    job = store.get(job_id)
    cfg = Settings.from_json((store.dir(job_id) / "settings.json").read_text())
    stop = {"flag": False}

    def on_term(_sig: int, _frm: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, on_term)

    def cancelled() -> bool:
        if stop["flag"]:
            return True
        try:
            return store.get(job_id).cancel
        except Exception:
            return False

    store.update(job_id, status="running", pid=os.getpid(), started=job.started or now())
    try:
        if job.kind == "poc":
            result = stages.run_poc(cfg, store, job, cancelled)
        elif job.kind == "prepare":
            result = stages.run_prepare(cfg, store, job, cancelled)
        elif job.kind == "setup":
            result = setup.run_install(cfg, store, job, cancelled)
        else:
            raise ValueError(f"unknown job kind {job.kind!r}")
        final = "cancelled" if cancelled() else "succeeded"
        store.update(job_id, status=final, ended=now(), result=result)
        return 0
    except Cancelled:
        store.update(job_id, status="cancelled", ended=now())
        return 0
    except Exception as e:
        store.update(job_id, status="failed", ended=now(), error=f"{e}")
        print(f"job {job_id} failed: {type(e).__name__}: {e}", flush=True)
        return 1


def main(argv: list[str] | None = None) -> int:
    """``python -m senseai_viewer.runner.jobs work --jobs DIR JOB_ID``."""
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("work")
    w.add_argument("--jobs", type=Path, required=True)
    w.add_argument("job_id")
    a = ap.parse_args(argv)
    return work(a.jobs, a.job_id)


if __name__ == "__main__":
    # Run the imported module, not this __main__ copy, so stages raise the Cancelled we catch.
    from senseai_viewer.runner import jobs as _jobs

    sys.exit(_jobs.main())
