#!/usr/bin/env python3
"""
game_text_speaker.py — OCR-to-speech accessibility pipeline for games.

Watches a chosen rectangle of your screen (e.g. a dialogue box), OCRs it on
a poll loop, and speaks new/changed text aloud via espeak-ng. Works on both
X11 (using slop + mss) and Wayland (using slurp + grim).

Usage:
    # One-time: drag a box around the text area you want watched.
    python3 game_text_speaker.py --select

    # Run the pipeline (uses the region saved by --select).
    python3 game_text_speaker.py --run

    # Handy combo: pick a region, then immediately start speaking it.
    python3 game_text_speaker.py --select --run

See README.md for install instructions and troubleshooting.
"""

import argparse
import asyncio
import difflib
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("region.json")
POPUP_MARKER_PATH = Path(__file__).with_name("popup_marker.json")


# --------------------------------------------------------------------------
# Session detection (X11 vs Wayland) — the two desktop stacks need
# different tools for "let the user drag a box" and "screenshot a box".
# --------------------------------------------------------------------------

def is_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE") == "wayland"


def check_dependency(cmd: str, apt_pkg: str) -> None:
    if shutil.which(cmd) is None:
        sys.exit(
            f"Missing required command '{cmd}'.\n"
            f"Install it with:  sudo apt install {apt_pkg}\n"
            f"(see README.md for the full dependency list)"
        )


def apply_cpu_affinity(affinity: str, log=print) -> None:
    """Pins this whole process (OCR loop + Tesseract calls + Kokoro/Piper
    synthesis, since affinity is inherited by every thread) to a specific
    set of CPU cores, e.g. "12,13,14,15". On a busy system -- a demanding
    game hogging every core -- this reserves real, uncontended CPU time for
    the narrator instead of leaving the OS scheduler to time-share it with
    everything else, which is what causes long, unpredictable pauses before
    a line starts playing. Linux-only (os.sched_setaffinity doesn't exist
    on macOS/Windows); a no-op if `affinity` is empty."""
    if not affinity:
        return
    if not hasattr(os, "sched_setaffinity"):
        log("[cpu-affinity] Not supported on this OS (Linux-only) -- ignoring --cpu-affinity.")
        return
    try:
        cores = {int(c.strip()) for c in affinity.split(",") if c.strip() != ""}
        if not cores:
            return
        os.sched_setaffinity(0, cores)
        log(f"[cpu-affinity] Pinned this process to CPU core(s): {sorted(cores)}")
    except Exception as e:
        log(f"[cpu-affinity] Couldn't set CPU affinity {affinity!r}: {e}")


# --------------------------------------------------------------------------
# Region selection — run once, saves {x, y, w, h} to region.json
# --------------------------------------------------------------------------

def select_region(log=print) -> dict:
    log("Drag a box around the text area you want watched (e.g. the dialogue box)...")
    if is_wayland():
        check_dependency("slurp", "slurp")
        out = subprocess.run(["slurp"], capture_output=True, text=True, check=True).stdout.strip()
        # slurp output format: "X,Y WxH"
        m = re.match(r"(\d+),(\d+)\s+(\d+)x(\d+)", out)
        if not m:
            sys.exit(f"Couldn't parse slurp output: {out!r}")
        x, y, w, h = map(int, m.groups())
    else:
        check_dependency("slop", "slop")
        out = subprocess.run(
            ["slop", "-f", "%x %y %w %h"], capture_output=True, text=True, check=True
        ).stdout.strip()
        x, y, w, h = map(int, out.split())

    region = {"x": x, "y": y, "w": w, "h": h}
    CONFIG_PATH.write_text(json.dumps(region, indent=2))
    log(f"Saved region {region} to {CONFIG_PATH}")
    return region


def load_region() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit("No saved region found. Run with --select first to pick one.")
    return json.loads(CONFIG_PATH.read_text())


# --------------------------------------------------------------------------
# Popup/overlay ignoring — pick a small spot that's only ever a certain
# color while a popup/dialog page is up (its border, its dimmed backdrop,
# whatever's consistent), fingerprint that color once, then on every poll
# check whether that spot currently matches it. If so, a popup is showing
# and the whole poll is skipped — no OCR, nothing spoken — so only the
# base screen ever gets read aloud.
# --------------------------------------------------------------------------

def average_color(img) -> list:
    """Average RGB of an image, computed cheaply via a 1x1 box-filter resize."""
    from PIL import Image

    tiny = img.resize((1, 1), resample=Image.BOX)
    r, g, b = tiny.getpixel((0, 0))[:3]
    return [r, g, b]


def color_distance(c1, c2) -> float:
    return sum((a - b) ** 2 for a, b in zip(c1, c2)) ** 0.5


def select_popup_marker(log=print) -> dict:
    log(
        "First, get the popup/overlay you want ignored showing on screen. "
        "Then drag a small box over a spot that's ALWAYS that same look while "
        "the popup is up — e.g. its border, or a patch of its background/dim "
        "overlay. Avoid text (it varies); a small solid-ish patch of color "
        "works best."
    )
    if is_wayland():
        check_dependency("slurp", "slurp")
        out = subprocess.run(["slurp"], capture_output=True, text=True, check=True).stdout.strip()
        m = re.match(r"(\d+),(\d+)\s+(\d+)x(\d+)", out)
        if not m:
            sys.exit(f"Couldn't parse slurp output: {out!r}")
        x, y, w, h = map(int, m.groups())
    else:
        check_dependency("slop", "slop")
        out = subprocess.run(
            ["slop", "-f", "%x %y %w %h"], capture_output=True, text=True, check=True
        ).stdout.strip()
        x, y, w, h = map(int, out.split())

    marker_region = {"x": x, "y": y, "w": w, "h": h}
    capturer = ScreenCapturer(marker_region)
    ref_color = average_color(capturer.grab())

    marker = {**marker_region, "ref_color": ref_color}
    POPUP_MARKER_PATH.write_text(json.dumps(marker, indent=2))
    log(f"Saved popup marker {marker} (fingerprinted color {ref_color}).")
    log(
        "Sanity check: watch the log for a bit with the popup CLOSED — if you "
        "see '[popup] skipping...' messages while no popup is showing, the spot "
        "you picked isn't unique to the popup; re-select the marker and pick "
        "somewhere more distinctive."
    )
    return marker


def load_popup_marker():
    if not POPUP_MARKER_PATH.exists():
        return None
    return json.loads(POPUP_MARKER_PATH.read_text())


