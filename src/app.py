#!/usr/bin/env python3
"""Chess Move Selector - live Stockfish suggestions for chess.com.

Watches a chess.com board through a Chrome tab you already have open (via CDP),
runs YOUR local Stockfish, and shows the best moves - plus arrows drawn on the
chess.com board itself.

GUI:       run app.py                 (or double-click the desktop shortcut)
Headless:  run app.py --console --port 9222
One-shot:  run app.py --dump --port 9222      (raw board state, then exit)
Self-test: run app.py --selfcheck
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PROJECT = HERE.parent

from board_reader import ChessComReader, open_tab_in_chrome  # noqa: E402
from sf_engine import Engine, uci_to_pretty  # noqa: E402

DEFAULT_ENGINES = [
    str(PROJECT / "bin" / "stockfish" / "stockfish-windows-x86-64-universal.exe"),
    str(PROJECT / "bin" / "stockfish" / "stockfish-windows-x86-64-avx2.exe"),
    # a Stockfish you downloaded into your own home folder (e.g. ~/stockfish/)
    str(Path.home() / "stockfish" / "stockfish-windows-x86-64-avx2.exe"),
    str(Path.home() / "stockfish" / "stockfish-windows-x86-64-universal.exe"),
]
PORT_CHOICES = ["9222", "9223", "9224", "9226", "9333"]
COLOR_NAMES = {"w": "White", "b": "Black"}

# Scored-move quality classes: (name, colour, max centipawn loss, desc).
# Loss is measured against the engine's best move, from the mover's view.
MOVE_CLASSES = [
    ("Best",       "#2ecc71", 10,   "engine's top move"),
    ("Excellent",  "#3aa0ff", 40,   "nearly as strong"),
    ("Good",       "#16a085", 90,   "solid - small edge given up"),
    ("Inaccuracy", "#ffc233", 200,  "lets some of the edge slip"),
    ("Mistake",    "#ff8c1a", 500,  "clearly worse"),
    ("Blunder",    "#e74c3c", None, "hangs material / throws the game"),
]
UNKNOWN_CLASS = ("?", "#9aa4ad")


def classify_loss(loss: float | None) -> tuple[str, str]:
    """Quality class + hex colour for a centipawn loss vs the best move."""
    if loss is None:
        return UNKNOWN_CLASS
    for name, color, thr, _desc in MOVE_CLASSES:
        if thr is not None and loss <= thr:
            return (name, color)
    return ("Blunder", "#e74c3c")


def legend_text() -> str:
    """One-line legend for console output."""
    return "  ".join(f"{name}={color}" for name, color, _t, _d in MOVE_CLASSES)


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def first_existing_engine() -> str:
    for p in DEFAULT_ENGINES:
        if Path(p).is_file():
            return p
    return DEFAULT_ENGINES[0]


# =============================================================== the analyzer
class Analyzer:
    """Worker thread: watch chess.com -> Stockfish -> suggestions + arrows."""

    def __init__(self, emit, opts: dict):
        self.emit = emit                 # emit(event_dict)
        self.opts = opts                 # shared settings dict
        self.cmd_q: queue.Queue = queue.Queue()
        self.stop_evt = threading.Event()
        self.thread: threading.Thread | None = None
        self.engine: Engine | None = None
        self.reader: ChessComReader | None = None
        self.my_color: str | None = None
        self.last_fen: str | None = None
        self.last_analyzed: str | None = None
        self.last_arrow_payload: list[dict] = []
        self.last_arrows_at = 0.0
        self.analyze_now_evt = threading.Event()

    # ------------------------------------------------------------ public API
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_evt.clear()
        self.thread = threading.Thread(target=self._main, name="analyzer", daemon=True)
        self.thread.start()

    def stop(self, join_timeout: float = 4.0):
        self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=join_timeout)

    def request_analyze(self):
        self.analyze_now_evt.set()

    def update(self, **kw):
        self.cmd_q.put(kw)

    # -------------------------------------------------------------- internals
    def _log(self, msg: str, level: str = "info"):
        self.emit({"kind": "log", "level": level, "msg": msg})

    def _status(self, connected: bool, msg: str, page_state=None):
        self.emit({
            "kind": "status",
            "connected": connected,
            "msg": msg,
            "my_color": self.my_color,
            "page_url": getattr(page_state, "page_url", "") if page_state else "",
        })

    def _main(self):
        try:
            self._loop()
        except Exception as e:
            self._log(f"analyzer crashed: {type(e).__name__}: {e}", "error")
            self._log(traceback.format_exc(), "error")
        finally:
            self._teardown()
            self.emit({"kind": "stopped"})

    def _teardown(self):
        if self.reader is not None:
            try:
                self.reader.clear_arrows()
            except Exception:
                pass
            try:
                self.reader.close()
            except Exception:
                pass
        self.reader = None
        if self.engine is not None:
            try:
                self.engine.quit()
            except Exception:
                pass
        self.engine = None

    def _sleep(self, seconds: float):
        self.stop_evt.wait(max(0.05, seconds))

    def _poll_s(self) -> float:
        """Poll interval in seconds; a garbage poll_ms option must not kill
        the loop (falls back to 0.6 s)."""
        try:
            v = float(self.opts["poll_ms"]) / 1000.0
        except (TypeError, ValueError):
            v = 0.6
        return max(0.05, v)

    def _loop(self):
        self._start_engine()
        while not self.stop_evt.is_set():
            self._drain_cmds()
            if self.engine is None:
                # engine failed to start (bad binary or options): retry
                # periodically instead of letting the analyzer thread die
                self._engine_retry_ticks = getattr(self, "_engine_retry_ticks", 0) + 1
                if self._engine_retry_ticks >= 40:
                    self._engine_retry_ticks = 0
                    self._start_engine()
            else:
                self._engine_retry_ticks = 0

            # --- browser connection
            if self.reader is None:
                try:
                    port = int(str(self.opts["port"]).strip())
                except (TypeError, ValueError):
                    self._log(f"bad CDP port option {self.opts['port']!r} - fix it and reconnect", "error")
                    self._sleep(2.0)
                    continue
                try:
                    self.reader = ChessComReader(port=port)
                    self.reader.connect()
                    page = self.reader.find_page()
                    if page is None:
                        self._status(True, f"connected to Chrome :{port} - open chess.com in that browser")
                        self._log(f"Connected to Chrome on :{port}. Open chess.com there and I'll pick it up.")
                    else:
                        self._status(True, f"watching: {self.reader.page_title or page.url}", None)
                        self._log(f"Found chess.com tab: {self.reader.page_title or page.url}")
                except Exception as e:
                    self.reader = None
                    self._status(False, f"cannot reach Chrome on :{port} ({type(e).__name__})")
                    self._log(f"CDP connect failed on :{port}: {e}", "warn")
                    try:
                        delay = float(self.opts["retry_s"])
                    except (TypeError, ValueError):
                        delay = 3.0
                    self._sleep(delay)
                    continue

            # --- read the board
            try:
                st = self.reader.read()
            except Exception as e:
                self._log(f"lost browser connection ({type(e).__name__}) - reconnecting", "warn")
                try:
                    self.reader.close()
                except Exception as ce:
                    self._log(f"reader close failed while reconnecting: {type(ce).__name__}", "warn")
                self.reader = None
                self._sleep(1.0)
                continue

            # keep ad-tech blocked on whichever chess.com page we're reading
            if self.opts.get("stabilize", True) and getattr(self.reader, "_page", None) is not None:
                if id(self.reader._page) not in self.reader._routed:
                    try:
                        self.reader.stabilize(self.reader._page)
                        self._log("stabilized tab: ad-tech blocked so the board stays readable")
                    except Exception as e:
                        self._log(f"stabilize failed: {type(e).__name__}", "warn")

            if not st.ok:
                self._status(True, st.err or "no board found")
                if self.last_arrows_at:
                    try:
                        self.reader.clear_arrows()
                    except Exception as e:
                        self._log(f"clear arrows failed: {type(e).__name__}", "warn")
                    # arrows are gone (or unreachable): drop the payload so the
                    # keep-alive can never redraw stale lines
                    self.last_arrow_payload = None
                    self.last_arrows_at = 0.0
                self.last_fen = self.last_analyzed = None
                self._sleep(self._poll_s())
                continue

            fen = st.fen
            if not fen:
                self._status(True, "board found - no position yet (game not started?)", st)
                self._sleep(self._poll_s())
                continue

            if st.playing_color in ("w", "b"):
                self.my_color = st.playing_color
            elif self.my_color is None and st.flipped is not None:
                # no explicit colour on the board object: the board is always
                # oriented towards the player, so bottom = you
                self.my_color = "b" if st.flipped else "w"
                self._log("you are " + ("Black" if st.flipped else "White") + " (from board orientation)")
            parts = fen.split()
            turn = parts[1] if len(parts) > 1 else "w"
            new_position = fen != self.last_fen
            if new_position:
                self.last_fen = fen
                who = "your" if turn == self.my_color else ("side-to-move" if self.my_color is None else "opponent")
                self._log(f"position: {fen}  (move {st.move_number}, {who} move)")
                self._status(True, f"position {st.move_number or '?'} - "
                                   f"{COLOR_NAMES.get(turn, turn)} to move"
                                   + (f" | you: {COLOR_NAMES.get(self.my_color, '?')}" if self.my_color else ""), st)

            if st.game_over:
                if new_position or self.last_arrows_at:
                    self._status(True, "game over", st)
                    self._log("game over detected - arrows cleared")
                    try:
                        self.reader.clear_arrows()
                    except Exception as e:
                        self._log(f"clear arrows failed: {type(e).__name__}", "warn")
                    self.last_arrow_payload = None
                    self.last_arrows_at = 0.0
                    self.last_analyzed = fen
                self._sleep(self._poll_s())
                continue

            my_turn = (self.my_color is None) or (turn == self.my_color)
            want = self.analyze_now_evt.is_set() or (
                fen != self.last_analyzed and (my_turn or not self.opts["only_my_turn"])
            )
            if want:
                self.analyze_now_evt.clear()
                if self._analyze_and_show(fen, turn, st):
                    self.last_analyzed = fen
                else:
                    # analysis failed (engine down / error): don't remember the
                    # position as analyzed (so it is retried), and drop stale
                    # arrow state so the keep-alive cannot redraw old lines
                    self.last_analyzed = None
                    self.last_arrow_payload = None
                    self.last_arrows_at = 0.0
            elif self.opts["arrows"] and self.last_arrows_at and (time.time() - self.last_arrows_at) > 3.0:
                # chess.com re-renders the board on its own; keep our overlay alive
                try:
                    self.reader.inject_arrows(self.last_arrow_payload)
                    self.last_arrows_at = time.time()
                except Exception as e:
                    # transient during page re-renders: retried every few seconds;
                    # log at most every 30 s so a dead overlay is still diagnosable
                    now = time.time()
                    if now - getattr(self, "_last_ka_err_log", 0.0) > 30.0:
                        self._last_ka_err_log = now
                        self._log(f"arrow keep-alive failed: {type(e).__name__}", "warn")

            self._sleep(self._poll_s())

    # ------------------------------------------------------------ engine side
    def _start_engine(self):
        """Start (or restart) the engine. Never raises: a bad binary or bad
        options must not kill the analyzer thread (the loop retries)."""
        path = self.opts["engine"]
        self._log(f"starting engine: {path}")
        try:
            self.engine = Engine(path)
            name = self.engine.start()
        except Exception as e:
            eng = self.engine
            self.engine = None
            if eng is not None:
                # a spawned-but-unresponsive engine must not be left running
                proc = getattr(eng, "proc", None)
                try:
                    if not eng.quit():
                        self._log("engine did not stop cleanly after failed start", "warn")
                except Exception as e:
                    self._log(f"engine quit failed after failed start: {type(e).__name__}", "warn")
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception as e:
                        self._log(f"could not kill leftover engine process: {type(e).__name__}", "warn")
            self._log(f"engine start failed: {type(e).__name__}: {e}", "error")
            return False
        self._log(f"engine ready: {name}")
        try:
            self._apply_engine_options()
        except Exception as e:
            # the "never raises" promise must hold literally: options are
            # best-effort and the engine still runs with defaults
            self._log(f"engine option apply failed: {type(e).__name__}: {e}", "error")
        try:
            self.engine.new_game()
        except Exception as e:
            self._log(f"engine init failed: {type(e).__name__}: {e}", "error")
            try:
                self.engine.quit()
            except Exception:
                pass
            self.engine = None    # let the loop retry from scratch
            return False
        return True

    def _apply_engine_options(self):
        if self.engine is None:
            return
        try:
            o = {
                "Threads": int(self.opts["threads"]),
                "Hash": int(self.opts["hash"]),
                "MultiPV": int(self.opts["multipv"]),
            }
            if self.opts["limit_strength"]:
                o["UCI_LimitStrength"] = "true"
                o["UCI_Elo"] = int(self.opts["elo"])
            else:
                o["UCI_LimitStrength"] = "false"
            self.engine.set_options(o)
        except Exception as e:
            self._log(f"engine option error: {type(e).__name__}: {e}", "error")

    def _drain_cmds(self):
        engine_keys = {"threads", "hash", "limit_strength", "elo", "multipv"}
        restart = False
        reapply = False
        while True:
            try:
                cmd = self.cmd_q.get_nowait()
            except queue.Empty:
                break
            if cmd.get("_action") == "clear_arrows":
                if self.reader is not None:
                    try:
                        self.reader.clear_arrows()
                    except Exception as e:
                        self._log(f"clear arrows failed: {type(e).__name__}", "warn")
                self.last_arrow_payload = None
                self.last_arrows_at = 0.0
                continue
            for k, v in cmd.items():
                self.opts[k] = v
                if k == "engine":
                    restart = True
                if k in engine_keys:
                    reapply = True
        if restart:
            old = self.engine
            self.engine = None
            if old is not None:
                proc = getattr(old, "proc", None)
                try:
                    old.quit()
                except Exception as e:
                    self._log(f"engine quit failed: {type(e).__name__}: {e}", "warn")
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                        self._log("killed a leftover engine process", "warn")
                    except Exception as e:
                        self._log(f"could not kill leftover engine process: {type(e).__name__}", "warn")
            self._start_engine()
        elif reapply:
            self._apply_engine_options()

    def _analyze_and_show(self, fen: str, turn: str, st) -> bool:
        """Analyze and show one position. Returns True only when suggestions
        were actually emitted (the caller remembers the FEN only on True)."""
        if self.engine is None:
            return False
        try:
            res = self.engine.analyze(
                fen,
                movetime_ms=int(self.opts["movetime"]),
                depth=int(self.opts["depth"]),
                multipv=int(self.opts["multipv"]),
            )
        except Exception as e:
            now = time.time()
            if now - getattr(self, "_last_engine_err_log", 0.0) > 5.0:
                self._last_engine_err_log = now
                self._log(f"engine error: {type(e).__name__}: {e}", "error")
            return False
        if res is None:
            # sf_engine returns None when no bestmove/analysis came back
            now = time.time()
            if now - getattr(self, "_last_engine_err_log", 0.0) > 5.0:
                self._last_engine_err_log = now
                self._log("engine returned no analysis for this position - skipping")
            return False

        values = [pv.value_cp() for pv in res.pvs]
        scored = [v for v in values if v is not None]
        best_value = max(scored) if scored else None
        rows = []
        for pv, value in zip(res.pvs, values):
            loss = None
            if best_value is not None and value is not None:
                loss = max(0.0, best_value - value)
            cls, cls_color = classify_loss(loss)
            pretty = [uci_to_pretty(m, fen) for m in pv.pv[:10]]
            rows.append({
                "n": pv.multipv,
                "best_uci": pv.best,
                "best": pretty[0] if pretty else "?",
                "score": pv.score_str(),
                "line": " ".join(pretty[:8]),
                "depth": pv.depth,
                "class": cls,
                "color": cls_color,
                "loss": None if loss is None else round(loss, 1),
            })
        head = rows[0] if rows else None
        self.emit({
            "kind": "suggestions",
            "fen": fen,
            "turn": turn,
            "my_color": self.my_color,
            "rows": rows,
            "best": (head["best"] if head else res.bestmove) or "?",
            "score": head["score"] if head else "",
            "depth": res.depth,
            "elapsed": res.elapsed_ms,
        })
        tag = "YOUR MOVE" if turn == self.my_color else ("side to move" if self.my_color is None else "opponent to move")
        best = (head["best"] if head else res.bestmove) or "?"
        sc = head["score"] if head else ""
        spread = "  ".join(f"{r['best']}[{r['class']}]" for r in rows[: int(self.opts["arrows_n"])])
        self._log(f"[{tag}] best: {best} {sc}   (depth {res.depth}, {res.elapsed_ms} ms)"
                  + (f"   candidates: {spread}" if spread else ""))

        if self.opts["arrows"]:
            payload = []
            for i, r in enumerate(rows[: int(self.opts["arrows_n"])]):
                u = r["best_uci"]
                if not u or len(u) < 4:
                    continue
                payload.append({
                    "from": u[0:2],
                    "to": u[2:4],
                    "color": r.get("color") or "#2ecc71",
                    "width": round(0.13 - i * 0.012, 3),
                    "opacity": round(0.95 - i * 0.11, 2),
                })
            try:
                self.reader.inject_arrows(payload)
                self.last_arrow_payload = payload
                self.last_arrows_at = time.time()
            except Exception as e:
                # nothing drawn: never record it as the on-screen payload
                self.last_arrow_payload = None
                self.last_arrows_at = 0.0
                self._log(f"arrow inject failed: {type(e).__name__}", "warn")
        return True


# ==================================================================== console
def console_emit(ev: dict):
    kind = ev.get("kind")
    if kind == "log":
        print(f"[{ts()}] {ev['msg']}", flush=True)
    elif kind == "status":
        print(f"[{ts()}] STATUS: {ev.get('msg', '')}", flush=True)
    elif kind == "suggestions":
        print(f"[{ts()}] >>> BEST {ev['best']}  {ev['score']}   "
              f"(depth {ev['depth']}, {ev['elapsed']} ms)", flush=True)
        for r in ev["rows"]:
            print(f"            {r['n']}. {r['best']:<8} {r['score']:<7} "
                  f"[{r.get('class', '?')}] {r['line']}", flush=True)
    elif kind == "stopped":
        print(f"[{ts()}] stopped", flush=True)


def make_opts(args) -> dict:
    return dict(
        port=str(args.port),
        engine=args.engine,
        threads=4, hash=256, multipv=int(args.multipv),
        movetime=int(args.movetime), depth=int(args.depth),
        limit_strength=False, elo=1800,
        only_my_turn=True, arrows=not args.no_arrows, arrows_n=int(args.multipv),
        poll_ms=int(args.poll), retry_s=3.0,
    )


def run_console(args) -> int:
    opts = make_opts(args)
    print("move colours: " + legend_text(), flush=True)
    done = threading.Event()
    if args.once:
        def emit(ev):
            console_emit(ev)
            if ev.get("kind") == "suggestions":
                done.set()
    else:
        emit = console_emit
    az = Analyzer(emit, opts)
    az.start()
    try:
        if args.once:
            if done.wait(timeout=60):
                az.stop()
                return 0
            print(f"[{ts()}] no analysis produced within 60s", flush=True)
            az.stop()
            return 2
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n[{ts()}] stopping...", flush=True)
        az.stop()
        return 0


def run_dump(args) -> int:
    reader = ChessComReader(port=int(args.port))
    try:
        reader.connect()
    except Exception as e:
        print(f"cannot reach Chrome on :{args.port}: {type(e).__name__}: {e}")
        try:
            reader.close()
        except Exception:
            pass
        return 2
    try:
        page = reader.find_page()
        print(f"tab: {reader.page_title or '(none)'}  {page.url if page else ''}")
        st = reader.read()
        print(json.dumps(asdict(st), indent=2))
        return 0 if st.ok else 1
    finally:
        reader.close()


# ======================================================================== GUI
def build_gui(root, args, autostart: bool = True):
    import tkinter as tk
    from tkinter import filedialog, ttk
    from tkinter import messagebox

    class App:
        def __init__(self):
            self.root = root
            self.opts = make_opts(args)
            self.q: queue.Queue = queue.Queue()
            self.analyzer = Analyzer(self.q.put, self.opts)
            self._build()
            self.root.after(120, self._pump)
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)
            if autostart:
                self.root.after(250, self.connect)

        # ------------------------------------------------------------ layout
        def _build(self):
            root.title("Chess Move Selector - Stockfish live for chess.com")
            root.geometry("1000x780")
            root.minsize(880, 640)

            main = ttk.Frame(root, padding=10)
            main.pack(fill="both", expand=True)

            # --- top row: browser + engine
            top = ttk.Frame(main)
            top.pack(fill="x")

            bf = ttk.LabelFrame(top, text="Browser (Chrome with remote debugging)", padding=8)
            bf.pack(side="left", fill="x", expand=True, padx=(0, 6))
            ttk.Label(bf, text="CDP port:").grid(row=0, column=0, sticky="w")
            self.port_var = tk.StringVar(value=self.opts["port"])
            ttk.Combobox(bf, textvariable=self.port_var, values=PORT_CHOICES, width=7).grid(
                row=0, column=1, sticky="w", padx=4)
            ttk.Button(bf, text="Connect & Watch", command=self.connect).grid(row=0, column=2, padx=4)
            ttk.Button(bf, text="Stop", command=self.stop).grid(row=0, column=3, padx=2)
            ttk.Button(bf, text="Open chess.com", command=self.open_chesscom).grid(row=0, column=4, padx=6)
            self.conn_lbl = ttk.Label(bf, text="not connected", foreground="#b03030")
            self.conn_lbl.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

            ef = ttk.LabelFrame(top, text="Stockfish engine", padding=8)
            ef.pack(side="left", fill="x", expand=True, padx=(6, 0))
            self.engine_var = tk.StringVar(value=self.opts["engine"])
            ttk.Combobox(ef, textvariable=self.engine_var, values=DEFAULT_ENGINES, width=46).grid(
                row=0, column=0, columnspan=2, sticky="we")
            ttk.Button(ef, text="Browse...", command=self.browse_engine).grid(row=0, column=2, padx=4)
            ttk.Button(ef, text="Use this engine", command=self.apply_engine_path).grid(row=0, column=3, padx=2)
            self.engine_lbl = ttk.Label(ef, text="engine not started", foreground="#777")
            self.engine_lbl.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

            # --- settings
            sf = ttk.LabelFrame(main, text="Analysis settings", padding=8)
            sf.pack(fill="x", pady=8)
            r = 0
            ttk.Label(sf, text="Think time (ms):").grid(row=r, column=0, sticky="w")
            self.movetime_var = tk.IntVar(value=self.opts["movetime"])
            ttk.Spinbox(sf, from_=100, to=10000, increment=100, textvariable=self.movetime_var,
                        width=7, command=self.push_settings).grid(row=r, column=1, sticky="w", padx=4)
            ttk.Label(sf, text="Depth cap (0 = use time):").grid(row=r, column=2, sticky="w", padx=(12, 0))
            self.depth_var = tk.IntVar(value=self.opts["depth"])
            ttk.Spinbox(sf, from_=0, to=45, increment=1, textvariable=self.depth_var,
                        width=5, command=self.push_settings).grid(row=r, column=3, sticky="w", padx=4)
            ttk.Label(sf, text="MultiPV (lines):").grid(row=r, column=4, sticky="w", padx=(12, 0))
            self.multipv_var = tk.IntVar(value=self.opts["multipv"])
            ttk.Spinbox(sf, from_=1, to=8, increment=1, textvariable=self.multipv_var,
                        width=4, command=self.push_settings).grid(row=r, column=5, sticky="w", padx=4)

            r = 1
            ttk.Label(sf, text="Threads:").grid(row=r, column=0, sticky="w", pady=(6, 0))
            self.threads_var = tk.IntVar(value=self.opts["threads"])
            ttk.Spinbox(sf, from_=1, to=32, increment=1, textvariable=self.threads_var,
                        width=7, command=self.push_settings).grid(row=r, column=1, sticky="w", padx=4, pady=(6, 0))
            ttk.Label(sf, text="Hash (MB):").grid(row=r, column=2, sticky="w", padx=(12, 0), pady=(6, 0))
            self.hash_var = tk.IntVar(value=self.opts["hash"])
            ttk.Spinbox(sf, from_=16, to=4096, increment=16, textvariable=self.hash_var,
                        width=7, command=self.push_settings).grid(row=r, column=3, sticky="w", padx=4, pady=(6, 0))
            self.limit_var = tk.BooleanVar(value=self.opts["limit_strength"])
            ttk.Checkbutton(sf, text="Limit strength to Elo:", variable=self.limit_var,
                            command=self.push_settings).grid(row=r, column=4, sticky="w", padx=(12, 0), pady=(6, 0))
            self.elo_var = tk.IntVar(value=self.opts["elo"])
            ttk.Spinbox(sf, from_=1320, to=3190, increment=20, textvariable=self.elo_var,
                        width=6, command=self.push_settings).grid(row=r, column=5, sticky="w", padx=4, pady=(6, 0))
            ttk.Button(sf, text="Apply engine settings", command=self.push_settings).grid(
                row=r, column=6, padx=10, pady=(6, 0))

            # --- watch options
            wf = ttk.LabelFrame(main, text="Watching", padding=8)
            wf.pack(fill="x")
            self.onlymy_var = tk.BooleanVar(value=self.opts["only_my_turn"])
            ttk.Checkbutton(wf, text="Analyze only when it's my turn", variable=self.onlymy_var,
                            command=self.push_settings).grid(row=0, column=0, sticky="w")
            self.arrows_var = tk.BooleanVar(value=self.opts["arrows"])
            ttk.Checkbutton(wf, text="Draw arrows on chess.com board", variable=self.arrows_var,
                            command=self.push_settings).grid(row=0, column=1, sticky="w", padx=(14, 0))
            ttk.Label(wf, text="Arrows:").grid(row=0, column=2, sticky="e", padx=(14, 0))
            self.arrown_var = tk.IntVar(value=self.opts["arrows_n"])
            ttk.Spinbox(wf, from_=1, to=8, increment=1, textvariable=self.arrown_var,
                        width=3, command=self.push_settings).grid(row=0, column=3, sticky="w", padx=4)
            self.topmost_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(wf, text="Always on top", variable=self.topmost_var,
                            command=self.toggle_topmost).grid(row=0, column=4, sticky="w", padx=(14, 0))
            self.stab_var = tk.BooleanVar(value=bool(self.opts.get("stabilize", True)))
            ttk.Checkbutton(wf, text="Stabilize page (block ad tech)", variable=self.stab_var,
                            command=self.push_settings).grid(row=1, column=0, columnspan=3,
                                                             sticky="w", pady=(6, 0))
            ttk.Label(wf, text="Poll rate (ms):").grid(row=0, column=5, sticky="e", padx=(14, 0))
            self.poll_var = tk.IntVar(value=self.opts["poll_ms"])
            ttk.Spinbox(wf, from_=200, to=3000, increment=100, textvariable=self.poll_var,
                        width=5, command=self.push_settings).grid(row=0, column=6, sticky="w", padx=4)
            ttk.Button(wf, text="Analyze now", command=lambda: self.analyzer.request_analyze()).grid(
                row=0, column=7, padx=(14, 0))
            ttk.Button(wf, text="Clear arrows", command=self.clear_arrows).grid(row=0, column=8, padx=4)

            # --- suggestions (top moves, each coloured by its quality class)
            pf = ttk.LabelFrame(main, text="Candidate moves (from your side) - coloured by quality", padding=8)
            pf.pack(fill="x", pady=8)
            self.best_var = tk.StringVar(value="-")
            ttk.Label(pf, textvariable=self.best_var, font=("Segoe UI", 17, "bold")).pack(anchor="w")
            self.row_vars = [tk.StringVar() for _ in range(8)]
            self.row_labels = []
            for var in self.row_vars:
                lbl = ttk.Label(pf, textvariable=var, font=("Consolas", 10))
                lbl.pack(anchor="w")
                self.row_labels.append(lbl)

            # --- colour legend: makes every arrow colour self-explanatory
            self.legend_entries = []
            lgf = ttk.LabelFrame(main, text="Arrow legend (colour = how good the move is)", padding=6)
            lgf.pack(fill="x", pady=(0, 8))
            lrow = ttk.Frame(lgf)
            lrow.pack(fill="x")
            for i, (name, color, _thr, desc) in enumerate(MOVE_CLASSES):
                cell = ttk.Frame(lrow)
                # 2 rows of 3 so six legend cells can never overflow the window
                cell.grid(row=i // 3, column=i % 3, padx=(0, 16), pady=2, sticky="w")
                ttk.Label(cell, text="\u25ac", foreground=color,
                          font=("Segoe UI", 13, "bold")).pack(side="left")
                ttk.Label(cell, text=f"{name} \u2013 {desc}", font=("Segoe UI", 8)).pack(side="left")
                self.legend_entries.append((name, color, desc))

            # --- FEN row
            ff = ttk.Frame(main)
            ff.pack(fill="x", pady=(0, 8))
            ttk.Label(ff, text="FEN:").pack(side="left")
            self.fen_var = tk.StringVar()
            fen_entry = ttk.Entry(ff, textvariable=self.fen_var, font=("Consolas", 9))
            fen_entry.pack(side="left", fill="x", expand=True, padx=6)
            ttk.Button(ff, text="Copy FEN", command=self.copy_fen).pack(side="left")

            # --- log
            lf = ttk.LabelFrame(main, text="Log", padding=6)
            lf.pack(fill="both", expand=True)
            self.log_text = tk.Text(lf, height=12, font=("Consolas", 9), wrap="word",
                                    state="disabled", background="#101418", foreground="#c8d2dc")
            sb = ttk.Scrollbar(lf, command=self.log_text.yview)
            self.log_text.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self.log_text.pack(side="left", fill="both", expand=True)

            self.root.attributes("-topmost", bool(self.topmost_var.get()))

        # ----------------------------------------------------------- actions
        def connect(self):
            try:
                port = int(str(self.port_var.get()).strip())
            except ValueError:
                messagebox.showerror("Chess Move Selector", "CDP port must be a number (e.g. 9222).")
                return
            self.opts["port"] = str(port)
            self.analyzer.stop()
            self.analyzer = Analyzer(self.q.put, self.opts)
            self.push_settings()
            self.analyzer.start()
            self._log_line(f"Connecting to Chrome on :{port} ...")

        def stop(self):
            self.analyzer.stop()
            self._log_line("stopped")

        def push_settings(self):
            self.analyzer.update(
                movetime=self.movetime_var.get(),
                depth=self.depth_var.get(),
                multipv=self.multipv_var.get(),
                threads=self.threads_var.get(),
                hash=self.hash_var.get(),
                limit_strength=bool(self.limit_var.get()),
                elo=self.elo_var.get(),
                only_my_turn=bool(self.onlymy_var.get()),
                arrows=bool(self.arrows_var.get()),
                arrows_n=self.arrown_var.get(),
                poll_ms=self.poll_var.get(),
                stabilize=bool(self.stab_var.get()),
            )

        def apply_engine_path(self):
            self.analyzer.update(engine=self.engine_var.get())
            self._log_line(f"engine -> {self.engine_var.get()}")

        def browse_engine(self):
            path = filedialog.askopenfilename(
                title="Pick a UCI engine binary",
                filetypes=[("Executables", "*.exe"), ("All files", "*.*")],
            )
            if path:
                self.engine_var.set(path)
                self.apply_engine_path()

        def open_chesscom(self):
            try:
                port = int(str(self.port_var.get()).strip() or "9222")
            except ValueError:
                messagebox.showerror("Chess Move Selector", "Port must be a number (e.g. 9222).")
                return
            ok = open_tab_in_chrome(port, "https://www.chess.com/play/online")
            self._log_line(("opened" if ok else "could not open") + f" chess.com in Chrome :{port}")

        def clear_arrows(self):
            self.analyzer.update(_action="clear_arrows")

        def copy_fen(self):
            self.root.clipboard_clear()
            self.root.clipboard_append(self.fen_var.get())
            self._log_line("FEN copied")

        def toggle_topmost(self):
            self.root.attributes("-topmost", bool(self.topmost_var.get()))

        def _on_close(self):
            try:
                self.analyzer.stop()
            finally:
                self.root.destroy()

        # --------------------------------------------------------------- pump
        def _pump(self):
            try:
                while True:
                    ev = self.q.get_nowait()
                    try:
                        self._handle(ev)
                    except Exception as e:
                        # a malformed event must not kill the pump
                        try:
                            self._log_line(f"internal error handling event: {type(e).__name__}: {e}")
                        except Exception:
                            pass
            except queue.Empty:
                pass
            finally:
                # the reschedule must run even if something above blew up:
                # a dead pump freezes the GUI silently
                self.root.after(120, self._pump)

        def _handle(self, ev: dict):
            kind = ev.get("kind")
            if kind == "log":
                self._log_line(ev["msg"])
            elif kind == "status":
                color = "#1a7f37" if ev.get("connected") else "#b03030"
                self.conn_lbl.configure(text=ev.get("msg", ""), foreground=color)
            elif kind == "suggestions":
                self.best_var.set(f"{ev['best']}    {ev['score']}    "
                                  f"(depth {ev['depth']}, {ev['elapsed']} ms)")
                self.fen_var.set(ev["fen"])
                rows = list(ev["rows"])
                for i, (var, lbl) in enumerate(zip(self.row_vars, self.row_labels)):
                    if i < len(rows):
                        r = rows[i]
                        var.set(f"{r['n']}. {r['best']:<8} {r['score']:<7} "
                                f"[{r.get('class', '?')}] {r['line'][:120]}")
                        lbl.configure(foreground=r.get("color") or "#c8d2dc")
                    else:
                        var.set("")
            elif kind == "stopped":
                self.conn_lbl.configure(text="stopped", foreground="#777")

        def _log_line(self, msg: str):
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{ts()}] {msg}\n")
            lines = int(self.log_text.index("end-1c").split(".")[0])
            if lines > 900:
                self.log_text.delete("1.0", f"{lines - 899}.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

    app = App()
    return app


# ======================================================================= main
def main() -> int:
    ap = argparse.ArgumentParser(description="Live Stockfish move selector for chess.com")
    ap.add_argument("--console", action="store_true", help="headless watcher, prints suggestions")
    ap.add_argument("--dump", action="store_true", help="print raw board state from the chess.com tab, then exit")
    ap.add_argument("--selfcheck", action="store_true", help="build the GUI, verify it loads, then exit")
    ap.add_argument("--port", default="9222", help="CDP port of the Chrome that has chess.com open")
    ap.add_argument("--engine", default=first_existing_engine(), help="path to a UCI engine binary")
    ap.add_argument("--movetime", type=int, default=700, help="think time in ms per position")
    ap.add_argument("--multipv", type=int, default=5,
                    help="candidate moves to compute and show (1-8, coloured by quality)")
    ap.add_argument("--depth", type=int, default=0, help="depth cap (0 = use time instead)")
    ap.add_argument("--poll", type=int, default=500, help="board poll rate in ms")
    ap.add_argument("--no-arrows", action="store_true", help="do not draw arrows on the board")
    ap.add_argument("--once", action="store_true", help="console mode: exit after the first analysis")
    args = ap.parse_args()

    if args.dump:
        return run_dump(args)
    if args.console:
        return run_console(args)
    if args.selfcheck:
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as e:
            print(f"GUI selfcheck: cannot init Tk ({type(e).__name__}: {e})")
            return 3
        build_gui(root, args, autostart=False)
        root.withdraw()
        root.after(600, root.destroy)
        root.mainloop()
        print("GUI SELFCHECK OK")
        return 0

    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as e:
        print(f"cannot open the GUI window ({type(e).__name__}: {e})")
        return 3
    build_gui(root, args, autostart=True)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
