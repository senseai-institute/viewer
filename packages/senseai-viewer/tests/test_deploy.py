# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""The EC2 deploy files keep their promises: loopback only, runs survive restarts."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def test_service_unit() -> None:
    unit = (DEPLOY / "senseai-viewer.service").read_text()
    assert "--host 127.0.0.1 --port 8000" in unit
    assert "KillMode=process" in unit and "Restart=always" in unit
    assert "SENSEAI_RUNNER=1" in unit and "EnvironmentFile=-/etc/senseai-viewer.env" in unit
    assert "0.0.0.0" not in unit


@pytest.mark.skipif(not shutil.which("bash"), reason="no bash")
def test_setup_script_parses() -> None:
    script = DEPLOY / "setup_ec2.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text()
    assert 'step() { echo "==> $*"; }' in text  # the Setup panel's progress lines
    assert 'SENSEAI_INSTALL_YOLO:-0}" == "1"' in text  # YOLO only on request
    assert text.rstrip().endswith('main "$@"')
