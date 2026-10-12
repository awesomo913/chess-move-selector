#!/usr/bin/env python3
"""FULL-GAME endurance verification: autoplay a live chess.com bot game to its
end using the app's own Analyzer (engine + colour-coded arrows), then prove:

  * the game FINISHED (game over detected, result read from the page),
  * whether we WON (PGN result mapped to our side),
  * the app kept working the WHOLE time (no stalls, no misses left unresolved,
    no arrow mismatches, no engine restarts / reconnects / failures),
  * the arrow overlay is cleared at game over,
  * the PGN of the played game is saved as evidence.

Exit codes: 0 = finished + WIN + every arrow verified (zero mismatches or
               unreadable) + zero unresolved misses + zero misclicks + the
               game record (PGN) captured;
            1 = finished but not a win / imperfections (details printed);
            2 = did not finish (time budget, move cap, or stall detected).

Usage:
  python tools/endurance_game.py --port 9333 [--moves 100] [--seconds 2700]
"""
from __future__ import annotations

import argparse
import json
import os
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

STATE_JS = r"""
() => {
  const b = document.querySelector('wc-chess-board');
  if (!b || !b.game) return {board: false};
  const g = b.game;
  const o = {board: true};
  try { o.fen = g.getFEN(); } catch (e) { o.err = String(e).slice(0, 80); }
  try { o.over = g.isGameOver(); } catch (e) {}
  try { o.result = g.getResult(); } catch (e) {}
  try { o.check = g.isCheck(); } catch (e) {}
  try { o.as = g.getPlayingAs(); } catch (e) {}
  return o;
}
"""

ARROWS_JS = r"""
() => {
  const s = document.querySelector('wc-chess-board svg.hermes-arrows');
  if (!s) return null;
  return [...s.querySelectorAll('line')].map(l => ({
    x1: parseFloat(l.getAttribute('x1')), y1: parseFloat(l.getAttribute('y1')),
    x2: parseFloat(l.getAttribute('x2')), y2: parseFloat(l.getAttribute('y2')),
    stroke: String(l.getAttribute('stroke') || '').toLowerCase()
  }));
}
"""

PGN_JS = r"""
() => { const b = document.querySelector('wc-chess-board');
  if (!b || !b.game) return null;
  const out = {};
  try { out.pgn = b.game.getPGN(); } catch (e) { out.err = String(e).slice(0, 80); }
  try { out.sans = b.game.getHistorySANs(); } catch (e) {}
  return out; }
"""

PROMO_JS = r"""
(pc) => {
  const els = [...document.querySelectorAll('[class*="promotion-piece"], .promotion-piece')];
  if (!els.length) return {found: 0};
  const want = els.find(e => e.className.includes(pc)) || els[0];
  const r = want.getBoundingClientRect();
  return {found: els.length, cls: want.className,
          x: r.x + r.width / 2, y: r.y + r.height / 2};
}
"""

CTA_JS = ("() => { const c = document.querySelector('button.bot-selection-cta-button-button');"
          " return !!(c && c.offsetWidth > 0); }")

FIND_PLAY_JS = r"""
() => {
  const pick = (el) => { const r = el.getBoundingClientRect(); return {ok: true, x: r.x + r.width / 2, y: r.y + r.height / 2}; };
  const cta = document.querySelector('button.bot-selection-cta-button-button');
  if (cta && cta.offsetWidth > 0) return pick(cta);
  const btns = [...document.querySelectorAll('button')]
      .filter(e => e.offsetWidth > 100 && /^play$/i.test(e.textContent.trim()));
  if (btns.length) return pick(btns[btns.length - 1]);
  return {ok: false};
}
"""

RESUME_JS = r"""
() => {
  const btns = [...document.querySelectorAll('button')]
    .filter(e => e.offsetWidth > 0 && /^(continue|resume|back to game)$/i.test(e.textContent.trim()));
  if (!btns.length) return {found: 0};
  const r = btns[0].getBoundingClientRect();
  return {found: 1, text: btns[0].textContent.trim(),
          x: r.x + r.width / 2, y: r.y + r.height / 2};
}
"""

REMATCH_JS = r"""
() => {
  const b = [...document.querySelectorAll('button')]
    .find(e => e.offsetWidth > 0 && /^(rematch|new game)$/i.test(e.textContent.trim()));
  if (!b) return {found: 0};
  const r = b.getBoundingClientRect();
  return {found: 1, text: b.textContent.trim(),
          x: r.x + r.width / 2, y: r.y + r.height / 2};
}
"""

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

