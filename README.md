# Chess Move Selector — live Stockfish suggestions for chess.com

A local desktop tool that watches a chess.com board you already have open,
runs **your own Stockfish** on the position in real time, and shows you the
best moves **plus the runner-up moves, colour-coded by how good they are** —
with matching arrows drawn straight onto the chess.com board.

```
chess.com tab (Chrome)  ──CDP──►  ChessComReader  ──FEN──►  Stockfish 19
        ▲                                                        │
        └────────────── arrows drawn as SVG on the board ────────┘
```

## Setup (once)

1. **Python 3.10+** and the one dependency: `pip install -r requirements.txt`
   (playwright — only the browser side needs it).
2. **Stockfish** — download the official Windows build from
   <https://stockfishchess.org/download/> and put the `.exe` in
   `bin\stockfish\` (or point **Browse…** at wherever you keep it).
3. **Chrome with remote debugging** — start a Chrome window the app can talk
   to (Chrome 136+ requires a separate profile folder for this):
   ```
   chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\chrome-debug"
   ```
   Log into chess.com in that window.

## Quick start

1. Open **chess.com** in that Chrome window (a live game, a bot game, or the
   analysis board) — whatever tab you want analysed.
2. Start the app: `.venv\Scripts\pythonw.exe src\app.py` — or make yourself a
   Desktop shortcut with `powershell -File tools\make_shortcut.ps1`.
3. The app auto-connects to the Chrome it finds chess.com in
   (CDP port, default **9222**; other local ports 9223/9224/9226/9333 are in
   the dropdown). If it says "no chess.com tab", open one and press
   **Connect & Watch**.
4. It starts watching immediately: when it's your turn you get the top
   candidate moves, each coloured by how good it is (the legend under the list
   tells you what each colour means), with the same colours drawn as arrows on
   the board. Anything you do in the app is *suggestions only* — it never
   moves pieces for you.

## What the GUI gives you

| Area | What it does |
|---|---|
| **Browser** | CDP port picker, Connect & Watch / Stop, "Open chess.com" button |
| **Stockfish engine** | pick the engine binary (SF 19 universal by default), Browse, hot-swap |
| **Analysis settings** | think time (ms), depth cap, MultiPV (1–8 candidates), Threads, Hash, optional Elo-limited strength (1320–3190) |
| **Watching** | analyze only on your turn, arrow overlay on/off + how many, always-on-top window, poll rate, "Analyze now", "Clear arrows" |
| **Candidate moves** | big best-move line + up to 8 candidates with scores and continuations, each coloured by its quality class |
| **Arrow legend** | the colour key, always on screen — what each colour means |
| **FEN** | the exact position, copyable |
| **Log** | every position, every suggestion, every problem |

## Move colours (the legend)

Every candidate move is graded by how much it loses against Stockfish's best
move, and drawn in the colour for that grade — both as arrows on the board and
as rows in the list:

| Colour | Class | Loss vs best |
|---|---|---|
| 🟩 green `#2ecc71` | **Best** | ≤ 0.10 pawns — the engine's top move |
| 🟦 blue `#3aa0ff` | **Excellent** | ≤ 0.40 pawns — nearly as strong |
| 🟩 teal `#16a085` | **Good** | ≤ 0.90 pawns — solid, small edge given up |
| 🟨 yellow `#ffc233` | **Inaccuracy** | ≤ 2.0 pawns — lets some of the edge slip |
| 🟧 orange `#ff8c1a` | **Mistake** | ≤ 5.0 pawns — clearly worse |
| 🟥 red `#e74c3c` | **Blunder** | more than that — hangs material / throws the game |

The green arrow is the move you should play; the others show what a
less-than-perfect (but still reasonable) move would cost you. Arrow width and
opacity fade with rank so the best move always stands out.

## Console mode (no GUI)

```bash
.venv\Scripts\python.exe src\app.py --console --port 9222
.venv\Scripts\python.exe src\app.py --dump --port 9222     # one-shot board read
.venv\Scripts\python.exe src\app.py --console --port 9222 --once   # one analysis, exit
.venv\Scripts\python.exe src\app.py --selfcheck            # verify the GUI loads
```

Useful flags: `--engine <path>`, `--movetime 700`, `--multipv 5` (how many
candidates to compute and colour), `--depth 0`, `--poll 500`, `--no-arrows`.

## Tools (for debugging / power use)

```bash
python tools\probe.py tabs   --port 9333           # list tabs in a Chrome
python tools\probe.py dump   --port 9333           # raw board state (FEN, turn, orientation)
python tools\probe.py dom    --port 9333           # board DOM details
python tools\probe.py click e2 e4 --port 9333      # click-move e2-e4
python tools\probe.py arrows e2e4 --port 9333      # draw a test arrow
python tools\probe.py nav URL --port 9333          # navigate the tab
python tools\watchnav.py 9333 25                   # watch for tabs reloading/bouncing
python tools\demo_game.py --port 9333 --moves 6 --autoplay   # full E2E bot-game test
python tools\check_colors.py --port 9333           # live check: colour-coded arrows
python tools\endurance_game.py --port 9333         # play a whole game to the end (verifies every arrow before each click; on a repeat it plays a next-best move and deepens the search)
python tools\test_engine.py                        # engine + parser self-test
```

