# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""One S3 round trip with moto: export --upload, then the app reads it back from S3."""

from __future__ import annotations

import base64
from pathlib import Path

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from senseai_viewer.api.app import create_app
from senseai_viewer.api.config import Settings
from senseai_viewer.api.storage import S3Storage
from senseai_viewer.cli import main


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "eu-central-1",
        "AWS_REGION": "eu-central-1",
        "S3_BUCKET": "summaries",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("S3_PREFIX", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def test_s3_round_trip(
    aws_env: None, dsec_tick_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-central-1")
        s3.create_bucket(
            Bucket="summaries", CreateBucketConfiguration={"LocationConstraint": "eu-central-1"}
        )
        s3.put_object(Bucket="summaries", Key="viewer/rec1/stale.txt", Body=b"old")
        s3.put_object(Bucket="summaries", Key="other/ignored.txt", Body=b"x")
        argv = ["export", str(dsec_tick_file), "--out", str(tmp_path), "--id", "rec1", "--upload"]
        assert main([*argv, "-v"]) == 0
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if ln.startswith("uploaded s3://")]
        assert lines[-1] == "uploaded s3://summaries/viewer/rec1/manifest.json"  # last
        assert f"uploaded {len(lines)} objects to s3://summaries/viewer/rec1/" in out
        keys = {o["Key"] for o in s3.list_objects_v2(Bucket="summaries")["Contents"]}
        assert "viewer/rec1/stale.txt" not in keys
        assert "viewer/rec1/timeline.parquet" in keys
        head = s3.head_object(Bucket="summaries", Key="viewer/rec1/manifest.json")
        assert head["ContentType"] == "application/json"

        settings = Settings.from_env()
        auth = {"Authorization": "Basic " + base64.b64encode(b"u:pw").decode()}
        app = create_app(Settings(**{**settings.__dict__, "password": "pw"}), static_dir=None)
        c = TestClient(app)
        rows = c.get("/api/recordings", headers=auth).json()["recordings"]
        assert [r["id"] for r in rows] == ["rec1"]
        assert c.get("/api/recordings/rec1/timeline", headers=auth).json()["n_rows"] == 200
        jpg = c.get("/api/recordings/rec1/thumbs/rgb_left/0.jpg", headers=auth)
        assert jpg.status_code == 200 and jpg.content[:2] == b"\xff\xd8"
        assert c.get("/api/recordings/rec1/traces/nope", headers=auth).status_code == 404

        st = S3Storage("summaries", prefix="", client=s3)
        assert st.list_ids() == ["other", "viewer"]
        with pytest.raises(Exception, match="NoSuchBucket"):
            S3Storage("nope", client=s3).read("x", "manifest.json")