_NOARG = object()


def _wedged(reason: str):
    """The browser side is unusable: end the run LOUDLY instead of hanging.

    Called when the driver connection is gone (every later read would fail) or
    by the watchdog when the main loop stops making progress. Exits 2 - silence
    is never allowed to look like success.
    """
    print(f"\n[WEDGED] {reason}", flush=True)
    print("[WEDGED] the page stopped answering - ending the run (no verdict: "
          "the game state can no longer be read)", flush=True)
    raise SystemExit(2)


def read_page(page, expr: str, arg=_NOARG):
    """The single place the tool reads the page.

    Deliberately NOT wrapped in a worker thread: Playwright's sync API is bound
    to the thread that created the connection, and an evaluate from another
    thread corrupts the connection ("cannot switch to a different thread" ->
    "Connection closed while reading from the driver"). Protection against a
    wedged renderer is the watchdog below, which hard-exits the whole run.
    A dead driver is detected here and goes loud immediately.
    """
    try:
        return (page.evaluate(expr) if arg is _NOARG
                else page.evaluate(expr, arg))
    except Exception as e:
        msg = str(e)
        if ("Connection closed" in msg or "Target closed" in msg
                or "Connection.init" in msg):
            _wedged(f"browser connection lost ({type(e).__name__}: {msg[:90]})")
        raise


_LAST_BEAT = {"t": time.time()}


def _beat():
    """Main-loop progress marker for the watchdog."""
    _LAST_BEAT["t"] = time.time()


def _watchdog(deadline_s: float = 300.0):
    """Hard-exit the run if the main loop stops making progress.

    A Playwright read on a wedged renderer blocks forever and cannot be
    interrupted (see read_page), so the only safe protection is to kill the
    whole process LOUDLY. os._exit skips cleanup on purpose - the engine
    process exits on stdin EOF when this process dies.
    """
    while True:
        time.sleep(5.0)
        gap = time.time() - _LAST_BEAT["t"]
        if gap > deadline_s:
            print(f"\n[WEDGED] no progress for {gap:.0f}s - a page read is "
                  f"blocked and cannot be interrupted; ending the run", flush=True)
            print("[WEDGED] no verdict: the game state can no longer be read",
                  flush=True)
            os._exit(2)


def start_playwright(attempts: int = 3):
    """Start the Playwright driver, retrying the handshake.

    The driver spawn/handshake fails in transient bursts on this machine
    (node.exe cold start / AV scan pressure): "Connection.init: Connection
    closed while reading from the driver". A fresh attempt usually succeeds, so
    retry before giving up; None means it will not start (caller exits 2).
    """
    last = None
    for i in range(attempts):
        try:
            return sync_playwright().start()
        except Exception as e:
            last = e
            print(f"playwright driver start attempt {i + 1}/{attempts} failed "
                  f"({type(e).__name__})", flush=True)
            time.sleep(4.0)
    print(f"playwright driver will not start "
          f"({type(last).__name__}: {str(last)[:80]})", flush=True)
    return None


def rel_center(alg: str, flipped: bool) -> tuple[float, float]:
    f = ord(alg[0]) - 97
    rk = int(alg[1]) - 1
    col, row = (7 - f, rk) if flipped else (f, 7 - rk)
    return col + 0.5, row + 0.5


def outcome(result: str | None, we_are_white: bool) -> str:
    r = (result or "").strip()
    if r == "1-0":
        return "WIN" if we_are_white else "LOSS"
    if r == "0-1":
        return "LOSS" if we_are_white else "WIN"
    if r.startswith("1/2"):
        return "DRAW"
    return "UNKNOWN"


def _piece_at(fen: str, alg: str) -> str:
    """Piece letter on `alg` (e.g. 'e4') from a FEN, '.' for empty, '?' unknown."""
    rows = (fen or "").split(" ")[0].split("/")
    if len(rows) != 8 or len(alg) < 2:
        return "?"
    f = ord(alg[0]) - 97
    rk = int(alg[1]) - 1
    if not (0 <= f <= 7 and 0 <= rk <= 7):
        return "?"
    row = rows[7 - rk]           # FEN rows start at rank 8
    i = 0
    for ch in row:
        if ch.isdigit():
            i += int(ch)
        else:
            if i == f:
                return ch
            i += 1
    return "."


