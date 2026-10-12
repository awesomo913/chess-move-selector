#!/usr/bin/env python3
"""Dev/ops probe for the chess.com CDP reader.

Examples:
  python tools/probe.py dump  --port 9333          # raw board state JSON
  python tools/probe.py dom   --port 9333          # DOM details (squares, pieces, z-index)
  python tools/probe.py arrows e2e4 d2d4 --port 9333
  python tools/probe.py clear --port 9333
  python tools/probe.py click e2 e4 --port 9333
  python tools/probe.py nav https://www.chess.com/play/computer --port 9333
  python tools/probe.py tabs  --port 9333
  python tools/probe.py js "document.title" --port 9333
  python tools/probe.py wait 1500 --port 9333
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from playwright.sync_api import sync_playwright  # noqa: E402

from board_reader import INJECT_JS, ChessComReader  # noqa: E402

DOM_JS = r"""
() => {
  const all = [...document.querySelectorAll('wc-chess-board')];
  const vis = all.filter(b => b.offsetWidth > 100 && b.offsetHeight > 100);
  vis.sort((a, b) => b.offsetWidth * b.offsetHeight - a.offsetWidth * a.offsetHeight);
  const b = vis[0];
  if (!b) return {err: 'no visible board', allBoards: all.length};
  const r = b.getBoundingClientRect();
  const sq = {};
  let n = 0;
  for (const el of b.querySelectorAll('[class*="square-"]')) {
    const m = /square-(\d)(\d)/.exec(el.className);
    if (m && !sq[m[1] + m[2]] && n < 250) {
      const q = el.getBoundingClientRect();
      sq[m[1] + m[2]] = [Math.round(q.x - r.x + q.width / 2), Math.round(q.y - r.y + q.height / 2),
                         Math.round(q.width), Math.round(q.height), String(el.className).slice(0, 60)];
      n++;
    }
  }
  const pieces = [...b.querySelectorAll('.piece')].slice(0, 20).map(p => {
    const q = p.getBoundingClientRect();
    return {cls: String(p.className).slice(0, 70),
            cx: Math.round(q.x - r.x + q.width / 2), cy: Math.round(q.y - r.y + q.height / 2)};
  });
  const kids = [...b.children].map(el => ({
    tag: el.tagName.toLowerCase(),
    cls: String((el.className && el.className.baseVal !== undefined) ? el.className.baseVal : (el.className || '')),
    z: getComputedStyle(el).zIndex, pos: getComputedStyle(el).position,
    w: el.offsetWidth, h: el.offsetHeight
  }));
  let flipped = null;
  try { if (typeof b.flipped === 'boolean') flipped = b.flipped; } catch (e) {}
  let hasGame = false, fen = null;
  try {
    const g = b.game;
    hasGame = !!g;
    if (g && typeof g.getFEN === 'function') fen = g.getFEN();
  } catch (e) {}
  return {rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
          flipped: flipped, hasGame: hasGame, fen: fen,
          squareCount: Object.keys(sq).length, squares: sq, pieces: pieces,
          children: kids, ownSvg: !!b.querySelector(':scope > svg.hermes-arrows')};
}
"""

CLICK_PROBE_JS = r"""
() => {
  const all = [...document.querySelectorAll('wc-chess-board')];
  const vis = all.filter(b => b.offsetWidth > 100 && b.offsetHeight > 100);
  vis.sort((a, b) => b.offsetWidth * b.offsetHeight - a.offsetWidth * a.offsetHeight);
  const b = vis[0];
  if (!b) return null;
  const r = b.getBoundingClientRect();
  let flipped = false;
  try { if (typeof b.flipped === 'boolean') flipped = b.flipped; } catch (e) {}
  return {x: r.x, y: r.y, w: r.width, h: r.height, flipped: flipped};
}
"""


def out(obj, limit=6000):
    s = json.dumps(obj, indent=2, default=str)
    print(s[:limit] + ("\n... (truncated)" if len(s) > limit else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["dump", "dom", "arrows", "clear", "click", "nav", "tabs", "js", "wait"])
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--port", default="9333")
    args = ap.parse_args()
    port = int(args.port)

    if args.cmd == "dump":
        r = ChessComReader(port=port)
        r.connect()
        try:
            page = r.find_page()
            print("tab:", r.page_title or "(none)", "|", page.url if page else "")
            st = r.read()
            out(st.__dict__)
        finally:
            r.close()
        return

    if args.cmd == "tabs":
        with sync_playwright() as pw:
            br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=10000)
            for ctx in br.contexts:
                for p in ctx.pages:
                    try:
                        print(f"{p.url}\n    title: {p.title()}")
                    except Exception as e:
                        print("(dead page)", e)
        return

    r = ChessComReader(port=port)
    r.connect()
    try:
        page = r.find_page()
        if page is None:
            # dev probe: only ever used against the scratch browser
            ctx = r._browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if page is None:
            print("no page found")
            return
        print("tab:", r.page_title or "(no title)", "|", page.url)

        if args.cmd == "dom":
            out(page.evaluate(DOM_JS), limit=9000)
        elif args.cmd == "js":
            out(page.evaluate(args.rest[0]) if args.rest else None)
        elif args.cmd == "wait":
            page.wait_for_timeout(int(args.rest[0]) if args.rest else 1000)
            print("waited")
        elif args.cmd == "arrows":
            arrows = []
            for i, spec in enumerate(args.rest):
                arrows.append({"from": spec[0:2], "to": spec[2:4],
                               "color": ["#2ecc71", "#3aa0ff", "#ffc233"][i % 3]})
            print(page.evaluate(INJECT_JS, {"arrows": arrows}))
        elif args.cmd == "clear":
            print(page.evaluate(INJECT_JS, {"arrows": []}))
        elif args.cmd == "click":
            if len(args.rest) < 2:
                print("usage: click e2 e4")
                return
            geo = page.evaluate(CLICK_PROBE_JS)
            if not geo:
                print("no board geometry")
                return
            size = geo["w"] / 8.0
            for alg in args.rest:
                f = ord(alg[0]) - 97
                rk = int(alg[1]) - 1
                col, row = (7 - f, rk) if geo["flipped"] else (f, 7 - rk)
                x = geo["x"] + (col + 0.5) * size
                y = geo["y"] + (row + 0.5) * size
                print(f"click {alg} at ({x:.0f},{y:.0f}) flipped={geo['flipped']}")
                page.mouse.click(x, y)
                page.wait_for_timeout(450)
        elif args.cmd == "nav":
            if not args.rest:
                print("usage: nav URL")
                return
            page.goto(args.rest[0], wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            print("navigated ->", page.url)
    finally:
        r.close()


if __name__ == "__main__":
    main()
