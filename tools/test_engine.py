#!/usr/bin/env python3
"""Self-test for the UCI client + move pretty-printer (no browser needed).

Usage: python tools/test_engine.py [engine.exe]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sf_engine import Engine, uci_to_pretty  # noqa: E402

DEFAULT = Path(__file__).resolve().parent.parent / "bin" / "stockfish" / "stockfish-windows-x86-64-universal.exe"
ENGINE = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT)


def main() -> int:
    e = Engine(ENGINE)
    print("engine:", e.start())
    e.set_options({"Threads": 2, "Hash": 64, "MultiPV": 3})
    e.new_game()

    # 1) opening position sanity
    a = e.analyze("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                  movetime_ms=600, multipv=3)
    print(f"startpos  best={a.bestmove} depth={a.depth} {a.elapsed_ms}ms")
    for pv in a.pvs:
        line = " ".join(uci_to_pretty(m, a.fen) for m in pv.pv[:6])
        print(f"   {pv.multipv}. {pv.score_str():>7}  {line}")
    assert a.bestmove, "no bestmove from startpos"
    assert a.pvs and a.pvs[0].multipv == 1

    # 2) mate in one: 1.Qxf7# (Scholar's mate)
    fen2 = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5Q2/PPPP1PPP/RNB1K1NR w KQkq - 4 4"
    a2 = e.analyze(fen2, movetime_ms=1200, multipv=3)
    print(f"mate test best={a2.bestmove} score={a2.pvs[0].score_str()} pretty={uci_to_pretty(a2.bestmove or '', fen2)}")
    assert a2.bestmove == "f3f7", f"expected f3f7, got {a2.bestmove}"
    assert a2.pvs[0].mate == 1, f"expected mate in 1, got {a2.pvs[0].score_str()}"

    # 3) checkmate position -> bestmove (none)
    fen3 = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"
    a3 = e.analyze(fen3, movetime_ms=300, multipv=1)
    print(f"mated position best={a3.bestmove}")
    assert a3.bestmove is None, f"expected no move, got {a3.bestmove}"

    # 4) pretty-printer spot checks
    assert uci_to_pretty("e2e4", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1") == "e4"
    assert uci_to_pretty("e1g1", "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1") == "O-O"
    assert uci_to_pretty("e7e8q", "8/4P3/8/8/8/8/8/4K2k w - - 0 1") == "e8=Q"

    e.quit()
    print("ALL ENGINE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
