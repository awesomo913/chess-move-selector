#!/usr/bin/env python3
"""Test: block ad-tech on the chess.com page and watch it settle.

Usage: python tools/stabilize_test.py [port] [seconds]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from board_reader import AD_BLOCK_SUBSTRINGS, ChessComReader  # noqa: E402


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9333
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

    r = ChessComReader(port=port)
    r.connect()
    try:
        page = r.find_page()
        if page is None:
            print("no chess.com page in this browser (hostname check)")
            return 1
        print("page:", page.url)

        blocked = []

        def handler(route):
            url = route.request.url.lower()
            if any(s in url for s in AD_BLOCK_SUBSTRINGS):
                blocked.append(url[:110])
                try:
                    route.abort()
                except Exception:
                    pass
            else:
                try:
                    route.continue_()
                except Exception:
                    pass

        page.route("**/*", handler)
        print("routes enabled; reloading page to apply ...")
        t0 = time.time()

        def on_nav(frame):
            if frame == page.main_frame:
                print(f"[{time.time() - t0:6.1f}s] NAV -> {frame.url[:110]}", flush=True)

        page.on("framenavigated", on_nav)
        page.reload(wait_until="domcontentloaded", timeout=30000)

        while time.time() - t0 < secs:
            try:
                info = page.evaluate(
                    "() => { const b = document.querySelector('wc-chess-board');"
                    " const g = b && b.game;"
                    " return {host: location.hostname, boards: b ? 1 : 0,"
                    " fen: (g && g.getFEN) ? g.getFEN() : null,"
                    " playColor: (g && g.getPlayingColor) ? g.getPlayingColor() : null}; }"
                )
                print(f"[{time.time() - t0:6.1f}s] ok: {info}", flush=True)
            except Exception as e:
                print(f"[{time.time() - t0:6.1f}s] EVAL FAIL: {str(e)[:90]}", flush=True)
            time.sleep(2)

        print(f"blocked {len(blocked)} ad requests; sample:")
        for u in blocked[:8]:
            print("   ", u)
    finally:
        r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
