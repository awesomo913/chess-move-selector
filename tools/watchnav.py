#!/usr/bin/env python3
"""Diagnostic: watch a chess.com page for navigations/reloads for N seconds.

Usage: python tools/watchnav.py [port] [seconds]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9333
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0

    with sync_playwright() as pw:
        br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000)
        page = None
        for ctx in br.contexts:
            for p in ctx.pages:
                try:
                    if "chess.com" in p.url:
                        page = p
                except Exception:
                    pass
        if page is None:
            print("no chess.com page found")
            return 1
        t0 = time.time()

        def on_nav(frame):
            if frame == page.main_frame:
                print(f"[{time.time() - t0:6.1f}s] NAV -> {frame.url}", flush=True)

        def on_load(p):
            print(f"[{time.time() - t0:6.1f}s] LOAD", flush=True)

        page.on("framenavigated", on_nav)
        page.on("load", on_load)

        while time.time() - t0 < secs:
            try:
                info = page.evaluate(
                    "() => location.href + ' | ready=' + document.readyState"
                    " + ' | boards=' + document.querySelectorAll('wc-chess-board').length"
                    " + ' | buttons=' + document.querySelectorAll('button').length"
                )
                print(f"[{time.time() - t0:6.1f}s] ok: {info}", flush=True)
            except Exception as e:
                print(f"[{time.time() - t0:6.1f}s] EVAL FAIL: {str(e)[:100]}", flush=True)
            time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
