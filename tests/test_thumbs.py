# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import numpy as np
import pytest

from senseai_viewer.export.thumbs import evenly_spaced, events_to_image, frame_to_image


def test_evenly_spaced() -> None:
    assert evenly_spaced(0, 60) == []
    assert evenly_spaced(3, 60) == [0, 1, 2]
    assert evenly_spaced(120, 60) == list(range(0, 120, 2))
    assert len(evenly_spaced(1000, 60)) == 60


@pytest.mark.parametrize(
    ("shape", "dtype", "encoding", "mode"),
    [
        ((10, 20, 3), np.uint8, "rgb8", "RGB"),
        ((10, 20, 3), np.uint8, None, "RGB"),
        ((10, 20, 3), np.uint8, "bgr8", "RGB"),
        ((10, 20, 1), np.uint8, "bayer_rggb8", "L"),
        ((10, 20, 1), np.uint16, "mono16", "L"),
        ((10, 20), np.uint8, "mono8", "L"),
        ((10, 20, 4), np.uint8, "weird", "L"),
        ((10, 20, 1), np.float32, None, "L"),
    ],
)
def test_frame_to_image(
    shape: tuple[int, ...], dtype: type, encoding: str | None, mode: str
) -> None:
    img = frame_to_image(np.full(shape, 200, dtype), encoding)
    assert img.mode == mode
    assert img.size == (20, 10)


def test_frame_bgr_swaps_channels_and_downscales() -> None:
    a = np.zeros((480, 640, 3), np.uint8)
    a[..., 0] = 255  # blue in BGR
    img = frame_to_image(a, "bgr8")
    assert img.size == (320, 240)
    assert img.getpixel((0, 0)) == (0, 0, 255)
    assert frame_to_image(np.full((2, 2, 1), 0xABCD, np.uint16), "mono16").getpixel((0, 0)) == 0xAB


def test_events_last_event_wins() -> None:
    ev = {
        "x": np.array([0, 0, 3], np.uint16),
        "y": np.array([0, 0, 1], np.uint16),
        "p": np.array([1, 0, 1], np.uint8),
    }
    a = np.asarray(events_to_image(ev, 4, 2))
    assert a[0, 0] == 0  # ON then OFF -> OFF
    assert a[1, 3] == 255
    assert a[0, 1] == 128
    assert events_to_image({}, None, None).size == (1, 1)