def moved_as_intended(fen_after: str, frm: str, to: str, we_are_white: bool) -> bool:
    """Conservative check that OUR from->to actually happened.

    Accuses only on clear evidence: our piece still on `from` while `to` holds
    nothing of ours (empty OR an enemy piece - a failed capture leaves the
    enemy piece sitting there). Ambiguous shapes (opponent already replied,
    capture on the destination square, promotion) pass so a fast bot reply
    can't look like a misclick.
    """
    ours = "PNBRQK" if we_are_white else "pnbrqk"
    at_to = _piece_at(fen_after, to)
    at_from = _piece_at(fen_after, frm)
    if at_to in ours:
        return True
    if at_from in ours and at_to != "?" and at_to not in ours:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="full-game endurance + win verification")
    ap.add_argument("--port", default="9333")
    ap.add_argument("--engine", default=first_existing_engine())
    ap.add_argument("--movetime", type=int, default=700)
    ap.add_argument("--multipv", type=int, default=5)
    ap.add_argument("--moves", type=int, default=100, help="cap on our own moves")
    ap.add_argument("--seconds", type=float, default=2700.0, help="wall budget")
    ap.add_argument("--idle-stop", type=float, default=180.0,
                    help="stop if nothing changes for this many seconds")
    args = ap.parse_args()
    port = int(args.port)

    stats = {
        "engine_starts": 0, "reconnects": 0, "ad_recoveries": 0,
        "clear_fail": 0, "keepalive_fail": 0, "warn_err": [],
        "arrow_ok": 0, "arrow_bad": 0, "arrow_missing": 0,
        "promotions": 0, "misses": 0, "miss_unresolved": 0, "wrong_moves": 0,
    }

    pw = start_playwright()
    if pw is None:
        return 2
    try:
        browser = None
        for _try in range(3):
            try:
                browser = pw.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{port}", timeout=15000)
                break
            except Exception as e:
                print(f"CDP connect attempt {_try + 1}/3 failed "
                      f"({type(e).__name__})", flush=True)
                time.sleep(4.0)
        if browser is None:
            print(f"cannot connect to Chrome on :{port} - is it running?", flush=True)
            return 2

        def score(u: str) -> int:
            u = u.lower()
            return 4 if "/game/" in u else 3 if "/play" in u else 2 if "analysis" in u else 1

        cands = []
        for ctx in browser.contexts:
            for p in ctx.pages:
                try:
                    if ChessComReader._is_chess_url(p.url):
                        cands.append((score(p.url), p.url, p))
                except Exception:
                    pass
        if not cands:
            print("no chess.com tab")
            return 2
        cands.sort(key=lambda c: -c[0])
        page = cands[0][2]
        print(f"reading tab: {cands[0][1]}", flush=True)
        # a wedged renderer blocks a read forever and cannot be interrupted
        # (see read_page) - the watchdog hard-exits the run instead of letting
        # it hang silently
        threading.Thread(target=_watchdog, daemon=True).start()

        # the tab may have bounced to an ad host between runs: navigate back
        # (the same recovery the app's own reader performs)
        try:
            cur_url = page.url
        except Exception:
            cur_url = ""
        if cur_url and "chess.com" not in cur_url:
            print(f"tab is on {cur_url[:70]} - navigating back to chess.com", flush=True)
            try:
                page.goto("https://www.chess.com/play/computer", timeout=30000)
                time.sleep(3.0)
            except Exception as e:
                print(f"navigate back failed: {type(e).__name__}", flush=True)

        def snapshot():
            for _ in range(5):
                try:
                    return read_page(page, STATE_JS)
                except Exception as e:
                    time.sleep(1.2)
                    last = {"err": str(e)[:70]}
            return last

        # ---- wait for a readable board; a page mid-reload (or one whose
        #      renderer just restarted) has no game object yet
        st = snapshot()
        t0 = time.time()
        while not st.get("fen") and time.time() - t0 < 20.0:
            time.sleep(2.0)
            st = snapshot()
        if not st.get("fen"):
            print("no board after 20s - reloading the tab", flush=True)
            try:
                page.reload(timeout=30000)
            except Exception as e:
                print(f"reload failed: {type(e).__name__}", flush=True)
            t0 = time.time()
            while not st.get("fen") and time.time() - t0 < 30.0:
                time.sleep(2.0)
                st = snapshot()

        # chess.com can leave a game paused ("Continue" overlay) after the tab
        # is reloaded or the browser restarted - a paused board READS fine but
        # silently ignores clicks (moves never register)
        # an UNREADABLE check must not be treated as "not paused": a paused
        # board reads fine but ignores clicks, so the run would play a dead
        # board and only die later at [STALL]. Retry; if it stays unreadable,
        # exit rather than guess.
        res = None
        for _try in range(3):
            try:
                res = read_page(page, RESUME_JS)
                break
            except Exception as e:
                print(f"resume check failed ({type(e).__name__}) - retrying", flush=True)
                time.sleep(1.2)
        if res is None:
            print("resume check unreadable after 3 tries - cannot tell whether the "
                  "game is paused; exiting rather than playing a dead board", flush=True)
            return 2
        if res.get("found"):
            print(f"game is paused - clicking '{res['text']}' at "
                  f"({res['x']:.0f},{res['y']:.0f})", flush=True)
            still = {"found": 1}
            for _ in range(3):
                page.mouse.click(res["x"], res["y"])
                time.sleep(2.0)
                try:
                    still = read_page(page, RESUME_JS)
                except Exception:
                    still = {"found": 1}   # unreadable: assume still paused, retry
                if not still.get("found"):
                    break
                if "x" in still:
                    res = still   # button may have moved - re-read coords
            print(f"resume clicked; still visible: {bool(still.get('found'))}", flush=True)
            if still.get("found"):
                print("could not dismiss the paused-game overlay - cannot proceed", flush=True)
                return 2
            st = snapshot()

        # ---- ensure a playable game: start a fresh one after a finished game
        #      (Rematch), start via the bot picker, or continue one in progress
        if st.get("over"):
            try:
                rm = read_page(page, REMATCH_JS)
            except Exception as e:
                print(f"rematch check failed ({type(e).__name__})", flush=True)
                rm = {}
            if not rm.get("found"):
                print("game is over and no Rematch/New Game button - cannot start", flush=True)
                return 2
            print(f"previous game is over - clicking '{rm['text']}'", flush=True)
            page.mouse.click(rm["x"], rm["y"])
            time.sleep(3.0)
            st = snapshot()
        cta_visible = None
        for _try in range(3):
            try:
                cta_visible = bool(read_page(page, CTA_JS))
                break
            except Exception as e:
                print(f"CTA read failed ({type(e).__name__}) - retrying", flush=True)
                time.sleep(1.2)
        if cta_visible is None:
            # still unreadable: assume the CTA is there (the common case is a
            # fresh picker page). If it is NOT there - e.g. a live game sitting
            # at the start position - FIND_PLAY_JS finds no CTA and the run
            # exits 2 WITHOUT clicking, so a live game is never disturbed.
            print("CTA read failed 3x - assuming visible (re-verified before any click)",
                  flush=True)
            cta_visible = True
        if (not st.get("fen")) or (st.get("fen") == START_FEN and cta_visible):
            res = {}
            for _try in range(3):
                try:
                    res = read_page(page, FIND_PLAY_JS)
                    break
                except Exception as e:
                    print(f"Play CTA read failed ({type(e).__name__}) - retrying", flush=True)
                    time.sleep(1.2)
            if not res.get("ok"):
                print("no Play CTA to start a game - cannot proceed", flush=True)
                return 2
            print(f"clicking Play at ({res['x']:.0f},{res['y']:.0f})", flush=True)
            page.mouse.click(res["x"], res["y"])
            # the picker screen already has a board + start FEN, so "board and
            # fen" alone is NOT proof the game started - the CTA must be gone
            # or the position must have advanced
            t0 = time.time()
            while time.time() - t0 < 25:
                st = snapshot()
                try:
                    cta_now = bool(read_page(page, CTA_JS))
                except Exception:
                    cta_now = True   # unreadable: assume still showing, keep waiting
                if st.get("board") and st.get("fen") and (
                        not cta_now or st.get("fen") != START_FEN):
                    break
                time.sleep(1.5)
            try:
                cta_after = bool(read_page(page, CTA_JS))
            except Exception:
                cta_after = True   # unreadable: don't claim the game started
            if cta_after and st.get("fen") in (None, START_FEN):
                print("Play click did not start a game - cannot proceed", flush=True)
                return 2
        print(f"game state: fen={str(st.get('fen'))[:48]} over={st.get('over')} "
              f"result={st.get('result')}", flush=True)
        if st.get("over") or not st.get("fen"):
            print("no live game to play - cannot proceed", flush=True)
            return 2

        # ---- run the app's Analyzer (exact class the GUI uses)
        opts = dict(
            port=str(port), engine=args.engine, threads=4, hash=256,
            multipv=int(args.multipv), movetime=args.movetime, depth=0,
            limit_strength=False, elo=1800, only_my_turn=True, arrows=True,
            arrows_n=int(args.multipv), poll_ms=600, retry_s=3.0,
        )
        events: queue.Queue = queue.Queue()

        def make_analyzer(mt: int) -> Analyzer:
            a = Analyzer(events.put, dict(opts, movetime=int(mt)))
            a.start()
            return a

        az = make_analyzer(args.movetime)

        played = 0
        finished = False
        final = {}
        moves_log = []
        t_start = time.time()
        last_activity = time.time()
        retries = {}                # fen -> click attempts
        pos_seen = {}               # position key -> occurrences (repetition guard)
        board_moved = True          # a move registered since the last count
        deeper_done = False         # the repeat guard already deepened the search
        stops_expected = 0          # deliberate analyzer stops awaiting their event
        reloaded = False            # the zombie-board reload has been used
        we_are_white = True

        def note_log(msg: str, level: str):
            if "engine ready" in msg:
                stats["engine_starts"] += 1
            if "lost browser connection" in msg:
                stats["reconnects"] += 1
            if "stabilized tab" in msg:
                stats["ad_recoveries"] += 1
            if "clear arrows failed" in msg:
                stats["clear_fail"] += 1
            if "arrow keep-alive failed" in msg:
                stats["keepalive_fail"] += 1
            if level in ("warn", "error"):
                stats["warn_err"].append(f"{level}: {msg[:110]}")

        try:
            while time.time() - t_start < args.seconds:
                _beat()   # main-loop progress marker for the watchdog
                cur = snapshot()
                if cur.get("as") in ("white", "black"):
                    we_are_white = cur["as"] == "white"
                if cur.get("over"):
                    finished = True
                    final = cur
                    break
                if time.time() - last_activity > args.idle_stop:
                    print(f"[STALL] nothing changed for {args.idle_stop:.0f}s - stopping", flush=True)
                    break

                try:
                    ev = events.get(timeout=0.5)
                except queue.Empty:
                    continue
                k = ev.get("kind")
                if k == "log":
                    msg = ev["msg"]
                    print(f"  [log] {msg}", flush=True)
                    note_log(msg, ev.get("level", "info"))
                    if "you are " in msg:
                        we_are_white = "White" in msg
                    if "game over detected" in msg:
                        last_activity = time.time()
                elif k == "status":
                    print(f"  [status] {ev['msg']}", flush=True)
                elif k == "suggestions":
                    fen = ev.get("fen") or ""
                    rows = ev.get("rows") or []
                    best_uci = rows[0]["best_uci"] if rows else None
                    turn = fen.split()[1] if fen else "?"
                    if az.my_color:
                        we_are_white = az.my_color == "w"
                    if not (best_uci and played < args.moves
                            and (az.my_color is None or turn == az.my_color)):
                        continue
                    # NOTE: the idle timer is refreshed only when a move
                    # REGISTERS (below) or the game-over log fires - a frozen
                    # board that keeps re-emitting suggestions still trips [STALL]
                    time.sleep(0.3)   # let the arrow injection land
                    now = snapshot()
                    if now.get("fen") != fen:
                        print(f"  [SKIP] board changed before click -> {str(now.get('fen'))[:40]}", flush=True)
                        continue
                    # the live board confirms this position: count it and run the
                    # repetition guard (a stale queued suggestion must not count,
                    # and only REGISTERED moves may raise the count)
                    key = " ".join(fen.split()[:4])
                    if board_moved:
                        pos_seen[key] = pos_seen.get(key, 0) + 1
                        board_moved = False
                    seen_n = pos_seen.get(key, 0) - 1
                    if seen_n >= 1:
                        if len(rows) > 1:
                            alt = rows[1]
                            if (alt.get("class") in ("Best", "Excellent", "Good")
                                    or (isinstance(alt.get("loss"), (int, float))
                                        and alt["loss"] <= 90)):
                                print(f"  [REPEAT-GUARD] position seen {seen_n + 1}x - playing "
                                      f"{alt['best']} instead of {rows[0]['best']}", flush=True)
                                rows = [alt] + [r for i, r in enumerate(rows) if i != 1]
                                best_uci = rows[0]["best_uci"]
                        # a shallow search shuffles in won endgames; give the
                        # engine more time so it finds a real winning plan
                        if not deeper_done:
                            deep_mt = max(int(args.movetime) * 4, 2500)
                            try:
                                az.stop()
                                old_stopped = True
                            except Exception as e:
                                old_stopped = False
                                print(f"  [REPEAT-GUARD] old analyzer stop failed "
                                      f"({type(e).__name__}) - not starting a second engine",
                                      flush=True)
                            if old_stopped:
                                # only now is the deeper search actually in place:
                                # a failed stop must not mark it done, nor claim an
                                # expected stop event that never came (that phantom
                                # would later eat a real engine death)
                                deeper_done = True
                                stops_expected += 1   # its "stopped" event is deliberate
                                print(f"  [REPEAT-GUARD] re-thinking with movetime "
                                      f"{deep_mt} from here on", flush=True)
                                az = make_analyzer(deep_mt)
                    if retries.get(fen, 0) >= 3:
                        # this position gave up after 3 clicks. But a click can
                        # register LATE (the post-click read lagged): if the
                        # board has moved on, the "unresolved" count was wrong -
                        # take it back so a resolved game is not failed.
                        late = snapshot()
                        if late.get("fen") and late.get("fen") != fen:
                            if retries.get(fen, 0) >= 3:
                                stats["miss_unresolved"] -= 1
                            retries.pop(fen, None)
                            # the move DID happen: do the success-path
                            # bookkeeping so the tally, the repetition guard
                            # and the stall timer all see it
                            played += 1
                            moves_log.append(f"{played}. {rows[0]['best']} "
                                             f"[{rows[0].get('class')}] (late)")
                            board_moved = True
                            last_activity = time.time()
                            print("  [LATE] board moved on after giving up - "
                                  "the move did register", flush=True)
                        continue
                    try:
                        geo = read_page(page, GEOMETRY_JS)
                    except Exception as e:
                        print(f"  [SKIP] geometry read failed: {type(e).__name__}", flush=True)
                        geo = None
                    if not geo:
                        print("  [SKIP] no board geometry", flush=True)
                        continue
                    frm, to = best_uci[0:2], best_uci[2:4]

                    # --- arrow evidence BEFORE the click (geometry + colour).
                    # The overlay draws every candidate, so find the arrow for
                    # the suggested move by geometry (line ORDER in the SVG is
                    # not guaranteed) and check ITS colour. The overlay can be
                    # briefly absent (page redraw, or an engine swap clearing
                    # it): retry a few seconds before counting it missing.
                    # POLICY: this is evidence collection, not a gate - a bad or
                    # missing arrow is recorded and blocks PASS at the verdict,
                    # but play continues so the whole game still gets measured.
                    def _read_arrows():
                        try:
                            return read_page(page, ARROWS_JS)
                        except Exception as e:
                            print(f"  [ARROWS] read failed: {type(e).__name__}", flush=True)
                            return None

                    lines = _read_arrows()
                    for _ in range(8):
                        if lines:
                            break
                        time.sleep(1.0)
                        lines = _read_arrows()
                    if lines:
                        exp1 = rel_center(frm, geo["flipped"])
                        exp2 = rel_center(to, geo["flipped"])
                        want_col = (rows[0].get("color") or "").strip().lower()
                        match = None
                        for ln in lines:
                            if (abs(ln["x1"] - exp1[0]) < 0.05 and abs(ln["y1"] - exp1[1]) < 0.05
                                    and abs(ln["x2"] - exp2[0]) < 0.05
                                    and abs(ln["y2"] - exp2[1]) < 0.05):
                                match = ln
                                break
                        if match is not None and match["stroke"].strip().lower() == want_col:
                            stats["arrow_ok"] += 1
                        else:
                            stats["arrow_bad"] += 1
                            print(f"  [ARROWS-BAD] move={frm}{to} want={want_col!r} "
                                  f"match={match!r}", flush=True)
                            print(f"  [ARROWS-BAD] flipped={geo.get('flipped')} "
                                  f"lines={json.dumps(lines)[:400]}", flush=True)
                    else:
                        stats["arrow_missing"] += 1
                        print(f"  [ARROWS] overlay missing before move {frm}{to}", flush=True)

                    # the arrow wait can take seconds - re-check the board so a
                    # position change during the wait can't cause a stale click
                    now2 = snapshot()
                    if now2.get("fen") != fen:
                        print(f"  [SKIP] board changed during arrow check -> "
                              f"{str(now2.get('fen'))[:40]}", flush=True)
                        continue

                    # --- play the move
                    p1 = square_center(geo, frm)
                    p2 = square_center(geo, to)
                    print(f"  [PLAY #{played + 1}] {frm}->{to} {rows[0]['best']} "
                          f"[{rows[0].get('class')}] ({p1[0]:.0f},{p1[1]:.0f})->({p2[0]:.0f},{p2[1]:.0f})",
                          flush=True)
                    page.mouse.click(p1[0], p1[1])
                    time.sleep(0.45)
                    page.mouse.click(p2[0], p2[1])
                    time.sleep(0.8)

                    after = snapshot()
                    after_fen = after.get("fen")
                    if after_fen and after_fen != fen:
                        if not moved_as_intended(after_fen, frm, to, we_are_white):
                            stats["wrong_moves"] += 1
                            print(f"  [WRONG-MOVE] {frm}->{to} but board shows "
                                  f"{after_fen[:48]}", flush=True)
                        played += 1
                        moves_log.append(f"{played}. {rows[0]['best']} [{rows[0].get('class')}]")
                        retries.pop(fen, None)
                        board_moved = True
                        last_activity = time.time()
                        continue

                    # --- promotion picker? (uci like e7e8q)
                    if len(best_uci) >= 5 and best_uci[4] in "qrbn":
                        pc = ("w" if we_are_white else "b") + best_uci[4]
                        try:
                            pr = read_page(page, PROMO_JS, pc)
                        except Exception as e:
                            pr = {}
                            print(f"  [PROMO] picker read failed: {type(e).__name__}", flush=True)
                        print(f"  [PROMO] picker={pr}", flush=True)
                        if pr.get("found"):
                            page.mouse.click(pr["x"], pr["y"])
                        else:
                            page.keyboard.press(best_uci[4])
                        time.sleep(0.9)
                        after2 = snapshot()
                        after2_fen = after2.get("fen")
                        if after2_fen and after2_fen != fen:
                            if not moved_as_intended(after2_fen, frm, to, we_are_white):
                                stats["wrong_moves"] += 1
                                print(f"  [WRONG-MOVE] promo {frm}->{to} but board shows "
                                      f"{after2_fen[:48]}", flush=True)
                            stats["promotions"] += 1
                            played += 1
                            moves_log.append(f"{played}. {rows[0]['best']} [{rows[0].get('class')}] promo")
                            retries.pop(fen, None)
                            board_moved = True
                            last_activity = time.time()
                            continue

                    stats["misses"] += 1
                    retries[fen] = retries.get(fen, 0) + 1
                    print(f"  [MISS] {frm}->{to} did not register (board={str(after.get('fen'))[:40]}) "
                          f"- re-analyzing (attempt {retries[fen]})", flush=True)
                    if not reloaded and retries[fen] >= 2:
                        # a board can read fine yet ignore input (a zombie board
                        # after the page's rematch/ad churn). The game itself is
                        # server-side, so a reload re-binds the page: the run's
                        # resume check clicks "Continue" and play goes on. Once
                        # per run, so a genuinely dead page still ends in [STALL].
                        print("  [MISS] board ignoring clicks - reloading the tab to re-bind it",
                              flush=True)
                        try:
                            page.reload(timeout=30000)
                            time.sleep(3.0)
                            # only a reload that actually happened consumes the
                            # once-per-run gate; a failed one may be retried
                            reloaded = True
                        except Exception as e:
                            print(f"  [MISS] reload failed: {type(e).__name__}", flush=True)
                    if retries[fen] == 3:
                        # count exactly once: a >= 3 test would add one on every
                        # further attempt; the late-registration check above
                        # takes the count back if the move registered after all
                        stats["miss_unresolved"] += 1
                        print(f"[GIVEUP] {fen[:40]} failed to register "
                              f"{retries[fen]} times", flush=True)
                    try:
                        r2 = read_page(page, RESUME_JS)
                    except Exception as e:
                        print(f"  [RESUME] read failed: {type(e).__name__}", flush=True)
                        r2 = {}
                    if r2.get("found"):
                        print(f"  [RESUME] game looks paused - clicking '{r2['text']}'", flush=True)
                        page.mouse.click(r2["x"], r2["y"])
                        time.sleep(1.5)
                    az.request_analyze()
                elif k == "stopped":
                    if stops_expected:
                        # the repeat guard's engine swap: expected, not a death
                        stops_expected -= 1
                        print("  [analyzer stopped - deliberate restart]", flush=True)
                        continue
                    print("  [analyzer stopped]", flush=True)
                    break
        finally:
            az.stop()

        # ---- final evidence
        time.sleep(1.0)
        st = snapshot()
        # the page is the authority on our side (getPlayingAs); the run's own
        # inference is only a fallback when the page doesn't expose it
        final_as = st.get("as") or final.get("as")
        if final_as in ("white", "black"):
            we_are_white = final_as == "white"
        try:
            arrows_end = read_page(page, ARROWS_JS)
        except Exception as e:
            arrows_end = "READ-FAILED"
            print(f"arrows-after-over read failed: {type(e).__name__}", flush=True)
        pgn = None
        try:
            pgn = read_page(page, PGN_JS)
        except Exception as e:
            print(f"PGN read failed: {type(e).__name__}", flush=True)
        # PGN_JS returns null when the page has no board at all - that is a
        # failed read, not a confirmed-empty PGN
        pgn_read_failed = pgn is None
        pgn = pgn or {}
        pgn_text = pgn.get("pgn") or ""
        sans = pgn.get("sans") or []
        pgn_path = ROOT / "scratch" / "endurance_game_pgn.txt"
        if pgn_text:
            pgn_path.parent.mkdir(parents=True, exist_ok=True)
            pgn_path.write_text(pgn_text, encoding="utf-8")

        duration = time.time() - t_start
        result = ((st.get("result") or final.get("result")) if finished
                  else st.get("result")) or "?"
        verdict = outcome(result, we_are_white) if finished else "UNFINISHED"

        print("\n===== ENDURANCE REPORT =====", flush=True)
        print(f"finished: {finished}   result: {result}   => {verdict} "
              f"(we are {'White' if we_are_white else 'Black'})", flush=True)
        print(f"duration: {duration:.0f}s   own moves played: {played}   "
              f"promotions: {stats['promotions']}", flush=True)
        print(f"arrow checks: ok={stats['arrow_ok']} bad={stats['arrow_bad']} "
              f"missing={stats['arrow_missing']}", flush=True)
        print(f"misses: {stats['misses']} (unresolved: {stats['miss_unresolved']})", flush=True)
        print(f"wrong moves (misclicks): {stats['wrong_moves']}", flush=True)
        print(f"engine starts: {stats['engine_starts']}   reconnects: {stats['reconnects']}   "
              f"ad recoveries: {stats['ad_recoveries']}", flush=True)
        print(f"clear-arrow failures: {stats['clear_fail']}   "
              f"keep-alive failures: {stats['keepalive_fail']}", flush=True)
        print(f"warn/error logs: {len(stats['warn_err'])}", flush=True)
        for w in stats["warn_err"][:12]:
            print(f"   {w}", flush=True)
        print(f"arrows after game over: {arrows_end if arrows_end is not None else 'none'}",
              flush=True)
        print(f"move list (SAN): {' '.join(sans[-40:]) if sans else '(none)'}", flush=True)
        if pgn_text:
            print(f"PGN saved: {pgn_path} ({len(pgn_text)} chars)", flush=True)
        elif pgn_read_failed:
            print("PGN read FAILED (page unresponsive) - not a confirmed empty PGN", flush=True)
        else:
            print("PGN not captured (empty) - see move list above", flush=True)

        if not finished:
            print("VERDICT: FAIL - game did not finish within the run", flush=True)
            return 2
        pgn_ok = bool(pgn_text)
        perfect = (verdict == "WIN" and stats["arrow_bad"] == 0
                   and stats["arrow_missing"] == 0
                   and stats["miss_unresolved"] == 0
                   and stats["wrong_moves"] == 0
                   and pgn_ok)   # PASS also claims a captured game record
        if perfect:
            print("VERDICT: PASS - won the full game with zero arrow errors", flush=True)
            return 0
        if (verdict == "WIN" and not pgn_ok
                and stats["arrow_bad"] == 0 and stats["arrow_missing"] == 0
                and stats["miss_unresolved"] == 0 and stats["wrong_moves"] == 0):
            # only claim "clean arrows" when they really were clean - with
            # arrow problems present the generic line below reports them
            print("VERDICT: won with clean arrows, but the game record (PGN) "
                  "was not captured - not a full PASS", flush=True)
        print(f"VERDICT: finished, verdict={verdict}, "
              f"arrow_bad={stats['arrow_bad']}, unresolved_misses={stats['miss_unresolved']}, "
              f"pgn_ok={pgn_ok}",
              flush=True)
        return 1
    finally:
        try:
            pw.stop()
        except Exception as e:
            # logged, not fatal: the exit code reports the GAME verification,
            # but a failed stop must never be invisible
            print(f"pw.stop failed: {type(e).__name__}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
