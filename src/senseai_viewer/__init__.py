# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Browse senseai-ingest recordings: tick-file export to summaries, and a small web viewer."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("senseai-viewer")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0"

__all__ = ["__version__"]