def select_region_from_image(image_path: str, master=None, log=print) -> dict:
    """Fallback region picker for when the game can't be alt-tabbed away
    from (e.g. exclusive fullscreen blocks slop/slurp's overlay). You take
    a full-screen screenshot *of your desktop* while the text is visible
    (Steam's F12 screenshot key works even over fullscreen games), then
    point this at that saved image file to click-drag a box on it at your
    own pace, with no window-switching timing involved.

    IMPORTANT: this only produces correct coordinates for the LIVE capture
    step later if the screenshot's pixel dimensions match what a live
    screen grab of that same monitor will see. If the game only ever runs
    in exclusive fullscreen, live polling captures generally can't read it
    either — see the README for why borderless/windowed mode is the real
    fix, not just a workaround for this selection step.

    `master`: pass an existing Tk root/widget (from the GUI) to open this
    as a child window of it instead of creating a whole new Tk instance —
    required when calling this from inside an already-running Tk app.
    When a failure happens with `master` set, a RuntimeError is raised
    instead of calling sys.exit, since exiting would kill the whole GUI
    process rather than just this one action.
    """
    def fail(msg):
        if master is not None:
            raise RuntimeError(msg)
        sys.exit(msg)

    try:
        import tkinter as tk
    except ImportError:
        fail(
            "Missing tkinter. Install it with:  sudo apt install python3-tk\n"
            "(needed only for --select-from-image; --select doesn't need it)"
        )
    from PIL import Image, ImageTk

    path = Path(image_path)
    if not path.exists():
        fail(f"Image not found: {path}")

    img = Image.open(path).convert("RGB")
    img_w, img_h = img.size

    window = tk.Toplevel(master) if master is not None else tk.Tk()
    window.title("Click and drag a box around the text, then release")
    screen_w, screen_h = window.winfo_screenwidth(), window.winfo_screenheight()
    scale = min(1.0, (screen_w * 0.9) / img_w, (screen_h * 0.9) / img_h)
    disp_w, disp_h = int(img_w * scale), int(img_h * scale)

    display_img = img.resize((disp_w, disp_h), resample=Image.LANCZOS) if scale < 1.0 else img
    photo = ImageTk.PhotoImage(display_img, master=window)

    label = tk.Label(window, text="Click and drag a box around the text area, then release.", fg="white", bg="black")
    label.pack(fill="x")
    canvas = tk.Canvas(window, width=disp_w, height=disp_h, cursor="crosshair")
    canvas.pack()
    canvas.create_image(0, 0, anchor="nw", image=photo)

    result = {}
    rect_id = {"id": None}
    start = {}

    def on_press(event):
        start["x"], start["y"] = event.x, event.y
        if rect_id["id"] is not None:
            canvas.delete(rect_id["id"])
        rect_id["id"] = canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="red", width=2)

    def on_drag(event):
        canvas.coords(rect_id["id"], start["x"], start["y"], event.x, event.y)

    def on_release(event):
        x0, y0 = start["x"], start["y"]
        x1, y1 = event.x, event.y
        left, right = sorted((x0, x1))
        top, bottom = sorted((y0, y1))
        # Scale back up from displayed (possibly shrunk) coords to the
        # original screenshot's pixel coordinates.
        result["x"] = int(left / scale)
        result["y"] = int(top / scale)
        result["w"] = int((right - left) / scale)
        result["h"] = int((bottom - top) / scale)
        window.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)

    if master is not None:
        master.wait_window(window)  # pumps the GUI's event loop until this window closes
    else:
        window.mainloop()

    if not result or result["w"] <= 0 or result["h"] <= 0:
        fail("No region selected (or selection had zero size). Try again.")

    log(f"Screenshot was {img_w}x{img_h}px.")
    try:
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[1]
            live_w, live_h = mon["width"], mon["height"]
        if (live_w, live_h) != (img_w, img_h):
            log(
                f"WARNING: your live screen is {live_w}x{live_h}px, but the screenshot was "
                f"{img_w}x{img_h}px. The selected coordinates likely won't line up during --run. "
                f"Make sure the screenshot was taken at your desktop's native resolution."
            )
    except Exception:
        pass  # best-effort check only; not fatal if mss/monitor info isn't available

    CONFIG_PATH.write_text(json.dumps(result, indent=2))
    log(f"Saved region {result} to {CONFIG_PATH}")
    return result


# --------------------------------------------------------------------------
# Screen capture
# --------------------------------------------------------------------------

class ScreenCapturer:
    """Grabs the configured region as a PIL Image, on X11 or Wayland."""

    def __init__(self, region: dict):
        self.region = region
        self.wayland = is_wayland()
        if self.wayland:
            check_dependency("grim", "grim")
        else:
            try:
                import mss  # noqa: F401
            except ImportError:
                sys.exit("Missing python package 'mss'. Install with: pip install --user mss")
            import mss as _mss
            self._mss = _mss.mss()

    def grab(self, region: dict = None):
        from PIL import Image

        r = region or self.region
        if self.wayland:
            geometry = f"{r['x']},{r['y']} {r['w']}x{r['h']}"
            proc = subprocess.run(["grim", "-g", geometry, "-"], capture_output=True, check=True)
            return Image.open(io.BytesIO(proc.stdout)).convert("RGB")
        else:
            box = {"left": r["x"], "top": r["y"], "width": r["w"], "height": r["h"]}
            shot = self._mss.grab(box)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------

def preprocess_for_ocr(img):
    """Upscale + grayscale + threshold — game fonts/backgrounds OCR much
    better this way than raw screenshots."""
    from PIL import Image, ImageOps

    img = img.convert("L")  # grayscale
    w, h = img.size
    img = img.resize((w * 3, h * 3), resample=Image.LANCZOS)
    img = ImageOps.autocontrast(img)
    return img


_PURE_PUNCT_RE = re.compile(r"^[\W_]+$", re.UNICODE)

# A handful of abbreviations that are legitimately followed by a lowercase
# word (unlike a real sentence-ending period) -- kept out of
# _fix_stray_periods()'s rewrite so "Mr. smith" -> "Mr smith" style
# over-correction doesn't happen for these specific cases. Deliberately
# short: this is a narrow safety net, not a full abbreviation dictionary.
_PERIOD_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "st", "jr", "sr", "prof", "rev", "gen", "col",
    "capt", "lt", "sgt", "vs", "etc", "approx", "no", "vol", "fig",
}
_MID_SENTENCE_PERIOD_RE = re.compile(r"\b([A-Za-z]+)\.(\s+)([a-z])")


