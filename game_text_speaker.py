#!/usr/bin/env python3
"""
game_text_speaker.py — OCR-to-speech accessibility pipeline for games.

Watches a chosen rectangle of your screen (e.g. a dialogue box), OCRs it on
a poll loop, and speaks new/changed text aloud via espeak-ng. Runs on Linux
(X11 via slop+mss, or Wayland via slurp+grim) and, experimentally, Windows —
see platform_adapter.py for exactly what differs between them.

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
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from platform_adapter import (
    get_platform_adapter, check_dependency, pick_region_from_image, subprocess_no_window_kwargs,
)
import game_profile
from game_profile import Observation, split_speaker_name

CONFIG_PATH = Path(__file__).with_name("region.json")
POPUP_MARKER_PATH = Path(__file__).with_name("popup_marker.json")

# The one place this process asks "which OS am I on" -- everywhere else in
# this file just calls PLATFORM's methods. See platform_adapter.py.
PLATFORM = get_platform_adapter()

# Extra subprocess.Popen kwargs so espeak-ng/piper don't flash a console
# window on Windows every time they're spawned -- see subprocess_no_window_kwargs().
_POPEN_KWARGS = subprocess_no_window_kwargs()


def apply_cpu_affinity(affinity: str, log=print) -> None:
    PLATFORM.set_cpu_affinity(affinity, log=log)


# --------------------------------------------------------------------------
# Region selection — run once, saves {x, y, w, h} to region.json
# --------------------------------------------------------------------------

def select_region(log=print) -> dict:
    log("Drag a box around the text area you want watched (e.g. the dialogue box)...")
    region = PLATFORM.select_region(log=log)
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
    marker_region = PLATFORM.select_region(log=log)
    capturer = PLATFORM.make_capturer(marker_region)
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
    from (e.g. exclusive fullscreen blocks slop/slurp's overlay on Linux).
    You take a full-screen screenshot *of your desktop* while the text is
    visible (Steam's F12 screenshot key works even over fullscreen games),
    then point this at that saved image file to click-drag a box on it at
    your own pace, with no window-switching timing involved. (On Windows,
    --select already screenshots first for the same no-time-pressure
    effect — see WindowsAdapter.select_region() — but this remains useful
    there too for picking a region from an old/saved screenshot.)

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

    from PIL import Image

    path = Path(image_path)
    if not path.exists():
        fail(f"Image not found: {path}")

    img = Image.open(path).convert("RGB")
    img_w, img_h = img.size
    result = pick_region_from_image(img, master=master, log=log)

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
    filter in _ocr_image_tesseract() (Tesseract only -- see ocr_image()):
    tokens with no letters or digits at all (a lone
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


def ocr_image(img, lang: str, min_confidence: int = 40, engine: str = "tesseract", log=print) -> str:
    """Dispatches to one of two OCR engines, then applies clean_ocr_text()
    to the result either way -- the punctuation/stray-period cleanup is
    engine-agnostic text cleanup, not something Tesseract-specific.

    "tesseract" (default, and the only option on Linux): needs the
    tesseract-ocr binary installed separately.

    "windows": Windows' own built-in OCR (Windows.Media.Ocr -- the same
    engine PowerToys' Text Extractor and the Snipping Tool use), via the
    'winocr' package. Nothing to install beyond `pip install winocr` --
    no separate binary, no PATH entry. The trade-off: unlike Tesseract's
    image_to_data(), Windows' OCR API doesn't expose a per-word confidence
    score, so `min_confidence`/--ocr-min-confidence has nothing to filter
    on and is silently ignored for this engine (see _ocr_image_windows())."""
    if engine == "windows":
        return clean_ocr_text(_ocr_image_windows(img, lang))
    return clean_ocr_text(_ocr_image_tesseract(img, lang, min_confidence))


def ocr_lines(img, lang: str, min_confidence: int = 40, engine: str = "tesseract") -> list:
    """Where each recognized line of text SITS, not just what it says.

    Returns [{"text", "x", "y", "w", "h"}, ...] with the geometry as
    FRACTIONS of the image rather than pixels, so a profile written at one
    resolution still describes the same layout at another.

    This costs nothing extra: Tesseract's image_to_data() -- already what
    _ocr_image_tesseract() calls, for the per-word confidence scores -- has
    been returning per-word bounding boxes all along, and we were throwing
    them away. That geometry is what lets the "margin" detector notice a
    speaker name set apart from the body text without being told where to
    look, which in turn is what makes speaker detection work in games whose
    dialogue isn't quoted.

    Returns [] rather than raising if the engine can't supply positions --
    the detectors that need geometry simply don't fire, and the ones that
    work on flat text carry on."""
    try:
        if engine == "windows":
            return _ocr_lines_windows(img, lang)
        return _ocr_lines_tesseract(img, lang, min_confidence)
    except Exception:
        return []


def _ocr_lines_tesseract(img, lang: str, min_confidence: int) -> list:
    import pytesseract
    from pytesseract import Output

    data = pytesseract.image_to_data(img, lang=lang, output_type=Output.DICT)
    width, height = img.size
    if not width or not height:
        return []

    grouped = {}
    for i, word in enumerate(data.get("text", [])):
        word = word.strip()
        if not word:
            continue
        try:
            if int(data["conf"][i]) < min_confidence:
                continue
        except (ValueError, TypeError, KeyError):
            continue
        # block/paragraph/line together are Tesseract's own idea of "same
        # line", which is more reliable than clustering by y ourselves.
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        left, top = data["left"][i], data["top"][i]
        right, bottom = left + data["width"][i], top + data["height"][i]
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = {"words": [word], "l": left, "t": top, "r": right, "b": bottom}
        else:
            entry["words"].append(word)
            entry["l"] = min(entry["l"], left)
            entry["t"] = min(entry["t"], top)
            entry["r"] = max(entry["r"], right)
            entry["b"] = max(entry["b"], bottom)

    lines = []
    for entry in grouped.values():
        lines.append({
            "text": clean_ocr_text(" ".join(entry["words"])),
            "x": entry["l"] / width, "y": entry["t"] / height,
            "w": (entry["r"] - entry["l"]) / width,
            "h": (entry["b"] - entry["t"]) / height,
        })
    return sorted(lines, key=lambda l: l["y"])


def _ocr_lines_windows(img, lang: str) -> list:
    """Windows' OCR API reports a bounding rect per line too. Read
    defensively -- winocr's dict shape has moved around between versions,
    and losing geometry should degrade the detectors, not break OCR."""
    import winocr

    result = winocr.recognize_pil_sync(img, lang)
    width, height = img.size
    if not width or not height:
        return []
    lines = []
    for line in (result.get("lines") or []):
        text = (line.get("text") or "").strip()
        if not text:
            continue
        rect = line.get("bounding_rect") or line.get("boundingRect") or {}
        try:
            x, y = float(rect["x"]), float(rect["y"])
            w, h = float(rect["width"]), float(rect["height"])
        except (KeyError, TypeError, ValueError):
            continue
        lines.append({"text": clean_ocr_text(text), "x": x / width, "y": y / height,
                      "w": w / width, "h": h / height})
    return sorted(lines, key=lambda l: l["y"])


def _ocr_image_tesseract(img, lang: str, min_confidence: int) -> str:
    """Screen artifacts -- dust on a texture, a UI border, a font's
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
    return " ".join(words)


