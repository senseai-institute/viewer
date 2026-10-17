# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Window-bundle encoding: a minimal msgpack writer and the JSON fallback.

The window API sends numeric arrays as ``{"dtype", "shape", "data"}`` with ``data`` a
little-endian ``bin`` (msgpack) so the browser can wrap it in a typed array without parsing.
Only the msgpack subset the bundle needs is implemented (nil, bool, int, float64, str, bin,
array, map), which keeps the server free of an extra dependency. The browser side is
``web/src/api/msgpack.ts``.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

_DTYPES = {"u1", "i1", "u2", "i2", "u4", "i4", "f2", "f4", "f8", "b1"}


def array(a: np.ndarray[Any, Any]) -> dict[str, object]:
    """Wrap a numpy array for the bundle. int64 is narrowed: browsers lack a cheap int64 view."""
    if a.dtype == np.bool_:
        a = a.astype(np.uint8)
        kind = "b1"
    else:
        if a.dtype in (np.int64, np.uint64):
            raise TypeError("int64 arrays are not sent; convert to seconds or narrow first")
        a = a.astype(a.dtype.newbyteorder("<"), copy=False)
        kind = a.dtype.str[1:]
    if kind not in _DTYPES:
        raise TypeError(f"unsupported dtype {a.dtype}")
    return {"dtype": kind, "shape": list(a.shape), "data": np.ascontiguousarray(a).tobytes()}


def _pack(obj: object, out: bytearray) -> None:
    if obj is None:
        out.append(0xC0)
    elif obj is True:
        out.append(0xC3)
    elif obj is False:
        out.append(0xC2)
    elif isinstance(obj, int | np.integer):
        v = int(obj)
        if 0 <= v < 0x80:
            out.append(v)
        elif -32 <= v < 0:
            out.append(v & 0xFF)
        elif 0 <= v <= 0xFFFFFFFF:
            out += struct.pack(">BI", 0xCE, v)
        elif -(2**31) <= v < 0:
            out += struct.pack(">Bi", 0xD2, v)
        elif v >= 0:
            out += struct.pack(">BQ", 0xCF, v)
        else:
            out += struct.pack(">Bq", 0xD3, v)
    elif isinstance(obj, float | np.floating):
        f = float(obj)
        if math.isnan(f) or math.isinf(f):
            out.append(0xC0)  # JSON parity: non-finite numbers travel as nil
        else:
            out += struct.pack(">Bd", 0xCB, f)
    elif isinstance(obj, str):
        b = obj.encode()
        n = len(b)
        if n < 32:
            out.append(0xA0 | n)
        else:
            out += struct.pack(">BI", 0xDB, n)
        out += b
    elif isinstance(obj, bytes | bytearray | memoryview):
        b = bytes(obj)
        out += struct.pack(">BI", 0xC6, len(b))
        out += b
    elif isinstance(obj, np.ndarray):
        _pack(array(obj), out)
    elif isinstance(obj, Mapping):
        out += struct.pack(">BI", 0xDF, len(obj))
        for k, v in obj.items():
            _pack(str(k), out)
            _pack(v, out)
    elif isinstance(obj, Sequence):
        out += struct.pack(">BI", 0xDD, len(obj))
        for v in obj:
            _pack(v, out)
    else:
        raise TypeError(f"cannot encode {type(obj).__name__}")


def packb(obj: object) -> bytes:
    """Encode ``obj`` as msgpack."""
    out = bytearray()
    _pack(obj, out)
    return bytes(out)


def to_json(obj: object) -> object:
    """Convert a bundle to plain JSON values: arrays become nested lists, NaN becomes null."""
    if isinstance(obj, np.ndarray):
        return to_json(obj.tolist())
    if isinstance(obj, Mapping):
        if set(obj) == {"dtype", "shape", "data"} and isinstance(obj["data"], bytes):
            dt = "u1" if obj["dtype"] == "b1" else obj["dtype"]
            a = np.frombuffer(obj["data"], dtype="<" + str(dt)).reshape(obj["shape"])
            return to_json(a.tolist())
        return {str(k): to_json(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_json(v) for v in obj]
    if isinstance(obj, float | np.floating):
        f = float(obj)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    return obj


def unpackb(data: bytes) -> object:
    """Decode msgpack produced by :func:`packb` (tests and the CLI only)."""
    pos = 0

    def take(n: int) -> bytes:
        nonlocal pos
        b = data[pos : pos + n]
        pos += n
        return b

    def read() -> object:
        (t,) = take(1)
        if t < 0x80:
            return t
        if t >= 0xE0:
            return t - 0x100
        if 0xA0 <= t <= 0xBF:
            return take(t & 0x1F).decode()
        if t == 0xC0:
            return None
        if t in (0xC2, 0xC3):
            return t == 0xC3
        fmt = {0xCE: ">I", 0xCF: ">Q", 0xD2: ">i", 0xD3: ">q", 0xCB: ">d"}
        if t in fmt:
            f = fmt[t]
            return struct.unpack(f, take(struct.calcsize(f)))[0]
        if t in (0xDB, 0xC6):
            (n,) = struct.unpack(">I", take(4))
            b = take(n)
            return b.decode() if t == 0xDB else b
        if t == 0xDD:
            (n,) = struct.unpack(">I", take(4))
            return [read() for _ in range(n)]
        if t == 0xDF:
            (n,) = struct.unpack(">I", take(4))
            return {str(read()): read() for _ in range(n)}
        raise ValueError(f"unsupported msgpack type 0x{t:02x}")

    return read()
