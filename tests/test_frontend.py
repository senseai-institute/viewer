# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Playwright smoke test against the built frontend, served by the real app (local-folder mode)."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from senseai_viewer.api.app import STATIC_DIR, create_app
from senseai_viewer.api.config import Settings

pytestmark = pytest.mark.skipif(
    not (STATIC_DIR / "index.html").is_file(), reason="frontend not built: run `make build`"
)
playwright = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def server(summaries: Path) -> Iterator[str]:
    app = create_app(Settings(local_summaries=str(summaries), password="pw"))
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        if time.monotonic() > deadline:  # pragma: no cover
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(timeout=5)


@pytest.fixture(scope="module")
def browser() -> Iterator[playwright.Browser]:
    with playwright.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _page(browser: playwright.Browser, width: int = 1280) -> playwright.Page:
    ctx = browser.new_context(
        http_credentials={"username": "viewer", "password": "pw"},
        locale="en-US",
        viewport={"width": width, "height": 900},
    )
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors  # type: ignore[attr-defined]
    return page


def test_list_recording_and_scrubber(server: str, browser: playwright.Browser) -> None:
    expect = playwright.expect
    page = _page(browser)
    page.goto(server + "/")
    link = page.locator("a.rec-link", has_text="zurich_city_04_a")
    expect(link).to_be_visible()
    expect(page.locator("tr[data-id='zurich_city_04_a'] .badge.ok")).to_have_count(2)
    link.click()

    expect(page.locator("#stats .tile")).to_have_count(6)
    expect(page.locator("#stats")).to_contain_text("Jitter p99")
    lanes = page.locator("#timeline .lane[data-source]")
    expect(lanes).to_have_count(4)
    expect(page.locator("#timeline .lane[data-source='events_left'] canvas")).to_have_count(1)

    img = page.locator("figure[data-source='rgb_left'] img")
    ev_img = page.locator("figure[data-source='events_left'] img")
    expect(img).to_have_attribute("src", "/api/recordings/zurich_city_04_a/thumbs/rgb_left/0.jpg")
    page.locator("#scrub-range").fill("150")
    expect(page.locator("#scrub-readout")).to_contain_text("tick 150 of 200")
    expect(img).to_have_attribute("src", "/api/recordings/zurich_city_04_a/thumbs/rgb_left/30.jpg")
    expect(ev_img).to_have_attribute(
        "src", "/api/recordings/zurich_city_04_a/thumbs/events_left/45.jpg"
    )
    assert (
        page.evaluate("document.querySelector(\"figure[data-source='rgb_left'] img\").naturalWidth")
        > 0
    )
    expect(page.locator(".counts tr[data-source='rgb_left'] td").first).to_have_text("1")

    # clicking the timeline moves the scrubber
    over = page.locator("#timeline .lane[data-source='events_left'] .u-over")
    box = over.bounding_box()
    assert box is not None
    over.click(position={"x": box["width"] * 0.25, "y": box["height"] / 2})
    expect(page.locator("#scrub-readout")).not_to_contain_text("tick 150 ")
    tick = int(page.locator("#scrub-range").input_value())
    assert 40 <= tick <= 60
    assert "?t=" in page.url
    assert page.errors == []  # type: ignore[attr-defined]


def test_missing_pieces_and_compare(server: str, browser: playwright.Browser) -> None:
    expect = playwright.expect
    page = _page(browser)
    page.goto(server + "/#/r/mixed")
    expect(page.locator("#timeline .lane[data-source='imu0'] canvas")).to_have_count(1)
    expect(page.locator("#scrubber")).to_contain_text("No camera or event sources")
    expect(page.locator("#stats")).to_contain_text("no stats")
    page.goto(server + "/#/r/does-not-exist")
    expect(page.locator("main")).to_contain_text("Recording not found")
    page.goto(server + "/#/compare")
    expect(page.locator(".compare-chart canvas")).to_have_count(1)
    assert page.errors == []  # type: ignore[attr-defined]


def test_phone_width(server: str, browser: playwright.Browser) -> None:
    expect = playwright.expect
    page = _page(browser, width=390)
    page.goto(server + "/")
    expect(page.locator("a.rec-link").first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    page.goto(server + "/#/r/zurich_city_04_a")
    expect(page.locator("#stats .tile")).to_have_count(6)
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
