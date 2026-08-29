# game-text-speaker

Tired of reading simulators? Bring voice to your silent RPGs, visual
novels, and JRPGs — game-text-speaker watches a rectangle of your screen,
reads new dialogue aloud the instant it appears, and can give every
character their own distinct voice. Your protagonist doesn't have to stay
the strong, silent type; neither does anyone else in the cast.

**Pipeline:** screen region → OCR (Tesseract, or Windows' built-in OCR) →
speech (espeak-ng, Piper, Kokoro, or Windows SAPI5)

Runs on Linux (X11 or Wayland) and Windows, entirely through a
point-and-click GUI — no command-line options to learn. Everything
game-specific (the region, popup marker, and cast of character voices) is
saved to a **profile** (`profiles/`), so switching games is just switching
profiles.

## Get the code

Download and extract a ZIP from GitHub's "Code" button, or `git clone`.
Voice models aren't included (they're large) — see "External components"
below.

## Set up — Windows

Install [Python 3](https://www.python.org/downloads/), checking "Add
python.exe to PATH" (this brings `tkinter` along too). Then, from the
extracted folder:

```powershell
python setup.py
```

A checkbox window opens where you pick which OCR engine(s), speech
engine(s), and extras you want, defaulting to **Windows OCR** and
**Windows SAPI5** — both built into the OS, so accepting the defaults
needs no separate installer, PATH entry, or downloaded model at all.
Re-run this any time to add more later; it remembers your picks and never
removes anything.

**Optional: build a standalone `.exe`.** So other people don't need Python
installed, check "build .exe" in the same window before clicking Install —
it wraps [PyInstaller](https://pyinstaller.org/) around whatever engines
you checked. Tesseract and espeak-ng (if either is checked) still have to
be installed separately on the machine that *runs* the `.exe` — PyInstaller
only bundles Python code, not system binaries. **Before handing a built
`.exe` to anyone else**, see "Licensing" below — Piper or Kokoro pull in a
GPL-3.0 component that has to travel with it.

## Set up — Linux

```bash
sudo apt install python3-venv python3-tk
sudo apt install slop          # X11 sessions
sudo apt install slurp grim    # Wayland sessions (not sure which you're on? echo $XDG_SESSION_TYPE)

python3 setup.py
```

`setup.py` creates a `venv/` for you and opens a checkbox window where you
pick which OCR engine(s), speech engine(s), and extras you want — it
installs exactly those pip packages and nothing else. Re-run it any time
to add more later; it remembers your picks and never removes anything.

Then launch with `python3 gui.py`, or run `./install.sh` once to add a
"Game Text Speaker" entry to your application menu (`./uninstall.sh`
removes it).

There's no compiled build on Linux — it always runs from source inside
`venv/`.

## Running it

Launch the app (`python3 gui.py` on Linux, or the built `.exe`/application
menu entry), pick or create a profile, click "Select Region" and drag a
box around the dialogue area, then click Start. Every other setting
(speech engine, voice, OCR language, and more) is a field or dropdown in
the window, each with an ⓘ button next to anything non-obvious.

A few features worth knowing about going in:

- **Game profiles** — each profile keeps its own region, popup marker, and
  per-character cast, so switching games doesn't mean re-dragging the box.
  The Export/Import buttons share a profile's cast (not your screen
  coordinates) with someone else playing the same game.
- **Popup/overlay ignoring** — the "Select Popup Marker" button
  fingerprints a spot that's only present when a popup or overlay is up;
  check "Ignore popups while running" to skip OCR/speech while it matches.
- **Pause hotkey** — a global key (default `space`, changeable in the OCR/
  timing section) pauses/resumes the narrator even while the game has
  focus.

## Wanted: voice casts for more games

Building a cast — matching a distinct voice to every named character in a
game — is the most time-consuming part of setting this up, and it's work
that only has to happen once per game if it's shared. If you've put
together a cast you're happy with, export it (the main window's Export
button, next to Import) and send it in as a pull request or attach it to
an issue on GitHub — it only contains character names and voice
assignments, never your screen coordinates. The more games with a ready-
made cast, the less setup everyone else has to do.

## External components (not installed by setup.py)

`setup.py` only installs pip packages. Anything that needs a real
installer, a download, or an OS package manager is on you — the GUI's ⓘ
buttons show these same instructions in context.

| Component | Needed for | Linux | Windows |
|---|---|---|---|
| Tesseract OCR binary | Tesseract OCR engine (Linux default) | `sudo apt install tesseract-ocr` | [UB Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki); add its install folder to PATH |
| espeak-ng binary | espeak-ng speech engine (Linux default) | `sudo apt install espeak-ng` | [espeak-ng releases](https://github.com/espeak-ng/espeak-ng/releases) `.msi` |
| Windows OCR language pack | Windows OCR engine, non-English | — | Settings → Time & Language → Language; the app prints the exact command if one's missing |
| Piper voice model (`.onnx` + `.onnx.json`) | Piper speech engine | Download from [piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main); preview at [rhasspy.github.io/piper-samples](https://rhasspy.github.io/piper-samples/) | same |
| Kokoro model files (`kokoro-v1.0.onnx` + `voices-v1.0.bin`) | Kokoro speech engine | Download from [kokoro-onnx releases](https://github.com/thewh1teagle/kokoro-onnx/releases) | same |
| mbrola voice (optional espeak-ng upgrade) | espeak-ng engine, typed into the GUI's Voice field (e.g. `mb-us1`) | `sudo apt install mbrola mbrola-us1` | not supported |
| `/dev/input` group access | pause hotkey | `sudo usermod -aG input $USER`, then log fully out and back in | n/a (uses the `keyboard` package instead) |

Put downloaded voice files in `voices/` (any name — point the GUI's Model
field at wherever they actually are).

## Licensing

This project's own code is **GPL-3.0** (see `LICENSE`). It optionally
depends on a number of third-party packages and models, most under
permissive licenses (MIT, BSD, Apache-2.0) that just require keeping their
notices — see `THIRD_PARTY_LICENSES.md` for the full breakdown, including:

- `piper-tts` and `kokoro-onnx` each embed a compiled copy of **espeak-ng
  (GPL-3.0)**. Running the app or reading its source triggers no
  obligation, but **sharing a built `.exe` that has Piper or Kokoro
  enabled** does — the recipient needs `LICENSE`/`THIRD_PARTY_LICENSES.md`
  alongside it. (The plain "espeak-ng (robotic)" engine calls a separately
  installed binary instead, which isn't affected by this.)
- The `en_US-libritts-high` Piper voice is **CC BY 4.0** and needs an
  attribution credit if you share it or its output.

The GUI's "Licenses" button (in the main window's Game row) opens
`THIRD_PARTY_LICENSES.md` directly.

## Troubleshooting

- **`error: externally-managed-environment` from pip**: you're outside the
  venv — use `python3 setup.py`, or `source venv/bin/activate` first.
- **No sound, no error in the log**: test the engine standalone (e.g.
  `espeak-ng "test"`); if that's silent too, it's an OS audio issue outside
  this app.
- **Region selection does nothing when you drag**: `slop` is X11-only,
  `slurp` is Wayland-only — install the one matching your session.
- **Fullscreen games**: switch to borderless/windowed — screen capture
  can't read exclusive fullscreen on Linux.

More specific errors are generally self-explanatory in the GUI's log or
the terminal; the GUI's Start button also validates your setup before
running and names exactly what's missing.