def _ocr_image_windows(img, lang: str) -> str:
    """Runs OCR via Windows.Media.Ocr (the 'winocr' package), UNTESTED on
    real Windows hardware like the rest of this project's Windows support.
    `lang` here is a BCP-47 tag ("en", "ja", "fr", ...), NOT Tesseract's
    3-letter code ("eng", "jpn", "fra") -- these are two unrelated naming
    schemes from two unrelated OCR engines. run() maps the CLI's Tesseract-
    flavored default ("eng") to "en" automatically when this engine is
    selected and --lang was left at its default; pass an explicit BCP-47
    tag yourself for anything else.

    winocr.recognize_pil_sync() raises AssertionError (with a ready-to-run
    PowerShell command as the message) when the requested language's OCR
    pack isn't installed -- see README for what that command does."""
    try:
        import winocr
    except ImportError:
        sys.exit(
            "Missing required package 'winocr', needed for --ocr-engine windows.\n"
            "Install it with:  pip install winocr   (inside your venv)\n"
            "(see README.md's Windows section for the full setup)"
        )
    try:
        result = winocr.recognize_pil_sync(img, lang)
    except AssertionError as e:
        sys.exit(
            f"Windows OCR language '{lang}' isn't installed. Install it by running this in an "
            f"administrator PowerShell, then try again:\n{e}"
        )
    return (result.get("text") or "").strip()


# split_speaker_name() now lives in game_profile.py, where it's registered as
# the "quote" pattern style alongside the other speaker detectors -- it was
# always one game's convention rather than a universal rule, and keeping it
# next to its siblings is what stopped this file from growing a second and
# third hardcoded convention beside it. Imported above so --speaker-name-mode
# and everything else here keeps working exactly as before.


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


def apply_speaker_name_mode_for(name, dialogue, original, mode):
    """Same three modes as apply_speaker_name_mode(), but for a name a
    detector has ALREADY separated out. The margin and zone detectors find
    the boundary from layout rather than punctuation, so there's nothing for
    the quote heuristic to re-derive and calling it again would just fail to
    split a line that's already split.

    Worth knowing: once characters have their own voices, "skip" often reads
    better than "announce" -- the voice change already tells you who's
    talking, so repeating the name every line gets tiring. It's per-profile
    precisely because that's a taste call."""
    if not name:
        return original
    if mode == "skip":
        return dialogue
    if mode == "announce":
        return f"{name}. {dialogue}"
    return original


# --------------------------------------------------------------------------
# Speech — always speaks the *latest* text; if something is still being
# said when new text arrives, the old utterance is cut off in favor of the
# new one (best fit for live, fast-moving game dialogue).
# --------------------------------------------------------------------------

_ONNX_TENSOR_TYPE_RE = re.compile(r"tensor\((\w+)\)")
_ONNX_TO_NUMPY_DTYPE = {
    "float": "float32", "float16": "float16", "double": "float64",
    "int8": "int8", "int16": "int16", "int32": "int32", "int64": "int64",
    "uint8": "uint8", "uint16": "uint16", "uint32": "uint32", "uint64": "uint64",
    "bool": "bool",
}


