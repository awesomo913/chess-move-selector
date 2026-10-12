#!/usr/bin/env python3
"""FULL end-to-end test: start a guest bot game, watch it with the analyzer,
optionally auto-play the suggested moves.

This exercises the exact code path the GUI app uses (Analyzer from src/app.py).

Usage:
  python tools/demo_game.py --port 9333 --moves 6 --autoplay --movetime 500
"""
from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from app import Analyzer, first_existing_engine  # noqa: E402
from board_reader import GEOMETRY_JS, ChessComReader, square_center  # noqa: E402

FIND_PLAY_JS = r"""
() => {
  const pick = (el) => { const r = el.getBoundingClientRect(); return {ok: true, x: r.x + r.width / 2, y: r.y + r.height / 2}; };
  const cta = document.querySelector('button.bot-selection-cta-button-button');
  if (cta && cta.offsetWidth > 0) return pick(cta);
  const btns = [...document.querySelectorAll('button')]
      .filter(e => e.offsetWidth > 100 && /^play$/i.test(e.textContent.trim()));
  if (btns.length) return pick(btns[btns.length - 1]);
  return {ok: false, buttons: [...document.querySelectorAll('button')]
      .filter(e => e.offsetWidth > 0).map(e => e.textContent.trim().slice(0, 26)).filter(Boolean).slice(0, 50)};
}
"""

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

BOARD_JS = (
    "() => { const b = document.querySelector('wc-chess-board'); if (!b) return {board:false};"
    " const g = b.game; return {board:true, id:b.id||null,"
    " fen: (g && g.getFEN) ? g.getFEN() : null,"
    " mn: (g && g.getMoveNumber) ? g.getMoveNumber() : null,"
    " color: (g && g.getPlayingColor) ? g.getPlayingColor() : null}; }"
)

CTA_JS = ("() => { const c = document.querySelector('button.bot-selection-cta-button-button');"
          " return !!(c && c.offsetWidth > 0); }")

ARROWS_JS = (
    "() => { const s = document.querySelector('wc-chess-board svg.hermes-arrows');"
    " if (!s) return null;"
    " return [...s.querySelectorAll('line')].map(l =>"
    "   [parseFloat(l.getAttribute('x1')), parseFloat(l.getAttribute('y1')),"
    "    parseFloat(l.getAttribute('x2')), parseFloat(l.getAttribute('y2'))]); }"
)


