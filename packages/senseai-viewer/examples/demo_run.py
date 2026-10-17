# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Build the synthetic Panther-vs-default run and get a share link for it.

  python examples/demo_run.py --local                      # write ./data/runs/, print the local URL
  python examples/demo_run.py --url https://… --token T    # upload to a deployed viewer

A deployed viewer with no runs builds this run itself on first start, so uploading is only
needed to replace it (e.g. a different --seed).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

from senseai_viewer import sample_data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--local", action="store_true", help="write into ./data/runs instead of uploading"
    )
    ap.add_argument("--url", help="base URL of a deployed viewer")
    ap.add_argument("--token", help="UPLOAD_TOKEN of that viewer")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--data", type=Path, default=Path("data"))
    args = ap.parse_args()
    if args.local or not args.url:
        runs = args.data / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        out = sample_data.build(runs / "synthetic-panther-vs-null.h5", seed=args.seed)
        print(f"wrote {out}")
        print("serve it:   uv run senseai-viewer serve")
        print("share link: http://127.0.0.1:8000/s/demo")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        path = sample_data.build(Path(tmp) / "demo.h5", seed=args.seed)
        req = urllib.request.Request(
            f"{args.url.rstrip('/')}/api/runs",
            data=path.read_bytes(),
            method="PUT",
            headers={
                "Authorization": f"Bearer {args.token or ''}",
                "Content-Type": "application/octet-stream",
            },
        )
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read())
    print(f"uploaded {body['run_id']} ({body['bytes'] / 1e6:.1f} MB)")
    print(f"share link: {body['share_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