def _fix_stray_periods(text: str) -> str:
    """OCR sometimes misreads a smudge, a comma, an accent mark, or a
    line-wrap artifact as a period in the *middle* of a sentence -- e.g.
    "a stately young. man dressed in a jacket" where nothing should be
    there at all. A real sentence-ending period in English is followed by a
    capitalized word (or the end of the text); one immediately followed by
    a lowercase word is almost always this kind of OCR noise rather than an
    intentional sentence break, so it gets dropped. A short list of common
    abbreviations ("Mr.", "Dr.", ...) is exempted since those are
    routinely followed by a lowercase word for legitimate reasons."""
    def repl(m):
        word, gap, next_char = m.group(1), m.group(2), m.group(3)
        if word.lower() in _PERIOD_ABBREVIATIONS:
            return m.group(0)  # leave it -- likely a real abbreviation
        return f"{word}{gap}{next_char}"  # drop the stray period
    return _MID_SENTENCE_PERIOD_RE.sub(repl, text)


def clean_ocr_text(text: str) -> str:
    """Drop OCR/screen-artifact noise that made it past the confidence
    filter in ocr_image(): tokens with no letters or digits at all (a lone
    ".", "|", "_", "''", or the stray glyph a game's "continue" arrow often
    gets misread as), plus a period stray-inserted mid-sentence (see
    _fix_stray_periods()). Deliberately conservative about whole-token
    removal -- it only drops a token when EVERY character in it is
    punctuation/symbols, so real words keep their attached punctuation
    ("don't", "well-known", "Hello!") and short real words ("a", "I") are
    untouched."""
    if not text:
        return text
    text = " ".join(tok for tok in text.split() if not _PURE_PUNCT_RE.match(tok))
    return _fix_stray_periods(text)


def ocr_image(img, lang: str, min_confidence: int = 40) -> str:
    """OCRs `img` and filters out low-confidence tokens before they're
    spoken. Screen artifacts -- dust on a texture, a UI border, a font's
    drop-shadow -- often get "recognized" as a stray character or short
    garbled word, but Tesseract itself is usually much less confident about
    those than it is about actual dialogue text, even when that dialogue
    has an unusual font. image_to_data() (rather than image_to_string())
    gives us that per-word confidence score to filter on."""
    import pytesseract
    from pytesseract import Output

    data = pytesseract.image_to_data(img, lang=lang, output_type=Output.DICT)
    words = []
    for word, conf in zip(data.get("text", []), data.get("conf", [])):
        word = word.strip()
        if not word:
            continue
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = -1.0  # Tesseract uses -1 for "not applicable" (non-text regions)
        if conf < min_confidence:
            continue
        words.append(word)
    return clean_ocr_text(" ".join(words))


# Many RPGs/visual novels show a speaking character's name in its own label
# above a fully-quoted line of dialogue -- OCR flattens that into one line
# where the name runs straight into the dialogue with nothing separating
# them (e.g. "Augustin El Borne "And this must be your younger son..."").
# split_speaker_name() recovers the boundary using the dialogue's own
# opening quote mark as the split point, without needing to know anything
# about font size or layout.
_SPEAKER_NAME_MAX_LENGTH = 40
_DIALOGUE_QUOTE_CHARS = "\"“"  # straight " and left curly double quote "


def split_speaker_name(text: str):
    """Returns (name, dialogue) if `text` looks like "<Name> "<Dialogue>"
    with the name label run into the dialogue's opening quote, or
    (None, text) if it doesn't -- e.g. the text already starts with a
    quote (no name present), there's no quote anywhere (this game doesn't
    wrap dialogue in quotes), or whatever comes before the quote reads like
    ordinary sentence text rather than a name (too long, or not every word
    Capitalized) -- so a narration line that merely *contains* a quote
    doesn't get chopped in half."""
    positions = [p for p in (text.find(ch) for ch in _DIALOGUE_QUOTE_CHARS) if p > 0]
    if not positions:
        return None, text
    idx = min(positions)
    candidate = text[:idx].strip()
    dialogue = text[idx:].strip()
    if not candidate or not dialogue or len(candidate) > _SPEAKER_NAME_MAX_LENGTH:
        return None, text
    words = [w for w in candidate.split() if any(c.isalpha() for c in w)]
    if not words or not all(w[0].isupper() for w in words):
        return None, text  # doesn't look Title Case -- probably not a name
    return candidate, dialogue


def apply_speaker_name_mode(text: str, mode: str) -> str:
    """mode "off" (default): leave text exactly as OCR'd. "skip": drop a
    detected speaker-name label, speaking only the dialogue. "announce":
    speak the name first with a pause (a period) before the dialogue,
    instead of it running straight into the first word. See
    split_speaker_name() for how the name is detected."""
    if mode == "off" or not text:
        return text
    name, dialogue = split_speaker_name(text)
    if name is None:
        return text
    if mode == "skip":
        return dialogue
    if mode == "announce":
        return f"{name}. {dialogue}"
    return text


# --------------------------------------------------------------------------
# Speech — always speaks the *latest* text; if something is still being
# said when new text arrives, the old utterance is cut off in favor of the
# new one (best fit for live, fast-moving game dialogue).
# --------------------------------------------------------------------------

