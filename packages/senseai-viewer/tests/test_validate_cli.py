# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np
import pytest
from senseai_viewer.cli import main
from senseai_viewer.validate import validate

from .conftest import write_minimal_runlog


def test_validate_sample_lights_every_panel(sample_run: Path) -> None:
    r = validate(sample_run)
    assert r.ok, r.render()
    assert set(r.panels.values()) == {"on"}


def test_validate_third_party_log(minimal_run: Path) -> None:
    r = validate(minimal_run)
    assert r.ok, r.render()
    assert r.panels["scene/map"] == "on" and r.panels["timeline"] == "on"
    assert r.panels["events feed"].startswith("off") and r.panels["lead time"].startswith("off")
    assert any("no reflex/fires table" in w for w in r.warnings)


def test_validate_catches_inconsistencies(tmp_path: Path) -> None:
    p = write_minimal_runlog(tmp_path / "bad.h5")
    with h5py.File(p, "a") as f:
        del f["pipelines/acme/reflex/risk"]
        f["pipelines/acme/reflex/risk"] = np.zeros(10, dtype=np.float32)
        g = f.create_group("pipelines/acme/reflex/fires")
        g.attrs["n_rows"] = 1
        g["tick"] = np.array([7])
        g["t_ns"] = np.array([0])
        f["recording/sources/dvs/tick_offsets"][5] = 0
        f["pipelines/acme/reflex/action"][0] = 99
        f["pipelines/acme/surprise"].attrs["heads"] = ["vision", "sonar"]
    r = validate(p)
    text = r.render()
    assert not r.ok
    for needle in (
        "reflex/risk: 10 rows",
        "disagree with action transitions",
        "tick_offsets",
        "outside actions enum",
        "'sonar'",
    ):
        assert needle in text, text


def test_validate_unreadable(tmp_path: Path) -> None:
    (tmp_path / "x.h5").write_text("nope")
    assert not validate(tmp_path / "x.h5").ok


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str], minimal_run: Path) -> None:
    out = tmp_path / "s.h5"
    assert main(["sample", str(out)]) == 0
    assert main(["validate", str(out), str(minimal_run)]) == 0
    assert "OK" in capsys.readouterr().out
    (tmp_path / "bad.h5").write_text("x")
    assert main(["validate", str(tmp_path / "bad.h5")]) == 1
    capsys.readouterr()
    assert main(["index", str(minimal_run)]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "third-party-run"
