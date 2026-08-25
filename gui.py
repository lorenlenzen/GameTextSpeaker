#!/usr/bin/env python3
"""
gui.py — desktop GUI for game_text_speaker.py.

Wraps the same OCR -> speech pipeline in a window with buttons, so you
never need a terminal after the first launch: pick the dialogue region,
optionally pick a popup marker, set your speech options, and hit Start.

Run it with:
    python3 gui.py

Needs tkinter (sudo apt install python3-tk if it's missing) plus whatever
game_text_speaker.py itself needs — see README.md.
"""

import argparse
import importlib.util
import json
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
except ImportError:
    sys.exit("Missing tkinter. Install it with:  sudo apt install python3-tk")

import game_text_speaker as core

# Sentinel shown in the espeak-ng Voice dropdown for "" (use espeak-ng's own
# system default voice, whatever that is). The dropdown is locked to its list
# (state="readonly") so a mistyped voice code -- e.g. "en-US" instead of the
# real "en-us" -- can never silently get saved and break speech, so this
# sentinel is how "no voice pinned" stays reachable through that dropdown.
EMPTY_VOICE_LABEL = "(system default)"


def _basename_or_placeholder(path_str: str) -> str:
    """For the read-only Model/Voices display fields: show just the
    filename rather than a long (often off-window) full path. The actual
    path is still what's stored and used -- this only changes what's shown."""
    return Path(path_str).name if path_str else "(none selected)"


def _get_espeak_voices():
    """Query espeak-ng for the voices it actually has installed, for the
    Voice dropdown. Falls back to a short list of common voices if
    espeak-ng isn't on PATH or the query fails for any reason -- this is a
    convenience, not something that should ever stop the GUI from opening."""
    fallback = ["en-us", "en-gb", "en-gb-x-rp", "en-gb-scotland", "en-au"]
    try:
        result = subprocess.run(
            ["espeak-ng", "--voices"], capture_output=True, text=True, timeout=3,
        )
        # Header line, then one row per voice: "Pty Language Age/Gender
        # VoiceName File Other Languages". The 2nd column (index 1,
        # "Language") is what -v actually expects -- e.g. "en-us",
        # "en-gb-x-rp", "mb-us1" -- confirmed against real output:
        #   2  en-us   --/M  English_(America)  gmw/en-US  (en 3)
        # It's tempting to reach for the "File" column instead (it looks
        # more like a canonical identifier), but that's a reference to the
        # underlying language/phoneme data file -- here "gmw/en-US" (gmw =
        # West Germanic) -- not the CLI voice name, and it's cased/formatted
        # independently of it (this is what previously put "en-US" in this
        # dropdown instead of the "en-us" that -v actually needs). VoiceName
        # has its spaces replaced with underscores by espeak-ng itself, so
        # it stays one token and can never shift these column positions.
        voices = set()
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                voices.add(parts[1])
        return sorted(voices) if voices else fallback
    except Exception:
        return fallback


def _get_kokoro_voices(voices_path: str):
    """List the voice names actually present in a Kokoro voices-*.bin file,
    for the Voice dropdown. That file is a numpy .npz archive (despite the
    .bin extension) mapping voice name -> style vector, so its keys are
    read directly rather than hardcoding a voice list that could drift out
    of sync with whatever file the user has. Falls back to a short list of
    well-regarded preset voices if no file is set yet, the path doesn't
    exist, or it can't be read for any reason -- same reasoning as the
    espeak-ng fallback above: a convenience, not something that should
    ever stop the GUI from opening."""
    fallback = ["af_heart", "af_bella", "af_nicole", "am_michael", "am_puck", "am_fenrir"]
    if not voices_path or not Path(voices_path).exists():
        return fallback
    try:
        import numpy as np
        with np.load(voices_path) as npz:
            names = sorted(npz.files)
        return names or fallback
    except Exception:
        return fallback

def _get_windows_native_voices():
    """List installed SAPI5 voice names (e.g. "Microsoft David Desktop"),
    for the Windows Native engine's Voice dropdown. Falls back to the two
    voices basically every Windows install ships if pyttsx3 isn't installed
    or the query fails for any reason -- same reasoning as the espeak-ng/
    Kokoro fallbacks above."""
    fallback = ["Microsoft David Desktop", "Microsoft Zira Desktop"]
    try:
        import pyttsx3
        engine = pyttsx3.init()
        names = [v.name for v in engine.getProperty("voices")]
        return names or fallback
    except Exception:
        return fallback


# Common Tesseract language codes mapped to the closest BCP-47 tag Windows'
# own OCR wants for the same language, and back. Not an exhaustive
# translation table -- just enough of one that switching the OCR engine
# radio carries the *same* language across to the new dropdown instead of
# silently resetting it (see _pick_lang_for_new_engine below).
_TESS_TO_BCP47 = {
    "eng": "en", "fra": "fr", "deu": "de", "spa": "es", "ita": "it",
    "por": "pt", "nld": "nl", "pol": "pl", "rus": "ru", "tur": "tr",
    "swe": "sv", "ukr": "uk", "vie": "vi", "ind": "id", "tha": "th",
    "jpn": "ja", "kor": "ko", "chi_sim": "zh-Hans", "chi_tra": "zh-Hant",
    "ara": "ar",
}
_BCP47_TO_TESS = {v: k for k, v in _TESS_TO_BCP47.items()}


def _get_tesseract_languages():
    """Live-queries the tesseract-ocr binary for the language packs it
    actually has installed (Tesseract's own .traineddata codes, e.g.
    "eng", "fra", "chi_sim"), for the Language dropdown. Same reasoning as
    the espeak-ng voices query above: only ever offers codes that will
    actually work on this install rather than a fixed list that could be
    wrong. Falls back to a short list of common codes if tesseract isn't
    on PATH, pytesseract isn't installed, or the query fails for any
    reason."""
    fallback = ["eng", "fra", "deu", "spa", "ita", "por", "jpn", "kor", "chi_sim", "rus"]
    try:
        import pytesseract
        langs = [name for name in pytesseract.get_languages(config="") if name != "osd"]
        return sorted(langs) if langs else fallback
    except Exception:
        return fallback


