# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""``senseai-viewer`` command line: serve, sample, validate, index."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(prog="senseai-viewer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    s.add_argument("--data", type=Path, help="data directory (default $DATA_DIR or ./data)")
    g = sub.add_parser("sample", help="write the synthetic Panther-vs-default run")
    g.add_argument("out", type=Path)
    g.add_argument("--seed", type=int, default=7)
    v = sub.add_parser("validate", help="check a RunLog and report which panels light up")
    v.add_argument("files", type=Path, nargs="+")
    i = sub.add_parser("index", help="print a RunLog's index as JSON")
    i.add_argument("file", type=Path)
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        if args.data:
            os.environ["DATA_DIR"] = str(args.data)
        uvicorn.run("senseai_viewer.app:create_app", factory=True, host=args.host, port=args.port)
        return 0
    if args.cmd == "sample":
        from senseai_viewer import sample_data

        out = sample_data.build(args.out, seed=args.seed)
        print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
        return 0
    if args.cmd == "validate":
        from senseai_viewer.validate import validate

        reports = [validate(f) for f in args.files]
        print("\n".join(r.render() for r in reports))
        return 0 if all(r.ok for r in reports) else 1
    from senseai_viewer.index import build_index
    from senseai_viewer.readers.runlog import RunLog

    with RunLog(args.file) as log:
        print(json.dumps(build_index(log), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
