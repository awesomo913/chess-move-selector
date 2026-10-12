"""Read a live chess.com board out of a Chrome tab over CDP, and draw arrows.

Design notes
------------
* The position comes straight from chess.com's own board object
  (`document.querySelector('wc-chess-board').game.getFEN()`), so castling
  rights / en-passant / side-to-move are exact.
* This module never opens or closes tabs, and never navigates on its own -
  with TWO deliberate exceptions, both under the opt-out `auto_recover`: an
  ad-bounced chess.com tab is navigated back to the last chess.com URL, and a
  GAME/PLAY page whose board element has vanished for several reads in a row
  is reloaded (a board-less game page is never a normal state - e.g. an ad
  document that kept the chess.com URL, or a broken SPA - and would otherwise
  strand the reader on "no wc-chess-board" forever). `open_tab_in_chrome()`
  stays reserved for explicit user actions.
* Arrows are drawn as an SVG overlay appended to the board element, using the
  board's own coordinate space via viewBox "0 0 8 8" (1 unit = 1 square).
* Board orientation is detected by majority vote over the board's own piece
  classes (`piece wr square-11` = white rook on a1): chess.com animates pieces
  by transform, and a throttled/background tab can freeze a piece mid-flight,
  so single-element sampling is not reliable.
* Pixel positions of squares are ALWAYS computed from the board rect + size +
  orientation (never from piece element rects, for the same reason).
"""
from __future__ import annotations

import urllib.parse
import urllib.request
from dataclasses import dataclass

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

ARROW_CLASS = "hermes-arrows"

# Ad-tech hosts observed bouncing the chess.com tab onto ad "user-sync"
# redirect URLs (guest pages in a fresh profile). Traffic to these is aborted
# only when the caller asks to "stabilize" the page.
AD_BLOCK_SUBSTRINGS = (
    "rubiconproject", "seedtag", "criteo", "pubmatic", "openx.net", "admanmedia",
    "undertone", "onetag-sys", "cootlogix", "a-mo.net", "presage.io", "eskimi",
    "drift-pixel", "vdo.ai", "nextmillmedia", "plusoneads", "thrtle.com",
    "ads-tinyorbit", "ingage.tech", "omnitagjs", "yellowblue", "doubleclick.net",
    "googlesyndication", "googleadservices", "adnxs.com", "amazon-adsystem",
    "taboola", "outbrain", "scorecardresearch", "quantserve", "casalemedia",
    "sharethrough", "sovrn", "lijit", "teads", "smartadserver", "33across",
    "bidswitch", "triplelift", "3lift", "indexww", "2mdn.net", "gumgum",
    "yieldmo", "adsrvr", "crwdcntrl",
    "demdex", "everesttech", "agkn.com", "rlcdn.com", "adsafeprotected",
    "moatads", "doubleverify", "iasds01", "disqus.com",
)

# Shared JS fragment: works out the board orientation into `flipped`.
# Requires local vars: b (board element), r (its client rect), sx, sy (square size).
_FLIP_DETECT_JS = r"""
  let flipped = null;
  try { if (typeof b.flipped === 'boolean') flipped = b.flipped; } catch (e) {}
  if (flipped === null) {
    let votesUn = 0, votesFl = 0, sampled = 0;
    for (const el of b.querySelectorAll('[class*="square-"]')) {
      const m = /square-(\d)(\d)/.exec(String(el.className));
      if (!m) continue;
      const file = +m[1] - 1, rank = +m[2] - 1;
      const q = el.getBoundingClientRect();
      const col = (q.x + q.width / 2 - r.x) / sx - 0.5;
      const row = (q.y + q.height / 2 - r.y) / sy - 0.5;
      const dUn = Math.abs(col - file) + Math.abs(row - (7 - rank));
      const dFl = Math.abs(col - (7 - file)) + Math.abs(row - rank);
      if (Math.min(dUn, dFl) > 1.25) continue;   // stray / mid-animation element
      if (dUn <= dFl) votesUn++; else votesFl++;
      sampled++;
      if (sampled >= 16) break;
    }
    flipped = votesFl > votesUn;
  }
"""

