# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""The POC pipeline, one function per stage, each reporting progress and resumable.

    ① ingest    senseai-ingest replay        DSEC sequence -> ticks.h5           (train + test)
    ② tokenize  senseai-tokens run           ticks.h5 -> tokens.h5, null profile (+ panther_vision
                                             when a tokens-format profile is configured)
    ③ train     senseai-world train          tokens -> world/last.ckpt (resumes from it)
    ④ package   bench's SDK builder          checkpoint + tokens.yaml + calibration -> senseai.yaml
                                             and the release key the SDK must trust
    ⑤ compare   senseai-bench replay-compare test sequences, every chosen contestant -> report,
                                             live events and RunLogs

When no chosen contestant needs a trained model, ① to ④ are skipped. A stage that finished
writes ``stages/<name>.done``; Resume skips it. Every command and its output go to
``logs/<stage>.log`` in the job folder.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from senseai_viewer.arms import BY_NAME
from senseai_viewer.runner import prepare as prep
from senseai_viewer.runner import s3 as s3mod
from senseai_viewer.runner import tools
from senseai_viewer.runner.jobs import Cancelled, Job, JobStore, now
from senseai_viewer.settings import Settings

STAGES: list[tuple[str, str]] = [
    ("ingest", "Read recordings"),
    ("tokenize", "Tokenize"),
    ("train", "Train model"),
    ("package", "Package"),
    ("compare", "Compare"),
]
SPLIT_TRAIN = 0.7
WORLD_CONFIGS = {"small": "configs/fixture.yaml", "gate1": "configs/world.yaml"}
PACKAGE_HELPER = Path(__file__).with_name("package_helper.py")
STEP_RE = re.compile(r"\bstep[s]?\b[^0-9]{0,3}(\d+)", re.IGNORECASE)


class StageError(RuntimeError):
    """A stage failed; the message is shown on the page."""


class Ctx:
    """What a stage needs: settings, the job folder, progress reporting, cancel checks."""

    def __init__(
        self, cfg: Settings, store: JobStore, job: Job, cancelled: Callable[[], bool]
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.job = job
        self.dir = store.dir(job.id)
        self.cancelled = cancelled
        self.params = job.params
        self.stage = ""
        (self.dir / "logs").mkdir(exist_ok=True)
        (self.dir / "stages").mkdir(exist_ok=True)

    def report(self, frac: float | None = None, msg: str | None = None) -> None:
        """Record the current stage's progress (and its message)."""
        fields: dict[str, Any] = {}
        if frac is not None:
            fields["frac"] = round(max(0.0, min(1.0, frac)), 4)
        if msg is not None:
            fields["msg"] = msg[:300]
        if fields:
            self.store.stage(self.job.id, self.stage, **fields)

    def log(self, line: str) -> None:
        """Append a line to the current stage's log."""
        with (self.dir / "logs" / f"{self.stage}.log").open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")

    def check(self) -> None:
        """Raise :class:`Cancelled` if the user pressed Cancel."""
        if self.cancelled():
            raise Cancelled()

    def run(
        self,
        cmd: list[str],
        parse: Callable[[str], tuple[float | None, str | None] | None] | None = None,
        env: dict[str, str] | None = None,
        tick: Callable[[], None] | None = None,
    ) -> None:
        """Run one command in its own process group, streaming output to the stage log.

        ``parse`` turns output lines into progress; ``tick`` runs about twice a second (for
        progress read from files). Cancel stops the whole process group.
        """
        self.log(f"$ {shlex.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=self.dir,
                env={**os.environ, **(env or {})},
                start_new_session=True,
                text=True,
                bufsize=1,
            )
        except OSError as e:
            raise StageError(
                f"could not start {cmd[0]}: {e} (is the SenseAI stack installed? see Setup)"
            ) from e
        lines: queue.Queue[str | None] = queue.Queue()

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        tail: list[str] = []
        done = False
        while not done:
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                line = ""
            if line is None:
                done = True
            elif line:
                self.log(line)
                tail = [*tail, line.rstrip()][-12:]
                if parse:
                    got = parse(line)
                    if got:
                        self.report(*got)
            if tick:
                tick()
            if self.cancelled():
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=10)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                raise Cancelled()
        code = proc.wait()
        self.log(f"# exit code {code}")
        if code != 0:
            raise StageError(
                f"{Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''} exited with code {code}: "
                + " | ".join(tail[-4:])
            )


