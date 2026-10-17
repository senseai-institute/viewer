# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Run by the SenseAI stack's own Python (not the viewer's): package the SDK for a run.

Calls senseai-bench's ``build_sdk_config`` (signed artefacts with a development key, the
trained world checkpoint, tokens config and DSEC calibration), then writes:

* ``<work>/senseai.yaml``: the SDK config bench's ``--sdk-config`` reads;
* ``<work>/package.json``: notes, whether the world is trained, and ``release_key``, the
  public key the SDK must trust (exported as ``SENSEAI_RELEASE_KEYS`` for the compare step).

Usage: python package_helper.py --work DIR --world-ckpt CKPT --tokens-yaml YAML
       [--calibration cam_to_cam.yaml] [--profiles-repo DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    """Entry point."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--world-ckpt", type=Path, required=True)
    ap.add_argument("--tokens-yaml", type=Path, required=True)
    ap.add_argument("--calibration", type=Path)
    ap.add_argument("--profiles-repo", type=Path)
    a = ap.parse_args()
    import yaml
    from senseai_bench.replay.sdkconfig import build_sdk_config  # type: ignore[import-not-found]

    b = build_sdk_config(
        a.work,
        world_ckpt=a.world_ckpt,
        tokens_yaml=a.tokens_yaml,
        calibration=a.calibration if a.calibration and a.calibration.exists() else None,
        profiles_repo=a.profiles_repo,
        endpoint_id="viewer-poc",
    )
    a.work.mkdir(parents=True, exist_ok=True)
    (a.work / "senseai.yaml").write_text(yaml.safe_dump(b.config, sort_keys=False))
    key = ""
    pub = getattr(b.keyring, "public_hex", None)
    if callable(pub):
        keys = pub()
        key = ",".join(keys) if isinstance(keys, (list, tuple)) else str(keys)
    info = {
        "release_key": key,
        "notes": list(b.notes),
        "untrained_world": bool(b.untrained_world),
        "event_id": b.event_id,
        "frame_id": b.frame_id,
    }
    (a.work / "package.json").write_text(json.dumps(info, indent=2))
    print(f"packaged SDK config at {a.work / 'senseai.yaml'} ({len(b.notes)} note(s))", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