All tools except `demo_game.py` and `endurance_game.py` are read-only against
the page unless you explicitly use `click` / `arrows` / `nav`.

`endurance_game.py` exits 0 only when the game **finished**, we **won**, and
every arrow was verified: zero colour mismatches, zero unreadable overlays,
zero unresolved misses, zero misclicks — **and the game record (PGN) was
captured**. It re-reads the board after the arrow
wait (so a slow check can never fire a stale click) and after each move it
confirms the intended from→to actually happened — a misclick is counted and
blocks PASS. Its repetition guard plays a next-best move and deepens the
search when a position repeats; that **reduces** threefold draws but does not
eliminate them — a drawn game is reported honestly as a non-PASS (exit 1),
never as a win. Two self-healing moves keep long runs alive: a second failed
click makes it reload the tab once (a board can read fine yet ignore input
after chess.com's rematch/ad churn — the game itself is server-side and
resumes via Continue), and a move that registers *late* is taken back from
the unresolved count, so a resolved game is never failed.

## How it works

* **Board reading** — attaches to your existing Chrome over the DevTools
  Protocol (`http://127.0.0.1:<port>`) and reads the position from
  chess.com's own board object: `document.querySelector('wc-chess-board')
  .game.getFEN()`. That gives exact FEN: castling rights, en-passant,
  side to move.
* **Your colour** — `getPlayingColor()` when available; otherwise inferred
  from board orientation (the board is always oriented toward the player).
* **Orientation detection** — chess.com's piece classes say which square
  each piece is on (`piece wr square-11` = white rook on a1). We vote across
  all pieces to decide whether the board is flipped — a single piece can be
  mid-animation (and a minimized/background tab can freeze that animation),
  so one sample is not trusted.
* **Clicks/arrows maths** — square pixel centres are ALWAYS computed from the
  board rectangle + square size + orientation, never from piece element
  positions (same animation reason).
* **Arrows** — an SVG overlay (`<svg class="hermes-arrows" viewBox="0 0 8 8">`)
  appended to the board element; 1 viewBox unit = 1 square, so it scales and
  stays aligned at any zoom/size.
* **Ad-tech blocking ("Stabilize page")** — guest chess.com pages bounce the
  whole tab onto ad "user-sync" redirect URLs (rubiconproject etc.), which
  makes the board unreadable for a few seconds at a time. With this on
  (default), requests to ad hosts are aborted while the app runs, which keeps
  the tab on chess.com. Leave it on unless you want the ads.
* **Engine** — Stockfish is started once and kept alive; each position is a
  `position fen … / go movetime …` exchange. MultiPV gives the top N lines.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "cannot reach Chrome on :XXXX" | that Chrome isn't running with remote debugging — start one or pick another port |
| "no chess.com tab open in this Chrome" | open chess.com in that browser window |
| Board not found | make sure the game/analysis board is actually on screen (not a lobby page) |
| Board element vanished on a game page | self-heals: the reader reloads the tab after ~15 board-less reads (an ad page can keep the chess.com URL and strand it) |
| Board reads fine but ignores clicks | chess.com churn can leave a "zombie" board; the endurance tool reloads the tab once after a second failed move (the game resumes server-side) |
| First analysis takes ~15 s | the 79 MB Stockfish binary is being cold-loaded (antivirus + disk cache). Later launches take <1 s. |
| Tab keeps jumping to ad pages | keep **Stabilize page** checked (default) |
| Tab ended up on an ad page anyway | it self-recovers: while watching, the reader navigates the tab back to chess.com and resumes |
| Engine binary missing | point **Browse…** at your Stockfish `.exe` (any UCI engine works — see Setup step 2) |

## Files

```
chess-move-selector/
├── src/sf_engine.py           # dependency-free UCI client + move pretty-printer
├── src/board_reader.py        # chess.com CDP reader, ad-blocking, SVG arrows
├── src/app.py                 # GUI + console app (single file)
├── tools/                     # probe / watchnav / demo_game / endurance_game / tests
└── bin/stockfish/             # (yours) the Stockfish binary you downloaded
```

The Stockfish binary and its source tree are **not** bundled in this repo:
download the official binary from <https://stockfishchess.org/download/> into
`bin\stockfish\`, or build from source (needs a C++17 toolchain):

```bash
git clone https://github.com/official-stockfish/Stockfish
cd Stockfish/src && make -j profile-build ARCH=x86-64-avx2   # MSYS2/MinGW or Linux
```

## Fair play — read this once

chess.com treats engine assistance in games against humans as cheating, and
their detection is good. Use this tool in **analysis, training, unrated games
with friends who know, or against bots**. Anything else risks the account.
This app only ever *suggests* moves and draws arrows on your own screen — it
never plays for you. (The bundled test tools `demo_game.py` / `endurance_game.py`
can autoplay **bot** games; that's verification only, and it's kept out of the app.)

## License

The wrapper code in this repo is **MIT** (see `LICENSE`). Stockfish itself is
GPL-3.0 and is **not** bundled: the app talks to the Stockfish you installed
yourself, as a separate process.
