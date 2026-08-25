# game-text-speaker

A small accessibility pipeline: watch a rectangle of your screen (a game's
dialogue box, subtitle area, etc.), OCR whatever text appears there, and
speak it aloud automatically. Built for Linux, works under both X11 and
Wayland desktop sessions. Windows is also supported, experimentally — see
the [Windows](#windows-experimental) section below.

Pipeline: **screen region → OCR (Tesseract, or Windows' built-in OCR) →
speech (espeak-ng/Piper/Kokoro/Windows SAPI5)**

## 0. Get the code

Download and extract a ZIP from GitHub's "Code" button.

Voice models aren't included in the repo (they're large, and GitHub
rejects files over 100MB anyway) — the "Better voices" section below
covers downloading them, if you want Piper or Kokoro instead of the
default espeak-ng.

## 1. Set up

A couple of system packages first, since these can't be installed by
anything below (Linux only — Windows' python.org installer already
includes everything this step covers):

```bash
sudo apt update
sudo apt install python3-venv python3-tk

# X11 desktops (GNOME on Xorg, most window managers, etc.):
sudo apt install slop

# Wayland desktops (GNOME on Wayland, Sway, etc.) — install these instead:
sudo apt install slurp grim
```

(Not sure if you're on X11 or Wayland? Run `echo $XDG_SESSION_TYPE`.)

Then, from the folder you extracted the code into:

```bash
python3 setup.py
```

(`python setup.py` on Windows.) This one command creates a virtual
environment for you — no separate "create and activate a venv" step to
remember — and opens a small window where you check off which OCR
engine(s), speech engine(s), and extras (pause hotkey, CPU affinity) you
want, then installs exactly those packages. Nothing you don't check ever
gets installed, and options that need something beyond a plain `pip
install` (the actual `tesseract-ocr`/`espeak-ng` programs, a downloaded
Piper/Kokoro voice model, a Windows OCR language pack) have a "(?)" button
telling you exactly what else to do — those genuinely can't be automated
without either running as an installer, an OS package manager, or
downloading a specific file yourself. Re-run `python3 setup.py` any time
later to add more (e.g. you started with just espeak-ng and now want
Kokoro) — it remembers what you picked and never uninstalls anything.

If you're not sure what to pick: the boxes checked by default match this
project's own defaults (Tesseract + espeak-ng on Linux, Windows OCR +
Windows SAPI5 on Windows) — good enough to get started with zero further
decisions, and safe to change your mind about later by running it again.

## 2. Launch the GUI

```bash
python3 gui.py
```

This opens a window with everything below as buttons, checkboxes, and text
fields — no more `--flags` to remember. It's a wrapper around the exact
same script and config files (`region.json`, `popup_marker.json`) as the
command-line version, so you can freely mix the two. Its own settings
(speech engine, rate, voice, etc.) are remembered between launches in
`gui_settings.json` next to the script — delete that file to reset the GUI
to defaults.

The window has, top to bottom: a **region** section with "Select Region…"
(same click-and-drag as `--select` below) and "From Screenshot…" (same as
`--select-from-image`); a **popup/overlay ignoring** section (optional —
see below); a **speech** section to switch between espeak-ng/Piper/Kokoro/
Windows SAPI5 and set their options (info buttons next to less-obvious
fields explain what they do, and options you don't have the dependencies
for yet are greyed out with a note on what to install); an **OCR/timing**
section for language, poll interval,
similarity, OCR cleanup, and speaker-name handling; and Start/Stop buttons
with a live log where the spoken text scrolls by.

You still need to open a terminal once to run `python3 gui.py` (and
`source venv/bin/activate` first). To skip even that, add it to your
application menu instead:

```bash
./install.sh
```

This writes a launcher into `~/.local/share/applications/` — the standard
per-user location every major Linux desktop (GNOME, KDE, XFCE, ...)
already scans for its app menu, so no root and no system-wide changes are
needed. "Game Text Speaker" should show up there afterward. A `.desktop`
file can't have a path baked in ahead of time that works on every
computer, so `install.sh` fills in the real, current path at install time
using `game-text-speaker.desktop.template` as the source — re-run it any
time after moving the folder to fix the launcher. `./uninstall.sh` removes
it. To do it by hand instead, see `game-text-speaker.desktop.template` for
the format, with `INSTALL_DIR` swapped for your actual path.

Everything past this point describes the same functionality from the
command line — useful for reference on what each GUI field does, for
scripting, or if you'd rather not use a GUI at all.

## Command-line usage

### Pick the screen region to watch

**First, put the game in Borderless Windowed mode** (in its in-game video/
display settings). Exclusive fullscreen bypasses your desktop compositor
entirely, so nothing can be drawn on top of the game *and* the live screen
capture used by `--run` generally can't read it either — this is a
prerequisite for the whole pipeline, not just for selecting the region.

With that set, get the game to a point where the dialogue/subtitle box is
visible, then (with the venv active, in a terminal):

```bash
python3 game_text_speaker.py --select
```

Your cursor becomes a crosshair and stays armed even if a different window
is focused — so right after running the command, Alt+Tab back to the
game, then click and drag a box tightly around the text area (a tighter
box means faster, cleaner OCR). This is saved to `region.json` next to the
script, so you only need to do this once per game/resolution; re-run
`--select` if the game window moves or resizes.

**If you can't get the game into windowed mode**, you likely can't run the
live pipeline at all on Linux. As a next-best option for picking the
region from a screenshot instead:

```bash
python3 game_text_speaker.py --select-from-image /path/to/screenshot.png
```

Take the screenshot with Steam's own screenshot key (default `F12`, works
even over exclusive fullscreen; find it via Library → right-click the game
→ Manage → Show Screenshots) while dialogue is on screen. Needs
`python3-tk`. Note: this only produces coordinates that line up with
`--run` later if the game can also be captured live.