def _patch_kokoro_onnx_input_dtypes(kokoro, log=print):
    """Works around a real bug in the kokoro-onnx package (confirmed
    present both in the currently pip-installable version AND its latest
    upstream source as of this writing -- see
    https://github.com/thewh1teagle/kokoro-onnx/issues/155 for the same
    class of bug against a different model file). Kokoro._create_audio()
    decides the "speed" input's numpy dtype by which code branch it takes
    (int32 if the model's inputs include one named "input_ids", float32
    otherwise) instead of asking the ONNX model what dtype it actually
    declared for that input -- and for the official kokoro-v1.0.onnx
    release specifically, that guess is wrong: the model wants
    tensor(float), but the "input_ids" branch sends tensor(int32), and
    onnxruntime rejects it outright:
        Unexpected input data type. Actual: (tensor(int32)), expected: (tensor(float))
    Worse, this happens inside a background asyncio task
    (kokoro_onnx.create_stream()'s process_batches()) that nothing here
    ever awaits directly -- see _stream_kokoro_chunks() below -- so
    instead of surfacing as an error, it silently produces zero audio and
    hangs that one background thread forever, which is indistinguishable
    from "Kokoro just isn't outputting sound" from the GUI.

    Reimplementing _create_audio from scratch to fix this properly risks
    drifting from upstream's own tokenizing/batching logic over time, so
    instead this monkey-patches the loaded session's own .run() to coerce
    each input array to whatever dtype the ONNX graph actually declared
    for it, right before every inference call -- self-correcting for
    whatever the real model wants instead of guessing, so it keeps working
    even if upstream changes _create_audio's branching logic later, and
    becomes a harmless no-op instead of a maintenance trap if upstream
    ever fixes this dtype bug outright."""
    try:
        sess = kokoro.sess
        expected_dtypes = {}
        for inp in sess.get_inputs():
            m = _ONNX_TENSOR_TYPE_RE.match(inp.type)
            if m and m.group(1) in _ONNX_TO_NUMPY_DTYPE:
                expected_dtypes[inp.name] = _ONNX_TO_NUMPY_DTYPE[m.group(1)]

        original_run = sess.run

        def patched_run(output_names, input_feed, run_options=None):
            import numpy as np
            fixed_feed = {}
            for name, value in input_feed.items():
                expected = expected_dtypes.get(name)
                if expected is not None and hasattr(value, "dtype") and str(value.dtype) != expected:
                    value = value.astype(expected)
                fixed_feed[name] = value
            return original_run(output_names, fixed_feed, run_options)

        sess.run = patched_run
    except Exception as e:
        # Best-effort only -- if this patch itself fails for any reason
        # (a kokoro-onnx internals change, an unexpected session shape),
        # fall back to whatever kokoro-onnx would have done unpatched
        # rather than blocking Kokoro from loading at all over a
        # workaround for a bug it might not even hit.
        log(f"[speech] Couldn't apply the Kokoro dtype workaround ({e}) -- continuing without it.")


