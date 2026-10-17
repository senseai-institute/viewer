# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Mean brightness of a PNG frame, without Pillow.

Used to sort DSEC sequences darkest first on the Run page. Handles what DSEC ships
(8-bit grey, RGB or RGBA, non-interlaced). Rows with linear filters (None, Sub, Up) are
unfiltered with numpy; Average and Paeth rows fall back to a per-byte loop, so a full
1440 x 1080 frame takes about a second. Callers cache the result.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

_SIG = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 6: 4}


class PngError(ValueError):
    """Not a PNG this reader handles."""


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode(data: bytes) -> np.ndarray:
    """Decode to a ``uint8`` array ``[H, W, C]``."""
    if not data.startswith(_SIG):
        raise PngError("not a PNG")
    pos, idat = 8, bytearray()
    width = height = depth = ctype = interlace = -1
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if depth != 8 or ctype not in _CHANNELS or interlace != 0:
        raise PngError(
            f"unsupported PNG (depth {depth}, colour type {ctype}, interlace {interlace})"
        )
    bpp = _CHANNELS[ctype]
    stride = width * bpp
    raw = zlib.decompress(bytes(idat))
    if len(raw) != height * (stride + 1):
        raise PngError("truncated image data")
    out = np.zeros((height, stride), dtype=np.uint8)
    prior = np.zeros(stride, dtype=np.int32)
    for y in range(height):
        f = raw[y * (stride + 1)]
        line = np.frombuffer(raw, dtype=np.uint8, count=stride, offset=y * (stride + 1) + 1).astype(
            np.int32
        )
        if f == 0:
            cur = line
        elif f == 2:
            cur = (line + prior) & 0xFF
        elif f == 1:
            cur = line.copy()
            for c in range(bpp):  # Sub is a running sum per channel, mod 256
                cur[c::bpp] = np.cumsum(line[c::bpp]) & 0xFF
        elif f in (3, 4):
            ln, pr = line.tolist(), prior.tolist()
            res = [0] * stride
            for i in range(stride):
                a = res[i - bpp] if i >= bpp else 0
                if f == 3:
                    res[i] = (ln[i] + ((a + pr[i]) >> 1)) & 0xFF
                else:
                    c = pr[i - bpp] if i >= bpp else 0
                    res[i] = (ln[i] + _paeth(a, pr[i], c)) & 0xFF
            cur = np.asarray(res, dtype=np.int32)
        else:
            raise PngError(f"bad filter type {f}")
        out[y] = cur
        prior = cur
    return out.reshape(height, width, bpp)


def mean_luma(path: Path) -> float:
    """Mean Rec. 601 luma of a PNG file, 0-255."""
    img = decode(path.read_bytes()).astype(np.float32)
    if img.shape[2] == 1:
        return float(img.mean())
    return float((0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2]).mean())
