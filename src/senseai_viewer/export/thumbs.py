# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Thumbnail rendering for FRAME and EVENT sources."""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import Any

import numpy as np
from PIL import Image

from senseai_viewer.export.format import JPEG_QUALITY, THUMB_MAX_WIDTH

__all__ = ["encode_jpeg", "evenly_spaced", "events_to_image", "frame_to_image"]

Array = np.ndarray[Any, Any]


def evenly_spaced(n_total: int, n_max: int) -> list[int]:
    """Return ``min(n_total, n_max)`` indices ``⌊i·N/n⌋``, evenly spread over ``range(N)``."""
    n = min(n_total, n_max)
    return [(i * n_total) // n for i in range(n)]


def _fit_width(w: int, h: int) -> tuple[int, int]:
    if w <= THUMB_MAX_WIDTH:
        return w, h
    return THUMB_MAX_WIDTH, max(1, round(h * THUMB_MAX_WIDTH / w))


def frame_to_image(frame: Array, encoding: str | None) -> Image.Image:
    """Convert a FRAME payload ``(H, W, C)`` (or ``(H, W)``) into a thumbnail-sized image.

    ``rgb*`` stays RGB, ``bgr*`` is swapped to RGB, 3-channel frames without an encoding are
    taken as RGB, and everything else (mono, Bayer, unknown) is shown as grayscale from the
    first channel. uint16 data is shifted right by 8 bits.
    """
    a = np.asarray(frame)
    if a.dtype == np.uint16:
        a = (a >> 8).astype(np.uint8)
    elif a.dtype != np.uint8:
        a = np.clip(a, 0, 255).astype(np.uint8)
    enc = (encoding or "").lower()
    if a.ndim == 3 and a.shape[2] >= 3 and (enc.startswith("rgb") or (not enc and a.shape[2] == 3)):
        img = Image.fromarray(np.ascontiguousarray(a[:, :, :3]), "RGB")
    elif a.ndim == 3 and a.shape[2] >= 3 and enc.startswith("bgr"):
        img = Image.fromarray(np.ascontiguousarray(a[:, :, 2::-1]), "RGB")
    else:
        gray = a[:, :, 0] if a.ndim == 3 else a.reshape(a.shape[0], -1)
        img = Image.fromarray(np.ascontiguousarray(gray), "L")
    size = _fit_width(img.width, img.height)
    if size != img.size:
        img = img.resize(size, Image.Resampling.BILINEAR)
    return img


def events_to_image(
    events: Mapping[str, Array], width: int | None, height: int | None
) -> Image.Image:
    """Accumulate events into a grey image: last event per pixel wins, ON = white, OFF = black.

    The image is ``width x height`` scaled down to at most 320 px wide. Without a sensor size,
    the maximum ``x + 1`` / ``y + 1`` of the events is used.
    """
    x = np.asarray(events.get("x", np.zeros(0)), np.int64)
    y = np.asarray(events.get("y", np.zeros(0)), np.int64)
    p = np.asarray(events.get("p", np.zeros(0)), np.uint8)
    w = int(width) if width else (int(np.max(x)) + 1 if len(x) else 1)
    h = int(height) if height else (int(np.max(y)) + 1 if len(y) else 1)
    tw, th = _fit_width(w, h)
    img = np.full((th, tw), 128, np.uint8)
    if len(x):
        xs = np.clip(x * tw // w, 0, tw - 1)
        ys = np.clip(y * th // h, 0, th - 1)
        lin = ys * tw + xs
        # Events are time-ordered, so the last occurrence of each pixel is the latest event.
        _, first_in_rev = np.unique(lin[::-1], return_index=True)
        last = len(lin) - 1 - first_in_rev
        img.reshape(-1)[lin[last]] = np.where(p[last] > 0, 255, 0).astype(np.uint8)
    return Image.fromarray(img, "L")


def encode_jpeg(img: Image.Image) -> bytes:
    """Encode ``img`` as a baseline JPEG without EXIF."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=False, progressive=False)
    return buf.getvalue()