EXTRACT_JS = r"""
() => {
  const out = {ok: false, boards: 0, hasGame: false};
  const all = [...document.querySelectorAll('wc-chess-board')];
  out.boards = all.length;
  const vis = all.filter(b => b.offsetWidth > 100 && b.offsetHeight > 100);
  if (!vis.length) {
    out.err = all.length ? 'board element present but not visible' : 'no wc-chess-board element on this page';
    return out;
  }
  vis.sort((a, b) => b.offsetWidth * b.offsetHeight - a.offsetWidth * a.offsetHeight);
  const b = vis[0];
  out.elId = b.id || null;
  const r = b.getBoundingClientRect();
  const sx = r.width / 8, sy = r.height / 8;
  out.rect = {x: r.x, y: r.y, w: r.width, h: r.height};
  const g = b.game;
  out.hasGame = !!g;
  const callErrors = [];
  const call = (name) => {
    try { return (g && typeof g[name] === 'function') ? g[name]() : undefined; }
    catch (e) { callErrors.push(name + ': ' + e); return undefined; }
  };
  if (g) {
    out.fen = call('getFEN') ?? call('getFen') ?? null;
    out.turn = call('getTurn') ?? null;
    out.playingColor = call('getPlayingColor') ?? null;
    out.moveNumber = call('getMoveNumber') ?? null;
    out.gameOver = call('isGameOver') ?? call('getGameOver') ?? null;
  } else {
    out.err = 'no game object yet (board present)';
  }
  if (callErrors.length) {
    out.callErrors = callErrors;
    if (!out.fen) out.err = 'chess.com game API error: ' + callErrors[0];
  }
""" + _FLIP_DETECT_JS + r"""
  out.flipped = flipped;
  if (!out.fen) out.err = out.err || 'no FEN from board.game';
  out.ok = !out.err;
  return out;
}
"""

GEOMETRY_JS = r"""
() => {
  const all = [...document.querySelectorAll('wc-chess-board')];
  const vis = all.filter(b => b.offsetWidth > 100 && b.offsetHeight > 100);
  vis.sort((a, b) => b.offsetWidth * b.offsetHeight - a.offsetWidth * a.offsetHeight);
  const b = vis[0];
  if (!b) return null;
  const r = b.getBoundingClientRect();
  const sx = r.width / 8, sy = r.height / 8;
""" + _FLIP_DETECT_JS + r"""
  const squares = {};
  for (let file = 0; file < 8; file++) {
    for (let rank = 1; rank <= 8; rank++) {
      const col = flipped ? 7 - file : file;
      const row = flipped ? rank - 1 : 8 - rank;
      squares[String.fromCharCode(97 + file) + rank] =
          [r.x + (col + 0.5) * sx, r.y + (row + 0.5) * sy];
    }
  }
  return {rect: {x: r.x, y: r.y, w: r.width, h: r.height},
          size: [sx, sy], flipped: flipped, squares: squares};
}
"""

INJECT_JS = r"""
(args) => {
  const arrows = (args && args.arrows) || [];
  const all = [...document.querySelectorAll('wc-chess-board')];
  const vis = all.filter(b => b.offsetWidth > 100 && b.offsetHeight > 100);
  if (!vis.length) return {ok: false, err: 'no visible board'};
  vis.sort((a, b) => b.offsetWidth * b.offsetHeight - a.offsetWidth * a.offsetHeight);
  const b = vis[0];
  let svg = b.querySelector(':scope > svg.hermes-arrows');
  if (!svg) {
    svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'hermes-arrows');
    svg.setAttribute('viewBox', '0 0 8 8');
    svg.setAttribute('preserveAspectRatio', 'none');
    const st = svg.style;
    st.position = 'absolute'; st.left = '0'; st.top = '0';
    st.width = '100%'; st.height = '100%';
    st.pointerEvents = 'none'; st.zIndex = '9999';
    if (getComputedStyle(b).position === 'static') b.style.position = 'relative';
    b.appendChild(svg);
  }
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  if (!arrows.length) return {ok: true, drawn: 0};
  const r = b.getBoundingClientRect();
  const sx = r.width / 8, sy = r.height / 8;
""" + _FLIP_DETECT_JS + r"""
  const SQ = /^[a-h][1-8]$/;
  const center = (alg) => {
    if (!SQ.test(alg)) return null;
    const file = alg.charCodeAt(0) - 97;
    const rank = parseInt(alg[1], 10) - 1;
    let col = file, row = 7 - rank;
    if (flipped) { col = 7 - file; row = rank; }
    return [col + 0.5, row + 0.5];
  };
  const NS = 'http://www.w3.org/2000/svg';
  let drawn = 0, skipped = 0;
  for (const a of arrows) {
    if (!a.from || !a.to) { skipped++; continue; }
    const c1 = center(a.from), c2 = center(a.to);
    if (!c1 || !c2) { skipped++; continue; }
    const x1 = c1[0], y1 = c1[1], x2 = c2[0], y2 = c2[1];
    const w = a.width || 0.13;
    const op = (a.opacity == null) ? 0.85 : a.opacity;
    const line = document.createElementNS(NS, 'line');
    line.setAttribute('x1', x1); line.setAttribute('y1', y1);
    line.setAttribute('x2', x2); line.setAttribute('y2', y2);
    line.setAttribute('stroke', a.color || '#2ecc71');
    line.setAttribute('stroke-width', w);
    line.setAttribute('stroke-linecap', 'round');
    line.setAttribute('opacity', op);
    svg.appendChild(line);
    const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const head = a.head || 0.30, halfw = w * 2.0;
    const tipx = x2 - ux * 0.05, tipy = y2 - uy * 0.05;
    const bx = tipx - ux * head, by = tipy - uy * head;
    const px = -uy * halfw, py = ux * halfw;
    const poly = document.createElementNS(NS, 'polygon');
    poly.setAttribute('points', `${tipx},${tipy} ${bx + px},${by + py} ${bx - px},${by - py}`);
    poly.setAttribute('fill', a.color || '#2ecc71');
    poly.setAttribute('opacity', op);
    svg.appendChild(poly);
    drawn++;
  }
  return {ok: true, drawn: drawn, skipped: skipped};
}
"""


