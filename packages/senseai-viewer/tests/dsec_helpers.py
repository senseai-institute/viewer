# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Synthetic DSEC data: PNG writer, sequence folders, and zipped downloads like DSEC's."""

from __future__ import annotations

import io
import struct
import zipfile
import zlib
from pathlib import Path

import h5py
import numpy as np
import yaml


def png_bytes(img: np.ndarray, filters: tuple[int, ...] = (0,)) -> bytes:
    """Minimal PNG encoder that cycles through the given filter types row by row."""
    h, w = img.shape[:2]
    c = 1 if img.ndim == 2 else img.shape[2]
    bpp = c
    rows = img.reshape(h, w * c).astype(np.int32)
    raw = bytearray()
    prior = np.zeros(w * c, dtype=np.int32)
    for y in range(h):
        f = filters[y % len(filters)]
        cur = rows[y]
        left = np.concatenate([np.zeros(bpp, dtype=np.int32), cur[:-bpp]])
        upleft = np.concatenate([np.zeros(bpp, dtype=np.int32), prior[:-bpp]])
        if f == 0:
            out = cur
        elif f == 1:
            out = cur - left
        elif f == 2:
            out = cur - prior
        elif f == 3:
            out = cur - ((left + prior) >> 1)
        else:
            p = left + prior - upleft
            pa, pb, pc = abs(p - left), abs(p - prior), abs(p - upleft)
            pred = np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, prior, upleft))
            out = cur - pred
        raw.append(f)
        raw += (out & 0xFF).astype(np.uint8).tobytes()
        prior = cur
    ctype = {1: 0, 3: 2, 4: 6}[c]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


def write_png(path: Path, img: np.ndarray, filters: tuple[int, ...] = (0,)) -> None:
    path.write_bytes(png_bytes(img, filters))


def events_h5(path: Path, *, ok: bool = True) -> None:
    with h5py.File(path, "w") as f:
        g = f.create_group("events" if ok else "evts")
        n = 100
        g["x"] = np.arange(n, dtype=np.uint16)
        g["y"] = np.arange(n, dtype=np.uint16)
        g["t"] = np.arange(n, dtype=np.uint32) * 1000
        g["p"] = np.ones(n, dtype=np.uint8)
        f["t_offset"] = np.int64(1_000_000)
        f["ms_to_idx"] = np.arange(10, dtype=np.uint64)


def calibration_yaml(*, rect: bool = True) -> str:
    intr: dict[str, object] = {
        "cam0": {
            "camera_matrix": [555.0, 555.0, 320.0, 240.0],
            "distortion_coeffs": [0.1, 0.0, 0.0, 0.0],
        }
    }
    if rect:
        intr["camRect1"] = {"camera_matrix": [1150.0, 1150.0, 720.0, 540.0]}
    else:
        intr["camRect0"] = {"camera_matrix": [1150.0, 1150.0, 720.0, 540.0]}
    return yaml.safe_dump({"intrinsics": intr, "extrinsics": {}})


def tracks_npy(*, ok: bool = True) -> bytes:
    if ok:
        dt = [
            ("t", "<u8"),
            ("x", "<f4"),
            ("y", "<f4"),
            ("w", "<f4"),
            ("h", "<f4"),
            ("class_id", "<u1"),
            ("track_id", "<u4"),
        ]
    else:
        dt = [
            ("timestamp_us", "<u8"),
            ("bx", "<f4"),
            ("by", "<f4"),
            ("bw", "<f4"),
            ("bh", "<f4"),
            ("cls", "<u1"),
            ("tid", "<u4"),
        ]
    arr = np.zeros(3, dtype=dt)
    buf = io.BytesIO()
    np.save(buf, arr)
    return buf.getvalue()


def frames(n: int = 4, level: int = 20) -> list[bytes]:
    return [png_bytes(np.full((6, 8, 3), level, dtype=np.uint8)) for _ in range(n)]


def make_local_quirky(root: Path, name: str = "zurich_city_04_a", level: int = 20) -> Path:
    """Like zurich_city_04_a on the EC2 box: PNGs directly under images/left, no labels."""
    seq = root / name
    (seq / "images" / "left").mkdir(parents=True)
    for i, b in enumerate(frames(4, level)):
        (seq / "images" / "left" / f"{i:06d}.png").write_bytes(b)
    (seq / "images" / "timestamps.txt").write_text(
        "\n".join(str(1_000_000 + 50_000 * i) for i in range(4)) + "\n"
    )
    (seq / "images" / "left" / "exposure_timestamps.txt").write_text("0 1\n")
    (seq / "events" / "left").mkdir(parents=True)
    events_h5(seq / "events" / "left" / "events.h5")
    (seq / "calibration").mkdir()
    (seq / "calibration" / "cam_to_cam.yaml").write_text(calibration_yaml())
    return seq


def make_dsec_downloads(
    out: Path, name: str, *, labels: bool = True, nested_frames: bool = True, level: int = 30
) -> list[Path]:
    """Zips and loose files named like DSEC's downloads for one sequence."""
    out.mkdir(parents=True, exist_ok=True)
    files = []
    ev = out / "_tmp_events.h5"
    events_h5(ev)
    z = out / f"{name}_events_left.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(ev, "events.h5")
        zf.writestr("rectify_map.h5", b"\x89HDF-not-checked")
    ev.unlink()
    files.append(z)
    z = out / f"{name}_images_rectified_left.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for i, b in enumerate(frames(5, level)):
            zf.writestr(f"{'left/rectified/' if nested_frames else ''}{i:06d}.png", b)
    files.append(z)
    t = out / f"{name}_image_timestamps.txt"
    t.write_text("\n".join(str(2_000_000 + 50_000 * i) for i in range(5)) + "\n")
    files.append(t)
    z = out / f"{name}_calibration.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("calibration/cam_to_cam.yaml", calibration_yaml())
        zf.writestr("calibration/cam_to_lidar.yaml", "{}")
    files.append(z)
    if labels:
        z = out / f"{name}_object_detections.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("object_detections/left/tracks.npy", tracks_npy())
        files.append(z)
    return files
