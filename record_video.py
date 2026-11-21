#!/usr/bin/env python3
"""Record a silent screen video (.webm) of a lesson playing in headless Chrome.

    python3 record_video.py cmp_flash/photosynthesis_0.json --out videos --speed 1.5
"""
from __future__ import annotations

import argparse
import http.server
import shutil
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
    ap.add_argument("lesson")
    ap.add_argument("--out", default="videos")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--channel", default="chrome")
    args = ap.parse_args()

    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rel = Path(args.lesson).resolve().relative_to(ROOT).as_posix()
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    tmp = out / "_tmp"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=args.channel)
        ctx = browser.new_context(
            viewport={"width": 1000, "height": 700},
            record_video_dir=str(tmp),
            record_video_size={"width": 1000, "height": 700},
        )
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/player.html?src={rel}&mute=1&speed={args.speed}&autoplay=1")
        page.wait_for_function("window.__pencil && window.__pencil.quiz === true", timeout=600000)
        page.wait_for_timeout(1500)
        video = page.video
        ctx.close()
        dest = out / (Path(args.lesson).parent.name + "_" + Path(args.lesson).stem + ".webm")
        shutil.move(video.path(), dest)
        browser.close()
    shutil.rmtree(tmp, ignore_errors=True)
    srv.shutdown()
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
