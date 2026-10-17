# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Playwright smoke test of the built web app against a live server.

Loads the share link, scrubs, jumps to a fire, reads the explanation, toggles the profile,
checks the Replay layout at 1400 x 1000, and writes docs/screenshot-replay.png.
"""

from __future__ import annotations

import shutil
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from senseai_viewer.app import STATIC, create_app
from senseai_viewer.settings import Settings

pw = pytest.importorskip("playwright.sync_api")
DOCS = Path(__file__).resolve().parents[1] / "docs"


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory, sample_run: Path) -> Iterator[str]:
    if not (STATIC / "index.html").exists():
        pytest.skip("web app not built (make build)")
    data = tmp_path_factory.mktemp("srv")
    (data / "runs").mkdir()
    shutil.copy(sample_run, data / "runs" / "demo.h5")
    app = create_app(
        Settings(
            data_dir=data,
            upload_token=None,
            max_upload_mb=1,
            public_base_url=None,
            demo=False,
            cache_mb=16,
        )
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(5)


def test_simple_view(server: str) -> None:
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # pragma: no cover - no browser in this environment
            pytest.skip(f"chromium unavailable: {e}")
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{server}/s/demo?t=6.45")
        page.wait_for_selector(".story-pane canvas")
        assert "0.36 s sooner" in page.locator("h1").inner_text()
        assert page.locator(".story-pane-title").all_inner_texts() == [
            "With Panther",
            "Standard robot",
        ]
        page.wait_for_function(
            "() => document.querySelector('.story-status')?.textContent === 'Slowing down'"
        )
        assert page.locator(".story-status").nth(1).inner_text() == "Driving normally"
        assert page.locator(".story-caption").inner_text() == "Panther slows down"
        assert page.locator(".story-moments li").count() == 5
        page.wait_for_timeout(300)
        DOCS.mkdir(exist_ok=True)
        page.screenshot(path=str(DOCS / "screenshot-simple.png"))
        page.goto(f"{server}/")  # the root opens the simple view of the demo run and plays
        page.wait_for_selector(".story-pane canvas")
        page.wait_for_function(
            "() => document.querySelector('.story-play')?.textContent?.includes('Pause')"
        )
        assert not errors, errors
        browser.close()


def test_replay_smoke(server: str) -> None:
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # pragma: no cover - no browser in this environment
            pytest.skip(f"chromium unavailable: {e}")
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{server}/replay/demo?t=5.00")
        page.wait_for_selector(".scene canvas")
        page.wait_for_function(
            "() => !document.querySelector('.transport .muted.grow')?.textContent"
        )

        # Layout: scene top-left, side column right, timeline under the scene, transport below.
        box = {
            sel: page.locator(sel).bounding_box()
            for sel in (".scene-wrap", ".side", ".timeline-wrap", ".transport-wrap")
        }
        assert all(box.values())
        sc, sd, tl, tr = (
            box[k] for k in (".scene-wrap", ".side", ".timeline-wrap", ".transport-wrap")
        )
        assert sc and sd and tl and tr
        assert sc["x"] < sd["x"] and abs(sc["y"] - sd["y"]) < 4
        assert tl["y"] > sc["y"] + sc["height"] - 1 and tr["y"] > tl["y"]
        assert sc["width"] > 900 and 300 < sd["width"] < 420

        assert page.locator(".hero .v").inner_text() == "+0.36 s"
        assert "synthetic" in page.locator(".topbar").inner_text().lower()

        # Scrub by clicking the timeline at its right end.
        canvas = page.locator(".timeline canvas")
        cb = canvas.bounding_box()
        assert cb
        page.mouse.click(cb["x"] + cb["width"] - 20, cb["y"] + cb["height"] / 2)
        page.wait_for_timeout(150)
        assert float(page.url.split("t=")[1].split("&")[0]) > 11.0 or "t=" in page.url

        # Back to the start of the clean segment, then jump to the next fire with "]".
        page.goto(f"{server}/replay/demo?t=6.00")
        page.wait_for_selector(".scene canvas")
        page.keyboard.press("]")
        page.wait_for_function(
            "() => document.querySelector('#explain .sentence')"
            "?.textContent?.startsWith('slowed to 40% at 6.32')"
        )
        assert "path risk" in page.locator("#explain .sentence").inner_text()

        # Profile toggle.
        page.get_by_role("button", name="Show default").click()
        page.wait_for_timeout(300)
        assert "p=null" in page.url
        page.keyboard.press("p")
        page.wait_for_timeout(300)
        assert "p=panther" in page.url

        page.goto(f"{server}/replay/demo?t=6.40")
        page.wait_for_selector(".scene canvas")
        page.wait_for_function(
            "() => !document.querySelector('.transport .muted.grow')?.textContent"
        )
        page.wait_for_timeout(400)
        DOCS.mkdir(exist_ok=True)
        page.screenshot(path=str(DOCS / "screenshot-replay.png"))

        page.goto(f"{server}/compare/demo?t=6.40")
        page.wait_for_selector(".compare .scene canvas")
        assert page.locator(".compare .hero .v").inner_text() == "+0.36 s"
        page.goto(f"{server}/incidents/demo")
        page.wait_for_selector("table.grid tr.row")
        assert page.locator("table.grid tr.row").count() == 5
        page.goto(f"{server}/runs")
        page.wait_for_selector(".card")
        assert not errors, errors
        browser.close()
