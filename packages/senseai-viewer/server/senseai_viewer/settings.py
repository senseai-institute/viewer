# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Environment-driven settings. Every variable is documented in ``.env.example``."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """Server settings."""

    data_dir: Path
    upload_token: str | None
    max_upload_mb: int
    public_base_url: str | None
    demo: bool
    cache_mb: int

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
        )
