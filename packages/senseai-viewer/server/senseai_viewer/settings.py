# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Environment-driven settings. Every variable is documented in ``.env.example``."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _path(name: str) -> Path | None:
    v = os.environ.get(name)
    return Path(v) if v else None


@dataclass(frozen=True)
class Settings:
    """Server settings."""

    data_dir: Path
    upload_token: str | None
    max_upload_mb: int
    public_base_url: str | None
    demo: bool
    cache_mb: int
    viewer_password: str | None = None
    runner: bool = False
    data_root: Path | None = None
    world_ckpt: Path | None = None
    tokens_yaml: Path | None = None
    sdk_config: Path | None = None
    bench_cmd: str = "senseai-bench"
    bench_python: str | None = None
    s3_uri: str = "s3://senseai-data/raw/dsec/"
    stack_dir: Path | None = None
    panther_tokens_profile: Path | None = None
    setup_script: Path | None = None
    research_url: str | None = None

    @property
    def dsec_root(self) -> Path:
        """Where recordings are prepared (``SENSEAI_DATA_ROOT``, default ``/data/dsec``)."""
        return self.data_root or Path("/data/dsec")

    @property
    def stack(self) -> Path:
        """Folder holding the SenseAI checkouts side by side (``SENSEAI_STACK_DIR``)."""
        return self.stack_dir or Path.home() / "senseai"

    def to_json(self) -> str:
        """Serialise for the background worker, which cannot read the server's objects."""
        return json.dumps(
            {
                f.name: (str(v) if isinstance(v, Path) else v)
                for f in fields(self)
                for v in [getattr(self, f.name)]
            }
        )

    @classmethod
    def from_json(cls, text: str) -> Settings:
        """Inverse of :meth:`to_json`."""
        raw = json.loads(text)
        out: dict[str, Any] = {}
        for f in fields(cls):
            v = raw.get(f.name)
            is_path = "Path" in str(f.type)
            out[f.name] = Path(v) if (is_path and v is not None) else v
        return cls(**out)

    @classmethod
    def from_env(cls) -> Settings:
        """Read settings from the environment."""
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR", "./data")),
            upload_token=os.environ.get("UPLOAD_TOKEN") or None,
            max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB", "200")),
            public_base_url=os.environ.get("PUBLIC_BASE_URL") or None,
            demo=_flag("DEMO_RUN", True),
            cache_mb=int(os.environ.get("WINDOW_CACHE_MB", "64")),
            viewer_password=os.environ.get("VIEWER_PASSWORD") or None,
            runner=_flag("SENSEAI_RUNNER", False),
            data_root=_path("SENSEAI_DATA_ROOT"),
            world_ckpt=_path("SENSEAI_WORLD_CKPT"),
            tokens_yaml=_path("SENSEAI_TOKENS_YAML"),
            sdk_config=_path("SENSEAI_SDK_CONFIG"),
            bench_cmd=os.environ.get("SENSEAI_BENCH_CMD", "senseai-bench"),
            bench_python=os.environ.get("SENSEAI_BENCH_PYTHON") or None,
            s3_uri=os.environ.get("SENSEAI_S3_URI", "s3://senseai-data/raw/dsec/"),
            stack_dir=_path("SENSEAI_STACK_DIR"),
            panther_tokens_profile=_path("SENSEAI_PANTHER_TOKENS_PROFILE"),
            setup_script=_path("SENSEAI_SETUP_SCRIPT"),
            research_url=os.environ.get("RESEARCH_URL") or None,
        )
