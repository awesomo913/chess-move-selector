"""Minimal, dependency-free UCI client for Stockfish (works with any UCI engine).

Part of the Chess Move Selector. Pure stdlib: spawns the engine once, keeps it
alive, sends `position`/`go`, and parses MultiPV info lines into structured
results.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class PV:
    multipv: int = 1
    depth: int = 0
    cp: int | None = None
    mate: int | None = None
    pv: list[str] = field(default_factory=list)

    @property
    def best(self) -> str | None:
        return self.pv[0] if self.pv else None

    def score_str(self) -> str:
        """Human score string, from the point of view of the side to move."""
        if self.mate is not None:
            return f"#{self.mate}" if self.mate > 0 else f"#-{abs(self.mate)}"
        if self.cp is None:
            return "?"
        return f"{self.cp / 100:+.2f}"

    def value_cp(self) -> float | None:
        """Comparable centipawn value (side-to-move view).

        Mate scores map to large values so they always outrank normal
        positions; used to grade candidate moves by how much they lose
        against the best move.
        """
        if self.mate is not None:
            return (10000.0 - min(abs(self.mate), 99) * 10.0) * (1.0 if self.mate > 0 else -1.0)
        if self.cp is not None:
            return float(self.cp)
        return None


@dataclass
class Analysis:
    fen: str
    pvs: list[PV]
    bestmove: str | None
    ponder: str | None
    depth: int
    elapsed_ms: int


class EngineError(RuntimeError):
    pass


class Engine:
    """A persistent UCI engine subprocess."""

    def __init__(self, path: str):
        self.path = path
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self.name = ""
        self.options: dict[str, str] = {}

    # ------------------------------------------------------------- lifecycle
    def start(self, timeout: float = 75.0) -> str:
        if self.proc is not None and self.proc.poll() is None:
            return self.name
        if not os.path.isfile(self.path):
            raise EngineError(f"engine binary not found: {self.path}")
        self.proc = subprocess.Popen(
            [self.path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        self.send("uci")
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._next_line(deadline - time.time())
            if line is None:
                break
            if line.startswith("id name "):
                self.name = line[8:].strip()
            elif line.startswith("option name "):
                self.options[line] = line
            elif line == "uciok":
                if self.name:
                    return self.name
                raise EngineError("engine answered uci but gave no name")
        raise EngineError("engine did not answer 'uci' in time")

    def _reader(self):
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            self.lines.put(line.rstrip("\r\n"))

    def _next_line(self, timeout: float) -> str | None:
        try:
            return self.lines.get(timeout=max(0.01, timeout))
        except queue.Empty:
            return None

    def send(self, cmd: str):
        if self.proc is None or self.proc.poll() is not None:
            raise EngineError("engine is not running")
        if self.proc.stdin is None:
            raise EngineError("engine stdin unavailable")
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def isready(self, timeout: float = 15.0) -> bool:
        self.send("isready")
        deadline = time.time() + timeout
        while True:
            line = self._next_line(deadline - time.time())
            if line is None:
                return False
            if line == "readyok":
                return True

    def set_options(self, options: dict):
        with self._lock:
            for k, v in options.items():
                self.send(f"setoption name {k} value {v}")
            self.isready()

    def new_game(self):
        with self._lock:
            self.send("ucinewgame")
            self.isready()

    def quit(self) -> bool:
        """Stop the engine. Returns True when the process is confirmed gone.

        A failed quit handshake (broken pipe etc.) must never leave the
        process running unreferenced: fall back to kill() before dropping it.
        """
        ok = True
        proc = self.proc
        try:
            if proc is not None and proc.poll() is None:
                self.send("quit")
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            ok = False
            try:
                if proc is not None and proc.poll() is None:
                    proc.kill()
            except Exception:
                ok = False
        finally:
            self.proc = None
        return ok

    # -------------------------------------------------------------- analysis
    def analyze(self, fen: str, movetime_ms: int = 800, depth: int = 0,
                multipv: int = 3, hard_timeout: float = 120.0) -> Analysis:
        """Run one search. Returns every PV seen (up to `multipv`)."""
        with self._lock:
            multipv = max(1, min(8, int(multipv)))
            self.send(f"setoption name MultiPV value {multipv}")
            self.send("position fen " + fen)
            if not self.isready():
                raise EngineError("engine stuck before go")
            t0 = time.time()
            if depth and depth > 0:
                self.send(f"go depth {int(depth)}")
            else:
                self.send(f"go movetime {max(50, int(movetime_ms))}")

            pvs: dict[int, PV] = {}
            bestmove: str | None = None
            ponder: str | None = None
            maxdepth = 0
            deadline = t0 + hard_timeout
            while True:
                line = self._next_line(max(0.01, deadline - time.time()))
                if line is None:
                    raise EngineError("timed out waiting for bestmove")
                if line.startswith("info "):
                    if " pv " in line:
                        pv = self._parse_info(line)
                        if pv is not None:
                            pvs[pv.multipv] = pv
                            maxdepth = max(maxdepth, pv.depth)
                elif line.startswith("bestmove"):
                    toks = line.split()
                    if len(toks) > 1 and toks[1] not in ("(none)", "none"):
                        bestmove = toks[1]
                    if len(toks) > 3 and toks[2] == "ponder":
                        ponder = toks[3]
                    break
            return Analysis(
                fen=fen,
                pvs=[pvs[k] for k in sorted(pvs)],
                bestmove=bestmove,
                ponder=ponder,
                depth=maxdepth,
                elapsed_ms=int((time.time() - t0) * 1000),
            )

    @staticmethod
    def _parse_info(line: str) -> PV | None:
        toks = line.split()
        pv = PV()
        i = 1
        try:
            while i < len(toks):
                t = toks[i]
                if t == "pv":
                    pv.pv = toks[i + 1:]
                    break
                if t == "multipv":
                    pv.multipv = int(toks[i + 1]); i += 2; continue
                if t == "depth":
                    pv.depth = int(toks[i + 1]); i += 2; continue
                if t == "score":
                    kind = toks[i + 1]
                    val = int(toks[i + 2])
                    if kind == "cp":
                        pv.cp = val
                    elif kind == "mate":
                        pv.mate = val
                    i += 3; continue
                i += 1
        except (IndexError, ValueError):
            return None
        if pv.multipv < 1:
            pv.multipv = 1
        if not pv.pv and pv.cp is None and pv.mate is None:
            return None
        return pv


# ---------------------------------------------------------------- formatting
def fen_board(fen: str) -> dict[str, str]:
    """FEN placement field -> {square: piece-letter}. Uppercase = white."""
    board: dict[str, str] = {}
    try:
        placement = fen.split()[0]
    except IndexError:
        return board
    rank = 8
    file_ = 0
    for ch in placement:
        if ch == "/":
            rank -= 1
            file_ = 0
        elif ch.isdigit():
            file_ += int(ch)
        else:
            board[chr(97 + file_) + str(rank)] = ch
            file_ += 1
    return board


def uci_to_pretty(uci: str, fen: str) -> str:
    """Best-effort human-readable move (not full SAN: no disambiguation)."""
    if not uci or len(uci) < 4:
        return uci or "?"
    board = fen_board(fen)
    frm, to, promo = uci[0:2], uci[2:4], uci[4:5]
    piece = board.get(frm, "")
    if piece.upper() == "K":
        castles = {"e1g1": "O-O", "e1c1": "O-O-O", "e8g8": "O-O", "e8c8": "O-O-O"}
        if frm + to in castles:
            return castles[frm + to]
    capture = to in board
    if piece and piece.lower() == "p":
        s = (frm[0] + "x" + to) if capture else to
        if promo:
            s += "=" + promo.upper()
        return s
    letter = piece.upper() if piece else ""
    s = f"{letter}{'x' if capture else ''}{to}"
    if promo:
        s += "=" + promo.upper()
    return s
