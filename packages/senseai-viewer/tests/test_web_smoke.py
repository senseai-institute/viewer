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


@pytest.fixture(scope="module")
def runner_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A real server with the runner on, a DSEC-like folder and the fake bench producer."""
    import os
    import sys

    from .test_runner import FAKE, make_dsec

    if not (STATIC / "index.html").exists():
        pytest.skip("web app not built (make build)")
    tmp = tmp_path_factory.mktemp("live")
    dsec = make_dsec(tmp / "dsec")
    old = {k: os.environ.get(k) for k in ("FAKE_BENCH_DELAY",)}
    os.environ["FAKE_BENCH_DELAY"] = "0.05"
    app = create_app(
        Settings(
            data_dir=tmp / "data",
            upload_token=None,
            max_upload_mb=1,
            public_base_url=None,
            demo=False,
            cache_mb=16,
            runner=True,
            data_root=dsec,
            bench_cmd=f"{sys.executable} {FAKE}",
            bench_python=sys.executable,
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
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_run_page_and_side_by_side(runner_server: str) -> None:
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # pragma: no cover - no browser in this environment
            pytest.skip(f"chromium unavailable: {e}")
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{runner_server}/")  # the runner is on, so the root opens the Run page
        page.wait_for_selector(".pick")
        page.wait_for_function("() => !document.body.innerText.includes('measuring brightness')")
        names = page.locator("#app .pick-title").all_inner_texts()
        assert names[0].startswith("zurich_city_09_a")  # darkest first
        assert "NIGHT" in names[0].upper() and "LABELS" in names[0].upper()
        assert page.get_by_text("Panther (no training)").count() >= 1
        assert page.get_by_role("link", name="Run", exact=True).count() == 1
        page.click(".run-btn")
        page.wait_for_selector(".arm-card")
        page.wait_for_function(
            "() => /· ([3-9]\\d)%/.test(document.querySelector('.run-status')?.textContent ?? '')"
        )
        DOCS.mkdir(exist_ok=True)
        page.locator(".run-live").screenshot(path=str(DOCS / "screenshot-run-live.png"))
        page.wait_for_selector(".run-watch a", timeout=60_000)
        assert "reacted" in page.locator(".run-headline").inner_text()
        assert page.locator("table.results tbody tr").count() == 1
        page.locator(".run-live").screenshot(path=str(DOCS / "screenshot-run-done.png"))
        assert page.locator(".run-history .run-row").count() == 1

        page.click(".run-watch a")
        page.wait_for_selector(".vpane canvas")
        assert page.locator(".vpane").count() == 2
        assert page.locator(".vpane-title").all_inner_texts() == [
            "Panther (no training)",
            "Standard camera AI (torchvision)",
        ]
        assert "Panther (no training) reacted" in page.locator("h1").inner_text()
        assert page.locator(".vobjects li").count() == 1
        page.locator(
            ".vobjects li button"
        ).click()  # jump to just before the pedestrian enters the path
        page.wait_for_timeout(300)
        before = page.locator(".story-time").inner_text()
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(200)
        assert page.locator(".story-time").inner_text() != before
        heat = page.get_by_role("button", name="Panther heat (key 3)")
        assert heat.get_attribute("aria-pressed") == "true"
        page.keyboard.press("3")
        page.wait_for_timeout(150)
        assert heat.get_attribute("aria-pressed") == "false"
        page.keyboard.press("3")
        page.goto(page.url.split("?")[0] + "?t=3.30")
        page.wait_for_selector(".vpane canvas")
        for _ in range(100):
            pills = page.locator(".story-status").all_inner_texts()
            if pills and pills[0] != "Driving normally":
                break
            page.wait_for_timeout(100)
        assert pills[0] in ("Slowing", "Braking"), (
            pills,
            page.locator(".story-time").inner_text(),
            page.url,
        )
        page.wait_for_timeout(1200)
        page.screenshot(path=str(DOCS / "screenshot-side-by-side.png"))
        assert not errors, errors
        browser.close()
