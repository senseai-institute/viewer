# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import boto3
import pytest
from moto import mock_aws
from senseai_viewer.runner import prepare as prep
from senseai_viewer.runner import s3 as s3mod

from .dsec_helpers import make_dsec_downloads

BUCKET = "senseai-data"
PREFIX = "raw/dsec/"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Any]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    with mock_aws():
        c = boto3.client("s3", region_name="eu-central-1")
        c.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-central-1"}
        )
        for f in make_dsec_downloads(tmp_path / "up", "zurich_city_09_a"):
            c.upload_file(str(f), BUCKET, PREFIX + f.name)
        c.put_object(Bucket=BUCKET, Key=PREFIX + "zurich_city_10_a/events_left.zip", Body=b"x" * 10)
        c.put_object(Bucket=BUCKET, Key=PREFIX + "README.txt", Body=b"hello")
        yield c


def test_parse_uri() -> None:
    assert s3mod.parse_uri("s3://senseai-data/raw/dsec") == ("senseai-data", "raw/dsec/")
    assert s3mod.parse_uri("s3://b/") == ("b", "")
    with pytest.raises(ValueError):
        s3mod.parse_uri("http://x")
    with pytest.raises(ValueError):
        s3mod.parse_uri("s3:///x")


def test_list_and_group(s3: Any) -> None:
    objs = s3mod.list_objects(s3, BUCKET, PREFIX)
    per, shared = s3mod.group(objs, PREFIX)
    assert set(per) == {"zurich_city_09_a", "zurich_city_10_a"}
    assert len(per["zurich_city_09_a"]) == 5
    assert [o.key for o in shared] == [PREFIX + "README.txt"]
    assert s3mod.useful_shared(shared) == []
    assert (
        s3mod.sequence_of("raw/dsec/interlaken_00_b_calibration.zip", PREFIX) == "interlaken_00_b"
    )


def test_download_resumes_and_cancels(s3: Any, tmp_path: Path) -> None:
    obj = next(
        o
        for o in s3mod.list_objects(s3, BUCKET, PREFIX)
        if o.key.endswith("events_left.zip") and "09" in o.key
    )
    full = s3.get_object(Bucket=BUCKET, Key=obj.key)["Body"].read()
    dest = tmp_path / "dl" / "e.zip"
    dest.parent.mkdir()
    (dest.parent / "e.zip.part").write_bytes(full[:100])
    (dest.parent / "e.zip.etag").write_text(obj.etag)
    seen: list[int] = []
    s3mod.download(s3, BUCKET, obj, dest, seen.append)
    assert dest.read_bytes() == full and seen[0] == 100 and seen[-1] == obj.size
    assert s3mod.download(s3, BUCKET, obj, dest, seen.append) == dest  # already there
    # A partial file from another version of the object is discarded.
    other = tmp_path / "dl" / "f.zip"
    (tmp_path / "dl" / "f.zip.part").write_bytes(b"junk" * 10)
    (tmp_path / "dl" / "f.zip.etag").write_text("old-etag")
    s3mod.download(s3, BUCKET, obj, other)
    assert other.read_bytes() == full
    with pytest.raises(InterruptedError):
        s3mod.download(s3, BUCKET, obj, tmp_path / "dl" / "g.zip", cancelled=lambda: True)
    assert (tmp_path / "dl" / "g.zip.part").exists()


def test_fetch_and_prepare_from_s3(s3: Any, tmp_path: Path) -> None:
    per, shared = s3mod.group(s3mod.list_objects(s3, BUCKET, PREFIX), PREFIX)
    seq = tmp_path / "data" / "zurich_city_09_a"
    stages: list[str] = []
    st = prep.prepare(
        seq,
        objects=per["zurich_city_09_a"],
        shared=shared,
        s3=s3,
        bucket=BUCKET,
        progress=lambda s, _f, _m: stages.append(s),
    )
    assert st["status"] == "ready", st
    assert st["labels"] and st["frames"] == 5 and st["size_bytes"] > 0
    assert (
        stages.index("downloading")
        < stages.index("unpacking")
        < stages.index("arranging")
        < stages.index("validating")
    )
    assert (seq / prep.FRAMES / "000004.png").exists() and (seq / prep.TIMESTAMPS).exists()
    again: list[str] = []
    prep.prepare(
        seq,
        objects=per["zurich_city_09_a"],
        s3=s3,
        bucket=BUCKET,
        progress=lambda _s, _f, m: again.append(m),
    )
    assert again == ["already ready"]


def test_failed_download_is_reported_and_resumable(s3: Any, tmp_path: Path) -> None:
    per, _ = s3mod.group(s3mod.list_objects(s3, BUCKET, PREFIX), PREFIX)
    seq = tmp_path / "zurich_city_09_a"
    st = prep.prepare(
        seq, objects=per["zurich_city_09_a"], s3=s3, bucket=BUCKET, cancelled=lambda: True
    )
    assert st["status"] == "failed" and "cancelled" in st["error"]
    gone = s3mod.S3Object(PREFIX + "zurich_city_09_a_missing.zip", 5, "x")
    st = prep.prepare(seq, objects=[gone], s3=s3, bucket=BUCKET)
    assert st["status"] == "failed" and "NoSuchKey" in st["error"]
    st = prep.prepare(seq, objects=per["zurich_city_09_a"], s3=s3, bucket=BUCKET)
    assert st["status"] == "ready"


def test_access_check_and_policy(s3: Any) -> None:
    ok, msg = s3mod.check_access(s3, BUCKET, PREFIX)
    assert ok and "reachable" in msg
    ok, msg = s3mod.check_access(s3, BUCKET, "nothing/")
    assert ok and "empty" in msg
    ok, msg = s3mod.check_access(s3, "no-such-bucket-xyz", PREFIX)
    assert not ok and "does not exist" in msg
    pol = json.loads(s3mod.policy_for(BUCKET, PREFIX))
    acts = {a for st in pol["Statement"] for a in st["Action"]}
    assert acts == {"s3:ListBucket", "s3:GetObject"}
    assert pol["Statement"][1]["Resource"] == f"arn:aws:s3:::{BUCKET}/{PREFIX}*"


def test_explain_errors() -> None:
    class Denied(Exception):
        response: ClassVar[dict[str, dict[str, str]]] = {"Error": {"Code": "AccessDenied"}}

    class NoCredentialsError(Exception):
        pass

    assert "not allowed" in s3mod._explain(Denied(), "list x")
    assert "instance role" in s3mod._explain(NoCredentialsError(), "list x")
    assert "could not list x" in s3mod._explain(RuntimeError("boom"), "list x")