@dataclass
class BoardState:
    ok: bool = False
    err: str = ""
    fen: str | None = None
    turn: str | None = None
    playing_color: str | None = None
    move_number: int | None = None
    game_over: bool | None = None
    flipped: bool | None = None
    rect: dict | None = None
    boards: int = 0
    el_id: str | None = None
    page_url: str = ""


def square_center(geo: dict, alg: str) -> tuple[float, float]:
    """Viewport centre (x, y) of an algebraic square, from get_geometry().

    Always computed from the board rect + detected orientation. Piece element
    rects are deliberately NOT used: chess.com animates pieces by transform and
    a throttled background tab can freeze one mid-flight.
    """
    f = ord(alg[0]) - 97
    rk = int(alg[1]) - 1
    col, row = (7 - f, rk) if geo.get("flipped") else (f, 7 - rk)
    sx, sy = geo["size"]
    r = geo["rect"]
    return r["x"] + (col + 0.5) * sx, r["y"] + (row + 0.5) * sy


class ChessComReader:
    """Attach to a Chrome via CDP and read the chess.com board from it."""

    def __init__(self, port: int = 9222, timeout_ms: int = 15000, auto_recover: bool = True):
        self.port = int(port)
        self.timeout_ms = int(timeout_ms)
        self.page_title = ""
        self._pw = None
        self._browser = None
        self._page = None
        self._routed: set[int] = set()
        self._routed_pages: list = []
        # ad-bounce auto-recovery: if the tab is found on an ad URL, navigate
        # it back to the last chess.com URL (armed once per bounce)
        self.auto_recover = bool(auto_recover)
        self._last_chess_url = ""
        self._recover_armed = True
        # stuck-board recovery: consecutive reads of a game/play page with no
        # board element at all (see read())
        self._board_missing_polls = 0
        self._board_recover_armed = True
        # last swallowed ad-route error (abort/continue_ races are normal;
        # kept for observability instead of discarding silently)
        self.last_route_error = ""
        # last failed arrow inject/clear (so callers can surface it)
        self.last_inject_error = ""

    # ------------------------------------------------------------ connection
    def connect(self):
        """Connect (or reconnect). Raises if CDP is unreachable."""
        self.close()
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self.port}", timeout=self.timeout_ms
            )
        except Exception:
            self.close()
            raise

    def close(self):
        """Detach without closing Chrome (removes our request routing first)."""
        for page in self._routed_pages:
            try:
                page.unroute("**/*")
            except Exception:
                pass
        self._routed_pages = []
        self._routed = set()
        self._page = None
        self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    # ----------------------------------------------------------------- pages
    @staticmethod
    def _host(url: str) -> str:
        try:
            from urllib.parse import urlparse
            return (urlparse(url).hostname or "").lower()
        except Exception:
            return ""

    @classmethod
    def _is_chess_url(cls, url: str) -> bool:
        host = cls._host(url)
        return host == "chess.com" or host.endswith(".chess.com")

    @staticmethod
    def _board_expected(url: str) -> bool:
        """A chess.com URL where a board is normally on screen."""
        u = (url or "").lower()
        return ("/game/" in u or "/play" in u or "/analysis" in u or "/puzzle" in u)

    def find_page(self):
        """Pick the best chess.com tab (live game > play > analysis > other)."""
        if self._browser is None:
            raise RuntimeError("not connected")
        best = None
        best_score = -1
        for ctx in self._browser.contexts:
            for p in ctx.pages:
                try:
                    url = p.url
                except PWError:
                    continue
                if not self._is_chess_url(url):
                    continue
                score = 0
                if "/game/" in url:
                    score = 4
                elif "/play" in url:
                    score = 3
                elif "/analysis" in url:
                    score = 2
                elif "/openings" in url or "/puzzle" in url:
                    score = 1
                if score > best_score:
                    best, best_score = p, score
        self._page = best
        if best is not None:
            try:
                self.page_title = best.title()
            except Exception:
                self.page_title = ""
        return best

    # ------------------------------------------------------------- ad taming
    def stabilize(self, page=None, enabled: bool = True) -> bool:
        """Abort ad-tech requests on a page so the tab stops bouncing.

        Guest chess.com pages bounce the whole tab onto ad 'user-sync'
        redirect URLs; blocking ad hosts keeps the board readable. Safe to
        call repeatedly - a page is only routed once.
        """
        page = page or self._page
        if page is None or not enabled:
            return False
        if id(page) in self._routed:
            return True
        try:
            page.route("**/*", self._ad_route)
            self._routed.add(id(page))
            self._routed_pages.append(page)
            return True
        except Exception:
            return False

    def _ad_route(self, route):
        try:
            url = route.request.url.lower()
        except Exception:
            url = ""
        if any(s in url for s in AD_BLOCK_SUBSTRINGS):
            try:
                route.abort()
                return
            except Exception as e:
                # benign race (request already finished): NEVER re-raise here -
                # it would surface inside page.evaluate/goto and break reads -
                # but remember it instead of discarding silently
                self.last_route_error = f"abort: {type(e).__name__}: {str(e)[:80]}"
        try:
            route.continue_()
        except Exception as e:
            self.last_route_error = f"continue: {type(e).__name__}: {str(e)[:80]}"

    # ---------------------------------------------------------------- reading
    def _eval(self, page, js, arg=None, retries: int = 4):
        """evaluate with retries - chess.com does late client-side navigations.

        If the tab dies mid-retry it is replaced with a live one, and the
        replacement gets its own attempt (relocating must not consume the
        retry budget on the final iteration).
        """
        last: Exception | None = None
        attempt = 0
        extra = 0
        while attempt < retries + extra:
            attempt += 1
            try:
                if arg is None:
                    return page.evaluate(js)
                return page.evaluate(js, arg)
            except PWError as e:
                last = e
                msg = str(e)
                transient = ("Execution context was destroyed" in msg
                             or "navigation" in msg
                             or "Target closed" in msg)
                if not transient:
                    raise
                try:
                    page.wait_for_timeout(350 + 250 * attempt)
                except Exception:
                    pass
                try:
                    closed = page.is_closed()
                except PWError:
                    closed = True
                if closed:
                    new_page = self.find_page()
                    if new_page is None:
                        raise e
                    page = new_page
                    if extra == 0:
                        extra = 1    # one bonus attempt on the fresh tab
        raise last  # type: ignore[misc]

    def read(self) -> BoardState:
        if self._browser is None:
            raise RuntimeError("not connected")
        page = self._page
        try:
            if page is None or page.is_closed():
                page = self.find_page()
        except PWError:
            page = self.find_page()
        if page is None:
            return BoardState(ok=False, err="no chess.com tab open in this Chrome")
        try:
            cur = page.url
        except PWError:
            cur = ""
        if cur and not self._is_chess_url(cur):
            recovered = ""
            if self.auto_recover and self._recover_armed:
                self._recover_armed = False
                target = self._last_chess_url or "https://www.chess.com/play/computer"
                try:
                    page.goto(target, timeout=20000, wait_until="domcontentloaded")
                    # re-arm: the fresh load can bounce again before our next
                    # read, and recovery must not be one-shot
                    self._recover_armed = True
                    recovered = " - navigated back, reloading"
                except Exception:
                    # transient failure: re-arm so the next read() tries again
                    # (otherwise auto-recovery would stay dead forever)
                    self._recover_armed = True
                    recovered = " - navigation back failed (will retry)"
            return BoardState(
                ok=False,
                err=f"tab is on {self._host(cur)} right now (ad redirect){recovered}",
            )
        if cur:
            self._last_chess_url = cur
            self._recover_armed = True
        try:
            d = self._eval(page, EXTRACT_JS)
            if not isinstance(d, dict):
                return BoardState(ok=False, err="board read returned no data (page still loading?)")
            # _eval may have relocated to a live tab; find_page() already
            # updated self._page, so never clobber it with the stale handle
            url = ""
            if self._page is not None:
                try:
                    url = self._page.url
                except PWError:
                    url = ""
        except PWError as e:
            return BoardState(ok=False, err=f"page evaluate failed: {e.__class__.__name__}: {str(e)[:90]}")
        st = BoardState(
            ok=bool(d.get("ok")),
            err=d.get("err", ""),
            fen=d.get("fen"),
            turn=d.get("turn"),
            playing_color=d.get("playingColor"),
            move_number=d.get("moveNumber"),
            game_over=d.get("gameOver"),
            flipped=d.get("flipped"),
            rect=d.get("rect"),
            boards=d.get("boards", 0),
            el_id=d.get("elId"),
            page_url=url,
        )
        # ---- stuck-board recovery: a game/play page with NO board element at
        # all for many reads in a row is not a normal state (the board went
        # away - an ad document that kept the chess.com URL, a broken SPA, a
        # wedged renderer). Reload the tab, or the reader reports "no board"
        # forever while the run spins until its stall timer.
        if (not st.ok and not st.boards and self.auto_recover
                and self._board_expected(url)):
            self._board_missing_polls += 1
            if self._board_missing_polls >= 15 and self._board_recover_armed:
                self._board_recover_armed = False
                target = self._last_chess_url or url or "https://www.chess.com/play/computer"
                try:
                    self._page.goto(target, timeout=20000, wait_until="domcontentloaded")
                    self._board_recover_armed = True
                    self._board_missing_polls = 0
                    st.err += " - board missing on a game page: reloading the tab"
                except Exception:
                    # transient failure: re-arm so the next reads try again
                    self._board_recover_armed = True
                    st.err += " - board missing; reload failed (will retry)"
        else:
            self._board_missing_polls = 0
            self._board_recover_armed = True
        return st

    def get_geometry(self):
        """Square geometry of the board (rect, size, orientation, centres).

        Returns None when there is no page or the board is unreadable; the
        reason is kept in self.last_geometry_error so callers/logs can tell
        the two cases apart.
        """
        page = self._page
        if page is None:
            self.last_geometry_error = "no chess.com page selected"
            return None
        try:
            geo = self._eval(page, GEOMETRY_JS, retries=2)
            self.last_geometry_error = "" if geo else "board geometry unavailable"
            return geo
        except PWError as e:
            self.last_geometry_error = f"{e.__class__.__name__}: {str(e)[:90]}"
            return None

    # ---------------------------------------------------------------- drawing
    def inject_arrows(self, arrows: list[dict]):
        page = self._page
        if page is None:
            self.last_inject_error = "no chess.com page selected"
            return {"ok": False, "err": "no chess.com page selected"}
        try:
            return self._eval(page, INJECT_JS, {"arrows": arrows})
        except PWError as e:
            self.last_inject_error = f"inject: {type(e).__name__}: {str(e)[:80]}"
            raise

    def clear_arrows(self):
        """Remove our overlay; returns the inject result instead of silently
        pretending success when the page is gone."""
        try:
            result = self.inject_arrows([])
            if isinstance(result, dict) and result.get("ok"):
                self.last_inject_error = ""
            return result
        except Exception as e:
            self.last_inject_error = f"clear: {type(e).__name__}: {str(e)[:80]}"
            return {"ok": False, "err": "clear failed"}


def open_tab_in_chrome(port: int, url: str, timeout: float = 6.0) -> bool:
    """Open a new tab in the Chrome listening on `port` (user-initiated only)."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{int(port)}/json/new?" + urllib.parse.quote(url, safe=""),
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
