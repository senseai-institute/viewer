# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""The dependency-free PNG decoder and the password gate."""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from senseai_viewer import png
from senseai_viewer.app import create_app
from senseai_viewer.settings import Settings


def write_png(path: Path, img: np.ndarray, filters: tuple[int, ...] = (0,)) -> None:
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

    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")
    path.write_bytes(data)


def settings(tmp: Path, *, password: str | None = None) -> Settings:
    return Settings(
        data_dir=tmp / "data",
        upload_token=None,
        max_upload_mb=1,
        public_base_url=None,
        demo=False,
        cache_mb=8,
        viewer_password=password,
        runner=True,
        data_root=tmp / "dsec",
    )


@pytest.mark.parametrize("channels", [1, 3, 4])
def test_png_decoder_all_filters(tmp_path: Path, channels: int) -> None:
    rng = np.random.default_rng(channels)
    shape = (11, 9) if channels == 1 else (11, 9, channels)
    img = rng.integers(0, 256, shape, dtype=np.uint8)
    p = tmp_path / "x.png"
    write_png(p, img, filters=(0, 1, 2, 3, 4))
    got = png.decode(p.read_bytes())
    np.testing.assert_array_equal(got.reshape(img.shape), img)


def test_png_against_pillow_and_errors(tmp_path: Path) -> None:
    from PIL import Image

    rng = np.random.default_rng(1)
    img = (rng.random((40, 50, 3)) * 255).astype(np.uint8)
    Image.fromarray(img).save(tmp_path / "p.png", optimize=True)
    np.testing.assert_array_equal(png.decode((tmp_path / "p.png").read_bytes()), img)
    luma = png.mean_luma(tmp_path / "p.png")
    assert luma == pytest.approx(float((img @ np.array([0.299, 0.587, 0.114])).mean()), abs=0.01)
    with pytest.raises(png.PngError):
        png.decode(b"not a png")
    Image.fromarray(img).convert("P").save(tmp_path / "pal.png")
    with pytest.raises(png.PngError, match="colour type 3"):
        png.decode((tmp_path / "pal.png").read_bytes())


def test_basic_auth(tmp_path: Path) -> None:
    def basic(pw: str) -> dict[str, str]:
        return {"Authorization": "Basic " + base64.b64encode(f"anyone:{pw}".encode()).decode()}

    with TestClient(create_app(settings(tmp_path, password="pw")), client=("10.1.2.3", 1)) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/healthz").status_code == 200
        r = c.get("/api/runs")
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        assert c.get("/api/runs", headers=basic("wrong")).status_code == 401
        assert c.get("/api/runs", headers={"Authorization": "Basic !!!"}).status_code == 401
        assert c.get("/api/runs", headers=basic("pw")).status_code == 200
        assert (
            c.get("/api/run/options", headers=basic("pw")).status_code == 200
        )  # any host with the password
        assert (
            c.get("/api/runs", headers={"Authorization": "Bearer x"}).status_code == 401
        )  # reads need the password
        assert (
            c.put("/api/runs", content=b"x", headers={"Authorization": "Bearer x"}).status_code
            == 403
        )  # token path
