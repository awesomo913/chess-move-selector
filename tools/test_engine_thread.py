#!/usr/bin/env python3
"""Check the engine starts both in the main thread and inside a worker thread."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sf_engine import Engine  # noqa: E402

EXE = str(ROOT / "bin" / "stockfish" / "stockfish-windows-x86-64-universal.exe")


def attempt(label: str, timeout: float = 15.0):
    t0 = time.time()
    e = Engine(EXE)
    try:
        name = e.start(timeout=timeout)
        print(f"{label}: OK -> {name}  ({time.time() - t0:.1f}s)")
    except Exception as ex:
        print(f"{label}: FAIL -> {type(ex).__name__}: {ex}  ({time.time() - t0:.1f}s)")
    finally:
        e.quit()


def main():
    attempt("main-thread")
    res = {}
    t = threading.Thread(target=lambda: attempt("worker-thread"))
    t.start()
    t.join(40)
    # and once more in the main thread to be sure
    attempt("main-thread-again")


if __name__ == "__main__":
    main()
