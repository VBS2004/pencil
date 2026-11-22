"""Headless check of player.html: sample plays to the quiz, nothing throws, bad input is reported."""
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


def main():
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), Quiet)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/player.html"
    errors = []
    with sync_playwright() as pw:
        linux_chrome = Path("/usr/bin/google-chrome")
        browser = (pw.chromium.launch(executable_path=str(linux_chrome), args=["--no-sandbox"]) if linux_chrome.exists()
                   else pw.chromium.launch(channel="chrome"))
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)

        # 1. Sample plays through silently at 3x and reaches the quiz.
        page.goto(base + "?mute=1&speed=3&autoplay=1")
        page.wait_for_function("window.__pencil && window.__pencil.cur >= 1", timeout=30000)
        page.wait_for_timeout(1800)
        page.screenshot(path=str(ROOT / "shot_mid.png"))
        page.wait_for_function("window.__pencil.quiz === true", timeout=60000)
        n_parts = page.evaluate("window.__pencil.beats.flatMap(b => b.parts).length")
        n_drawn = page.evaluate("window.__pencil.beats.flatMap(b => b.parts).filter(p => p.g.classList.contains('drawn')).length")
        assert n_parts == 7 and n_drawn == 7, (n_parts, n_drawn)
        assert page.locator(".beat.visible").count() == 4
        page.screenshot(path=str(ROOT / "shot_end.png"), full_page=True)

        # 1b. Wobble is on by default (geometry-based), keeps endpoints/shape, and toggles back to exact geometry.
        assert page.is_checked("#wobble-toggle")
        info = page.evaluate("""() => [...document.querySelectorAll('.figure path')].filter(e => e._dp).map(e => [e._dp, e.getAttribute('d')])""")
        assert info
        changed = [1 for dp, d in info if dp != d]
        assert len(changed) >= len(info) // 2, (len(changed), len(info))
        assert page.evaluate("getComputedStyle(document.querySelector('.figure svg')).filter") == "none"
        page.uncheck("#wobble-toggle")
        assert page.evaluate("[...document.querySelectorAll('.figure path')].filter(e => e._dp).every(e => e.getAttribute('d') === e._dp)")
        page.check("#wobble-toggle")
        assert page.evaluate("[...document.querySelectorAll('.figure path')].filter(e => e._dp && e._dw).every(e => e.getAttribute('d') === e._dw)")
        # Same lesson, same wobble: reloading gives identical geometry.
        page.reload(); page.wait_for_selector(".figure path")
        again = page.evaluate("[...document.querySelectorAll('.figure path')].filter(e => e._dp).map(e => e.getAttribute('d'))")
        assert again == [d for _, d in info], "wobble must be deterministic"

        # 2. Quiz buttons respond.
        page.locator(".q").first.locator("button").nth(1).click()
        assert page.locator(".q .right").count() == 1 and page.locator(".q .why").first.is_visible()

        # 3. Replay and prev/next navigation.
        page.click("#next") if not page.is_disabled("#next") else None
        page.click("#dots button >> nth=2")
        page.wait_for_function("window.__pencil.cur === 2")
        assert page.locator(".quiz").count() == 0
        page.click("#prev")
        page.wait_for_function("window.__pencil.cur === 1")
        page.click("#play")  # pause
        assert page.inner_text("#play") == "Play"

        # 4. Broken lesson loads with validation issues shown and does not crash.
        page.evaluate("document.getElementById('loader').open = true")
        page.fill("#paste", (ROOT / "samples" / "broken.json").read_text())
        page.click("#paste-btn")
        issues = page.inner_text("#issues")
        # 6 deliberate defects + 1 label the player's layout fixer could not place near its drawing
        assert "7 validation issues" in issues, issues

        # 5. Invalid JSON and an empty lesson are reported, not thrown.
        page.fill("#paste", "{nope")
        page.click("#paste-btn")
        assert "Invalid JSON" in page.inner_text("#issues")
        page.fill("#paste", '{"version":1,"beats":[]}')
        page.click("#paste-btn")
        assert "no beats" in page.inner_text("#stage").lower()

        # 6. Lesson text is rendered as text, never as markup.
        page.fill("#paste", '{"version":1,"meta":{"title":"<img src=x onerror=alert(1)>","language":"en"},'
                            '"canvas":{"logicalWidth":1000},"beats":[{"id":"b","narration":"one two three four five six",'
                            '"ops":[{"id":"p","type":"paragraph","text":"<b>bold?</b>"}]}]}')
        page.click("#paste-btn")
        assert page.locator("#stage img, #stage b").count() == 0
        browser.close()
    srv.shutdown()
    assert not errors, errors
    print("player OK")


if __name__ == "__main__":
    main()