def _get_windows_ocr_languages():
    """Live-queries Windows' own OCR language packs via the same WinRT call
    winocr itself wraps (OcrEngine.available_recognizer_languages), for the
    Language dropdown -- BCP-47 tags, e.g. "en", "ja", "zh-Hans". Falls
    back to a short list of common tags if winrt/winocr isn't installed,
    this isn't Windows, or the query fails for any reason -- same
    reasoning as the fallbacks above."""
    fallback = ["en", "fr", "de", "es", "it", "pt", "ja", "ko", "zh-Hans", "zh-Hant", "ru", "ar"]
    try:
        from winrt.windows.media.ocr import OcrEngine
        tags = sorted({lang.language_tag for lang in OcrEngine.available_recognizer_languages})
        return tags or fallback
    except Exception:
        return fallback


def _pick_lang_for_new_engine(current_lang: str, new_engine: str, new_values: list) -> str:
    """When the OCR engine radio changes, try to carry the same language
    across to the new engine's own code scheme (e.g. Tesseract's "fra" ->
    Windows Native's "fr") instead of resetting to the first item in the
    new dropdown, which would silently change the recognized language out
    from under the user. Falls back to the first available value if
    there's no known mapping for the current code."""
    if current_lang in new_values:
        return current_lang
    table = _BCP47_TO_TESS if new_engine == "tesseract" else _TESS_TO_BCP47
    mapped = table.get(current_lang)
    if mapped in new_values:
        return mapped
    return new_values[0] if new_values else current_lang


def _package_available(module_name: str) -> bool:
    """True if `module_name` can actually be imported.

    This deliberately does a real import rather than the cheaper
    importlib.util.find_spec() metadata-only check. In a normal venv the
    two agree, but in a PyInstaller-frozen .exe they can disagree: a
    package can be *findable* (its frozen module entry exists) while a
    real import still fails, because something it needs at import or
    runtime time didn't get bundled alongside it -- exactly the shape of
    both bugs already hit here: kokoro-onnx's own config.json (a
    non-Python data file sitting next to its code, invisible to
    PyInstaller's import-based analysis) and pyttsx3's SAPI5 driver module
    (loaded via a dynamic importlib call find_spec() can't see either).
    find_spec() would happily call both of those "available" right up
    until the moment they're actually used and blow up -- which is exactly
    what disabling-if-unavailable is supposed to prevent. A real import is
    the only check that can't produce that false positive, since it's
    doing the exact same thing the corresponding engine would do."""
    try:
        importlib.import_module(module_name)
        return True
    except Exception:
        return False


def _binary_available(binary_name: str) -> bool:
    return shutil.which(binary_name) is not None


# Per-engine dependency status, checked once at import time (if the user
# installs something after the GUI is already open, a restart is needed to
# pick it up -- an acceptable trade-off for how rarely that happens). Used
# both to disable options that would just fail immediately if selected, and
# to compose the "not installed" notes shown in the ⓘ info buttons below.
#
# "piper" used to need its own special check here (a venv-adjacent
# `piper.exe`/PATH lookup, mirroring Speaker.__init__'s old subprocess-based
# binary resolution) -- now that both import the `piper` package directly
# and synthesize in-process (see game_text_speaker.py's "piper" branch for
# why), a plain _package_available() check is enough, exactly like kokoro.
ENGINE_AVAILABILITY = {
    "espeak": _binary_available("espeak-ng"),
    "piper": _package_available("piper"),
    "kokoro": _package_available("kokoro_onnx"),
    "windows": sys.platform == "win32" and _package_available("pyttsx3"),
}

ENGINE_LABELS = {
    "espeak": "espeak-ng",
    "piper": "Piper",
    "kokoro": "Kokoro",
    "windows": "Windows Native",
}

ENGINE_INSTALL_HINT = {
    "espeak": "sudo apt install espeak-ng  (Linux), or download the installer from "
              "https://github.com/espeak-ng/espeak-ng/releases  (Windows)",
    "piper": "pip install piper-tts  (inside your venv) plus a downloaded voice model -- see README",
    "kokoro": "pip install kokoro-onnx  plus two downloaded model files -- see README",
    "windows": "pip install pyttsx3  (wraps the OS's own built-in SAPI5 voices -- no extra download needed)",
}

OCR_ENGINE_AVAILABILITY = {
    "tesseract": _binary_available("tesseract"),
    "windows": sys.platform == "win32" and _package_available("winocr"),
}

OCR_ENGINE_LABELS = {
    "tesseract": "Tesseract",
    "windows": "Windows Native",
}

OCR_ENGINE_INSTALL_HINT = {
    "tesseract": "install the tesseract-ocr binary separately (any OS) -- see README",
    "windows": "pip install winocr  (may also need an OCR language pack added via Windows Settings -- see README)",
}

SETTINGS_PATH = Path(__file__).with_name("gui_settings.json")

DEFAULT_SETTINGS = {
    "engine": "windows" if sys.platform == "win32" else "espeak",
    "rate": 175,
    "voice": "",
    "piper_model": "",
    "piper_speaker": "",
    "piper_length_scale": "",
    "kokoro_model": "",
    "kokoro_voices": "",
    "kokoro_voice": "af_heart",
    "kokoro_speed": "",
    "kokoro_cpu_threads": "",
    "cpu_affinity": "",
    "interval": 0.5,
    "ocr_engine": "windows" if sys.platform == "win32" else "tesseract",
    "lang": "eng",
    "similarity": 0.92,
    "ocr_min_confidence": 40,
    "speaker_name_mode": "off",
    "ignore_popups": False,
    "popup_threshold": 20.0,
    "quiet": False,
    "pause_key": "space",
}


def _region_status_text() -> str:
    if core.CONFIG_PATH.exists():
        try:
            r = json.loads(core.CONFIG_PATH.read_text())
            return f"Region set: {r['x']},{r['y']}  {r['w']}x{r['h']}"
        except Exception:
            return "Region: (couldn't read region.json)"
    return "Region: not set yet"