### Run it

```bash
python3 game_text_speaker.py --run
```

Polls the region twice a second by default and speaks any new text via
espeak-ng. Press Ctrl+C to stop. Combine both steps:
`python3 game_text_speaker.py --select --run`

## Ignoring popups/overlays (optional)

*(In the GUI: the "Ignore popups/overlays" section — same idea, a "Select
Popup Marker…" button plus a checkbox and threshold field.)*

Some games show popups or overlays on top of the base screen — item
pickups, achievement toasts, menu confirmations — that you may not want
read. `--ignore-popups` handles this by fingerprinting a small spot that's
a giveaway the overlay is up, then skipping the whole poll whenever that
spot matches.

First, get the popup/overlay showing on screen, then:

```bash
python3 game_text_speaker.py --select-popup-marker
```

Same click-and-drag mechanic as `--select`. Pick somewhere small that's
*always* that exact look while the overlay is up and never otherwise —
a border, corner tint, fixed icon. Avoid text (OCR varies letter to
letter). Saved to `popup_marker.json`. Then run with it enabled:

```bash
python3 game_text_speaker.py --run --ignore-popups
```

While the marker matches, polls are skipped entirely (`[popup] skipping
poll, marker matched` in the terminal). `--popup-threshold` (default `20`,
range `0`–`441`) controls how close a live color has to be to the saved
one to count as a match — raise it if real popups get missed, lower it if
it skips dialogue that isn't a popup, or re-pick a more distinctive spot.

## Useful options

| Flag | Default | What it does |
|---|---|---|
| `--interval` | `0.5` | Seconds between screenshots. Lower = more responsive, more CPU. |
| `--engine` | `espeak` on Linux, `windows` on Windows | TTS engine: `espeak` (robotic, needs espeak-ng installed separately), `piper` (natural neural voices), `kokoro` (most natural of the four), or `windows` (Windows-only, and the zero-setup option there: the OS's own built-in SAPI5 voices, comparable quality to espeak-ng, via `pip install pyttsx3` — no PATH setup or download needed; see [Windows](#windows-experimental)). |
| `--rate` | `175` | Speaking rate (words/minute) for `--engine espeak` or `windows`. Ignored by `--engine piper`/`kokoro`. |
| `--voice` | system default | Voice name/code for `--engine espeak` or `windows` — for `espeak` e.g. `--voice en-us`, `--voice en-gb`, or an mbrola voice like `--voice mb-us1` (run `espeak-ng --voices` to list all); for `windows`, a substring of an installed SAPI5 voice's name, e.g. `--voice zira` (see the GUI's Voice dropdown for exact installed names). Ignored by `--engine piper`/`kokoro`. |
| `--piper-model` | — | Path to a Piper `.onnx` voice file. Required when `--engine piper`. |
| `--piper-speaker` | `0` (if multi-speaker) | Speaker ID for a multi-speaker Piper model. Ignored by single-speaker models. |
| `--piper-length-scale` | `1.0` | Piper speed multiplier. **Lower = faster** (`0.5` = double speed), **higher = slower** (`2.0` = half speed). |
| `--kokoro-model` | — | Path to Kokoro's `kokoro-v1.0.onnx` file. Required when `--engine kokoro`. |
| `--kokoro-voices` | — | Path to Kokoro's `voices-v1.0.bin` file. Required when `--engine kokoro`. |
| `--kokoro-voice` | `af_heart` | Kokoro voice name (see below for the full list). |
| `--kokoro-speed` | `1.0` | Kokoro speed multiplier. **Higher = faster**, **lower = slower** — the *opposite* direction from `--piper-length-scale`. |
| `--kokoro-lang` | `en-us` | Kokoro language code. |
| `--kokoro-cpu-threads` | unset | Caps CPU threads for a single Kokoro synthesis call. If the game is already maxing out your CPU, letting one call fight for every thread tends to cause *longer* pauses, not faster speech — try capping it (e.g. `6` on an 8-core/16-thread CPU) and see if pauses smooth out. Needs a `kokoro-onnx` with `Kokoro.from_session()`; older installs log a note and ignore this. |
| `--cpu-affinity` | unset | Pins the whole process (OCR + Kokoro) to specific CPU cores, e.g. `--cpu-affinity 4,5,6,7`, reserving real uncontended time instead of time-sharing with the game. Pair with `--kokoro-cpu-threads` set to the same core count. Works on Linux (built in) and Windows (needs `pip install psutil`); a no-op elsewhere. |
| `--ocr-engine` | `tesseract` on Linux, `windows` on Windows | OCR backend. `tesseract` needs the `tesseract-ocr` binary installed separately (any OS). `windows` uses Windows' own built-in OCR instead (via `pip install winocr`) — no separate binary or PATH entry, but no per-word confidence score, so `--ocr-min-confidence` has no effect on it. See [Windows](#windows-experimental). |
| `--lang` | `eng` | OCR language code — meaning depends on `--ocr-engine`. Tesseract wants its own codes, e.g. `--lang fra` for French (needs `tesseract-ocr-<lang>` installed); the `windows` engine wants a BCP-47 tag instead, e.g. `--lang fr` (needs that language's Windows OCR pack installed). The default is remapped from `eng` to `en` automatically when `--ocr-engine windows` is used and `--lang` wasn't set explicitly. |
| `--similarity` | `0.92` | How similar new OCR text has to be to the last line to count as "unchanged" and get skipped (0–1). Lower it if fast-scrolling text gets skipped; raise it if OCR jitter causes repeats. If a line is still being read aloud when new text arrives, the old one is cut off in favor of the new. |
| `--ocr-min-confidence` | `40` | Drops OCR'd words below this confidence score (0–100) before they're spoken — cleans up screen artifacts (dust, UI borders, icons) that would otherwise be read as stray punctuation or gibberish. Raise it if artifacts still slip through; lower it if real dialogue starts getting dropped. A period landing mid-sentence (e.g. a smudge OCR'd as "young. man") is always dropped too, since real sentence-ending periods are followed by a capitalized word — a short list of abbreviations (`Mr.`, `Dr.`, `etc.`, ...) is exempted. Only applies to `--ocr-engine tesseract` — the `windows` engine has no per-word confidence to filter on. |
| `--speaker-name-mode` | `off` | For dialogue boxes that show a character's name above their quoted line (which OCR runs straight into the dialogue, e.g. `Augustin El Borne "And this must be...`): `skip` drops the name and speaks only the dialogue; `announce` speaks the name first with a pause. `off` speaks the text exactly as OCR'd. Detection looks for the dialogue's own opening quote mark and treats short, Title-Case text before it as a name — a narration line that merely contains a quote mid-sentence won't match. Only helps for games that fully quote their dialogue. |
| `--ignore-popups` | off | Skip polls while the saved popup marker matches (see above). Needs `--select-popup-marker` run first. |
| `--popup-threshold` | `20` | How close a live color has to be to the saved marker to count as a match (0–441). |
| `--quiet` | off | Print recognized text but don't speak it — useful for testing region/OCR setup. |
| `--pause-key` | `space` | Key that pauses/resumes the narrator from anywhere, even with the game focused (see below). Empty string disables it. |

## Pausing the narrator

*(In the GUI: the "Pause key" field under OCR/timing.)*

Press the pause key at any time — even while the game window has focus —
to pause the narrator (stops mid-sentence, stops watching the screen), and
press it again to resume (it re-speaks whatever's currently in the
dialogue box). Default is **space**; change it with `--pause-key <name>`
(try `f9`, `scrolllock`, etc.), or set it to an empty string to disable.

On Linux, this reads raw keyboard events from the kernel (via `evdev`)
rather than a "global hotkey" library, since those don't work under
Wayland. Two things follow:

- **Needs the `evdev` package** — check "Pause hotkey" in `python3
  setup.py` (or `pip install evdev` by hand). If it's missing,
  `/dev/input` isn't readable, or the key name isn't recognized, the
  hotkey just logs a note and disables itself.
- **Needs read access to `/dev/input`** — usually the `input` group:
  `sudo usermod -aG input $USER`, then **log all the way out and back in**
  for it to take effect.

On Windows, there's no X11-vs-Wayland split to work around, so this uses
the `keyboard` package's global hook instead — same "Pause hotkey"
checkbox installs `keyboard` there. Same degrade-gracefully behavior if
it's missing or the key name isn't recognized.

**Caveat:** this only *watches* the key, it doesn't intercept it, so
whatever the game does with that key still happens too. If your game
binds `space` to something (advance dialogue, jump, interact), pick a key
it doesn't use — `f9` or `scrolllock` work well since games rarely bind
them.

**If pausing "undoes itself" instantly** (pauses for a split second, then
keeps talking) rather than staying paused, that's usually a wireless
keyboard/mouse receiver reporting as more than one `/dev/input` device, so
a single press fires the toggle twice. The script already collapses a
second toggle arriving within 0.3s of the first, so this should be rare —
if it still happens, check the log's `[hotkey] Paused`/`Resumed`
timestamps to tell a duplicate-device issue (right on top of each other)
from a real second keypress (later).

## Better voices

*(In the GUI: the "Speech" section's engine choice — the mbrola tweak
below still applies, just typed into the GUI's Voice field.)*

Piper's and Kokoro's own pip packages (`piper-tts`, `kokoro-onnx`) install
via checking their box in `python3 setup.py` — the `pip install` commands
below are shown for reference/troubleshooting, not as a separate step you
need to run by hand. The voice model downloads below, though, aren't
something setup.py can do for you (there's no single "the" model — you're
picking one), so those stay a manual step either way.

espeak-ng (the default on Linux — see [Windows](#windows-experimental) for
what's default there) is a formant synthesizer — fast, tiny, simple to
install, but always sounds robotic. Two upgrade paths:

**Quick tweak, no new software — mbrola voices.** Plug into espeak-ng and
sound noticeably smoother, though still clearly synthetic:

```bash
sudo apt install mbrola mbrola-us1
python3 game_text_speaker.py --run --voice mb-us1
```

Swap `mbrola-us1`/`mb-us1` for another accent — `apt search mbrola-` lists
what's packaged.

**Real upgrade — Piper.** [Piper](https://github.com/rhasspy/piper) is a
small neural TTS engine: genuinely natural-sounding, fully local/offline,
fast enough for live dialogue on CPU.

```bash
pip install piper-tts
```

Download a voice model — a `.onnx` file plus a matching `.onnx.json`
config, both required, sitting next to each other with matching names:

```bash
mkdir -p voices
curl -Lo voices/en_US-lessac-medium.onnx https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
curl -Lo voices/en_US-lessac-medium.onnx.json https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json
```

Browse/preview voices at
[rhasspy.github.io/piper-samples](https://rhasspy.github.io/piper-samples/)
(files at [piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main),
full catalog in [VOICES.md](https://github.com/rhasspy/piper/blob/master/VOICES.md)).
Run with:

```bash
python3 game_text_speaker.py --run --engine piper --piper-model voices/en_US-lessac-medium.onnx
```

Needs `aplay` or `paplay` on Linux (`sudo apt install alsa-utils` or
`pulseaudio-utils` if missing); on Windows, needs `pip install sounddevice`
instead (see [Windows](#windows-experimental)).

**Multi-speaker models** (check a model's `.onnx.json` for `"num_speakers"`
> 1) need `--piper-speaker <id>` — left unset, the script defaults to
speaker 0 and logs a note, which is arbitrary for a hundreds-of-speakers
model. `preview_piper_speakers.py` generates short preview clips for a
spread of speakers so you're not guessing one at a time:

```bash
python3 preview_piper_speakers.py --model voices/en_US-libritts-high.onnx
```

Writes one `.wav` per sampled speaker into `speaker_previews/` (16 by
default — `--count` to change, or `--speakers 0,42,100,500` for exact
IDs). Play through them and put the number you like into `--piper-speaker`.

**Piper speed** uses `--piper-length-scale <value>` instead of a
words-per-minute rate: **lower = faster** (`0.5` = double speed),
**higher = slower** (`2.0` = half speed), `1.0` = normal. Push it too low
and words slur, so nudge in small steps (`0.9`, `0.8`, `0.7`...).

**Even more natural — Kokoro.** [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)
is a newer, larger neural TTS model than Piper — more natural still, at
the cost of a bigger download. Fully local/offline, fast enough for live
dialogue on a normal CPU.

```bash
pip install kokoro-onnx
```

Download two files (unlike Piper, names don't need to match — just point
`--kokoro-model`/`--kokoro-voices` at them):

```bash
mkdir -p voices
curl -Lo voices/kokoro-v1.0.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -Lo voices/voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

(The releases page also has a ~80MB "quantized" `.onnx` if the ~300MB full
one is too much download — same usage, slightly lower quality.) Run with:

```bash
python3 game_text_speaker.py --run --engine kokoro --kokoro-model voices/kokoro-v1.0.onnx --kokoro-voices voices/voices-v1.0.bin
```

Needs `aplay`/`paplay` (or `sounddevice` on Windows) like Piper. The first line spoken has a short delay
while the model loads; after that, synthesis runs in the background and
is played back chunk-by-chunk as it's generated, so it doesn't hold up
screen-watching or the pause hotkey. If CPU load is causing pauses, see
the `--kokoro-cpu-threads`/`--cpu-affinity` rows in the options table
above.

**Picking a voice.** Kokoro ships ~50 preset voices named like `af_heart`
or `am_michael` (language+gender prefix, arbitrary name after the
underscore). `af_heart` (the default) is generally rated best-sounding;
`af_bella` a close second. Change with `--kokoro-voice <name>` — full list
with quality gradings in the model's
[VOICES.md](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md).

**Kokoro speed** — `--kokoro-speed <value>` works the *opposite* direction
from Piper: **higher = faster** (`2.0` = double), **lower = slower**
(`0.5` = half), `1.0`/blank = normal.

**Reorganizing:** `region.json`, `popup_marker.json`, and
`gui_settings.json` always save next to the scripts and can't be moved.
Voice models can live anywhere — just update `--piper-model`/
`--kokoro-model` to match (keep Piper's `.onnx`/`.onnx.json` pair together;
Kokoro's two files can live independently).

## Windows (experimental)

Windows support exists but is new and, since this project is developed on
Linux, hasn't been run on real Windows hardware — if something below
doesn't work, that's expected territory; please open an issue with what
happened. Everything above this section (usage, options, voices, pausing)
works the same way on Windows; this section covers what's actually
different: getting set up, and building a standalone `.exe`.

**Setup is the same one command as Linux** — see "1. Set up" near the top
of this README: install [Python 3](https://www.python.org/downloads/)
(check "Add python.exe to PATH" — this also brings `tkinter` along, no
separate install needed), then from the folder you extracted the code
into:

```powershell
python setup.py
```

The checkbox GUI that opens is exactly the same script as Linux, it just
offers different boxes here: **Windows OCR** and **Windows SAPI5** (both
built into the OS itself — genuinely zero setup beyond the pip package
each needs, and both checked by default) alongside the same Tesseract/
espeak-ng/Piper/Kokoro options Linux has. Checking Windows OCR and Windows
SAPI5 (the defaults) and nothing else gets you a fully working setup with
no separate installer, PATH entry, or downloaded model needed anywhere —
if you'd rather use Tesseract or espeak-ng instead (e.g. you're already
set up with Tesseract, or want `--ocr-min-confidence`'s screen-artifact
filtering, which the Windows OCR engine can't do), check those boxes
too and follow their "(?)" button's install instructions.

Two Windows-only wrinkles worth knowing about, both handled for you
already — mentioned here only so they don't look like bugs if you go
looking:

- **Region selection works differently.** There's no Windows equivalent
  of `slop`/`slurp`, so `--select` (and the GUI's "Select Region…" button)
  takes a screenshot first and lets you drag a box on that — the same
  no-time-pressure mechanism `--select-from-image` already uses on Linux
  for games that can't be alt-tabbed away from.
- **No console-window flicker.** espeak-ng runs as a subprocess, and
  Windows opens a console window for every one it spawns by default; this
  is suppressed internally so nothing flashes on screen while lines are
  being spoken. (Piper, Kokoro, and Windows SAPI5 all synthesize
  in-process instead of spawning anything, so this only ever applied to
  espeak-ng in the first place.)

**Building a standalone `.exe`.** So people you share this with don't need
Python installed at all, `setup.py`'s window has a "build .exe" checkbox
(Windows only) that wraps [PyInstaller](https://pyinstaller.org/) right
after Install finishes. It bundles Python and whichever pip packages are
checked *in that same click* — so check the boxes for everything you want
included, then click Install with "build .exe" also checked. With Windows
OCR and Windows SAPI5 both checked before building, the resulting `.exe`
needs no outside dependencies at all on the machine it's run on. espeak-ng
and Tesseract (if you're using either instead of the Windows-only
defaults) are still separate system installs either way — PyInstaller only
bundles Python code, not standalone binaries — so anyone you hand the
`.exe` to still needs those installed if they're using those specific
features. Like the rest of Windows support, this is unverified on real
hardware — see the checkbox's own code comments (in `setup.py`,
`_build_exe()`) for the most likely things to check if a build or the
resulting `.exe` misbehaves.

The built `.exe` itself is intentionally never committed to the repo —
see `.gitignore` for why.

## Troubleshooting

- **`error: externally-managed-environment` from pip**: you're installing
  outside the venv — either run `python3 setup.py` instead (it always
  installs into `venv/`, never system-wide), or `source venv/bin/activate`
  first if you're installing something by hand.
- **OCR text is garbled/empty**: tighten the region to just the text
  itself. Try increasing in-game contrast (some games have a "text box
  opacity" setting). Run with `--quiet` first to iterate without wading
  through speech.
- **`slop`/`slurp` does nothing when you drag**: wrong tool for your
  session type — `slop` is X11-only, `slurp` is Wayland-only.
- **No sound (either engine)**: the log surfaces speech-process failures
  automatically as a `[speech] ... exited with code N: <error text>` line
  — read that first, it usually names the actual problem.
- **No sound, and no `[speech]` error line**: confirm the engine works
  outside this script — `espeak-ng "test"`, or for Piper: `echo "test" |
  piper --model voices/en_US-lessac-medium.onnx --output_file /tmp/t.wav`
  then `aplay /tmp/t.wav`. Silent there too means it's an audio/ALSA-
  PulseAudio issue outside this script's control.
- **No sound specifically via the GUI/`.desktop` launcher, but fine from a
  terminal**: usually a `PATH` difference — the launcher calls
  `venv/bin/python3` directly, skipping `source venv/bin/activate`, so
  a system-level command found only via PATH (`espeak-ng`) won't be found
  by name. (This doesn't apply to Piper/Kokoro/Windows SAPI5 — they're
  imported as regular pip packages, which doesn't care about PATH or
  activation either way.) Test `python3 gui.py` from an activated terminal
  to confirm.
- **Fullscreen exclusive games**: switch to "borderless windowed" or
  "windowed" mode — screen-capture tools can't grab exclusive fullscreen
  on Linux.
- **`python3 setup.py` or `python3 gui.py` fails with "Missing tkinter"**:
  `sudo apt install python3-tk` (a system package, not pip-installable into
  the venv), then try again.
- **GUI window never appears / errors on launch**: run it from a terminal
  (not the `.desktop` launcher) to see the actual error message.
- **GUI's Start button does nothing / shows a warning dialog**: it
  validates before starting — the popup names exactly what's missing
  (no region selected, ignore-popups checked but no marker saved, Piper
  selected but no model chosen, etc.).
- **GUI's Stop button feels slow**: checked roughly every 50ms, so it
  should be near-instant — if it's genuinely hanging, check for a zombie
  `espeak-ng` process (`ps aux | grep espeak`). (Piper/Kokoro/Windows
  SAPI5 synthesize in-process rather than as a separate OS process, so
  there's nothing to find this way for those three — they're stopped by
  the same utterance-id mechanism instead; see `_stop_current()` in
  `game_text_speaker.py` if one of those genuinely won't stop.)
- **`setup.py` seems stuck on "Creating a virtual environment..."**: venv
  creation itself is normally quick (a few seconds) — if it's genuinely
  hung, Ctrl+C, delete the `venv/` folder, and run `python3 setup.py`
  again.
- **`setup.py`'s Install button reports a pip error**: usually a network
  hiccup — scroll up in its log for the actual pip error text, then just
  click Install again. If a specific package keeps failing, that package's
  own PyPI page is the next place to check.
- **Build standalone .exe fails with "pythoncom won't import cleanly" (or
  the older, more confusing "Failed to retrieve attribute \_\_file\_\_
  from module pythoncom")**: pywin32 (pulled in by pyttsx3 for the SAPI5
  engine) got installed but never finished its own setup step. `setup.py`
  now catches this itself and prints the exact fix commands to try before
  clicking Build again (in order: reinstall pywin32 with `--force-
  reinstall`, run its `pywin32_postinstall.py -install` script directly if
  present, or remove a stray `pypiwin32` alongside it) — if you're seeing
  the raw PyInstaller error instead of that friendlier message, you're
  likely on an older copy of `setup.py` from before this was added.