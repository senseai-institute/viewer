# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Constants and helpers shared by the exporter and the web app (the summary contract)."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

import numpy as np

__all__ = [
    "EXPORT_FORMAT",
    "EXPORT_VERSION",
    "MAX_THUMBS",
    "MAX_TRACE_POINTS",
    "PRIVATE_META_KEYS",
    "RECORDING_ID_RE",
    "SOURCE_ID_RE",
    "SUPPORTED_VERSIONS",
    "THUMB_MAX_WIDTH",
    "derive_recording_id",
    "dumps_json",
    "is_valid_recording_id",
    "is_valid_source_id",
    "to_jsonable",
]

EXPORT_FORMAT = "senseai-viewer/summary"
#: Summary format version. Bump on breaking changes (see docs/summary-format.md, "Versioning").
EXPORT_VERSION = 1
#: Versions the web app can display.
SUPPORTED_VERSIONS = frozenset({1})

MAX_TRACE_POINTS = 5000
MAX_THUMBS = 60
THUMB_MAX_WIDTH = 320
JPEG_QUALITY = 80

#: Spec ``meta`` keys that hold paths on the replay computer; never exported.
PRIVATE_META_KEYS = frozenset({"file", "dir", "path", "root"})

RECORDING_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def is_valid_recording_id(rid: str) -> bool:
    """Return whether ``rid`` is a valid recording id."""
    return bool(RECORDING_ID_RE.match(rid))


def is_valid_source_id(sid: str) -> bool:
    """Return whether ``sid`` is usable as a source id (and therefore as a file name)."""
    return bool(SOURCE_ID_RE.match(sid)) and sid not in (".", "..")


def derive_recording_id(name: str) -> str:
    """Derive a recording id from a file name: lowercase, other characters become ``-``.

    >>> derive_recording_id("zurich_city_04_a.h5")
    'zurich_city_04_a'
    >>> derive_recording_id("My Run (2).h5")
    'my-run--2'
    """
    stem = name.rsplit("/", 1)[-1]
    for suffix in (".h5", ".hdf5"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    rid = re.sub(r"[^a-z0-9._-]", "-", stem.lower())
    rid = re.sub(r"^[^a-z0-9]+", "", rid).rstrip("-")[:128]
    return rid or "recording"


def to_jsonable(obj: Any) -> Any:
    """Convert ``obj`` into plain JSON types: NaN/inf → ``None``, numpy → Python, bytes → hex."""
    if isinstance(obj, Mapping):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return to_jsonable(obj.tolist())
    if isinstance(obj, np.generic):
        return to_jsonable(obj.item())
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, bytes | bytearray):
        return bytes(obj).hex()
    if obj is None or isinstance(obj, bool | int | str):
        return obj
    return str(obj)


def dumps_json(obj: Any) -> bytes:
    """Serialize ``obj`` deterministically (sorted keys, 2-space indent, trailing newline)."""
    text = json.dumps(to_jsonable(obj), indent=2, sort_keys=True, allow_nan=False)
    return (text + "\n").encode("utf-8")
