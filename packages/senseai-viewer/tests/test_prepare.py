# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from senseai_viewer.runner import prepare as prep

from .dsec_helpers import calibration_yaml, make_dsec_downloads, make_local_quirky, tracks_npy


def test_quirky_local_sequence_ends_ready(tmp_path: Path) -> None:
    """zurich_city_04_a as it is on the box: PNGs directly under images/left, no labels."""
    seq = make_local_quirky(tmp_path)
    seen: list[str] = []
    st = prep.prepare(seq, progress=lambda s, _f, _m: seen.append(s))
    assert st["status"] == "ready", st
    assert sorted(p.name for p in (seq / prep.FRAMES).glob("*.png")) == [
        f"{i:06d}.png" for i in range(4)
    ]
    assert not list((seq / "images" / "left").glob("*.png"))
    assert (seq / "images" / "left" / "exposure_timestamps.txt").exists()  # left alone
    assert st["labels"] is False and any("no labels" in w for w in st["warnings"])
    assert st["frames"] == 4 and st["duration_s"] == pytest.approx(0.15) and st["events"]
    assert st["brightness"] == pytest.approx(20.0) and st["night"]
    assert any("moved 4 PNG frames from images/left" in a for a in st["actions"])
    assert seen[-1] == "ready" and "arranging" in seen and "validating" in seen
    state = json.loads((seq / prep.STATE_FILE).read_text())
    assert state["files"]["images/left/rectified"] == 4


def test_idempotent(tmp_path: Path) -> None:
    seq = make_local_quirky(tmp_path)
    first = prep.prepare(seq)
    stamp = (seq / prep.STATE_FILE).stat().st_mtime_ns
    msgs: list[str] = []
    again = prep.prepare(seq, progress=lambda _s, _f, m: msgs.append(m))
    assert again == first and msgs == ["already ready"]
    assert (seq / prep.STATE_FILE).stat().st_mtime_ns == stamp  # nothing rewritten
    # Files changed after validation: no longer ready, and preparing again re-validates.
    (seq / prep.FRAMES / "000000.png").unlink()
    assert not prep.is_ready(seq)
    st = prep.prepare(seq)
    assert st["status"] == "problem" and "3 frames but 4 timestamps" in " ".join(st["problems"])


def test_field_names_are_reported_not_patched(tmp_path: Path) -> None:
    seq = make_local_quirky(tmp_path)
    (seq / "calibration" / "cam_to_cam.yaml").write_text(calibration_yaml(rect=False))
    (seq / "object_detections" / "left").mkdir(parents=True)
    bad = tracks_npy(ok=False)
    (seq / prep.LABELS).write_bytes(bad)
    st = prep.prepare(seq)
    assert st["status"] == "problem"
    text = " ".join(st["problems"])
    assert "intrinsics.camRect1.camera_matrix" in text and "['cam0', 'camRect0']" in text
    assert "fields found: ['timestamp_us', 'bx', 'by', 'bw', 'bh', 'cls', 'tid']" in text
    assert "class_id" in text and "track_id" in text
    assert (seq / prep.LABELS).read_bytes() == bad  # never rewritten


def test_events_problems(tmp_path: Path) -> None:
    from .dsec_helpers import events_h5

    seq = make_local_quirky(tmp_path)
    events_h5(seq / prep.EVENTS, ok=False)
    st = prep.prepare(seq)
    assert st["status"] == "problem"
    assert "missing ['x', 'y', 't', 'p']" in " ".join(st["problems"]) and "evts" in " ".join(
        st["problems"]
    )
    (seq / prep.EVENTS).write_text("not hdf5")
    assert "not a readable HDF5" in " ".join(prep.prepare(seq)["problems"])


def test_missing_everything(tmp_path: Path) -> None:
    seq = tmp_path / "thun_00_a"
    seq.mkdir()
    st = prep.prepare(seq)
    assert st["status"] == "problem"
    text = " ".join(st["problems"])
    for need in (
        "no RGB frames",
        "images/timestamps.txt",
        "events/left/events.h5",
        "calibration/cam_to_cam.yaml",
    ):
        assert need in text


def test_unpack_zips_without_s3(tmp_path: Path) -> None:
    """Zips already copied into the sequence's download folder are unpacked and arranged."""
    seq = tmp_path / "zurich_city_09_a"
    make_dsec_downloads(seq / prep.DOWNLOADS, "zurich_city_09_a", nested_frames=False)
    st = prep.prepare(seq)
    assert st["status"] == "ready", st["problems"]
    assert st["labels"] and st["frames"] == 5
    assert (seq / prep.EVENTS).exists() and (seq / "events" / "left" / "rectify_map.h5").exists()
    assert not (seq / prep.DOWNLOADS).exists() and not (seq / prep.STAGING).exists()


def test_unpack_refuses_escaping_members(tmp_path: Path) -> None:
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../etc/x", "no")
    with pytest.raises(ValueError, match="escapes"):
        prep.unpack(z, tmp_path / "staging")


def test_shared_archive_only_extracts_this_sequence(tmp_path: Path) -> None:
    z = tmp_path / "train_calibration.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("zurich_city_09_a/calibration/cam_to_cam.yaml", "a: 1")
        zf.writestr("zurich_city_10_a/calibration/cam_to_cam.yaml", "a: 2")
    n = prep.unpack(z, tmp_path / "st", only_sequence="zurich_city_09_a")
    assert n == 1
    assert (tmp_path / "st" / "train_calibration" / "zurich_city_09_a").exists()
    assert not (tmp_path / "st" / "train_calibration" / "zurich_city_10_a").exists()
    assert prep.unpack(z, tmp_path / "st", only_sequence="zurich_city_09_a") == 0  # already done


def test_scan(tmp_path: Path) -> None:
    from senseai_viewer.runner.s3 import S3Object

    seq = make_local_quirky(tmp_path, "zurich_city_04_a", level=20)
    prep.prepare(seq)
    make_local_quirky(tmp_path, "zurich_city_00_a", level=140)
    rows = prep.scan(
        tmp_path,
        {"zurich_city_10_a": [S3Object("raw/dsec/zurich_city_10_a_events_left.zip", 1234, "e")]},
    )
    by = {r["name"]: r for r in rows}
    assert by["zurich_city_04_a"]["status"] == "ready" and by["zurich_city_04_a"]["night"]
    assert by["zurich_city_00_a"]["status"] == "local"
    assert (
        by["zurich_city_10_a"]["status"] == "in_s3" and by["zurich_city_10_a"]["size_bytes"] == 1234
    )
    assert rows[0]["name"] == "zurich_city_04_a"  # measured darkest first