def _popup_status_text() -> str:
    if core.POPUP_MARKER_PATH.exists():
        try:
            m = json.loads(core.POPUP_MARKER_PATH.read_text())
            return f"Marker set: {m['x']},{m['y']}  {m['w']}x{m['h']}  color {m['ref_color']}"
        except Exception:
            return "Marker: (couldn't read popup_marker.json)"
    return "Marker: not set"


class App:
    def __init__(self, root):
        self.root = root
        root.title("Game Text Speaker")
        root.geometry("620x700")
        root.minsize(520, 560)

        self.log_queue = queue.Queue()
        self.stop_event = None
        self.settings = self._load_settings()
        self._action_buttons = []
        # settings["engine"] is always a real engine name (never "quiet" --
        # that value didn't exist before the speech-engine radio absorbed
        # Quiet mode), so it's always a safe starting point to fall back to
        # if the user picks Quiet and then picks a real engine again later.
        self._last_engine = self.settings["engine"]

        self._build_ui()
        self._refresh_region_status()
        self._refresh_popup_status()
        self.root.after(100, self._drain_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- settings persistence ----------------

    def _load_settings(self) -> dict:
        settings = dict(DEFAULT_SETTINGS)
        if SETTINGS_PATH.exists():
            try:
                settings.update(json.loads(SETTINGS_PATH.read_text()))
            except Exception:
                pass
        return settings

    def _collect_settings(self) -> dict:
        selected = self.engine_var.get()
        # "quiet" is a value in the same radio group as the real engines
        # (see _build_ui), not a real --engine value -- resolve it back to
        # the last real engine chosen (game_text_speaker.py ignores
        # --engine entirely when --quiet is set, so this is only for a
        # sane persisted default) plus the quiet flag itself.
        engine = self._last_engine if selected == "quiet" else selected
        quiet = selected == "quiet"
        return {
            "engine": engine,
            "rate": self._as_int(self.rate_var.get(), DEFAULT_SETTINGS["rate"]),
            "voice": "" if self.voice_var.get() == EMPTY_VOICE_LABEL else self.voice_var.get().strip(),
            "piper_model": self.piper_model_var.get().strip(),
            "piper_speaker": self.piper_speaker_var.get().strip(),
            "piper_length_scale": self.piper_length_scale_var.get().strip(),
            "kokoro_model": self.kokoro_model_var.get().strip(),
            "kokoro_voices": self.kokoro_voices_var.get().strip(),
            "kokoro_voice": self.kokoro_voice_var.get().strip(),
            "kokoro_speed": self.kokoro_speed_var.get().strip(),
            "kokoro_cpu_threads": self.kokoro_cpu_threads_var.get().strip(),
            "cpu_affinity": self.cpu_affinity_var.get().strip(),
            "interval": self._as_float(self.interval_var.get(), DEFAULT_SETTINGS["interval"]),
            "ocr_engine": self.ocr_engine_var.get(),
            "lang": self.lang_var.get().strip() or "eng",
            "similarity": self._as_float(self.similarity_var.get(), DEFAULT_SETTINGS["similarity"]),
            "ocr_min_confidence": self._as_int(self.ocr_min_confidence_var.get(), DEFAULT_SETTINGS["ocr_min_confidence"]),
            "speaker_name_mode": self.speaker_name_mode_var.get(),
            "ignore_popups": self.ignore_popups_var.get(),
            "popup_threshold": self._as_float(self.popup_threshold_var.get(), DEFAULT_SETTINGS["popup_threshold"]),
            "quiet": quiet,
            "pause_key": self.pause_key_var.get().strip(),
        }

    def _save_settings(self):
        try:
            SETTINGS_PATH.write_text(json.dumps(self._collect_settings(), indent=2))
        except Exception:
            pass  # not worth bothering the user about

    @staticmethod
    def _as_int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_float(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    # ---------------- UI construction ----------------

    def _add_info_button(self, parent, text: str, title: str = "Info", side: str = "left"):
        """Small circled-i button that pops up `text` in a dialog when
        clicked. Used instead of an always-visible wrapped hint label under
        a field -- keeps the explanation one click away without it
        permanently taking up window height. `side` controls which edge of
        `parent` it packs against, so it can either sit right next to the
        field it explains (the default) or be pinned to a corner (e.g. a
        section's top-right)."""
        padx = (4, 0) if side == "left" else (0, 4)
        ttk.Button(parent, text="ⓘ", width=2,
                   command=lambda: messagebox.showinfo(title, text)).pack(side=side, padx=padx)

    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        region_frame = ttk.LabelFrame(self.root, text="1. Dialogue region")
        region_frame.pack(fill="x", **pad)
        self.region_status_label = ttk.Label(region_frame, text="")
        self.region_status_label.pack(side="left", padx=6, pady=6)
        btn = ttk.Button(region_frame, text="From Screenshot…", command=self._on_select_region_from_image)
        btn.pack(side="right", padx=6, pady=6)
        self._action_buttons.append(btn)
        btn = ttk.Button(region_frame, text="Select Region…", command=self._on_select_region)
        btn.pack(side="right", padx=6, pady=6)
        self._action_buttons.append(btn)

        popup_frame = ttk.LabelFrame(self.root, text="2. Ignore popups/overlays (optional)")
        popup_frame.pack(fill="x", **pad)
        top_row = ttk.Frame(popup_frame)
        top_row.pack(fill="x", padx=6, pady=(6, 0))
        self.popup_status_label = ttk.Label(top_row, text="")
        self.popup_status_label.pack(side="left")
        btn = ttk.Button(top_row, text="Select Popup Marker…", command=self._on_select_popup_marker)
        btn.pack(side="right")
        self._action_buttons.append(btn)

        bottom_row = ttk.Frame(popup_frame)
        bottom_row.pack(fill="x", padx=6, pady=6)
        self.ignore_popups_var = tk.BooleanVar(value=self.settings["ignore_popups"])
        ttk.Checkbutton(bottom_row, text="Ignore popups while running", variable=self.ignore_popups_var).pack(side="left")
        ttk.Label(bottom_row, text="   Threshold:").pack(side="left")
        self.popup_threshold_var = tk.StringVar(value=str(self.settings["popup_threshold"]))
        ttk.Entry(bottom_row, textvariable=self.popup_threshold_var, width=6).pack(side="left")

        speech_frame = ttk.LabelFrame(self.root, text="3. Speech")
        speech_frame.pack(fill="x", **pad)

        engine_col = ttk.Frame(speech_frame)
        engine_col.pack(fill="x", padx=6, pady=(6, 0))
        # Quiet mode and the real TTS engines are mutually exclusive in
        # practice -- game_text_speaker.py's run() never even constructs a
        # Speaker when --quiet is set, so whichever engine happens to be
        # selected underneath it is irrelevant. Folding "Quiet" into this
        # same radio group (instead of a separate checkbox elsewhere) makes
        # that mutual exclusivity the obvious, enforced default instead of
        # something the user has to keep straight themselves, and it's
        # listed first since picking it is the one choice that overrides
        # everything below it.
        self.engine_var = tk.StringVar(value="quiet" if self.settings["quiet"] else self.settings["engine"])
        engine_choices = [("quiet", "Quiet — log recognized text, don't speak it")]
        if sys.platform == "win32":
            # Listed first among the real engines on Windows: it's the
            # zero-setup option there (no separate install, unlike
            # espeak-ng/Piper/Kokoro) and the default engine on this
            # platform -- see DEFAULT_SETTINGS.
            engine_choices.append(("windows", "Windows Native — built-in, zero setup"))
        engine_choices += [
            ("espeak", "espeak-ng — robotic, needs espeak-ng installed separately"),
            ("piper", "Piper — natural, needs a model"),
            ("kokoro", "Kokoro — most natural, bigger download"),
        ]
        # Disabling every unavailable engine is only useful when at least
        # one real engine remains selectable -- if something about this
        # install is stripped-down enough that NONE of them are available,
        # disabling all of them would leave the selector with only "Quiet"
        # reachable and no way to actually hear anything. In that unlikely
        # case, every option is left enabled and _on_start()'s own checks
        # are what surface the real problem once the user hits Start.
        any_available = any(ENGINE_AVAILABILITY.get(name) for name, _ in engine_choices if name != "quiet")
        self.engine_radios = {}
        engine_list_col = ttk.Frame(engine_col)
        engine_list_col.pack(fill="x")
        for i, (name, label) in enumerate(engine_choices):
            if name == "quiet":
                state = "normal"
            else:
                state = "normal" if (not any_available or ENGINE_AVAILABILITY.get(name)) else "disabled"
            if i == 0:
                # The info button lines up with this first radio's row
                # (rather than sitting in its own row above, or centered
                # against the full stacked height of every radio below) --
                # pinned to the right edge of that one row.
                first_engine_row = ttk.Frame(engine_list_col)
                first_engine_row.pack(fill="x")
                rb = ttk.Radiobutton(first_engine_row, text=label, variable=self.engine_var,
                                      value=name, command=self._on_engine_selected, state=state)
                rb.pack(side="left", anchor="w")
                self._add_info_button(first_engine_row, self._engine_info_text(), title="Speech engine", side="right")
            else:
                rb = ttk.Radiobutton(engine_list_col, text=label, variable=self.engine_var,
                                      value=name, command=self._on_engine_selected, state=state)
                rb.pack(anchor="w")
            self.engine_radios[name] = rb

        self.espeak_frame = ttk.Frame(speech_frame)
        espeak_row = ttk.Frame(self.espeak_frame)
        espeak_row.pack(fill="x")
        ttk.Label(espeak_row, text="Rate (wpm):").pack(side="left")
        self.rate_var = tk.StringVar(value=str(self.settings["rate"]))
        ttk.Entry(espeak_row, textvariable=self.rate_var, width=6).pack(side="left")
        ttk.Label(espeak_row, text="  Voice:").pack(side="left")
        self.voice_var = tk.StringVar(value=self.settings["voice"] or EMPTY_VOICE_LABEL)
        ttk.Combobox(espeak_row, textvariable=self.voice_var,
                     values=[EMPTY_VOICE_LABEL] + _get_espeak_voices(),
                     width=14, state="readonly").pack(side="left")

        self.piper_frame = ttk.Frame(speech_frame)
        piper_row1 = ttk.Frame(self.piper_frame)
        piper_row1.pack(fill="x")
        ttk.Label(piper_row1, text="Model:").pack(side="left")
        self.piper_model_var = tk.StringVar(value=self.settings["piper_model"])
        self.piper_model_display_var = tk.StringVar(value=_basename_or_placeholder(self.settings["piper_model"]))
        ttk.Label(piper_row1, textvariable=self.piper_model_display_var).pack(side="left", padx=(4, 8))
        ttk.Button(piper_row1, text="Browse…", command=self._on_browse_piper_model).pack(side="left")
        piper_row2 = ttk.Frame(self.piper_frame)
        piper_row2.pack(fill="x", pady=(4, 0))
        ttk.Label(piper_row2, text="Speaker ID:").pack(side="left")
        self.piper_speaker_var = tk.StringVar(value=str(self.settings["piper_speaker"]))
        ttk.Entry(piper_row2, textvariable=self.piper_speaker_var, width=5).pack(side="left")
        ttk.Label(piper_row2, text="  Speed:").pack(side="left")
        self.piper_length_scale_var = tk.StringVar(value=str(self.settings["piper_length_scale"]))
        ttk.Entry(piper_row2, textvariable=self.piper_length_scale_var, width=5).pack(side="left")
        self._add_info_button(piper_row2, "Speed: lower is faster, higher is slower.", title="Piper speed")

        self.kokoro_frame = ttk.Frame(speech_frame)
        kokoro_row1 = ttk.Frame(self.kokoro_frame)
        kokoro_row1.pack(fill="x")
        ttk.Label(kokoro_row1, text="Model:").pack(side="left")
        self.kokoro_model_var = tk.StringVar(value=self.settings["kokoro_model"])
        self.kokoro_model_display_var = tk.StringVar(value=_basename_or_placeholder(self.settings["kokoro_model"]))
        ttk.Label(kokoro_row1, textvariable=self.kokoro_model_display_var).pack(side="left", padx=(4, 8))
        ttk.Button(kokoro_row1, text="Browse…", command=self._on_browse_kokoro_model).pack(side="left")
        kokoro_row2 = ttk.Frame(self.kokoro_frame)
        kokoro_row2.pack(fill="x", pady=(4, 0))
        ttk.Label(kokoro_row2, text="Voices:").pack(side="left")
        self.kokoro_voices_var = tk.StringVar(value=self.settings["kokoro_voices"])
        self.kokoro_voices_display_var = tk.StringVar(value=_basename_or_placeholder(self.settings["kokoro_voices"]))
        ttk.Label(kokoro_row2, textvariable=self.kokoro_voices_display_var).pack(side="left", padx=(4, 8))
        ttk.Button(kokoro_row2, text="Browse…", command=self._on_browse_kokoro_voices).pack(side="left")
        kokoro_row3 = ttk.Frame(self.kokoro_frame)
        kokoro_row3.pack(fill="x", pady=(4, 0))
        ttk.Label(kokoro_row3, text="Voice:").pack(side="left")
        self.kokoro_voice_var = tk.StringVar(value=self.settings["kokoro_voice"])
        self.kokoro_voice_combo = ttk.Combobox(
            kokoro_row3, textvariable=self.kokoro_voice_var,
            values=_get_kokoro_voices(self.settings["kokoro_voices"]), width=12,
            state="readonly",
        )
        self.kokoro_voice_combo.pack(side="left")
        ttk.Label(kokoro_row3, text="  Speed:").pack(side="left")
        self.kokoro_speed_var = tk.StringVar(value=str(self.settings["kokoro_speed"]))
        ttk.Entry(kokoro_row3, textvariable=self.kokoro_speed_var, width=5).pack(side="left")
        self._add_info_button(kokoro_row3, "Speed: higher is faster, lower is slower.", title="Kokoro speed")

        kokoro_row4 = ttk.Frame(self.kokoro_frame)
        kokoro_row4.pack(fill="x", pady=(4, 0))
        ttk.Label(kokoro_row4, text="CPU threads:").pack(side="left")
        self.kokoro_cpu_threads_var = tk.StringVar(value=str(self.settings["kokoro_cpu_threads"]))
        ttk.Entry(kokoro_row4, textvariable=self.kokoro_cpu_threads_var, width=5).pack(side="left")
        self._add_info_button(
            kokoro_row4,
            "Caps how many CPU threads a single Kokoro synthesis call can use. Blank lets onnxruntime pick "
            "its own default (usually one per logical core) -- fastest when the system is otherwise idle, "
            "but on a busy system (a demanding game using every core) that can mean Kokoro fights the game "
            "for CPU on all of them at once, causing longer pauses rather than shorter ones. Try a smaller "
            "number (e.g. 4-6 on an 8-core/16-thread CPU) and see if pauses smooth out -- there's no single "
            "right answer, it depends on how much CPU the game leaves available. Needs a kokoro-onnx version "
            "with Kokoro.from_session(); older installs ignore this and log a note.",
            title="Kokoro CPU threads",
        )

        if sys.platform == "win32":
            self.windows_frame = ttk.Frame(speech_frame)
            windows_row = ttk.Frame(self.windows_frame)
            windows_row.pack(fill="x")
            ttk.Label(windows_row, text="Rate (wpm):").pack(side="left")
            # Deliberately the SAME rate_var/voice_var as the espeak-ng
            # frame above -- --rate/--voice are shared top-level flags in
            # game_text_speaker.py, not namespaced per engine, and the
            # Windows engine reuses them exactly the way espeak-ng does.
            ttk.Entry(windows_row, textvariable=self.rate_var, width=6).pack(side="left")
            ttk.Label(windows_row, text="  Voice:").pack(side="left")
            self.windows_voice_combo = ttk.Combobox(
                windows_row, textvariable=self.voice_var,
                values=[EMPTY_VOICE_LABEL] + _get_windows_native_voices(),
                width=22, state="readonly",
            )
            self.windows_voice_combo.pack(side="left")

        self._update_engine_widgets()

        ocr_frame = ttk.LabelFrame(self.root, text="4. OCR / timing")
        ocr_frame.pack(fill="x", **pad)

        ocr_engine_row = ttk.Frame(ocr_frame)
        ocr_engine_row.pack(fill="x", padx=6, pady=(6, 0))
        self.ocr_engine_var = tk.StringVar(value=self.settings["ocr_engine"])
        ocr_engine_choices = (
            [("windows", "Windows Native — built-in, zero setup")] if sys.platform == "win32" else []
        ) + [("tesseract", "Tesseract — needs the tesseract-ocr binary installed separately")]
        # Same reasoning as the speech engine radios above: disabling every
        # unavailable OCR engine is only useful when at least one remains
        # selectable -- if neither is available, every option is left
        # enabled instead and _on_start() surfaces the real problem.
        any_ocr_available = any(OCR_ENGINE_AVAILABILITY.get(name) for name, _ in ocr_engine_choices)
        self.ocr_engine_radios = {}
        ocr_engine_list_col = ttk.Frame(ocr_engine_row)
        ocr_engine_list_col.pack(fill="x")
        for i, (name, label) in enumerate(ocr_engine_choices):
            state = "normal" if (not any_ocr_available or OCR_ENGINE_AVAILABILITY.get(name)) else "disabled"
            if i == 0:
                # Same reasoning as the speech engine radios above: lines up
                # with this first radio's own row, pinned to the right edge,
                # rather than centered against the full stacked height.
                first_ocr_row = ttk.Frame(ocr_engine_list_col)
                first_ocr_row.pack(fill="x")
                rb = ttk.Radiobutton(first_ocr_row, text=label, variable=self.ocr_engine_var,
                                      value=name, command=self._update_ocr_engine_widgets, state=state)
                rb.pack(side="left", anchor="w")
                self._add_info_button(first_ocr_row, self._ocr_engine_info_text(), title="OCR engine", side="right")
            else:
                rb = ttk.Radiobutton(ocr_engine_list_col, text=label, variable=self.ocr_engine_var,
                                      value=name, command=self._update_ocr_engine_widgets, state=state)
                rb.pack(anchor="w")
            self.ocr_engine_radios[name] = rb

        self.lang_row = ttk.Frame(ocr_frame)
        self.lang_row.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Label(self.lang_row, text="Language:").pack(side="left")
        self.lang_var = tk.StringVar(value=self.settings["lang"])
        # Tesseract and Windows Native use unrelated language-code schemes
        # (Tesseract's own "eng"/"fra"/... vs. Windows' BCP-47 "en"/"fr"/...)
        # -- see _get_tesseract_languages/_get_windows_ocr_languages -- so
        # the dropdown is seeded with whichever engine is selected right
        # now, remapping the saved code across if it doesn't fit.
        initial_lang_values = (
            _get_tesseract_languages() if self.ocr_engine_var.get() == "tesseract" else _get_windows_ocr_languages()
        )
        if self.lang_var.get() not in initial_lang_values:
            self.lang_var.set(
                _pick_lang_for_new_engine(self.lang_var.get(), self.ocr_engine_var.get(), initial_lang_values)
            )
        self.lang_combo = ttk.Combobox(self.lang_row, textvariable=self.lang_var,
                                        values=initial_lang_values, width=8, state="readonly")
        self.lang_combo.pack(side="left")
        ttk.Label(self.lang_row, text="  Interval:").pack(side="left")
        self.interval_var = tk.StringVar(value=str(self.settings["interval"]))
        ttk.Entry(self.lang_row, textvariable=self.interval_var, width=6).pack(side="left")
        ttk.Label(self.lang_row, text="  Similarity:").pack(side="left")
        self.similarity_var = tk.StringVar(value=str(self.settings["similarity"]))
        ttk.Entry(self.lang_row, textvariable=self.similarity_var, width=6).pack(side="left")

        # Windows Native's OCR has no per-word confidence score at all (see
        # _ocr_engine_info_text below), so this field would silently do
        # nothing if left visible for it. _update_ocr_engine_widgets shows
        # this row only while Tesseract is selected, rather than leaving it
        # up with a note explaining it does nothing.
        self.confidence_row = ttk.Frame(ocr_frame)
        ttk.Label(self.confidence_row, text="OCR cleanup (min confidence):").pack(side="left")
        self.ocr_min_confidence_var = tk.StringVar(value=str(self.settings["ocr_min_confidence"]))
        ttk.Entry(self.confidence_row, textvariable=self.ocr_min_confidence_var, width=5).pack(side="left")
        self._add_info_button(
            self.confidence_row,
            "Drops low-confidence OCR results (0-100) before they're spoken -- raise this if "
            "screen artifacts are read aloud as stray punctuation or gibberish; lower it if real "
            "dialogue is getting dropped.",
            title="OCR cleanup",
        )

        self.speaker_name_row = ttk.Frame(ocr_frame)
        self.speaker_name_row.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Label(self.speaker_name_row, text="Speaker name:").pack(side="left")
        self.speaker_name_mode_var = tk.StringVar(value=self.settings["speaker_name_mode"])
        ttk.Combobox(self.speaker_name_row, textvariable=self.speaker_name_mode_var,
                     values=["off", "skip", "announce"], width=10, state="readonly").pack(side="left")
        self._add_info_button(
            self.speaker_name_row,
            "For boxes that show a character's name above their quoted line: 'skip' drops the "
            "name and speaks only the dialogue; 'announce' speaks the name with a pause before "
            "the dialogue instead of it running straight into the first word. 'off' speaks the "
            "text exactly as OCR'd.",
            title="Speaker name",
        )

        self._update_ocr_engine_widgets()

        row3 = ttk.Frame(ocr_frame)
        row3.pack(fill="x", padx=6, pady=(4, 6))
        ttk.Label(row3, text="Pause key:").pack(side="left")
        self.pause_key_var = tk.StringVar(value=self.settings["pause_key"])
        ttk.Entry(row3, textvariable=self.pause_key_var, width=10).pack(side="left")
        self._add_info_button(row3, "Pauses narration even while the game is focused. Blank disables.",
                               title="Pause key")
        # CPU affinity pinning isn't offered on the Windows build -- left
        # out of this row entirely rather than shown with a "Linux only"
        # note next to it. self.cpu_affinity_var still exists (forced blank
        # here) purely so _collect_settings/core.run() always have a value
        # to read; there's just no widget on this platform to change it.
        if sys.platform == "win32":
            self.cpu_affinity_var = tk.StringVar(value="")
        else:
            ttk.Label(row3, text="  CPU affinity:").pack(side="left")
            self.cpu_affinity_var = tk.StringVar(value=self.settings["cpu_affinity"])
            ttk.Entry(row3, textvariable=self.cpu_affinity_var, width=10).pack(side="left")
            self._add_info_button(
                row3,
                "Pins this whole process to specific CPU cores, e.g. '4,5,6,7' -- reserves uncontended CPU "
                "time for OCR + speech instead of leaving the OS to time-share it with a demanding game on "
                "every core. Linux only. Blank leaves this unset (no pinning).",
                title="CPU affinity",
            )

        control_frame = ttk.Frame(self.root)
        control_frame.pack(fill="x", **pad)
        self.start_button = ttk.Button(control_frame, text="▶ Start", command=self._on_start)
        self.start_button.pack(side="left", padx=6, pady=6)
        self._action_buttons.append(self.start_button)
        self.stop_button = ttk.Button(control_frame, text="■ Stop", command=self._on_stop, state="disabled")
        self.stop_button.pack(side="left", padx=6, pady=6)
        self.status_label = ttk.Label(control_frame, text="Idle", font=("", 10, "bold"))
        self.status_label.pack(side="left", padx=12)

        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=14, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

    def _on_engine_selected(self):
        # Remember the last real (non-"quiet") engine so _collect_settings
        # can still persist a legitimate --engine value even while "Quiet"
        # is selected -- game_text_speaker.py never constructs a Speaker
        # when --quiet is set, so this is only for a sensible default to
        # come back to if the user un-selects Quiet later.
        if self.engine_var.get() != "quiet":
            self._last_engine = self.engine_var.get()
        self._update_engine_widgets()

    def _update_engine_widgets(self):
        engine = self.engine_var.get()
        frames = {"espeak": self.espeak_frame, "piper": self.piper_frame, "kokoro": self.kokoro_frame}
        if hasattr(self, "windows_frame"):
            frames["windows"] = self.windows_frame
        for name, frame in frames.items():
            if name == engine:
                frame.pack(fill="x", padx=6, pady=(0, 6))
            else:
                frame.pack_forget()

    def _update_ocr_engine_widgets(self):
        engine = self.ocr_engine_var.get()
        values = _get_tesseract_languages() if engine == "tesseract" else _get_windows_ocr_languages()
        self.lang_combo["values"] = values
        if self.lang_var.get() not in values:
            self.lang_var.set(_pick_lang_for_new_engine(self.lang_var.get(), engine, values))

        if engine == "tesseract":
            self.confidence_row.pack(fill="x", padx=6, pady=(4, 0), before=self.speaker_name_row)
        else:
            self.confidence_row.pack_forget()

    def _engine_info_text(self) -> str:
        shown = (["windows"] if sys.platform == "win32" else []) + ["espeak", "piper", "kokoro"]
        lines = ["Speech engine status on this system:", ""]
        for name in shown:
            ok = ENGINE_AVAILABILITY.get(name)
            line = f"{ENGINE_LABELS[name]}: {'installed' if ok else 'NOT installed'}"
            if not ok:
                line += f"\n    -> {ENGINE_INSTALL_HINT[name]}"
            lines.append(line)
        lines.append("")
        lines.append("A greyed-out option means it isn't installed yet -- install it per the note above, "
                      "then restart this app to pick it up.")
        return "\n".join(lines)

    def _ocr_engine_info_text(self) -> str:
        shown = (["windows"] if sys.platform == "win32" else []) + ["tesseract"]
        lines = ["OCR engine status on this system:", ""]
        for name in shown:
            ok = OCR_ENGINE_AVAILABILITY.get(name)
            line = f"{OCR_ENGINE_LABELS[name]}: {'installed' if ok else 'NOT installed'}"
            if not ok:
                line += f"\n    -> {OCR_ENGINE_INSTALL_HINT[name]}"
            lines.append(line)
        return "\n".join(lines)

    # ---------------- status labels ----------------

    def _refresh_region_status(self):
        self.region_status_label.config(text=_region_status_text())

    def _refresh_popup_status(self):
        self.popup_status_label.config(text=_popup_status_text())

    # ---------------- logging (thread-safe: workers push, main thread drains) ----------------

    def log(self, msg):
        self.log_queue.put(("log", msg))

    def _append_log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", str(msg) + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _drain_log_queue(self):
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "action_done":
                    self._set_actions_enabled(True)
                    if payload:
                        payload()
                elif kind == "run_finished":
                    self.start_button.config(state="normal")
                    self.stop_button.config(state="disabled")
                    self.status_label.config(text="Idle")
                    self.stop_event = None
                elif kind == "pause_state":
                    self.status_label.config(text="Paused" if payload else "Running…")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    def _set_actions_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for b in self._action_buttons:
            b.config(state=state)

    # ---------------- region / marker selection ----------------

    def _on_select_region(self):
        self._set_actions_enabled(False)
        self.log("Select Region: click the button, then Alt+Tab back to the game — "
                  "your cursor stays armed to drag a box even once the game is focused.")

        def worker():
            try:
                core.select_region(log=self.log)
            except (SystemExit, Exception) as e:
                self.log(f"Error: {e}")
            finally:
                self.log_queue.put(("action_done", self._refresh_region_status))

        threading.Thread(target=worker, daemon=True).start()

    def _on_select_region_from_image(self):
        path = filedialog.askopenfilename(
            title="Select a screenshot with the dialogue visible",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            core.select_region_from_image(path, master=self.root, log=self.log)
        except Exception as e:
            self.log(f"Error: {e}")
            messagebox.showerror("Selection failed", str(e))
        self._refresh_region_status()

    def _on_select_popup_marker(self):
        self._set_actions_enabled(False)
        self.log("Select Popup Marker: get the popup showing, click the button, then Alt+Tab back "
                  "and drag a small box over a spot unique to the popup.")

        def worker():
            try:
                core.select_popup_marker(log=self.log)
            except (SystemExit, Exception) as e:
                self.log(f"Error: {e}")
            finally:
                self.log_queue.put(("action_done", self._refresh_popup_status))

        threading.Thread(target=worker, daemon=True).start()

    def _on_browse_piper_model(self):
        path = filedialog.askopenfilename(
            title="Select a Piper voice model",
            filetypes=[("Piper voice model", "*.onnx"), ("All files", "*.*")],
        )
        if path:
            self.piper_model_var.set(path)
            self.piper_model_display_var.set(_basename_or_placeholder(path))

    def _on_browse_kokoro_model(self):
        path = filedialog.askopenfilename(
            title="Select Kokoro's model file (kokoro-v1.0.onnx)",
            filetypes=[("Kokoro model", "*.onnx"), ("All files", "*.*")],
        )
        if path:
            self.kokoro_model_var.set(path)
            self.kokoro_model_display_var.set(_basename_or_placeholder(path))

    def _on_browse_kokoro_voices(self):
        path = filedialog.askopenfilename(
            title="Select Kokoro's voices file (voices-v1.0.bin)",
            filetypes=[("Kokoro voices", "*.bin"), ("All files", "*.*")],
        )
        if path:
            self.kokoro_voices_var.set(path)
            self.kokoro_voices_display_var.set(_basename_or_placeholder(path))
            self.kokoro_voice_combo["values"] = _get_kokoro_voices(path)

    # ---------------- run / stop ----------------

    def _on_start(self):
        if not core.CONFIG_PATH.exists():
            messagebox.showwarning("No region set", "Select a dialogue region first (step 1).")
            return

        settings = self._collect_settings()
        self._save_settings()

        if settings["ignore_popups"] and not core.POPUP_MARKER_PATH.exists():
            messagebox.showwarning(
                "No popup marker",
                "'Ignore popups' is checked but no popup marker is saved yet. "
                "Select one first (step 2), or uncheck it.",
            )
            return

        # These engine-specific checks only matter when an engine is
        # actually going to be used -- game_text_speaker.py's run() never
        # constructs a Speaker at all when --quiet is set (settings["engine"]
        # still holds the last real engine chosen underneath Quiet, purely
        # for persistence -- see _collect_settings), so skip them entirely
        # while Quiet is selected rather than blocking Start over a model
        # file quiet mode will never touch.
        if not settings["quiet"] and settings["engine"] == "piper" and not settings["piper_model"]:
            messagebox.showwarning(
                "Piper model required",
                "Select a Piper .onnx model file first, or switch to espeak-ng.",
            )
            return

        if not settings["quiet"] and settings["engine"] == "kokoro" and not (settings["kokoro_model"] and settings["kokoro_voices"]):
            messagebox.showwarning(
                "Kokoro model required",
                "Select both Kokoro's model (.onnx) and voices (.bin) files first, or switch engines.",
            )
            return

        if not settings["quiet"] and settings["engine"] == "windows" and not ENGINE_AVAILABILITY.get("windows"):
            # The Radiobutton for this is normally disabled when pyttsx3
            # isn't installed (see ENGINE_AVAILABILITY), but a persisted
            # gui_settings.json from before it was uninstalled -- or before
            # this was even Windows -- can still leave engine_var pointing
            # at "windows" underneath a disabled, still-"selected" radio.
            messagebox.showwarning(
                "Windows Native not available",
                "This engine needs Windows plus 'pip install pyttsx3' (dependency checks run once when "
                "this app starts, so if you just installed pyttsx3, restart it first). Switch to a "
                "different engine, or install pyttsx3 and restart.",
            )
            return

        piper_speaker = None
        if settings["piper_speaker"]:
            try:
                piper_speaker = int(settings["piper_speaker"])
            except ValueError:
                messagebox.showwarning(
                    "Invalid speaker ID",
                    f"Speaker ID {settings['piper_speaker']!r} isn't a number. "
                    "Leave it blank to default to speaker 0, or enter a whole number.",
                )
                return

        piper_length_scale = None
        if settings["piper_length_scale"]:
            try:
                piper_length_scale = float(settings["piper_length_scale"])
            except ValueError:
                messagebox.showwarning(
                    "Invalid speed",
                    f"Speed {settings['piper_length_scale']!r} isn't a number. "
                    "Leave it blank for normal speed, or enter a number (e.g. 0.5 for 2x faster, 2.0 for half speed).",
                )
                return

        kokoro_speed = None
        if settings["kokoro_speed"]:
            try:
                kokoro_speed = float(settings["kokoro_speed"])
            except ValueError:
                messagebox.showwarning(
                    "Invalid speed",
                    f"Speed {settings['kokoro_speed']!r} isn't a number. "
                    "Leave it blank for normal speed, or enter a number (e.g. 2.0 for 2x faster, 0.5 for half speed).",
                )
                return

        kokoro_cpu_threads = None
        if settings["kokoro_cpu_threads"]:
            try:
                kokoro_cpu_threads = int(settings["kokoro_cpu_threads"])
            except ValueError:
                messagebox.showwarning(
                    "Invalid CPU threads",
                    f"CPU threads {settings['kokoro_cpu_threads']!r} isn't a whole number. "
                    "Leave it blank to let onnxruntime pick its own default, or enter a whole number (e.g. 6).",
                )
                return

        args = argparse.Namespace(
            ignore_popups=settings["ignore_popups"],
            popup_threshold=settings["popup_threshold"],
            interval=settings["interval"],
            ocr_engine=settings["ocr_engine"],
            lang=settings["lang"],
            ocr_min_confidence=settings["ocr_min_confidence"],
            speaker_name_mode=settings["speaker_name_mode"],
            engine=settings["engine"],
            rate=settings["rate"],
            voice=settings["voice"],
            piper_model=settings["piper_model"],
            piper_speaker=piper_speaker,
            piper_length_scale=piper_length_scale,
            kokoro_model=settings["kokoro_model"],
            kokoro_voices=settings["kokoro_voices"],
            kokoro_voice=settings["kokoro_voice"] or "af_heart",
            kokoro_speed=kokoro_speed,
            kokoro_cpu_threads=kokoro_cpu_threads,
            kokoro_lang="en-us",
            cpu_affinity=settings["cpu_affinity"],
            quiet=settings["quiet"],
            similarity=settings["similarity"],
            pause_key=settings["pause_key"],
        )

        self.stop_event = threading.Event()
        self.status_label.config(text="Running…")
        self.stop_button.config(state="normal")
        self._set_actions_enabled(False)  # disables Start + selection buttons; Stop isn't in that list

        stop_event = self.stop_event

        def on_pause_change(paused):
            self.log_queue.put(("pause_state", paused))

        def worker():
            try:
                core.run(args, stop_event=stop_event, log=self.log, on_pause_change=on_pause_change)
            except (SystemExit, Exception) as e:
                self.log(f"Error: {e}")
            finally:
                self.log_queue.put(("run_finished", None))
                self.log_queue.put(("action_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _on_stop(self):
        if self.stop_event:
            self.stop_event.set()
        self.status_label.config(text="Stopping…")
        self.stop_button.config(state="disabled")

    def _on_close(self):
        if self.stop_event:
            self.stop_event.set()
        self._save_settings()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