def rel_center(alg: str, flipped: bool) -> tuple[float, float]:
    f = ord(alg[0]) - 97
    rk = int(alg[1]) - 1
    col, row = (7 - f, rk) if flipped else (f, 7 - rk)
    return col + 0.5, row + 0.5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="9333")
    ap.add_argument("--engine", default=first_existing_engine())
    ap.add_argument("--movetime", type=int, default=500)
    ap.add_argument("--moves", type=int, default=6, help="max own moves to auto-play")
    ap.add_argument("--autoplay", action="store_true")
    ap.add_argument("--seconds", type=float, default=180.0)
    args = ap.parse_args()
    port = int(args.port)

    # ---- 1) get a game going
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)
        page = None
        page_errors = []
        for ctx in browser.contexts:
            for p in ctx.pages:
                try:
                    if ChessComReader._is_chess_url(p.url):
                        page = p
                except Exception as e:
                    page_errors.append(str(e)[:60])
        if page is None:
            extra = (" (page errors: " + "; ".join(page_errors[:3]) + ")") if page_errors else ""
            print("no chess.com page" + extra)
            return 1
        print("page:", page.url)

        def snapshot():
            try:
                st = page.evaluate(BOARD_JS)
                st["cta"] = page.evaluate(CTA_JS)
                return st
            except Exception as e:
                return {"err": str(e)[:70]}

        started = False
        st = snapshot()
        for _ in range(6):          # transient navigation errors are retried
            if "err" not in st:
                break
            time.sleep(1.5)
            st = snapshot()
        print("initial:", st)
        if st.get("fen") and st["fen"] != START_FEN:
            print("game already in progress - continuing it")
            started = True
        elif st.get("cta"):
            res = page.evaluate(FIND_PLAY_JS)
            if res.get("ok"):
                print(f"clicking Play at ({res['x']:.0f},{res['y']:.0f})")
                page.mouse.click(res["x"], res["y"])
            else:
                print("no Play CTA:", res)
            t0 = time.time()
            while time.time() - t0 < 25:
                st = snapshot()
                if "err" in st:          # page mid-navigation: not evidence of success
                    time.sleep(1.5)
                    continue
                # positive evidence: a visible board with a real position
                # (a fresh game legitimately sits at the start FEN, so the
                # CTA merely disappearing must not count as success)
                if st.get("board") and st.get("fen"):
                    started = True
                    break
                time.sleep(1.5)
            print("after click:", st)
        else:
            print("no CTA and no game; nothing to start")
        if not started:
            print("WARNING: could not confirm a live game - best effort, exit code will be 2")

        # ---- 2) run the analyzer (exact same class the GUI uses)
        opts = dict(
            port=str(port), engine=args.engine, threads=4, hash=256, multipv=5,
            movetime=args.movetime, depth=0, limit_strength=False, elo=1800,
            only_my_turn=True, arrows=True, arrows_n=5, poll_ms=600, retry_s=3.0,
        )
        events: queue.Queue = queue.Queue()
        az = Analyzer(events.put, opts)
        az.start()

        played = 0
        arrow_check = None
        last_best = None
        try:
            t0 = time.time()
            while time.time() - t0 < args.seconds:
                try:
                    ev = events.get(timeout=0.5)
                except queue.Empty:
                    continue
                k = ev.get("kind")
                if k == "log":
                    print(f"  [log] {ev['msg']}")
                elif k == "status":
                    print(f"  [status] {ev['msg']}")
                elif k == "suggestions":
                    print(f"  [SUGGEST] best={ev['best']} {ev['score']} depth={ev['depth']} {ev['elapsed']}ms")
                    for r in ev["rows"]:
                        print(f"            {r['n']}. {r['best']:<8} {r['score']:<7} "
                              f"[{r.get('class', '?')}] {r['line']}")
                    if not ev.get("fen"):
                        continue
                    best_uci = ev["rows"][0]["best_uci"] if ev["rows"] else None
                    last_best = {"fen": ev["fen"], "uci": best_uci}
                    turn = ev["fen"].split()[1]
                    my_color = az.my_color
                    if args.autoplay and best_uci and played < args.moves and (my_color is None or turn == my_color):
                        time.sleep(0.3)  # let the arrow injection land first
                        now_st = snapshot()
                        if now_st.get("fen") != ev["fen"]:
                            print(f"  [SKIP] board changed before click -> {now_st.get('fen')}")
                            continue
                        geo = page.evaluate(GEOMETRY_JS)
                        if geo:
                            frm, to = best_uci[0:2], best_uci[2:4]
                            p1 = square_center(geo, frm)
                            p2 = square_center(geo, to)
                            print(f"  [PLAY] {frm}->{to} click ({p1[0]:.0f},{p1[1]:.0f}) -> ({p2[0]:.0f},{p2[1]:.0f}) flipped={geo['flipped']}")
                            if arrow_check is None:
                                lines = page.evaluate(ARROWS_JS)
                                if lines:
                                    exp1 = rel_center(frm, geo["flipped"])
                                    exp2 = rel_center(to, geo["flipped"])
                                    got = lines[0]
                                    ok = (abs(got[0] - exp1[0]) < 0.05 and abs(got[1] - exp1[1]) < 0.05
                                          and abs(got[2] - exp2[0]) < 0.05 and abs(got[3] - exp2[1]) < 0.05)
                                    arrow_check = {"move": f"{frm}{to}", "expected": [list(exp1), list(exp2)],
                                                   "got": got, "match": ok, "n_arrows": len(lines)}
                                    print(f"  [ARROWS] expected {exp1}->{exp2}, overlay has {got} "
                                          f"({len(lines)} arrows) => {'MATCH' if ok else 'MISMATCH'}")
                            page.mouse.click(p1[0], p1[1])
                            time.sleep(0.45)
                            page.mouse.click(p2[0], p2[1])
                            time.sleep(0.8)
                            after = snapshot()
                            if "err" not in after and after.get("fen") and after["fen"] != ev["fen"]:
                                played += 1
                            else:
                                print(f"  [MISS] move {frm}->{to} did not register (board={after.get('fen')})")
                            time.sleep(0.4)
                elif k == "stopped":
                    print("  [analyzer stopped]")
                    break
                if args.autoplay and played >= args.moves:
                    time.sleep(2.0)
                    break

            # ---- 3) final checks
            time.sleep(0.5)
            st = snapshot()
            print("final board:", st)
            arrows = page.evaluate(ARROWS_JS)
            print("arrow overlay at end:", arrows)
            if arrow_check:
                print("arrow check:", arrow_check)
        finally:
            az.stop()
        print(f"done: played {played} own moves; last suggestion: {last_best}")
    return 0 if started else 2


if __name__ == "__main__":
    raise SystemExit(main())
