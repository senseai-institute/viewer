# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""The Research page: the proxy to senseai-research, and a browser test with a fake backend."""

from __future__ import annotations

import base64
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from senseai_viewer import research
from senseai_viewer.app import STATIC, create_app
from senseai_viewer.settings import Settings

from tests.fake_research import CLAIM, make_app

DOCS = Path(__file__).resolve().parents[1] / "docs"


def _settings(data: Path, research_url: str | None, password: str | None = None) -> Settings:
    (data / "runs").mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data,
        upload_token=None,
        max_upload_mb=1,
        public_base_url=None,
        demo=False,
        cache_mb=16,
        viewer_password=password,
        research_url=research_url,
    )


@pytest.fixture
def fake() -> Iterator[object]:
    app = make_app()
    research.transport = httpx.ASGITransport(app=app)
    yield app
    research.transport = None


def test_hidden_without_research_url(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path, None))) as c:
        assert c.get("/api/research-status").json() == {"enabled": False}
        assert c.get("/api/research/start").status_code == 404


def test_proxy_sets_the_user_and_drops_a_spoofed_one(tmp_path: Path, fake: object) -> None:
    app = create_app(_settings(tmp_path, "http://research.test", password="pw"))
    auth = "Basic " + base64.b64encode(b"alice:pw").decode()
    with TestClient(app) as c:
        assert c.get("/api/research-status", headers={"Authorization": auth}).json() == {
            "enabled": True
        }
        r = c.get(
            "/api/research/start", headers={"Authorization": auth, "X-SenseAI-User": "mallory"}
        )
        assert r.status_code == 200 and r.json()["least_verified"][0]["claim_id"] == CLAIM
        assert c.get("/api/research/start").status_code == 401  # the viewer password still applies
    assert fake.state.users == ["alice"]  # type: ignore[attr-defined]


def test_proxy_defaults_the_user_and_streams_sse(tmp_path: Path, fake: object) -> None:
    with TestClient(create_app(_settings(tmp_path, "http://research.test"))) as c:
        r = c.post(
            "/api/research/conversations/conv-1/messages", json={"text": "use the optics value"}
        )
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.headers.get("x-accel-buffering") == "no"
        assert "event: pending_action" in r.text and r.text.rstrip().endswith("}")
        d = c.post("/api/research/actions/act-1/approve", json={"rationale": "Lens, not pupil."})
        assert d.status_code == 200
    assert fake.state.decisions == [("act-1", "approve", "Lens, not pupil.", "senseai")]  # type: ignore[attr-defined]


def test_proxy_reports_a_missing_backend(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path, "http://127.0.0.1:9"))) as c:
        r = c.get("/api/research/start")
    assert r.status_code == 502 and "not answering" in r.json()["detail"]


@pytest.fixture(scope="module")
def servers(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not (STATIC / "index.html").exists():
        pytest.skip("web app not built (make build)")

    def serve(app: object) -> tuple[uvicorn.Server, int]:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))  # type: ignore[arg-type]
        threading.Thread(target=srv.run, daemon=True).start()
        for _ in range(100):
            if srv.started:
                break
            time.sleep(0.05)
        return srv, port

    back, bport = serve(make_app())
    view, vport = serve(
        create_app(_settings(tmp_path_factory.mktemp("srv"), f"http://127.0.0.1:{bport}"))
    )
    yield f"http://127.0.0.1:{vport}"
    view.should_exit = back.should_exit = True


def test_research_page(servers: str) -> None:
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # pragma: no cover - no browser in this environment
            pytest.skip(f"chromium unavailable: {e}")
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{servers}/research")
        page.wait_for_selector(".start-row")
        assert page.locator("nav a[href='/research']").count() == 1
        page.get_by_role("button", name="Least-verified values Panther depends on").click()
        row = page.locator(f".start-row[data-claim='{CLAIM}']")
        assert row.locator(".badge").get_attribute("data-status") == "unverified"
        row.click()
        page.wait_for_selector(".claim-value")
        assert page.locator(".claim-value").inner_text() == "-0.74 log10 re human"
        page.fill(".composer textarea", "Use the optics-removed value for the camera.")
        page.click(".composer button[type=submit]")
        page.wait_for_selector(".action-card[data-status='pending']")
        assert page.locator("details.tool-chip").count() == 3
        assert (
            page.locator("details.tool-chip summary").first.inner_text() == f"Reading claim {CLAIM}"
        )
        assert "{" not in page.locator(".chat-log").inner_text(), "no raw JSON in the chat"
        assert "-0.08" in page.locator(".msg.assistant").last.inner_text()
        page.locator("#tab-source").click()
        assert "retinal illuminance" in page.locator("blockquote.passage mark").first.inner_text()
        page.locator("#tab-experiment").click()
        table = page.locator(".experiment-table")
        assert "50% → 67%" in table.inner_text() and "49% → 64%" in table.inner_text()
        assert page.locator("td.too-few").inner_text() == "too few objects to judge"
        card = page.locator(".action-card")
        card.get_by_role("button", name="Approve").click()
        assert "full sentence" in card.locator(".card-status").inner_text()
        card.locator("textarea.rationale").fill("For a camera the lens replaces the pupil.")
        card.get_by_role("button", name="Approve").click()
        page.wait_for_selector(".action-card[data-status='approved']")
        page.wait_for_function(
            "() => document.querySelector('.claim-value')?.textContent === '-0.08 log10 re human'"
        )
        page.locator("#tab-claim").click()
        assert page.locator("ol.history li").count() == 2
        page.screenshot(path=str(DOCS / "screenshot-research.png"), full_page=False)
        assert errors == []
        browser.close()
