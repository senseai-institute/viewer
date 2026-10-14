# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""End-to-end demo behind ``make demo``.

1. Generate two synthetic DSEC sequences (senseai_ingest.testing.make_dsec_fixture).
2. Replay them into tick files with the ``senseai-ingest replay`` CLI.
3. Export summaries to ./out with ``senseai-viewer export``.
4. Serve ./out at http://127.0.0.1:$DEMO_PORT (default 8000), take a Playwright screenshot of the
   recording view into docs/screenshot.png, and keep serving until Ctrl-C.

Pass ``--no-serve`` to stop after the screenshot (used in CI-like runs).
"""

from __future__ import annotations

import argparse
import os
import shutil
import threading
import time
from pathlib import Path

import uvicorn
from senseai_ingest.cli import main as ingest
from senseai_ingest.testing import make_dsec_fixture

from senseai_viewer.api.app import STATIC_DIR, create_app
from senseai_viewer.api.config import Settings
from senseai_viewer.cli import main as viewer

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "demo"
OUT = ROOT / "out"
RECORDINGS = [
    # id, duration_s, events/s, seed
    ("dsec-synthetic-a", 6.0, 200_000, 0),
    ("dsec-synthetic-b", 4.0, 120_000, 1),
]


def build() -> None:
    WORK.mkdir(exist_ok=True)
    for rid, duration, rate, seed in RECORDINGS:
        seq = WORK / rid
        ticks = WORK / f"{rid}.h5"
        if seq.exists():
            shutil.rmtree(seq)
        print(f"[demo] fixture   {seq.relative_to(ROOT)}")
        make_dsec_fixture(
            seq, duration_s=duration, events_per_s=rate, image_shape=(120, 160), seed=seed
        )
        print(f"[demo] replay -> {ticks.relative_to(ROOT)}")
        assert (
            ingest(
                ["replay", "--dataset", "dsec", "--path", str(seq), "--out", str(ticks), "--stats"]
            )
            == 0
        )
        print("[demo] export")
        assert viewer(["export", str(ticks), "--id", rid, "--out", str(OUT)]) == 0


def screenshot(url: str, path: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900}, locale="en-US")
        page.goto(f"{url}/#/r/{RECORDINGS[0][0]}?t=230")
        page.wait_for_selector("#scrubber figure img")
        page.wait_for_function(
            "[...document.querySelectorAll('#scrubber img')]"
            ".every(i => i.complete && i.naturalWidth > 0)"
        )
        page.screenshot(path=str(path), full_page=True)
        browser.close()
    print(f"[demo] screenshot -> {path.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-serve", action="store_true", help="exit after the screenshot")
    args = ap.parse_args()
    if not (STATIC_DIR / "index.html").is_file():
        raise SystemExit("frontend not built: run `make build` first")
    build()

    port = int(os.environ.get("DEMO_PORT", "8000"))
    app = create_app(Settings(local_summaries=str(OUT), cache_ttl_s=0))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}"
    screenshot(url, ROOT / "docs" / "screenshot.png")
    if args.no_serve:
        server.should_exit = True
        thread.join()
        return
    print(f"[demo] serving {OUT.relative_to(ROOT)}/ at {url}  (Ctrl-C to stop)")
    try:
        thread.join()
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join()


if __name__ == "__main__":
    main()
