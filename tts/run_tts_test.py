#!/usr/bin/env python3
"""Narrate real lesson text with the in-browser Supertonic model (headless Chrome), save WAVs, print stats.

    python3 tts/run_tts_test.py eval_final4/how_tides_work_0.json --beats 2 --out tts/out
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def wait_for_server(port: int) -> None:
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/tts/test.html", timeout=1)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    raise SystemExit("server did not start")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("lesson")
    ap.add_argument("--beats", type=int, default=2)
    ap.add_argument("--out", default="tts/out")
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--model", default="supertonic-3-int8", help="folder under tts/models/")
    ap.add_argument("--nocache", action="store_true", help="skip Cache Storage (saves disk), single visit")
    args = ap.parse_args()

    lesson = json.loads(Path(args.lesson).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    srv = subprocess.Popen([sys.executable, str(ROOT / "tts" / "serve.py"), str(args.port)], stdout=subprocess.DEVNULL)
    try:
        wait_for_server(args.port)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome")
            page = browser.new_context().new_page()
            logs = []
            page.on("console", lambda m: logs.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: logs.append(f"pageerror: {e}"))
            url = f"http://127.0.0.1:{args.port}/tts/test.html?model={args.model}" + ("&nocache=1" if args.nocache else "")

            for visit in ((1,) if args.nocache else (1, 2)):  # second visit shows the models coming from Cache Storage
                page.goto(url)
                page.wait_for_function("window.__ready === true", timeout=30000)
                info = page.evaluate("window.__init()")
                print(f"visit {visit}: model load {info['loadSeconds']:.1f}s, fromCache={info['fromCache']}, "
                      f"threads={info['threads']}, crossOriginIsolated={info['isolated']}")
                if visit == 1:
                    for i, beat in enumerate(lesson["beats"][: args.beats]):
                        r = page.evaluate("([t, o]) => window.__speak(t, o)", [beat["narration"], {"steps": args.steps}])
                        path = out / f"{Path(args.lesson).stem}_beat{i}.wav"
                        path.write_bytes(base64.b64decode(r["wav"]))
                        print(f"  beat {i}: {r['audioSeconds']:.1f}s audio in {r['genSeconds']:.1f}s (RTF {r['rtf']:.2f}), "
                              f"peak={r['peak']:.2f} rms={r['rms']:.3f}, {len(r['words'])} words, {len(r['chunks'])} sentences -> {path}")
                        (out / f"{path.stem}.words.json").write_text(json.dumps(r["words"]), encoding="utf-8")
            if logs:
                print("browser messages:", *logs[:8], sep="\n  ")
            browser.close()
    finally:
        srv.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