class Speaker:
    """Four engines:

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
    - "windows": Windows' own built-in SAPI5 voices (the classic "Microsoft
      David"/"Microsoft Zira" tier, comparable quality to espeak-ng — NOT
      the newer neural "Natural voices", which Microsoft restricts to
      Narrator/Edge only), via `pip install pyttsx3`. Runs in-process (no
      subprocess spawn at all), so it needs no PATH setup and produces no
      console-window flicker. Windows-only; on any other platform this
      engine simply won't be selectable (pyttsx3 wraps SAPI5, which doesn't
      exist elsewhere).
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
        # Guards self._utterance_id for engines that synthesize in a
        # background thread (Kokoro, Windows/SAPI) rather than handing text
        # straight to a subprocess -- see _say_kokoro_blocking_worker()'s
        # comment for the full explanation of why this exists. Initialized
        # unconditionally (not just inside those engines' branches below) so
        # _stop_current() can safely bump it regardless of which engine is
        # active.
        self._utterance_id = 0
        self._utterance_lock = threading.Lock()

        if engine == "espeak":
            check_dependency(
                "espeak-ng",
                "Install it with:  sudo apt install espeak-ng   (Linux), or download the installer "
                "from https://github.com/espeak-ng/espeak-ng/releases   (Windows)",
            )
            self.rate = rate
            # A bare variant ("+f3") is a suffix meant to ride on a base
            # voice, not a voice in its own right -- espeak-ng rejects it
            # outright ("Voice +f3 not found in available voices") if it
            # ever arrives alone. say() below already forgives exactly this
            # for a PER-CHARACTER variant, prepending "en" when there's no
            # base to attach to (see the "+" branch there) -- the base voice
            # itself deserves the same forgiveness rather than a runtime
            # error, since it can arrive as a bare variant too: not through
            # the GUI's own Voice dropdown (that only ever offers real base
            # voices queried from espeak-ng itself), but via --voice on the
            # command line or a hand-edited gui_settings.json, both of which
            # bypass that dropdown entirely.
            if voice and voice.startswith("+"):
                voice = f"en{voice}"
            self.voice = voice
        elif engine == "piper":
            # Historically this shelled out to a "piper" binary (subprocess,
            # stdin/stdout piping) -- the right model for the OLD standalone
            # Piper (a separate C++ project with real prebuilt binaries from
            # its own GitHub releases). `pip install piper-tts` (what this
            # project actually installs -- see setup.py) is a DIFFERENT,
            # newer project: a pure Python + onnxruntime reimplementation.
            # Its "piper" console-script binary (venv\Scripts\piper.exe on
            # Windows) is just a pip-generated launcher stub hardcoded to
            # that specific venv's own python.exe -- it works fine run from
            # source (that venv genuinely exists there), but can't survive
            # being frozen into a standalone .exe: there's no venv, and no
            # python.exe file at all, shipped alongside it. Importing the
            # package directly instead -- exactly how the "kokoro" branch
            # below already does it -- sidesteps the whole problem: there's
            # no external binary to go looking for, so there's nothing
            # PyInstaller needs to bundle beyond the package itself (plus
            # its bundled espeak-ng-data, handled by --collect-data=piper in
            # setup.py/build_windows.py).
            try:
                from piper import PiperVoice
            except ImportError:
                sys.exit(
                    "Missing required package 'piper-tts'.\n"
                    "Install it with:  pip install piper-tts   (inside your venv — this one's pip, not apt)\n"
                    "(see README.md for the full setup, including downloading a voice model)"
                )
            if not piper_model:
                sys.exit("--engine piper requires --piper-model /path/to/voice.onnx (see README for how to get one).")
            self.piper_model = Path(piper_model)
            if not self.piper_model.exists():
                sys.exit(f"Piper voice model not found: {self.piper_model}")
            PLATFORM.resolve_player("Piper", log=self.log)

            self.log(f"Loading Piper model ({self.piper_model.name})...")
            try:
                self._piper_voice = PiperVoice.load(str(self.piper_model))
            except Exception as e:
                sys.exit(f"Failed to load Piper model: {e}")
            self.log("Piper model loaded.")

            self.piper_length_scale = piper_length_scale

            self.piper_speaker = piper_speaker
            num_speakers = self._piper_voice.config.num_speakers
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

            PLATFORM.resolve_player("Kokoro", log=self.log)

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
            _patch_kokoro_onnx_input_dtypes(self._kokoro, log=self.log)

            self.kokoro_voice = kokoro_voice or "af_heart"
            self.kokoro_speed = kokoro_speed if kokoro_speed is not None else 1.0
            self.kokoro_lang = kokoro_lang or "en-us"
            self._kokoro_streaming = hasattr(self._kokoro, "create_stream")
            if not self._kokoro_streaming:
                self.log(
                    "[speech] Installed kokoro-onnx doesn't support streaming synthesis (create_stream) -- "
                    "speech will wait for the whole line to finish synthesizing before playing anything. "
                    "Try: pip install -U kokoro-onnx"
                )
        elif engine == "windows":
            if sys.platform != "win32":
                sys.exit("--engine windows only works on Windows (it wraps SAPI5 via pyttsx3).")
            try:
                import pyttsx3
            except ImportError:
                sys.exit(
                    "Missing required package 'pyttsx3'.\n"
                    "Install it with:  pip install pyttsx3   (inside your venv — this one's pip, not apt)\n"
                    "This wraps Windows' own built-in SAPI5 voices, so no extra download is needed beyond the package itself."
                )
            self._sapi_lock = threading.Lock()
            self._sapi_engine = pyttsx3.init()
            self._sapi_engine.setProperty("rate", rate)
            if voice:
                chosen = None
                for v in self._sapi_engine.getProperty("voices"):
                    if voice.lower() in v.name.lower() or voice == v.id:
                        chosen = v.id
                        break
                if chosen is not None:
                    self._sapi_engine.setProperty("voice", chosen)
                else:
                    self.log(
                        f"[speech] No installed SAPI5 voice matched {voice!r} -- using the system default voice instead. "
                        f"Leave --voice blank, or check the GUI's Voice dropdown for installed options."
                    )
        else:
            sys.exit(f"Unknown --engine {engine!r} (expected 'espeak', 'piper', 'kokoro', or 'windows')")

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

    def say(self, text: str, voice=None, speaker_id=None, speed=None):
        """`voice`/`speaker_id`/`speed` override this one utterance's
        identity and pace -- how a cast member's per-model settings (see
        game_profile.Cast.get_model()) are delivered. `voice` is whatever
        the active engine expects as a COMPLETE, ready-to-use value -- a
        Kokoro voice id, a full espeak-ng voice string INCLUDING its base
        language ("en-us+m3", never a bare "+m3"), a SAPI5 voice name.
        `speaker_id` is Piper-only: an integer index into a multi-speaker
        model. `speed` means whatever that engine's own pacing knob means
        (words/min for espeak-ng and SAPI5, a length_scale multiplier for
        Piper, a speed multiplier for Kokoro). Any of these left as None
        means "use whatever this Speaker was configured with", which is
        what the narrator and any character nobody's assigned a voice to
        under the model currently running get. Nothing in this class needs
        to know where those per-model settings came from -- that's entirely
        game_profile's and the Cast panel's business."""
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
            rate = int(speed) if speed is not None else self.rate
            cmd = ["espeak-ng", "-s", str(rate)]
            chosen = voice if voice else self.voice
            if chosen:
                cmd += ["-v", chosen]
            cmd.append(text)
            self._proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, **_POPEN_KWARGS)
        elif self.engine == "piper":
            self._say_piper(text, speaker_id, speed)
        elif self.engine == "windows":
            self._say_windows_sapi(text, voice, speed)
        else:
            self._say_kokoro(text, voice, speed)

    def set_paused(self, paused: bool):
        """Called by the pause hotkey (see platform_adapter.HotkeyWatcher
        and on_pause_toggle in run()). Setting this to True makes any
        say() call already in flight -- or one that lands moments later,
        mid-OCR -- refuse to start anything new, then stops whatever's
        currently playing. This is what makes pause behave like stop
        instead of merely stopping *current* playback and hoping nothing
        new sneaks in behind it."""
        with self._pause_lock:
            self._paused = paused
        if paused:
            self._stop_current()

    def _say_piper(self, text: str, speaker_id=None, speed=None):
        # Runs in-process now (see the "piper" branch of __init__ for why),
        # same shape as Kokoro just below: synthesis is CPU-bound, so it
        # always happens in its own thread rather than blocking here and
        # stalling the OCR poll loop.
        with self._utterance_lock:
            my_id = self._utterance_id
        threading.Thread(target=self._say_piper_worker, args=(text, my_id, speaker_id, speed), daemon=True).start()

    def _say_piper_worker(self, text: str, my_id: int, speaker_id=None, speed=None):
        """voice.synthesize() yields one AudioChunk per sentence, played as
        each one is ready instead of waiting for the whole line -- same
        streaming-over-waiting reasoning as Kokoro's streaming worker below,
        just without asyncio since Piper's synthesize() is a plain,
        synchronous generator."""
        from piper import SynthesisConfig

        # A Piper speaker is just a different integer index into the loaded
        # multi-speaker model, so per-character casting costs nothing here --
        # no second model to load, just a different argument on this call.
        sid = self.piper_speaker if speaker_id is None else speaker_id
        scale = self.piper_length_scale if speed is None else speed
        syn_config = SynthesisConfig(
            speaker_id=sid,
            length_scale=scale,
        )
        player = None
        try:
            for chunk in self._piper_voice.synthesize(text, syn_config):
                with self._utterance_lock:
                    if my_id != self._utterance_id:
                        return  # superseded -- stop pulling/playing further chunks immediately
                if player is None:
                    player = PLATFORM.open_pcm_player(chunk.sample_rate)
                    with self._utterance_lock:
                        if my_id != self._utterance_id:
                            player.terminate()
                            return
                        self._player_proc = player
                player.write(chunk.audio_int16_bytes)
        except Exception as e:
            self.log(f"[speech] Piper synthesis failed: {e}")
        finally:
            if player is not None:
                player.close_stdin()

    def _say_kokoro(self, text: str, voice=None, speed=None):
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

        # Kokoro voices are style vectors inside the one loaded model, so
        # switching per character is just a different argument -- no reload,
        # no extra memory.
        target = self._say_kokoro_streaming_worker if self._kokoro_streaming else self._say_kokoro_blocking_worker
        resolved_speed = speed if speed is not None else self.kokoro_speed
        threading.Thread(target=target, args=(text, my_id, voice or self.kokoro_voice, resolved_speed), daemon=True).start()

    def _say_kokoro_blocking_worker(self, text: str, my_id: int, voice=None, speed=None):
        """Fallback for older kokoro-onnx installs without create_stream():
        synthesizes the whole line, then plays it all at once. Everything
        has to be ready before anything is audible, so a slow synthesis
        (e.g. the CPU being contended with a game) shows up as one long
        silent pause before the line starts."""
        import numpy as np
        try:
            samples, sample_rate = self._kokoro.create(
                text, voice=voice or self.kokoro_voice,
                speed=speed if speed is not None else self.kokoro_speed, lang=self.kokoro_lang,
            )
        except Exception as e:
            self.log(f"[speech] Kokoro synthesis failed: {e}")
            return

        with self._utterance_lock:
            if my_id != self._utterance_id:
                return  # superseded while synthesizing -- drop it

        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        player = PLATFORM.open_pcm_player(sample_rate)
        with self._utterance_lock:
            if my_id != self._utterance_id:
                # Went stale in the gap between the check above and actually
                # starting playback -- kill it before it plays over whatever
                # superseded it.
                player.terminate()
                return
            self._player_proc = player
        player.write(pcm)
        player.close_stdin()

    def _say_kokoro_streaming_worker(self, text: str, my_id: int, voice=None, speed=None):
        """Uses kokoro-onnx's create_stream() to synthesize and play a line
        chunk-by-chunk (roughly sentence-by-sentence) instead of waiting for
        the whole thing. Playback of the first chunk starts as soon as it's
        ready, so only that chunk's synthesis time gates how long the line
        stays silent -- a CPU stall partway through a long line delays the
        *next* chunk, not the start of speech. This is the single biggest
        lever against "long pause when the CPU gets busy": before, the
        entire paragraph had to finish synthesizing before anything played."""
        try:
            asyncio.run(self._stream_kokoro_chunks(text, my_id, voice, speed))
        except Exception as e:
            self.log(f"[speech] Kokoro streaming synthesis failed: {e}")

    async def _stream_kokoro_chunks(self, text: str, my_id: int, voice=None, speed=None):
        import numpy as np

        player = None
        try:
            stream = self._kokoro.create_stream(
                text, voice=voice or self.kokoro_voice,
                speed=speed if speed is not None else self.kokoro_speed, lang=self.kokoro_lang,
            )
            async for samples, sample_rate in stream:
                with self._utterance_lock:
                    if my_id != self._utterance_id:
                        return  # superseded -- stop pulling/playing further chunks immediately
                if player is None:
                    player = PLATFORM.open_pcm_player(sample_rate)
                    with self._utterance_lock:
                        if my_id != self._utterance_id:
                            player.terminate()
                            return
                        self._player_proc = player
                pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
                player.write(pcm)
        finally:
            if player is not None:
                player.close_stdin()

    def _say_windows_sapi(self, text: str, voice=None, speed=None):
        # pyttsx3's own .say()/.runAndWait() plays audio itself and only
        # supports being interrupted via a documented-but-flaky cross-thread
        # .stop() call. Rather than depend on that, this mirrors Kokoro's
        # blocking-worker approach: synthesize to a temp WAV file (fast for
        # SAPI5's classic voices, so the lack of streaming here is much less
        # noticeable than it was for Kokoro), then hand the raw PCM to
        # PLATFORM.open_pcm_player() -- the same generic player object that
        # _stop_current() already knows how to terminate cleanly.
        with self._utterance_lock:
            my_id = self._utterance_id
        threading.Thread(target=self._say_windows_sapi_worker, args=(text, my_id, voice, speed), daemon=True).start()

    def _say_windows_sapi_worker(self, text: str, my_id: int, voice=None, speed=None):
        import tempfile
        import wave

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name
        try:
            with self._sapi_lock:
                # The voice AND rate changes are held inside this same lock:
                # the SAPI5 engine is one shared object, so setting them and
                # synthesizing have to be a single atomic step or another
                # character's line could land between them and steal this
                # one's voice or pace.
                previous_voice = None
                if voice:
                    try:
                        previous_voice = self._sapi_engine.getProperty("voice")
                        self._sapi_engine.setProperty("voice", voice)
                    except Exception:
                        previous_voice = None
                previous_rate = None
                if speed is not None:
                    try:
                        previous_rate = self._sapi_engine.getProperty("rate")
                        self._sapi_engine.setProperty("rate", speed)
                    except Exception:
                        previous_rate = None
                try:
                    self._sapi_engine.save_to_file(text, wav_path)
                    self._sapi_engine.runAndWait()
                finally:
                    if previous_voice is not None:
                        try:
                            self._sapi_engine.setProperty("voice", previous_voice)
                        except Exception:
                            pass
                    if previous_rate is not None:
                        try:
                            self._sapi_engine.setProperty("rate", previous_rate)
                        except Exception:
                            pass

            with self._utterance_lock:
                if my_id != self._utterance_id:
                    return  # superseded while synthesizing -- drop it

            try:
                with wave.open(wav_path, "rb") as wf:
                    sample_rate = wf.getframerate()
                    pcm = wf.readframes(wf.getnframes())
            except Exception as e:
                self.log(f"[speech] Couldn't read SAPI5 output audio: {e}")
                return

            player = PLATFORM.open_pcm_player(sample_rate)
            with self._utterance_lock:
                if my_id != self._utterance_id:
                    # Went stale in the gap between the check above and
                    # actually starting playback -- kill it before it plays
                    # over whatever superseded it.
                    player.terminate()
                    return
                self._player_proc = player
            player.write(pcm)
            player.close_stdin()
        except Exception as e:
            self.log(f"[speech] SAPI5 synthesis failed: {e}")
        finally:
            try:
                os.remove(wav_path)
            except OSError:
                pass

    def _check_finished(self, proc, label: str):
        """If `proc` already exited (on its own, not because we're about to
        interrupt it) with a nonzero code, surface why — this is what makes
        a silently-failing speech engine visible instead of just... silent,
        especially when running from the GUI with no terminal to catch
        stderr output."""
        if proc is None or proc.poll() is None or proc.returncode == 0:
            return
        if proc.returncode == -15 or (sys.platform == "win32" and proc.returncode == 1):
            # -15 means "killed by SIGTERM" on Linux; subprocess.Popen.terminate()
            # on Windows has no signal concept and always exits real
            # subprocesses with code 1 instead. Either way, the only thing
            # in this script that ever kills one of these processes is the
            # .terminate() call a few lines down in _stop_current() itself
            # (a real Popen for espeak-ng, and _SoundDevicePcmPlayer
            # self-reports 0 on intentional shutdown rather than reaching
            # this branch at all). So this is never a real failure: it's
            # this same method, on some earlier call, having intentionally
            # cut off speech that was still playing (new dialogue
            # interrupting old, or the pause hotkey stopping mid-sentence)
            # — surfacing it as an error would just be misreporting our own
            # doing. NOTE: a genuine espeak-ng crash on Windows that happens
            # to also exit with code 1 would be masked by this same check --
            # an acceptable trade-off here, same as it already is for -15 on
            # Linux.
            return
        # self._proc is a real subprocess.Popen only for espeak-ng now;
        # self._player_proc may instead be a platform_adapter.PcmPlayer
        # (Kokoro, Piper, and Windows/SAPI5 all synthesize in-process and
        # only ever produce a PcmPlayer, never a subprocess) -- read_stderr()
        # is how a PcmPlayer surfaces error text since it may not have a
        # real subprocess .stderr to read from (see _SoundDevicePcmPlayer).
        if hasattr(proc, "read_stderr"):
            stderr_text = proc.read_stderr()
        else:
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
        if self.engine in ("kokoro", "windows", "piper"):
            # Invalidate any Kokoro/SAPI5/Piper synthesis still running in
            # the background so it drops its result instead of playing a
            # superseded (or post-pause/stop) line once it finishes. Piper
            # joined this list once it moved from a subprocess (killed by
            # the .terminate() loop below same as espeak-ng always was) to
            # in-process synthesis in a background thread -- a thread can't
            # be killed the way a process can, so it needs this same
            # cooperative "check the id, bail if superseded" mechanism
            # Kokoro/SAPI5 already used.
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


def run(args, stop_event=None, log=print, on_pause_change=None,
        profile=None, on_new_speaker=None, on_speaker_ready=None):
    """Runs the OCR -> speech loop until Ctrl+C (CLI) or stop_event is set
    (GUI, from a Stop button on another thread). `log` receives each status
    line — defaults to print for CLI use; the GUI passes a callback that
    forwards into its log panel instead.

    `profile` is an optional game_profile.Profile supplying speaker detection
    and the cast. Without one this loop behaves exactly as it always did, so
    the CLI and any existing setup are unaffected. `on_new_speaker(entry,
    line)` fires the first time a character speaks, which is what the GUI
    hangs its "who is this?" prompt on; it must not block, since it runs on
    this loop's thread."""
    apply_cpu_affinity(getattr(args, "cpu_affinity", "") or "", log=log)

    marker = None
    if args.ignore_popups:
        marker = (profile.get("popup_marker") if profile is not None else None) or load_popup_marker()
        if marker is None:
            sys.exit("--ignore-popups needs a saved marker. Run with --select-popup-marker first.")

    ocr_engine = getattr(args, "ocr_engine", "tesseract") or "tesseract"
    ocr_lang = args.lang
    if ocr_engine == "windows":
        if ocr_lang == "eng":
            # "eng" is the CLI's Tesseract-flavored default -- Windows OCR
            # wants a BCP-47 tag like "en" instead. Only remapped when
            # --lang was left at that default, so an explicit non-English
            # --lang (e.g. "ja") still passes through untouched.
            ocr_lang = "en"
        log(
            "OCR: Windows built-in engine (Tesseract not required). Note: this engine has no "
            "per-word confidence score, so --ocr-min-confidence has no effect."
        )

    # The profile owns the region when there is one -- that's what makes
    # switching games a dropdown instead of re-dragging the box. region.json
    # remains the fallback for the CLI and for anyone with no profile.
    region = (profile.get("region") if profile is not None else None) or load_region()
    capturer = PLATFORM.make_capturer(region)
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

    # Identifies which model is actually running, for looking a character's
    # per-model config up in the cast (see game_profile.model_key() and
    # Cast.get_model()) -- fixed for the life of this run, since the engine
    # and model don't change without restarting the reader.
    model_key = game_profile.model_key(
        args.engine,
        piper_model=getattr(args, "piper_model", None),
        kokoro_model=getattr(args, "kokoro_model", None),
    )

    if speaker is not None and on_speaker_ready:
        # Hands the live Speaker to the GUI so its Cast panel can audition a
        # voice on demand. Nothing in this loop depends on it, and building a
        # second Speaker just to preview a voice would mean loading a whole
        # second copy of the Kokoro/Piper model.
        try:
            on_speaker_ready(speaker)
        except Exception:
            pass

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
    hotkey = PLATFORM.make_hotkey_watcher(pause_key, on_pause_toggle, log=log) if pause_key else None
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

            raw = capturer.grab()
            img = preprocess_for_ocr(raw)
            min_conf = getattr(args, "ocr_min_confidence", 40)
            text = ocr_image(img, lang=ocr_lang, min_confidence=min_conf,
                              engine=ocr_engine, log=log)

            # Compare BEFORE the speaker name is stripped or announced, so the
            # dedup check sees the same shape of string every poll. Deciding
            # who's talking is comparatively expensive (it can involve a second
            # OCR pass for the zone detector), and there's no reason to do it
            # again for a line we already spoke.
            changed = bool(text) and similar(text, last_text) < args.similarity

            if changed:
                spoken, who = text, None
                cfg = None
                if profile is not None:
                    lines = ocr_lines(img, lang=ocr_lang, min_confidence=min_conf, engine=ocr_engine)

                    def _crop(x0, y0, x1, y1, _src=raw):
                        w, h = _src.size
                        return _src.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))

                    def _ocr(sub):
                        return ocr_image(preprocess_for_ocr(sub), lang=ocr_lang,
                                         min_confidence=min_conf, engine=ocr_engine, log=log)

                    name, dialogue, _via = profile.detect(
                        Observation(text, lines, crop=_crop, ocr=_ocr))
                    entry, is_new = profile.cast.observe(name) if name else (profile.cast.narrator(), False)
                    who = entry["name"] if entry else None
                    cfg = profile.voice_for(entry, model_key) if entry else None
                    spoken = apply_speaker_name_mode_for(
                        name, dialogue, text, profile.get("speaker_name_mode", "announce"))
                    if is_new and on_new_speaker:
                        # Deliberately fired AFTER the voice is resolved and
                        # just before speaking: a brand-new character says
                        # their first line immediately -- in whatever the
                        # engine's already configured with, since nothing's
                        # assigned them a voice yet -- while the prompt waits
                        # for an answer. Nothing here blocks on the user.
                        try:
                            on_new_speaker(entry, dialogue)
                        except Exception as e:
                            log(f"[cast] new-speaker callback failed: {e}")
                else:
                    spoken = apply_speaker_name_mode(text, getattr(args, "speaker_name_mode", "off"))

                timestamp = time.strftime("%H:%M:%S")
                log(f"[{timestamp}] {('<' + who + '> ') if who else ''}{spoken}")
                if speaker:
                    cfg = cfg or {}
                    speaker.say(spoken, voice=cfg.get("voice"),
                                speaker_id=cfg.get("speaker"), speed=cfg.get("speed"))
                last_text = text
                if profile is not None:
                    # Only ever writes when something actually changed -- a new
                    # character confirmed, or a learned spelling. The poll
                    # interval is the debounce.
                    profile.save_if_dirty()
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
    parser.add_argument("--ocr-engine", choices=["tesseract", "windows"],
                         default="windows" if sys.platform == "win32" else "tesseract",
                         help="OCR backend: 'tesseract' (default on Linux, needs the tesseract-ocr binary "
                              "installed separately) or 'windows' (default on Windows, uses the OS's own "
                              "built-in OCR via the 'winocr' package -- no separate binary or PATH entry "
                              "needed, but it has no per-word confidence score, so --ocr-min-confidence has "
                              "no effect on it). Pass --ocr-engine tesseract on Windows to use Tesseract "
                              "instead, if you have it installed and prefer its cleanup filtering.")
    parser.add_argument("--lang", default="eng",
                         help="OCR language code (default 'eng'). Meaning depends on --ocr-engine: "
                              "Tesseract wants its own 3-letter codes (e.g. 'eng', 'fra' -- needs "
                              "tesseract-ocr-<lang> installed for anything but English); the 'windows' "
                              "engine wants a BCP-47 tag instead (e.g. 'en', 'ja', 'fr' -- needs that "
                              "language's OCR pack installed in Windows). The default is remapped from "
                              "'eng' to 'en' automatically when --ocr-engine windows is used and --lang "
                              "wasn't set explicitly.")
    parser.add_argument("--ocr-min-confidence", type=int, default=40, metavar="0-100",
                         help="Discard OCR'd words below this confidence score (default 40). "
                              "Raise it if screen artifacts (dust, UI borders, icons) are getting spoken as "
                              "stray punctuation or gibberish words; lower it if real dialogue is getting "
                              "dropped. Only applies to --ocr-engine tesseract -- the 'windows' engine has "
                              "no per-word confidence score to filter on, so this is silently ignored there.")
    parser.add_argument("--speaker-name-mode", choices=["off", "skip", "announce"], default="off",
                         help="For dialogue boxes that show a character's name above their quoted line (which "
                              "OCR runs straight into the dialogue, e.g. 'Augustin El Borne \"And this must "
                              "be...\"'): 'skip' drops the name and speaks only the dialogue; 'announce' speaks "
                              "the name first with a pause before continuing into the dialogue. Default 'off' "
                              "speaks the text exactly as OCR'd. Detection only fires on a short Title-Cased "
                              "label immediately before a quote mark, so ordinary narration is left alone.")
    parser.add_argument("--rate", type=int, default=175, help="espeak-ng speech rate, words/min (default 175, ignored by --engine piper/kokoro).")
    parser.add_argument("--voice", default="", help="espeak-ng voice, e.g. en-us or mb-us1 (default: system default). Ignored by --engine piper/kokoro.")
    parser.add_argument("--engine", choices=["espeak", "piper", "kokoro", "windows"],
                         default="windows" if sys.platform == "win32" else "espeak",
                         help="TTS engine: 'espeak' (default on Linux; robotic, needs espeak-ng installed separately), 'piper' (natural neural voices, needs pip install piper-tts + a downloaded model), 'kokoro' (most natural of the four, needs pip install kokoro-onnx + two downloaded model files), or 'windows' (default on Windows; Windows-only, the built-in SAPI5 voices, comparable quality to espeak-ng, needs pip install pyttsx3 but no PATH setup or downloaded model — see README).")
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
                              "it with a demanding game on every core. Works on Linux and Windows (the latter "
                              "needs 'pip install psutil'); a no-op elsewhere. Default: unset (no pinning).")
    parser.add_argument("--similarity", type=float, default=0.92,
                         help="0-1 threshold above which new text is treated as 'unchanged' and not re-spoken (default 0.92).")
    parser.add_argument("--pause-key", default="space", metavar="KEY",
                         help="Key that pauses/resumes the narrator from anywhere, even while the game has "
                              "keyboard focus (e.g. 'space', 'f9', 'scrolllock'). Empty string disables the "
                              "hotkey entirely. Needs the 'evdev' package and read access to /dev/input on "
                              "Linux, or the 'keyboard' package on Windows (see README) — degrades "
                              "gracefully with a log note if unavailable.")
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
