#!/usr/bin/env python3
"""Play lesson JSON files silently in headless Chrome and save a full-page screenshot of each.

    python3 render_lesson.py lesson_tides.json eval_lessons/photosynthesis_0.json --out shots
"""
from __future__ import annotations

import argparse
import http.server
import socketserver
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent


class Quiet(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT), **k)

    def log_message(self, *a):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lessons", nargs="+")
    ap.add_argument("--out", default="shots")
    ap.add_argument("--channel", default="chrome", help="chrome or msedge")
    args = ap.parse_args()

    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), Quiet)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    bad = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=args.channel)
        for lesson in args.lessons:
            rel = Path(lesson).resolve().relative_to(ROOT).as_posix()
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(f"http://127.0.0.1:{port}/player.html?src={rel}&mute=1&speed=3&autoplay=1")
            try:
                page.wait_for_function("window.__pencil && window.__pencil.quiz === true", timeout=120000)
                parts = page.evaluate("window.__pencil.beats.flatMap(b => b.parts).length")
                drawn = page.evaluate(
                    "window.__pencil.beats.flatMap(b => b.parts).filter(p => p.g.classList.contains('drawn')).length"
                )
                status = f"played, {drawn}/{parts} figure parts drawn"
            except Exception as exc:  # noqa: BLE001
                status = f"DID NOT FINISH: {str(exc)[:80]}"
                bad += 1
            p = Path(lesson)
            shot = out / (f"{p.parent.name}_{p.stem}.png" if p.parent.name else p.stem + ".png")
            page.screenshot(path=str(shot), full_page=True)
            print(f"{lesson}: {status}; errors={errors or 'none'} -> {shot}")
            page.close()
        browser.close()
    srv.shutdown()
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
