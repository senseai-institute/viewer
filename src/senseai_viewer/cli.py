# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Command line: ``senseai-viewer export`` and ``senseai-viewer serve``."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Sequence

__all__ = ["main"]


def _export(args: argparse.Namespace) -> int:
    from senseai_viewer.export.exporter import ExportError, export_tick_file

    t = time.monotonic()
    try:
        res = export_tick_file(args.tick_file, args.out, args.id)
    except (ExportError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    size = sum((res.out_dir / f).stat().st_size for f in res.files)
    m = res.manifest
    print(
        f"{res.recording_id}: {m['n_ticks']} ticks, {len(m['sources'])} sources, "
        f"{len(res.files)} files, {size / 1e6:.2f} MB -> {res.out_dir} "
        f"({time.monotonic() - t:.1f} s)"
    )
    if args.upload:
        from senseai_viewer.export.upload import upload_summary

        bucket = os.environ.get("S3_BUCKET")
        if not bucket:
            print("error: --upload needs S3_BUCKET (and AWS credentials)", file=sys.stderr)
            return 2
        prefix = os.environ.get("S3_PREFIX", "viewer")
        keys = upload_summary(
            res.out_dir, bucket=bucket, prefix=prefix, log=print if args.verbose else None
        )
        print(f"uploaded {len(keys)} objects to s3://{bucket}/{prefix}/{res.recording_id}/")
    return 0


def _serve(args: argparse.Namespace) -> int:  # pragma: no cover - thin wrapper around uvicorn
    import uvicorn

    if args.summaries:
        os.environ["LOCAL_SUMMARIES"] = args.summaries
    uvicorn.run(
        "senseai_viewer.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="senseai-viewer", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="turn one tick file into a viewer summary")
    e.add_argument("tick_file", help="tick file written by `senseai-ingest replay --out`")
    e.add_argument("--id", help="recording id (default: derived from the file name)")
    e.add_argument("--out", default="out", help="local output root (default: ./out)")
    e.add_argument(
        "--upload",
        action="store_true",
        help="also upload to s3://$S3_BUCKET/$S3_PREFIX/<id>/ (S3_PREFIX defaults to viewer)",
    )
    e.add_argument("-v", "--verbose", action="store_true", help="print every uploaded key")
    e.set_defaults(func=_export)

    s = sub.add_parser("serve", help="run the web app (reads S3, or LOCAL_SUMMARIES)")
    s.add_argument("--summaries", help="serve this local folder instead of S3")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    s.set_defaults(func=_serve)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``senseai-viewer`` console script."""
    args = _parser().parse_args(argv)
    rc: int = args.func(args)
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
