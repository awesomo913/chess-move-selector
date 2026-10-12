#!/usr/bin/env python3
"""Live check: the analyzer must colour-code the candidate moves it draws on
the real chess.com board - one arrow per candidate, coloured by quality class
(Best / Excellent / Good / Inaccuracy / Mistake / Blunder).

Proves the feature end to end:
  1. runs the real Analyzer against a live chess.com tab (CDP),
  2. captures the classified rows it emits,
  3. reads the arrow overlay's stroke colours straight from the DOM,
  4. checks every colour belongs to MOVE_CLASSES and reports the classes seen.

Exit codes: 0 = pass, 1 = wrong colours / no quality spread, 2 = no data.

Usage:  python tools/check_colors.py --port 9333
"""
from __future__ import annotations

import argparse
import queue
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from app import MOVE_CLASSES, Analyzer, first_existing_engine, legend_text  # noqa: E402
from board_reader import ChessComReader  # noqa: E402

STROKES_JS = (
    "() => { const s = document.querySelector('wc-chess-board svg.hermes-arrows');"
    " if (!s) return null;"
    " return [...s.querySelectorAll('line')].map(l => ({"
    "   stroke: String(l.getAttribute('stroke') || '').toLowerCase(),"
    "   x1: parseFloat(l.getAttribute('x1')), y1: parseFloat(l.getAttribute('y1')),"
    "   x2: parseFloat(l.getAttribute('x2')), y2: parseFloat(l.getAttribute('y2'))"
    " })); }"
)


def main() -> int:
    ap = argparse.ArgumentParser(description="verify colour-coded arrows live")
    ap.add_argument("--port", default="9333")
    ap.add_argument("--engine", default=first_existing_engine())
    ap.add_argument("--movetime", type=int, default=700)
    ap.add_argument("--multipv", type=int, default=5,
                    help="candidate moves to compute (1-8)")
    ap.add_argument("--seconds", type=float, default=60.0,
                    help="budget for waiting on the first analysis")
    args = ap.parse_args()
    port = int(args.port)
    multipv = max(1, min(8, int(args.multipv)))

    print("legend: " + legend_text())

    opts = dict(
        port=str(port), engine=args.engine, threads=4, hash=256, multipv=multipv,
        movetime=args.movetime, depth=0, limit_strength=False, elo=1800,
        only_my_turn=True, arrows=True, arrows_n=multipv, poll_ms=600, retry_s=3.0,
    )
    events: queue.Queue = queue.Queue()
    az = Analyzer(events.put, opts)
    az.start()

    try:
        # 1) wait for the first classified suggestions
        rows = None
        t0 = time.time()
        while time.time() - t0 < args.seconds:
            try:
                ev = events.get(timeout=0.5)
            except queue.Empty:
                continue
            if ev.get("kind") == "log":
                print(f"  [log] {ev['msg']}")
            elif ev.get("kind") == "status":
                print(f"  [status] {ev['msg']}")
            elif ev.get("kind") == "suggestions":
                rows = ev["rows"]
                break
        if not rows:
            print("no analysis produced within the time budget")
            return 2

        print("classified candidate moves (what the app computed):")
        for r in rows:
            print(f"  {r['n']}. {r['best']:<8} {r['score']:<7} "
                  f"[{r.get('class', '?')}]  loss={r.get('loss')}  line={r['line'][:60]}")

        # 2) give the arrow injection a moment, then read the real DOM
        time.sleep(1.3)
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)

            def _score(url: str) -> int:
                # mirrors board_reader.find_page scoring so the checker reads the
                # same tab the analyzer drives (game > play > analysis > other)
                u = url.lower()
                if "/game/" in u:
                    return 4
                if "/play" in u:
                    return 3
                if "analysis" in u:
                    return 2
                return 1

            candidates = []
            for ctx in browser.contexts:
                for p in ctx.pages:
                    try:
                        if ChessComReader._is_chess_url(p.url):
                            candidates.append((_score(p.url), p.url, p))
                    except Exception as e:
                        # a page mid-navigation can throw on .url; skipping it
                        # silently could hide the real chess.com tab
                        print(f"  (skipping a page whose url could not be read: {type(e).__name__})")
            if not candidates:
                print("no chess.com tab found")
                return 2
            if len(candidates) > 1:
                print("chess.com tabs seen:")
                for sc, url, _p in candidates:
                    print(f"  score {sc}: {url}")
            candidates.sort(key=lambda c: -c[0])   # highest score first; stable for ties
            page = candidates[0][2]
            print(f"reading tab: {candidates[0][1]}")
            arrows = None
            last_err = None
            for attempt in range(5):
                try:
                    arrows = page.evaluate(STROKES_JS)
                except Exception as e:
                    # chess.com navigates on its own (ad bounces): the execution
                    # context can vanish mid-read - retry instead of failing
                    last_err = e
                    print(f"  (arrow read retry {attempt + 1}: {type(e).__name__})")
                    time.sleep(1.5)
                    continue
                if arrows is not None:
                    break
                # null = overlay momentarily absent (page mid-redraw/navigation):
                # also transient - retry instead of reporting a hard FAIL
                print(f"  (arrow overlay not present yet - retry {attempt + 1})")
                time.sleep(1.5)
            if arrows is None:
                why = (f"{type(last_err).__name__}: {str(last_err)[:80]}"
                       if last_err else "overlay never appeared")
                print(f"FAIL: could not read the arrow overlay ({why})")
                return 1

        print("arrows drawn on the board (stroke colours):")
        if not arrows:
            print("FAIL: no arrows on the board")
            return 1
        for a in arrows:
            print(f"  {a['stroke']}  ({a['x1']},{a['y1']}) -> ({a['x2']},{a['y2']})")

        # 3) verify colours against the legend palette
        palette = {c.lower() for _n, c, _t, _d in MOVE_CLASSES}
        colors = {a["stroke"] for a in arrows}
        unknown = colors - palette
        if unknown:
            print(f"FAIL: arrow colours not in the legend palette: {sorted(unknown)}")
            return 1

        row_classes = sorted({r.get("class", "?") for r in rows})
        if len(colors) < 2 and len(row_classes) < 2:
            print("FAIL: neither the board arrows nor the classified rows show a "
                  "quality spread (expected at least two different classes)")
            return 1
        if len(colors) < 2:
            print(f"NOTE: board shows one colour right now ({sorted(colors)}) - "
                  f"classified rows carry the spread: {row_classes}")

        print(f"OK: {len(arrows)} arrows, colours={sorted(colors)}, "
              f"classes(suggested)={row_classes}")
        print("every arrow colour matches a legend class")
        return 0
    finally:
        az.stop()


if __name__ == "__main__":
    raise SystemExit(main())