# --------------------------------------------------------------------------- plan


def needs_training(arms: list[str]) -> bool:
    """Whether any chosen contestant uses the trained world model."""
    return any(BY_NAME[a].needs == "checkpoint" for a in arms if a in BY_NAME)


def _sequences(cfg: Settings, names: list[str]) -> list[Path]:
    local = prep.find_local(cfg.dsec_root)
    out = []
    for n in names:
        if n not in local:
            raise StageError(f"recording {n} is not on this machine; press Fetch & prepare first")
        if not prep.is_ready(local[n]):
            raise StageError(f"recording {n} is not ready; press Fetch & prepare first")
        out.append(local[n])
    return out


def _duration(seq: Path) -> float:
    st = prep.read_state(seq) or {}
    d = st.get("duration_s")
    if not d:
        raise StageError(f"{seq.name}: unknown duration (prepare it again)")
    return float(d)


def plan(cfg: Settings, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve the run: which ticks files to make, what trains, what is compared."""
    train = _sequences(cfg, params.get("train") or [])
    test = _sequences(cfg, params.get("test") or [])
    split = bool(params.get("split"))
    ticks: list[dict[str, Any]] = []
    compare_start: float | None = None
    if split:
        seq = (train or test)[0]
        dur = _duration(seq)
        cut = round(dur * SPLIT_TRAIN, 2)
        ticks.append(
            {
                "seq": str(seq),
                "name": f"{seq.name}.train",
                "role": "train",
                "start": None,
                "duration": cut,
            }
        )
        ticks.append(
            {
                "seq": str(seq),
                "name": f"{seq.name}.test",
                "role": "test",
                "start": cut,
                "duration": None,
            }
        )
        test = [seq]
        compare_start = cut
    else:
        for s in train:
            ticks.append(
                {
                    "seq": str(s),
                    "name": f"{s.name}.train",
                    "role": "train",
                    "start": None,
                    "duration": None,
                }
            )
        for s in test:
            ticks.append(
                {
                    "seq": str(s),
                    "name": f"{s.name}.test",
                    "role": "test",
                    "start": None,
                    "duration": None,
                }
            )
    return {"ticks": ticks, "test": [str(s) for s in test], "compare_start": compare_start}


# --------------------------------------------------------------------------- stages


def _done(ctx: Ctx, name: str) -> bool:
    return (ctx.dir / "stages" / f"{name}.done").exists()


def _mark(ctx: Ctx, name: str, info: dict[str, Any] | None = None) -> None:
    (ctx.dir / "stages" / f"{name}.done").write_text(json.dumps(info or {}))


def stage_ingest(ctx: Ctx, p: dict[str, Any]) -> None:
    """① replay each sequence (or its train/test part) into an ingest tick file."""
    out = ctx.dir / "ticks"
    out.mkdir(exist_ok=True)
    items = p["ticks"]
    for i, t in enumerate(items):
        ctx.check()
        dest = out / f"{t['name']}.h5"
        if dest.exists() and (out / f"{t['name']}.ok").exists():
            continue
        ctx.report(i / len(items), f"reading {t['name']}")
        cmd = [
            *tools.stack_tool(ctx.cfg, "senseai-ingest"),
            "replay",
            "--dataset",
            "dsec",
            "--path",
            t["seq"],
            "--out",
            str(dest),
            "--stats",
        ]
        if t["start"]:
            cmd += ["--start-s", f"{t['start']:g}"]
        if t["duration"]:
            cmd += ["--duration-s", f"{t['duration']:g}"]
        ctx.run(cmd)
        if not dest.exists():
            raise StageError(f"senseai-ingest wrote no {dest.name}")
        (out / f"{t['name']}.ok").write_text(now())
    ctx.report(1.0, f"{len(items)} tick file(s)")


def _tokens_yaml(cfg: Settings) -> Path:
    p = cfg.tokens_yaml or (cfg.stack / "tokens" / "tokens.yaml")
    if not p.exists():
        raise StageError(
            f"no tokens config at {p} (set SENSEAI_TOKENS_YAML or install the stack: Setup)"
        )
    return p


def stage_tokenize(ctx: Ctx, p: dict[str, Any]) -> None:
    """② tokenize every tick file with the null profile (and panther_vision if configured)."""
    out = ctx.dir / "tokens"
    out.mkdir(exist_ok=True)
    cfg_yaml = _tokens_yaml(ctx.cfg)
    profiles: list[tuple[str, str]] = [("null", "null")]
    if ctx.cfg.panther_tokens_profile:
        profiles.append(("panther_vision", str(ctx.cfg.panther_tokens_profile)))
    else:
        ctx.log(
            "# panther_vision tokens skipped: no tokens-format profile "
            "(SENSEAI_PANTHER_TOKENS_PROFILE); "
            "the SDK applies the panther_vision profile at compare time"
        )
    jobs = [(t, tag, prof) for t in p["ticks"] for tag, prof in profiles]
    for i, (t, tag, prof) in enumerate(jobs):
        ctx.check()
        dest = out / f"{t['name']}.{tag}.h5"
        if dest.exists():
            continue
        ctx.report(i / len(jobs), f"tokenizing {t['name']} ({tag})")
        tmp = dest.with_suffix(".part.h5")
        ctx.run(
            [
                *tools.stack_tool(ctx.cfg, "senseai-tokens"),
                "run",
                "--ticks",
                str(ctx.dir / "ticks" / f"{t['name']}.h5"),
                "--out",
                str(tmp),
                "--config",
                str(cfg_yaml),
                "--profile",
                prof,
                "--stats",
            ]
        )
        if not tmp.exists():
            raise StageError(f"senseai-tokens wrote no {tmp.name}")
        tmp.replace(dest)
    ctx.report(1.0, f"{len(jobs)} tokens file(s)")


def stage_train(ctx: Ctx, p: dict[str, Any]) -> None:
    """③ train the world model on the train tokens (null profile); resumes from last.ckpt."""
    size = str(ctx.params.get("size", "small"))
    steps = int(ctx.params.get("steps") or 2000)
    world_cfg = ctx.cfg.stack / "world" / WORLD_CONFIGS.get(size, WORLD_CONFIGS["small"])
    if not world_cfg.exists():
        raise StageError(f"no world config at {world_cfg} (install the stack: Setup)")
    tok = ctx.dir / "tokens"
    train = [str(tok / f"{t['name']}.null.h5") for t in p["ticks"] if t["role"] == "train"]
    held = [str(tok / f"{t['name']}.null.h5") for t in p["ticks"] if t["role"] == "test"]
    out = ctx.dir / "world"

    def parse(line: str) -> tuple[float | None, str | None] | None:
        m = STEP_RE.search(line)
        if not m:
            return None
        k = int(m.group(1))
        return (k / steps if k <= steps else None, f"step {k:,} / {steps:,} · {line.strip()[:120]}")

    cmd = [
        *tools.stack_tool(ctx.cfg, "senseai-world"),
        "train",
        "--tokens",
        *train,
        "--config",
        str(world_cfg),
        "--out",
        str(out),
        "--steps",
        str(steps),
        "--device",
        "auto",
    ]
    if held:
        cmd += ["--heldout", *held]
    ctx.report(0.0, f"training ({size}, {steps:,} steps)")
    ctx.run(cmd, parse=parse)
    if not (out / "last.ckpt").exists():
        raise StageError("training finished without world/last.ckpt")
    ctx.report(1.0, f"checkpoint ready ({steps:,} steps)")


def stage_package(ctx: Ctx, p: dict[str, Any]) -> None:
    """④ Package the SDK: signed artefacts, senseai.yaml and the release key (bench's builder)."""
    py = tools.bench_python(ctx.cfg)
    if not py:
        raise StageError("cannot find the SenseAI stack's Python (install the stack: Setup)")
    work = ctx.dir / "package"
    calib = Path(p["test"][0]) / prep.CALIBRATION
    cmd = [
        py,
        str(PACKAGE_HELPER),
        "--work",
        str(work),
        "--world-ckpt",
        str(ctx.dir / "world" / "last.ckpt"),
        "--tokens-yaml",
        str(_tokens_yaml(ctx.cfg)),
        "--calibration",
        str(calib),
    ]
    profiles = ctx.cfg.stack / "profiles"
    if profiles.is_dir():
        cmd += ["--profiles-repo", str(profiles)]
    ctx.report(0.1, "building the SDK package")
    ctx.run(cmd)
    info_path = work / "package.json"
    if not info_path.exists():
        raise StageError("the package step wrote no package.json")
    info = json.loads(info_path.read_text())
    (ctx.dir / "env.json").write_text(
        json.dumps({"SENSEAI_RELEASE_KEYS": info.get("release_key", "")})
    )
    for n in info.get("notes", []):
        ctx.log(f"# note: {n}")
    ctx.report(
        1.0,
        "SDK package ready"
        + (f" · {len(info.get('notes', []))} note(s)" if info.get("notes") else ""),
    )


def stage_compare(ctx: Ctx, p: dict[str, Any], trained: bool) -> dict[str, Any]:
    """⑤ run senseai-bench replay-compare on the test sequences with every chosen contestant."""
    arms = list(ctx.params["arms"])
    seqs = [Path(s) for s in p["test"]]
    events = ctx.dir / "bench_events.jsonl"
    events.unlink(missing_ok=True)  # a resumed compare starts its stream again
    env: dict[str, str] = {}
    sdk_config = ctx.cfg.sdk_config  # a ready-made config for compare-only runs, if given
    if trained:
        env.update(json.loads((ctx.dir / "env.json").read_text()))
        sdk_config = ctx.dir / "package" / "senseai.yaml"
    cmd = tools.compare_command(
        ctx.cfg,
        sequences=seqs,
        arms=arms,
        out_dir=ctx.dir / "report",
        events=events,
        runlog_dir=ctx.dir / "runlog",
        run_id=ctx.job.id,
        max_seconds=ctx.params.get("max_seconds"),
        start_seconds=p.get("compare_start"),
        sdk_config=sdk_config,
        calibration=None if sdk_config else seqs[0] / prep.CALIBRATION,
        profiles_repo=None
        if sdk_config or not (ctx.cfg.stack / "profiles").is_dir()
        else ctx.cfg.stack / "profiles",
    )
    total = max(1, len(seqs) * len(arms))
    state: dict[str, Any] = {"pos": 0, "done": 0, "frac": 0.0, "now": ""}

    def tick() -> None:
        try:
            with events.open("rb") as f:
                f.seek(state["pos"])
                chunk = f.read()
        except OSError:
            return
        if not chunk or not chunk.endswith(b"\n"):
            chunk = chunk[: chunk.rfind(b"\n") + 1] if b"\n" in chunk else b""
        state["pos"] += len(chunk)
        for raw in chunk.splitlines():
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "arm_start":
                state["frac"], state["now"] = 0.0, f"{ev.get('arm')} on {ev.get('sequence')}"
            elif kind == "progress":
                state["frac"] = float(ev.get("frac") or 0)
            elif kind == "arm_end":
                state["done"] += 1
                state["frac"] = 0.0
        ctx.report(
            (state["done"] + state["frac"]) / total,
            f"comparing: {state['now']}" if state["now"] else "starting the comparison",
        )

    ctx.report(0.0, "starting the comparison")
    ctx.run(cmd, env=env, tick=tick)
    tick()
    evs = tools.read_events(events)
    end = next((e for e in reversed(evs) if e.get("type") == "run_end"), None)
    if end is None:
        raise StageError("senseai-bench finished without a run_end event")
    if end.get("error"):
        raise StageError(f"senseai-bench: {end['error']}")
    summaries = [
        {
            "arm": e.get("arm"),
            "sequence": e.get("sequence"),
            "error": e.get("error"),
            **(e.get("summary") or {}),
        }
        for e in evs
        if e.get("type") == "arm_end"
    ]
    runlogs = (
        sorted(str(x) for x in (ctx.dir / "runlog").rglob("*.h5"))
        if (ctx.dir / "runlog").is_dir()
        else []
    )
    report_md = ctx.dir / "report" / "replay_report.md"
    report_json = ctx.dir / "report" / "replay_report.json"
    return {
        "headline": end.get("headline") or "",
        "summaries": summaries,
        "runlogs": runlogs,
        "report_md": str(report_md) if report_md.exists() else end.get("report_md"),
        "report_json": str(report_json) if report_json.exists() else end.get("report_json"),
        "trained": trained,
    }


def run_poc(
    cfg: Settings, store: JobStore, job: Job, cancelled: Callable[[], bool]
) -> dict[str, Any]:
    """Run the whole chain for a POC job; returns the result shown on the page."""
    ctx = Ctx(cfg, store, job, cancelled)
    p = plan(cfg, job.params)
    trained = needs_training(list(job.params["arms"]))
    result: dict[str, Any] = {}
    for name, _label in STAGES:
        ctx.stage = name
        if name != "compare" and not trained:
            store.stage(
                job.id,
                name,
                status="skipped",
                msg="not needed: no chosen contestant uses a trained model",
            )
            continue
        if _done(ctx, name):
            store.stage(job.id, name, status="done")
            continue
        store.stage(job.id, name, status="running", started=now(), ended=None, frac=0.0, msg="")
        t0 = time.monotonic()
        try:
            if name == "ingest":
                stage_ingest(ctx, p)
            elif name == "tokenize":
                stage_tokenize(ctx, p)
            elif name == "train":
                stage_train(ctx, p)
            elif name == "package":
                stage_package(ctx, p)
            else:
                result = stage_compare(ctx, p, trained)
        except Cancelled:
            store.stage(job.id, name, status="cancelled", ended=now())
            raise
        except Exception as e:
            store.stage(job.id, name, status="failed", ended=now(), msg=str(e)[:300])
            raise
        _mark(ctx, name, {"seconds": round(time.monotonic() - t0, 1)})
        store.stage(job.id, name, status="done", ended=now(), frac=1.0)
    return result


def run_prepare(
    cfg: Settings, store: JobStore, job: Job, cancelled: Callable[[], bool]
) -> dict[str, Any]:
    """Run a prepare job: fetch (if in S3) and prepare one sequence."""
    name = str(job.params["sequence"])
    seq = prep.find_local(cfg.dsec_root).get(name, cfg.dsec_root / name)
    objects: list[s3mod.S3Object] = []
    shared: list[s3mod.S3Object] = []
    client = None
    bucket = ""
    if job.params.get("from_s3"):
        bucket, prefix = s3mod.parse_uri(cfg.s3_uri)
        client = s3mod.client()
        per, shared = s3mod.group(s3mod.list_objects(client, bucket, prefix), prefix)
        objects = per.get(name, [])

    def progress(status: str, frac: float | None, msg: str) -> None:
        fields: dict[str, Any] = {"msg": f"{status}: {msg}"}
        if frac is not None:
            fields["frac"] = round(frac, 4)
        store.stage(job.id, "prepare", **fields)

    store.stage(job.id, "prepare", status="running", started=now())
    st = prep.prepare(
        seq,
        objects=objects,
        shared=shared,
        s3=client,
        bucket=bucket,
        progress=progress,
        cancelled=cancelled,
    )
    status = st.get("status")
    store.stage(
        job.id,
        "prepare",
        status="done" if status == "ready" else ("cancelled" if cancelled() else "failed"),
        ended=now(),
    )
    if cancelled():
        raise Cancelled()
    if status == "failed":
        raise StageError(st.get("error") or "preparing failed")
    return {
        "sequence": name,
        "status": status,
        "problems": st.get("problems", []),
        "warnings": st.get("warnings", []),
    }