class Speaker:
    """Two engines:

    - "espeak" (default): espeak-ng, always available via apt, robotic but
      zero extra setup.
    - "piper": a small neural TTS engine — genuinely natural-sounding
      voices, still fully local/offline and fast enough for live game
      dialogue. Requires `pip install piper-tts` (in the venv) plus a
      downloaded voice model (see README).
    - "kokoro": a newer, larger neural TTS engine — noticeably more
      natural than Piper, still fully local/offline. Requires
      `pip install kokoro-onnx` plus two downloaded model files (see
      README). Synthesis is CPU-bound and happens in a background thread
      per line so it doesn't stall the OCR poll loop or delay pause/stop.
    """

    def __init__(self, engine: str, rate: int, voice: str, piper_model: str, piper_speaker=None,
                 piper_length_scale=None, kokoro_model=None, kokoro_voices=None, kokoro_voice=None,
                 kokoro_speed=None, kokoro_lang=None, kokoro_cpu_threads=None, log=print):
        self.engine = engine
        self.log = log
        self._proc = None
        self._player_proc = None
        # Guards self._paused for ALL engines (not just Kokoro) -- see
        # set_paused() and say() below for why this exists: pausing needs to
        # stop a call to say() that was already *in flight* when the pause
        # hotkey fired, not just whatever's currently playing.
        self._pause_lock = threading.Lock()
        self._paused = False

        if engine == "espeak":
            check_dependency("espeak-ng", "espeak-ng")
            self.rate = rate
            self.voice = voice
        elif engine == "piper":
            # Prefer the piper binary living alongside the Python interpreter
            # currently running (i.e. in the same venv) over whatever a bare
            # PATH search turns up. This matters because a launcher that
            # runs venv/bin/python3 directly (skipping `source
            # venv/bin/activate`, e.g. a .desktop entry) does NOT put
            # venv/bin on PATH the way activation does — so shutil.which
            # could silently find a different, incompatible `piper`
            # elsewhere on the system instead of the one actually installed
            # for this project.
            venv_piper = Path(sys.executable).parent / "piper"
            self.piper_bin = str(venv_piper) if venv_piper.exists() else shutil.which("piper")
            if self.piper_bin is None:
                sys.exit(
                    "Missing required command 'piper'.\n"
                    "Install it with:  pip install piper-tts   (inside your venv — this one's pip, not apt)\n"
                    "(see README.md for the full setup, including downloading a voice model)"
                )
            self.log(f"Piper binary: {self.piper_bin}")
            if not piper_model:
                sys.exit("--engine piper requires --piper-model /path/to/voice.onnx (see README for how to get one).")
            self.piper_model = Path(piper_model)
            if not self.piper_model.exists():
                sys.exit(f"Piper voice model not found: {self.piper_model}")
            self.player_cmd_base = shutil.which("paplay")
            if not self.player_cmd_base:
                aplay = shutil.which("aplay")
                if not aplay:
                    sys.exit("Need 'paplay' or 'aplay' to play Piper's audio. Install with: sudo apt install alsa-utils")
                self.player_cmd_base = aplay
                self.log(
                    f"Note: using aplay ({aplay}) to play Piper's audio — paplay isn't installed. "
                    f"aplay talks to ALSA directly rather than through PulseAudio/PipeWire, which on some "
                    f"systems means it 'succeeds' silently without any audible output (wrong/dummy default "
                    f"device) even though nothing errors. If Piper stays silent with no error here, try "
                    f"`sudo apt install pulseaudio-utils` for paplay instead."
                )
            else:
                self.log(f"Piper audio playback: {self.player_cmd_base}")

            self.piper_length_scale = piper_length_scale

            self.piper_speaker = piper_speaker
            num_speakers = self._piper_config().get("num_speakers", 1)
            if num_speakers and num_speakers > 1:
                if self.piper_speaker is None:
                    self.piper_speaker = 0
                    self.log(
                        f"Note: {self.piper_model.name} is a multi-speaker model ({num_speakers} speakers) "
                        f"— no --piper-speaker given, defaulting to speaker 0. Pass a different ID (or set "
                        f"the GUI's Speaker ID field) to pick a different voice from this model."
                    )
        elif engine == "kokoro":
            try:
                from kokoro_onnx import Kokoro
            except ImportError:
                sys.exit(
                    "Missing required package 'kokoro-onnx'.\n"
                    "Install it with:  pip install kokoro-onnx   (inside your venv — this one's pip, not apt)\n"
                    "(see README.md for the full setup, including downloading the model files)"
                )
            if not kokoro_model or not kokoro_voices:
                sys.exit(
                    "--engine kokoro requires --kokoro-model /path/to/kokoro-v1.0.onnx and "
                    "--kokoro-voices /path/to/voices-v1.0.bin (see README for where to get them)."
                )
            kokoro_model_path = Path(kokoro_model)
            kokoro_voices_path = Path(kokoro_voices)
            if not kokoro_model_path.exists():
                sys.exit(f"Kokoro model not found: {kokoro_model_path}")
            if not kokoro_voices_path.exists():
                sys.exit(f"Kokoro voices file not found: {kokoro_voices_path}")

            self.player_cmd_base = shutil.which("paplay")
            if not self.player_cmd_base:
                aplay = shutil.which("aplay")
                if not aplay:
                    sys.exit("Need 'paplay' or 'aplay' to play Kokoro's audio. Install with: sudo apt install alsa-utils")
                self.player_cmd_base = aplay
                self.log(
                    f"Note: using aplay ({aplay}) to play Kokoro's audio — paplay isn't installed. "
                    f"aplay talks to ALSA directly rather than through PulseAudio/PipeWire, which on some "
                    f"systems means it 'succeeds' silently without any audible output (wrong/dummy default "
                    f"device) even though nothing errors. If Kokoro stays silent with no error here, try "
                    f"`sudo apt install pulseaudio-utils` for paplay instead."
                )
            else:
                self.log(f"Kokoro audio playback: {self.player_cmd_base}")

            self.log(f"Loading Kokoro model ({kokoro_model_path.name})... this can take a few seconds the first time.")
            session = None
            if kokoro_cpu_threads:
                session = self._build_kokoro_session(str(kokoro_model_path), kokoro_cpu_threads)
            try:
                if session is not None and hasattr(Kokoro, "from_session"):
                    self._kokoro = Kokoro.from_session(session, str(kokoro_voices_path))
                else:
                    if session is not None:
                        self.log(
                            "[speech] Installed kokoro-onnx doesn't support from_session() -- ignoring "
                            "--kokoro-cpu-threads. Try: pip install -U kokoro-onnx"
                        )
                    self._kokoro = Kokoro(str(kokoro_model_path), str(kokoro_voices_path))
            except Exception as e:
                sys.exit(f"Failed to load Kokoro model: {e}")
            self.log("Kokoro model loaded.")

            self.kokoro_voice = kokoro_voice or "af_heart"
            self.kokoro_speed = kokoro_speed if kokoro_speed is not None else 1.0
            self.kokoro_lang = kokoro_lang or "en-us"
            self._utterance_id = 0
            self._utterance_lock = threading.Lock()
            self._kokoro_streaming = hasattr(self._kokoro, "create_stream")
            if not self._kokoro_streaming:
                self.log(
                    "[speech] Installed kokoro-onnx doesn't support streaming synthesis (create_stream) -- "
                    "speech will wait for the whole line to finish synthesizing before playing anything. "
                    "Try: pip install -U kokoro-onnx"
                )
        else:
            sys.exit(f"Unknown --engine {engine!r} (expected 'espeak', 'piper', or 'kokoro')")

    def _build_kokoro_session(self, model_path: str, num_threads: int):
        """Builds an onnxruntime InferenceSession with intra_op_num_threads
        capped at `num_threads`, for Kokoro.from_session() to use instead of
        letting onnxruntime pick its own default (typically one thread per
        logical core). Capping this matters on a busy system: onnxruntime
        trying to grab all 16 threads for one synthesis call means it's
        fighting the game itself for CPU time on every single one of those
        threads, which is exactly the kind of contention that produces long,
        unpredictable pauses. A smaller, fixed thread budget can't use every
        core when the system is idle, but it also can't be starved as badly
        when it isn't -- worth experimenting with on your own machine, since
        the right number depends on how much CPU the game itself is using.
        Returns None (falling back to kokoro-onnx's own default session) if
        onnxruntime isn't importable or session creation fails for any
        reason -- this is an optional tuning knob, never a hard requirement."""
        try:
            import onnxruntime as rt
            opts = rt.SessionOptions()
            opts.intra_op_num_threads = int(num_threads)
            opts.inter_op_num_threads = 1  # only one synthesis call ever runs at a time
            return rt.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        except Exception as e:
            self.log(f"[speech] Couldn't build a custom Kokoro session ({e}) -- using kokoro-onnx's default threading.")
            return None

    def say(self, text: str):
        # OCR (grabbing a screenshot, running Tesseract) takes real time --
        # tens to hundreds of milliseconds, not nothing -- so the pause
        # hotkey can fire in the gap between the main loop deciding "this
        # text changed, speak it" and this call actually running. Without
        # this check, say() would have no way to know a pause had just been
        # requested: it always calls _stop_current() itself and treats
        # itself as the new, current utterance, so it would go ahead and
        # start playing regardless -- audibly, sometimes for a full line --
        # even though the user had already paused. set_paused(True) takes
        # this same lock, so whichever of the two runs first wins cleanly:
        # either we see paused and refuse to start, or set_paused() sees us
        # already past this check and its own _stop_current() call (made
        # right after, outside the lock) cuts off what we just started
        # almost immediately instead of letting it run to completion.
        with self._pause_lock:
            if self._paused:
                return
        self._stop_current()
        if self.engine == "espeak":
            cmd = ["espeak-ng", "-s", str(self.rate)]
            if self.voice:
                cmd += ["-v", self.voice]
            cmd.append(text)
            self._proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        elif self.engine == "piper":
            self._say_piper(text)
        else:
            self._say_kokoro(text)

    def set_paused(self, paused: bool):
        """Called by the pause hotkey (see PauseHotkey/on_pause_toggle in
        run()). Setting this to True makes any say() call already in
        flight -- or one that lands moments later, mid-OCR -- refuse to
        start anything new, then stops whatever's currently playing. This
        is what makes pause behave like stop instead of merely stopping
        *current* playback and hoping nothing new sneaks in behind it."""
        with self._pause_lock:
            self._paused = paused
        if paused:
            self._stop_current()

    def _say_piper(self, text: str):
        piper_cmd = [self.piper_bin, "--model", str(self.piper_model), "--output-raw"]
        if self.piper_speaker is not None:
            piper_cmd += ["--speaker", str(self.piper_speaker)]
        if self.piper_length_scale is not None:
            piper_cmd += ["--length_scale", str(self.piper_length_scale)]
        self._proc = subprocess.Popen(
            piper_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self._proc.stdin.write(text.encode("utf-8"))
        self._proc.stdin.close()

        rate = self._piper_config().get("audio", {}).get("sample_rate", 22050)
        if "paplay" in self.player_cmd_base:
            play_cmd = [self.player_cmd_base, "--raw", f"--rate={rate}", "--format=s16le", "--channels=1"]
        else:
            play_cmd = [self.player_cmd_base, "-q", "-r", str(rate), "-f", "S16_LE", "-t", "raw", "-c", "1", "-"]
        self._player_proc = subprocess.Popen(play_cmd, stdin=self._proc.stdout, stderr=subprocess.PIPE)

    def _kokoro_play_cmd(self, sample_rate) -> list:
        if "paplay" in self.player_cmd_base:
            return [self.player_cmd_base, "--raw", f"--rate={sample_rate}", "--format=s16le", "--channels=1"]
        return [self.player_cmd_base, "-q", "-r", str(sample_rate), "-f", "S16_LE", "-t", "raw", "-c", "1", "-"]

    def _say_kokoro(self, text: str):
        # Kokoro's synthesis is a blocking, CPU-bound call, unlike
        # espeak-ng/Piper where we just hand text to an external process and
        # return immediately. Running it directly here would stall the OCR
        # poll loop (and delay pause/stop responsiveness) for however long
        # synthesis takes, so it always runs in its own thread.
        #
        # More than one utterance can be "in flight" at once (new dialogue
        # arriving while an older line is still being synthesized) --
        # self._utterance_id tags each one so a superseded synthesis, or one
        # that finishes after we've since been stopped or paused, gets
        # dropped instead of playing out of turn. _stop_current() bumps this
        # id on every call (new line, pause, or shutdown), so capturing it
        # here at dispatch time is enough to detect that later.
        with self._utterance_lock:
            my_id = self._utterance_id

        target = self._say_kokoro_streaming_worker if self._kokoro_streaming else self._say_kokoro_blocking_worker
        threading.Thread(target=target, args=(text, my_id), daemon=True).start()

    def _say_kokoro_blocking_worker(self, text: str, my_id: int):
        """Fallback for older kokoro-onnx installs without create_stream():
        synthesizes the whole line, then plays it all at once. Everything
        has to be ready before anything is audible, so a slow synthesis
        (e.g. the CPU being contended with a game) shows up as one long
        silent pause before the line starts."""
        import numpy as np
        try:
            samples, sample_rate = self._kokoro.create(
                text, voice=self.kokoro_voice, speed=self.kokoro_speed, lang=self.kokoro_lang,
            )
        except Exception as e:
            self.log(f"[speech] Kokoro synthesis failed: {e}")
            return

        with self._utterance_lock:
            if my_id != self._utterance_id:
                return  # superseded while synthesizing -- drop it

        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        proc = subprocess.Popen(self._kokoro_play_cmd(sample_rate), stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        with self._utterance_lock:
            if my_id != self._utterance_id:
                # Went stale in the gap between the check above and actually
                # starting playback -- kill it before it plays over whatever
                # superseded it.
                proc.terminate()
                return
            self._player_proc = proc
        try:
            proc.stdin.write(pcm)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def _say_kokoro_streaming_worker(self, text: str, my_id: int):
        """Uses kokoro-onnx's create_stream() to synthesize and play a line
        chunk-by-chunk (roughly sentence-by-sentence) instead of waiting for
        the whole thing. Playback of the first chunk starts as soon as it's
        ready, so only that chunk's synthesis time gates how long the line
        stays silent -- a CPU stall partway through a long line delays the
        *next* chunk, not the start of speech. This is the single biggest
        lever against "long pause when the CPU gets busy": before, the
        entire paragraph had to finish synthesizing before anything played."""
        try:
            asyncio.run(self._stream_kokoro_chunks(text, my_id))
        except Exception as e:
            self.log(f"[speech] Kokoro streaming synthesis failed: {e}")

    async def _stream_kokoro_chunks(self, text: str, my_id: int):
        import numpy as np

        proc = None
        try:
            stream = self._kokoro.create_stream(
                text, voice=self.kokoro_voice, speed=self.kokoro_speed, lang=self.kokoro_lang,
            )
            async for samples, sample_rate in stream:
                with self._utterance_lock:
                    if my_id != self._utterance_id:
                        return  # superseded -- stop pulling/playing further chunks immediately
                if proc is None:
                    proc = subprocess.Popen(self._kokoro_play_cmd(sample_rate),
                                             stdin=subprocess.PIPE, stderr=subprocess.PIPE)
                    with self._utterance_lock:
                        if my_id != self._utterance_id:
                            proc.terminate()
                            return
                        self._player_proc = proc
                pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
                try:
                    proc.stdin.write(pcm)
                except (BrokenPipeError, OSError):
                    return
        finally:
            if proc is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

    def _piper_config(self) -> dict:
        cfg_path = Path(str(self.piper_model) + ".json")
        if cfg_path.exists():
            try:
                return json.loads(cfg_path.read_text())
            except Exception:
                pass
        return {}

    def _check_finished(self, proc, label: str):
        """If `proc` already exited (on its own, not because we're about to
        interrupt it) with a nonzero code, surface why — this is what makes
        a silently-failing speech engine visible instead of just... silent,
        especially when running from the GUI with no terminal to catch
        stderr output."""
        if proc is None or proc.poll() is None or proc.returncode == 0:
            return
        if proc.returncode == -15:
            # -15 means "killed by SIGTERM" — and the only thing in this
            # script that ever sends a process a signal is the .terminate()
            # call a few lines down in _stop_current() itself. So this is
            # never a real failure: it's this same method, on some earlier
            # call, having intentionally cut off speech that was still
            # playing (new dialogue interrupting old, or the pause hotkey
            # stopping mid-sentence) — surfacing it as an "exited with
            # code -15" error would just be misreporting our own doing.
            return
        stderr_text = ""
        try:
            if proc.stderr:
                stderr_text = proc.stderr.read().decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        msg = f"[speech] {label} exited with code {proc.returncode}"
        if stderr_text:
            msg += f": {stderr_text}"
        self.log(msg)

    def _stop_current(self):
        self._check_finished(self._proc, "speech process")
        self._check_finished(self._player_proc, "playback process")
        if self.engine == "kokoro":
            # Invalidate any Kokoro synthesis still running in the
            # background so it drops its result instead of playing a
            # superseded (or post-pause/stop) line once it finishes.
            with self._utterance_lock:
                self._utterance_id += 1
        for p in (self._player_proc, self._proc):
            if p and p.poll() is None:
                p.terminate()


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def similar(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _interruptible_sleep(seconds: float, stop_event) -> None:
    """time.sleep(), but wakes up promptly if stop_event gets set partway
    through — otherwise a GUI Stop button would feel laggy on a long
    --interval."""
    if stop_event is None:
        time.sleep(seconds)
        return
    step = 0.05
    elapsed = 0.0
    while elapsed < seconds and not stop_event.is_set():
        chunk = min(step, seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk


# --------------------------------------------------------------------------
# Global pause hotkey
# --------------------------------------------------------------------------

# A few friendlier spellings for common evdev key names.
_KEY_NAME_ALIASES = {
    "space": "KEY_SPACE",
    "spacebar": "KEY_SPACE",
    "scrolllock": "KEY_SCROLLLOCK",
    "scroll_lock": "KEY_SCROLLLOCK",
    "scroll-lock": "KEY_SCROLLLOCK",
    "pause": "KEY_PAUSE",
    "break": "KEY_PAUSE",
}


class PauseHotkey:
    """Listens for a key press anywhere on the system — not just while this
    app's own window has focus — and calls `on_toggle()` each time it's
    pressed, so the narrator can be paused/resumed while a fullscreen game
    has keyboard focus instead of this app.

    This reads raw keyboard events straight from the kernel (/dev/input)
    via the 'evdev' package, rather than through a display-server-level
    "global hotkey" hook: those typically only work under X11 and silently
    do nothing under Wayland, which is exactly the split this project
    already has to handle for screen capture (see is_wayland() above).
    Reading straight from the kernel works under either.

    Deliberately does NOT grab() the input device exclusively — it only
    *observes* key events alongside however the game/desktop already
    handles them. That means pressing the pause key still does whatever it
    always did in the game (e.g. advance dialogue) *in addition to*
    pausing/resuming the narrator — see README for why, and how to pick a
    key that avoids stepping on something the game already uses.

    Gracefully disables itself (logs a note, leaves `self.available =
    False`) rather than raising, if evdev isn't installed, the requested
    key name isn't recognized, or /dev/input isn't readable (usually a
    permissions issue — see README).
    """

    def __init__(self, key_name: str, on_toggle, log=print):
        self.log = log
        self.on_toggle = on_toggle
        self.key_name = key_name
        self.available = False
        self.key_code = None
        self._stop = threading.Event()
        self._thread = None
        self._devices = []

        try:
            import evdev  # noqa: F401
        except ImportError:
            self.log(
                f"Note: pause hotkey ('{key_name}') disabled — the 'evdev' package isn't installed. "
                f"Install it with: pip install evdev   (inside your venv — see README)."
            )
            return

        from evdev import ecodes
        attr_name = _KEY_NAME_ALIASES.get(key_name.strip().lower(), f"KEY_{key_name.strip().upper()}")
        self.key_code = getattr(ecodes, attr_name, None)
        if self.key_code is None:
            self.log(
                f"Note: pause hotkey disabled — '{key_name}' isn't a recognized key name "
                f"(try 'space', 'f9', 'scrolllock', ...)."
            )
            return

        self._devices = self._find_devices()
        if self._devices:
            self.available = True

    def _find_devices(self):
        # Enumerate /dev/input/event* ourselves via glob rather than relying
        # on evdev.list_devices() — on at least one real setup that function
        # came back empty even though the device files were plainly present
        # and listable by the same user in a shell, for reasons that were
        # never pinned down. Doing the same glob() evdev does internally,
        # directly, sidesteps whatever that mismatch was.
        from evdev import InputDevice, ecodes

        paths = sorted(glob.glob("/dev/input/event*"))
        if not paths:
            try:
                raw_listing = sorted(os.listdir("/dev/input"))
            except Exception as e:
                raw_listing = [f"<couldn't list /dev/input: {e}>"]
            self.log(
                "Note: pause hotkey disabled — no /dev/input/event* device files found. "
                f"Raw listing of /dev/input: {raw_listing!r}. If `ls /dev/input` in a terminal shows "
                "event devices when this doesn't, something about how this process is launched is "
                "hiding /dev/input from it (a sandboxed/containerized launch, an unusual .desktop "
                "setup, etc.) — running from a normal terminal is the way to rule that in or out."
            )
            return []

        opened = []
        permission_denied = []
        for path in paths:
            try:
                opened.append(InputDevice(path))
            except PermissionError:
                permission_denied.append(path)
            except OSError:
                continue
        if not opened:
            self.log(
                f"Note: pause hotkey disabled — found {len(paths)} input device(s) "
                f"({', '.join(paths)}) but couldn't open any of them ({len(permission_denied)} "
                f"permission denied). Add yourself to the 'input' group with "
                f"`sudo usermod -aG input $USER`, then log all the way out and back in (see README)."
            )
            return []

        matching = [d for d in opened if self.key_code in d.capabilities().get(ecodes.EV_KEY, [])]
        for d in opened:
            if d not in matching:
                try:
                    d.close()
                except Exception:
                    pass
        if not matching:
            self.log(
                f"Note: pause hotkey disabled — opened {len(opened)} input device(s) "
                f"({', '.join(d.path for d in opened)}) but none report a '{self.key_name}' key."
            )
        return matching

    def start(self):
        if not self.available:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        for dev in self._devices:
            try:
                dev.close()
            except Exception:
                pass

    def _run(self):
        import selectors
        from evdev import ecodes
        sel = selectors.DefaultSelector()
        try:
            registered = 0
            for dev in self._devices:
                try:
                    sel.register(dev, selectors.EVENT_READ)
                    registered += 1
                except Exception as e:
                    self.log(f"[hotkey] couldn't watch {getattr(dev, 'path', dev)}: {e}")
            if not registered:
                self.log("[hotkey] no input devices could be watched — pause hotkey is inactive.")
                return
            while not self._stop.is_set():
                for key, _ in sel.select(timeout=0.5):
                    dev = key.fileobj
                    try:
                        for event in dev.read():
                            if event.type == ecodes.EV_KEY and event.code == self.key_code and event.value == 1:
                                self.on_toggle()
                    except (OSError, BlockingIOError):
                        continue
        except Exception as e:
            self.log(f"[hotkey] listener stopped unexpectedly: {e}")
        finally:
            sel.close()


def run(args, stop_event=None, log=print, on_pause_change=None):
    """Runs the OCR -> speech loop until Ctrl+C (CLI) or stop_event is set
    (GUI, from a Stop button on another thread). `log` receives each status
    line — defaults to print for CLI use; the GUI passes a callback that
    forwards into its log panel instead."""
    apply_cpu_affinity(getattr(args, "cpu_affinity", "") or "", log=log)

    marker = None
    if args.ignore_popups:
        marker = load_popup_marker()
        if marker is None:
            sys.exit("--ignore-popups needs a saved marker. Run with --select-popup-marker first.")

    region = load_region()
    capturer = ScreenCapturer(region)
    speaker = None if args.quiet else Speaker(
        engine=args.engine, rate=args.rate, voice=args.voice, piper_model=args.piper_model,
        piper_speaker=getattr(args, "piper_speaker", None),
        piper_length_scale=getattr(args, "piper_length_scale", None),
        kokoro_model=getattr(args, "kokoro_model", None),
        kokoro_voices=getattr(args, "kokoro_voices", None),
        kokoro_voice=getattr(args, "kokoro_voice", None),
        kokoro_speed=getattr(args, "kokoro_speed", None),
        kokoro_lang=getattr(args, "kokoro_lang", None),
        kokoro_cpu_threads=getattr(args, "kokoro_cpu_threads", None),
        log=log,
    )

    log(f"Watching region {region} every {args.interval}s.")
    if marker:
        log(f"Ignoring popups matching marker at {marker['x']},{marker['y']} (threshold {args.popup_threshold}).")

    last_text = ""
    pause_event = threading.Event()
    last_toggle_at = 0.0
    # Some keyboards — especially wireless keyboard/mouse combo receivers —
    # enumerate as more than one /dev/input device, and more than one of
    # them can report the very same physical keypress. Without this, a
    # single press of the pause key can fire on_pause_toggle() twice in
    # quick succession (once per device) — pausing and then immediately
    # un-pausing again, which looks exactly like "it resumed by itself"
    # even though nothing else touched it. Collapsing any second toggle
    # that arrives within this window fixes that without needing to guess
    # which of several matching devices is the "real" one.
    PAUSE_DEBOUNCE_SECONDS = 0.3

    def on_pause_toggle():
        nonlocal last_text, last_toggle_at
        now = time.monotonic()
        if now - last_toggle_at < PAUSE_DEBOUNCE_SECONDS:
            return
        last_toggle_at = now

        timestamp = time.strftime("%H:%M:%S")
        if pause_event.is_set():
            pause_event.clear()
            if speaker:
                speaker.set_paused(False)
            log(f"[{timestamp}] [hotkey] Resumed.")
            if on_pause_change:
                on_pause_change(False)
        else:
            pause_event.set()
            if speaker:
                speaker.set_paused(True)
            log(f"[{timestamp}] [hotkey] Paused — press the pause key again to resume.")
            if on_pause_change:
                on_pause_change(True)
        # Treat whatever's on screen next as "new" so the current line gets
        # (re-)spoken on resume instead of staying silent because it still
        # looks unchanged from before the pause.
        last_text = ""

    pause_key = (getattr(args, "pause_key", "space") or "").strip()
    hotkey = PauseHotkey(pause_key, on_pause_toggle, log=log) if pause_key else None
    if hotkey:
        hotkey.start()
        if hotkey.available:
            log(f"Pause hotkey: press '{pause_key}' anywhere (even with the game focused) to pause/resume.")

    def stopped():
        return stop_event is not None and stop_event.is_set()

    try:
        while not stopped():
            if pause_event.is_set():
                _interruptible_sleep(args.interval, stop_event)
                continue

            if marker:
                marker_region = {"x": marker["x"], "y": marker["y"], "w": marker["w"], "h": marker["h"]}
                cur_color = average_color(capturer.grab(marker_region))
                if color_distance(cur_color, marker["ref_color"]) <= args.popup_threshold:
                    log(f"[{time.strftime('%H:%M:%S')}] [popup] skipping poll, marker matched")
                    _interruptible_sleep(args.interval, stop_event)
                    continue

            img = capturer.grab()
            img = preprocess_for_ocr(img)
            text = ocr_image(img, lang=args.lang, min_confidence=getattr(args, "ocr_min_confidence", 40))
            text = apply_speaker_name_mode(text, getattr(args, "speaker_name_mode", "off"))

            if text and similar(text, last_text) < args.similarity:
                timestamp = time.strftime("%H:%M:%S")
                log(f"[{timestamp}] {text}")
                if speaker:
                    speaker.say(text)
                last_text = text
            elif not text:
                # Box went empty (dialogue closed) — reset so the same
                # line can be re-spoken if it reappears later.
                last_text = ""

            _interruptible_sleep(args.interval, stop_event)
    except KeyboardInterrupt:
        pass
    finally:
        if hotkey:
            hotkey.stop()
        if speaker:
            speaker._stop_current()
        log("Stopped.")


def main():
    parser = argparse.ArgumentParser(description="OCR game text on screen and speak it aloud.")
    parser.add_argument("--select", action="store_true", help="Interactively pick the screen region to watch (live, click-and-drag over the screen).")
    parser.add_argument("--select-from-image", metavar="PATH", default="",
                         help="Pick the region from a saved screenshot instead of live (use when you can't alt-tab to a terminal over a fullscreen game, e.g. a Steam F12 screenshot).")
    parser.add_argument("--select-popup-marker", action="store_true",
                         help="Interactively pick a small spot that fingerprints when a popup/overlay is showing (see --ignore-popups).")
    parser.add_argument("--ignore-popups", action="store_true",
                         help="Skip OCR/speech entirely while the saved popup marker is matched (needs --select-popup-marker first).")
    parser.add_argument("--popup-threshold", type=float, default=20.0,
                         help="Max color distance (0-441) for the popup marker to still count as matched (default 20). Raise if popups get missed; lower if false positives skip real dialogue.")
    parser.add_argument("--run", action="store_true", help="Start the OCR -> speech loop.")
    parser.add_argument("--interval", type=float, default=0.5, help="Seconds between screen captures (default 0.5).")
    parser.add_argument("--lang", default="eng", help="Tesseract language code (default eng).")
    parser.add_argument("--ocr-min-confidence", type=int, default=40, metavar="0-100",
                         help="Discard OCR'd words below this Tesseract confidence score (default 40). "
                              "Raise it if screen artifacts (dust, UI borders, icons) are getting spoken as "
                              "stray punctuation or gibberish words; lower it if real dialogue is getting dropped.")
    parser.add_argument("--speaker-name-mode", choices=["off", "skip", "announce"], default="off",
                         help="For dialogue boxes that show a character's name above their quoted line (which "
                              "OCR runs straight into the dialogue, e.g. 'Augustin El Borne \"And this must "
                              "be...\"'): 'skip' drops the name and speaks only the dialogue; 'announce' speaks "
                              "the name first with a pause before continuing into the dialogue. Default 'off' "
                              "speaks the text exactly as OCR'd. Detection only fires on a short Title-Cased "
                              "label immediately before a quote mark, so ordinary narration is left alone.")
    parser.add_argument("--rate", type=int, default=175, help="espeak-ng speech rate, words/min (default 175, ignored by --engine piper/kokoro).")
    parser.add_argument("--voice", default="", help="espeak-ng voice, e.g. en-us or mb-us1 (default: system default). Ignored by --engine piper/kokoro.")
    parser.add_argument("--engine", choices=["espeak", "piper", "kokoro"], default="espeak",
                         help="TTS engine: 'espeak' (default, robotic, zero setup), 'piper' (natural neural voices, needs pip install piper-tts + a downloaded model), or 'kokoro' (most natural of the three, needs pip install kokoro-onnx + two downloaded model files — see README).")
    parser.add_argument("--piper-model", default="", metavar="PATH",
                         help="Path to a Piper voice .onnx model file. Required when --engine piper.")
    parser.add_argument("--piper-speaker", type=int, default=None, metavar="ID",
                         help="Speaker ID for a multi-speaker Piper model (check the model's .onnx.json 'num_speakers'). Defaults to 0 if the model is multi-speaker and this is unset.")
    parser.add_argument("--piper-length-scale", type=float, default=None, metavar="SCALE",
                         help="Piper speech-rate multiplier. Lower = faster (0.5 = double speed), higher = slower (2.0 = half speed). Default (unset) uses Piper's own default of 1.0. Ignored by --engine espeak/kokoro (use --rate/--kokoro-speed instead).")
    parser.add_argument("--kokoro-model", default="", metavar="PATH",
                         help="Path to Kokoro's kokoro-v1.0.onnx model file. Required when --engine kokoro (see README for where to download it).")
    parser.add_argument("--kokoro-voices", default="", metavar="PATH",
                         help="Path to Kokoro's voices-v1.0.bin file. Required when --engine kokoro (see README for where to download it).")
    parser.add_argument("--kokoro-voice", default="af_heart", metavar="NAME",
                         help="Kokoro voice name (default 'af_heart', a top-rated American English female voice). See README for more options.")
    parser.add_argument("--kokoro-speed", type=float, default=None, metavar="SPEED",
                         help="Kokoro speaking-speed multiplier. HIGHER = faster (2.0 = double speed), LOWER = slower (0.5 = half speed) — note this is the OPPOSITE direction from --piper-length-scale. Default (unset) is Kokoro's normal speed (1.0). Ignored by --engine espeak/piper.")
    parser.add_argument("--kokoro-lang", default="en-us", metavar="LANG",
                         help="Kokoro language code (default en-us). Ignored by --engine espeak/piper.")
    parser.add_argument("--kokoro-cpu-threads", type=int, default=None, metavar="N",
                         help="Caps how many CPU threads a single Kokoro synthesis call can use (default: "
                              "unset, kokoro-onnx/onnxruntime picks its own default, usually one per logical "
                              "core). On a busy system -- a demanding game using every core -- letting one "
                              "synthesis call fight for all of them tends to produce longer, less predictable "
                              "pauses, not faster speech. Try capping this at a handful of threads (e.g. 4-6 "
                              "on an 8-core/16-thread CPU) and see if pauses smooth out. Requires a kokoro-onnx "
                              "version with Kokoro.from_session(); older installs log a note and ignore this.")
    parser.add_argument("--cpu-affinity", default="", metavar="CORE,CORE,...",
                         help="Pin this whole process to specific CPU cores, e.g. '4,5,6,7' -- reserves "
                              "uncontended CPU time for OCR + speech instead of leaving the OS to time-share "
                              "it with a demanding game on every core. Linux only. Default: unset (no pinning).")
    parser.add_argument("--similarity", type=float, default=0.92,
                         help="0-1 threshold above which new text is treated as 'unchanged' and not re-spoken (default 0.92).")
    parser.add_argument("--pause-key", default="space", metavar="KEY",
                         help="Key that pauses/resumes the narrator from anywhere, even while the game has "
                              "keyboard focus (e.g. 'space', 'f9', 'scrolllock'). Empty string disables the "
                              "hotkey entirely. Needs the 'evdev' package and read access to /dev/input "
                              "(see README) — degrades gracefully with a log note if unavailable.")
    parser.add_argument("--quiet", action="store_true", help="Print recognized text but don't speak it.")
    args = parser.parse_args()

    if not any([args.select, args.select_from_image, args.select_popup_marker, args.run]):
        parser.print_help()
        sys.exit(1)

    if args.select:
        select_region()
    if args.select_from_image:
        select_region_from_image(args.select_from_image)
    if args.select_popup_marker:
        select_popup_marker()
    if args.run:
        run(args)


if __name__ == "__main__":
    main()
