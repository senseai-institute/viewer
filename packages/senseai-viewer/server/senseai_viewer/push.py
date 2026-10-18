# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""``senseai-viewer push``: put a bench run folder on the deployed viewer.

    senseai-viewer push --run ~/runs/zurich_smoke --to https://<viewer>.up.railway.app

1. finds the run's RunLogs, ``replay_report.{md,json}`` and the world training log;
2. checks every RunLog with the viewer's own validator;
3. copies them unchanged to ``s3://senseai-data/runs/<run>/`` (``--s3``; credentials from
   the machine, e.g. the EC2 instance role) with a ``viewer.json`` manifest, written last;
4. with ``--to``: asks the viewer to load it now when ``SENSEAI_VIEWER_TOKEN`` is set,
   otherwise waits until the viewer lists it (it rereads S3 every minute), and prints the
   Compare, Replay and share links.

``--direct`` skips S3 and uploads a tarball to ``POST /api/runs/import`` (needs the token);
the server stores it in S3 itself when it can.
"""

from __future__ import annotations

import json
import os
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from senseai_viewer import bundle as bundle_mod

DEFAULT_S3 = "s3://senseai-data/runs/"
TOKEN_ENV = "SENSEAI_VIEWER_TOKEN"


def _req(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    body_file: Path | None = None,
    timeout: float = 600,
) -> Any:
    hdrs = dict(headers or {})
    if body_file is not None:
        hdrs["Content-Length"] = str(body_file.stat().st_size)
        with body_file.open("rb") as fh:
            req = urllib.request.Request(url, data=fh, method=method, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"null")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def _err(e: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(e.read()).get("detail")
    except (ValueError, AttributeError):
        detail = None
    return f"HTTP {e.code}: {detail or e.reason}"


def push(
    run: Path, to: str | None, s3_uri: str, name: str | None, direct: bool, wait_s: float = 180
) -> int:
    """Run the command; returns the exit code."""
    to = to.rstrip("/") if to else None
    try:
        b = bundle_mod.collect(run, name)
    except bundle_mod.BundleError as e:
        print(f"error: {e}")
        return 2
    print(f"run {b.name} from {b.root}")
    for r in b.runlogs:
        print(f"  RunLog {r.rel}  ->  {r.run_id}  ({r.size / 1e6:.1f} MB, {r.sequence})")
    for label, rel in (
        ("report", b.report_md),
        ("report data", b.report_json),
        ("training log", b.training),
    ):
        print(f"  {label}: {rel or 'none'}")

    from senseai_viewer.validate import validate

    bad = False
    for r in b.runlogs:
        rep = validate(b.root / r.rel)
        why = {"events feed": "events feed (no Cortex events: replay-compare does not run Cortex)"}
        off = [why.get(k, k) for k, v in rep.panels.items() if v != "on"]
        print(
            f"  check {r.run_id}: {'OK' if rep.ok else 'FAILED'}"
            + (f"; not shown: {', '.join(off)}" if off else "")
        )
        if not rep.ok:
            print(rep.render())
            bad = True
    if bad:
        return 3
    bundle_mod.summarise(b)
    token = os.environ.get(TOKEN_ENV, "")

    if direct:
        if not (to and token):
            print(f"error: --direct needs --to and the {TOKEN_ENV} environment variable")
            return 2
        with tempfile.TemporaryDirectory() as tmp:
            tar = Path(tmp) / "run.tar"
            with tarfile.open(tar, "w") as tf:
                for rel in b.files():
                    tf.add(b.root / rel, arcname=f"{b.name}/{rel}")
            print(f"uploading {tar.stat().st_size / 1e6:.1f} MB to {to} ...")
            try:
                res = _req(
                    f"{to}/api/runs/import?name={b.name}",
                    method="POST",
                    body_file=tar,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/x-tar",
                    },
                )
            except urllib.error.HTTPError as e:
                print(f"error: {_err(e)}")
                return 4
        if res.get("note"):
            print(f"note: {res['note']}")
        _print_links(res["runs"])
        return 0

    from senseai_viewer.runner import s3 as s3mod

    try:
        bucket, prefix = s3mod.parse_uri(s3_uri)
        client = s3mod.client()
        print(f"copying to {s3_uri}{b.name}/ ...")
        stored = bundle_mod.upload(b, client, bucket, prefix, say=print)
    except Exception as e:
        msg = (
            s3mod._explain(e, f"write to {s3_uri}")
            if not isinstance(e, (s3mod.S3Unavailable, ValueError))
            else str(e)
        )
        print(f"error: {msg}")
        if "not allowed" in msg or "credentials" in msg:
            print("This machine's role needs, on top of reading the recordings:")
            print(
                json.dumps(
                    {
                        "Effect": "Allow",
                        "Action": ["s3:PutObject"],
                        "Resource": f"arn:aws:s3:::{bucket}/{prefix}*",
                    },
                    indent=2,
                )
            )
        return 4
    print(f"stored at {stored}")
    if not to:
        print("done (no --to: the viewer picks it up from S3 within a minute)")
        return 0
    ids = [r.run_id for r in b.runlogs]
    if token:
        try:
            res = _req(
                f"{to}/api/runs/import",
                method="POST",
                data=json.dumps({"s3": stored}).encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            _print_links(res["runs"])
            return 0
        except urllib.error.HTTPError as e:
            print(f"the viewer did not load it now ({_err(e)}); waiting for its next S3 refresh")
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        try:
            listed = {r["run_id"] for r in _req(f"{to}/api/runs", timeout=30)}
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"cannot reach {to}: {e}")
            return 5
        if set(ids) <= listed:
            _print_links(
                [
                    {
                        "run_id": i,
                        "compare": f"{to}/compare/{i}",
                        "replay": f"{to}/replay/{i}",
                        "share": f"{to}/s/{i}",
                    }
                    for i in ids
                ]
            )
            return 0
        time.sleep(10)
    print(
        f"{to} does not list the run yet. Is SENSEAI_RUNS_S3={s3_uri} set on the viewer, "
        f"with AWS read credentials? See {to}/api/health"
    )
    return 6


def _print_links(runs: list[dict[str, str]]) -> None:
    for r in runs:
        print(f"\n{r['run_id']}")
        print(f"  Compare  {r['compare']}")
        print(f"  Replay   {r['replay']}")
        print(f"  Share    {r['share']}")
