# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""A bench run folder as the viewer imports it, and its manifest in S3.

``senseai-bench replay-compare --runlog DIR`` writes one RunLog per sequence (viewer layout
v0) next to ``replay_report.{md,json}``; a run folder such as ``~/runs/zurich_smoke`` also
holds ingest ticks, tokens, the world checkpoint and logs. The viewer needs only:

* every RunLog (HDF5 files whose root attribute ``format`` is ``senseai/runlog``; ingest and
  tokens HDF5 files are skipped by that test),
* ``replay_report.md`` and ``replay_report.json`` (bench's own numbers for all arms),
* the world training log, if any (shown as text).

Files are copied byte for byte: the viewer reads the public contracts and never rewrites a
run. In S3 a run lives at ``<prefix><run>/`` with the files at their paths relative to the run
folder, plus ``viewer.json`` (this module's manifest), written last.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import h5py

MANIFEST = "viewer.json"
FORMAT = "senseai/viewer-run"
SKIP_DIRS = {"ticks", "tokens", ".git", "__pycache__"}  # big HDF5 that is never a RunLog
TRAINING_LOGS = ("train.log", "training.log", "train_log.txt", "log.txt")
NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class BundleError(ValueError):
    """The folder is not something the viewer can show; the message says what is missing."""


@dataclass
class RunFile:
    """One RunLog of the run."""

    rel: str  # path relative to the run folder (and to the S3 run prefix)
    run_id: str  # the id the viewer serves it under
    sequence: str
    size: int
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class Bundle:
    """What the viewer takes from a run folder."""

    root: Path
    name: str
    runlogs: list[RunFile]
    report_md: str | None = None
    report_json: str | None = None
    training: str | None = None

    def files(self) -> list[str]:
        """Every file to copy, relative to the root."""
        extra = [p for p in (self.report_md, self.report_json, self.training) if p]
        return [r.rel for r in self.runlogs] + extra


def safe_name(name: str) -> str:
    """Make a run id from a folder name: letters, digits, ``._-``, at most 100 characters."""
    out = NAME_RE.sub("-", name).strip("-.") or "run"
    return out[:100]


def is_runlog(p: Path) -> tuple[bool, str]:
    """Return ``(True, sequence)`` if ``p`` is a viewer RunLog (layout v0)."""
    try:
        with h5py.File(p, "r") as f:
            a = f.attrs
            fmt = a.get("format", b"")
            fmt = fmt.decode() if isinstance(fmt, bytes) else str(fmt)
            if fmt != "senseai/runlog":
                return False, ""
            if p.stem.startswith("runlog_"):  # bench: runlog_<sequence>.h5
                return True, p.stem.removeprefix("runlog_")
            seq = a.get("scenario", p.stem)
            return True, seq.decode() if isinstance(seq, bytes) else str(seq)
    except (OSError, ValueError):
        return False, ""


def _walk(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).parts
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel[:-1]):
            continue
        if p.is_file() and not p.is_symlink():
            out.append(p)
    return out


def collect(root: Path, name: str | None = None) -> Bundle:
    """Find the RunLogs, the report and the training log of a run folder."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise BundleError(f"{root} is not a folder")
    base = safe_name(name or root.name)
    files = _walk(root)
    logs: list[tuple[Path, str]] = []
    for p in files:
        if p.suffix in (".h5", ".hdf5"):
            ok, seq = is_runlog(p)
            if ok:
                logs.append((p, seq))
    if not logs:
        raise BundleError(
            f"no RunLog in {root}. Bench writes one only with --runlog: re-run the compare step "
            "with  --runlog <run>/compare/runlog  (and the same --run-id), then push again. "
            "Timelines (.npz) and the report alone do not hold the video, surprise or reflex "
            "commands the viewer shows."
        )
    seqs = [s for _, s in logs]
    runlogs = []
    for p, seq in logs:
        rid = base if len(logs) == 1 else safe_name(f"{base}-{seq}")
        if len(logs) > 1 and seqs.count(seq) > 1:
            rid = safe_name(f"{base}-{p.stem}")
        runlogs.append(RunFile(str(p.relative_to(root)), rid, seq, p.stat().st_size))

    def first(names: tuple[str, ...]) -> str | None:
        hits = [p for p in files if p.name in names]
        hits.sort(key=lambda p: (len(p.relative_to(root).parts), str(p)))
        return str(hits[0].relative_to(root)) if hits else None

    training = None
    world = root / "world"
    if world.is_dir():
        cands = [p for p in files if world in p.parents and p.name in TRAINING_LOGS]
        training = str(cands[0].relative_to(root)) if cands else None
    return Bundle(
        root,
        base,
        runlogs,
        report_md=first(("replay_report.md",)),
        report_json=first(("replay_report.json",)),
        training=training,
    )


def summarise(bundle: Bundle) -> None:
    """Fill each RunLog's run-list row (the viewer's own index of the file)."""
    from senseai_viewer import index as index_mod
    from senseai_viewer.readers.runlog import RunLog

    for r in bundle.runlogs:
        with RunLog(bundle.root / r.rel) as log:
            idx = index_mod._clean(index_mod.build_index(log))
        r.summary = index_mod.list_row(idx) | {"run_id": r.run_id}


def manifest(bundle: Bundle) -> dict[str, Any]:
    """Build the ``viewer.json`` written beside the run's files in S3."""
    return {
        "format": FORMAT,
        "version": 1,
        "run": bundle.name,
        "pushed": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "runlogs": [
            {
                "key": r.rel,
                "run_id": r.run_id,
                "sequence": r.sequence,
                "size": r.size,
                "summary": r.summary,
            }
            for r in bundle.runlogs
        ],
        "report_md": bundle.report_md,
        "report_json": bundle.report_json,
        "training": bundle.training,
    }


def upload(
    bundle: Bundle,
    s3: Any,
    bucket: str,
    prefix: str,
    say: Callable[[str], None] = lambda _m: None,
) -> str:
    """Copy the bundle's files to ``s3://bucket/prefix<run>/``; the manifest goes last."""
    base = f"{prefix}{bundle.name}/"
    for rel in bundle.files():
        p = bundle.root / rel
        say(f"  {rel} ({p.stat().st_size / 1e6:.1f} MB)")
        s3.upload_file(str(p), bucket, base + rel)
    s3.put_object(
        Bucket=bucket,
        Key=base + MANIFEST,
        Body=json.dumps(manifest(bundle), indent=2).encode(),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{base}"
