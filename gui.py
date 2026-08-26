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
import game_profile as gp

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


def _piper_model_speaker_count(model_path: str):
    """How many speakers the configured Piper model actually has, or None if
    that isn't knowable right now (no model chosen, or its sidecar file is
    missing/unreadable). Used only to hint a valid range next to the Cast
    panel's Speaker field ("0-903") when it's showing the model actually
    running -- Piper ships the count right alongside the model, in
    <model>.onnx.json's "num_speakers", so reading that sidecar file is
    enough to know it without loading the (possibly large) .onnx model
    itself just to ask."""
    if not model_path:
        return None
    try:
        cfg = json.loads(Path(f"{model_path}.json").read_text(encoding="utf-8"))
        return max(1, int(cfg.get("num_speakers") or 1))
    except Exception:
        return None


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

# The old engine-wide "fallback" rate: with a profile always loaded and
# Narrator always present in Cast, that row IS the default now (see
# App._on_start()) -- this constant only ever matters for the extreme edge
# case of no profile and no per-character speed anywhere, so it isn't worth
# a Settings field of its own anymore.
DEFAULT_RATE = 175

DEFAULT_SETTINGS = {
    "engine": "windows" if sys.platform == "win32" else "espeak",
    "piper_model": "",
    "kokoro_model": "",
    "kokoro_voices": "",
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
    # Which game profile is active. The profile itself holds the region,
    # timing and cast -- see game_profile.py for why those live apart from
    # this file.
    "profile": "",
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


class CastWindow:
    """The cast list for the current game — a separate window on purpose.

    It's a LIVE OUTPUT of reading, not a form you fill in first. You cannot
    know an RPG's cast before you play it, so characters appear here as they
    first speak, and you pick a voice for each one once you've decided what
    they should sound like. Keeping it in its own resizable window means it
    can sit beside the game while you play, and a cast of twenty doesn't
    stretch the main window off the bottom of the screen.

    This is also where an engine's own configuration lives now -- Piper's
    and Kokoro's Model file(s), right above the per-character rows that use
    them. Speech (the main window) keeps only the engine choice itself
    (which one Start actually uses) and Kokoro's CPU-thread cap (a machine
    performance knob, not a voice); everything else that used to be a
    "default voice" fallback there is gone outright -- Narrator is always
    present in this list, so setting a voice on Narrator's own row IS the
    default now, for anyone nobody's given their own voice yet.

    Every field here is hand-curated, on purpose. Nothing is inferred from a
    class or auto-picked from a pool: there's no reliable way to guess that
    Piper speaker #472 sounds like a child, or that espeak-ng's "+f3" sounds
    like anyone in particular, so nothing tries to. The voice/speaker dropdown
    only ever offers what that model can actually produce; you choose exactly
    what you want, for exactly one engine (and, for Piper/Kokoro, one model)
    at a time, and that's what plays -- see
    game_profile.Cast.get_model()/set_model()."""

    ROW_BG_NEW = "#fff4c2"  # a just-introduced character, so they're easy to spot

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("Cast")
        self.win.geometry("760x480")
        self.win.minsize(640, 280)
        self._rows = []

        header = ttk.Frame(self.win, padding=(10, 8, 10, 4))
        header.pack(fill="x")
        self.title_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.title_var, font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Button(header, text="Refresh", command=self.refresh).pack(side="right")

        # Which engine's cast/config this window is showing -- independent of
        # whichever one Speech has selected to actually run, so you can set
        # up (or just double check) any engine's characters ahead of time.
        # Starts on whatever's currently active, since that's the one you
        # most likely came here to work on.
        engine_row = ttk.Frame(self.win, padding=(10, 0, 10, 4))
        engine_row.pack(fill="x")
        ttk.Label(engine_row, text="Engine:").pack(side="left")
        self.engine_var = tk.StringVar(value=self._initial_engine())
        self.engine_radios = {}
        for name in ("espeak", "piper", "kokoro", "windows"):
            if not ENGINE_AVAILABILITY.get(name):
                continue
            rb = ttk.Radiobutton(engine_row, text=ENGINE_LABELS[name], value=name,
                                  variable=self.engine_var, command=self.refresh)
            rb.pack(side="left", padx=(6, 0))
            self.engine_radios[name] = rb

        # Rebuilt per engine by refresh(): Piper/Kokoro's Model (and, for
        # Kokoro, Voices) file fields -- see _rebuild_model_area(). espeak-ng
        # and Windows Native have no model file, so this stays empty for them.
        self.model_area = ttk.Frame(self.win, padding=(10, 0, 10, 0))
        self.model_area.pack(fill="x")

        self.hint_var = tk.StringVar(value="")
        ttk.Label(
            self.win, padding=(10, 0, 10, 6), justify="left", wraplength=520,
            textvariable=self.hint_var,
        ).pack(fill="x")

        # Canvas + inner frame: the cast list has to scroll, since it grows
        # for as long as you keep playing.
        outer = ttk.Frame(self.win, padding=(10, 0, 10, 10))
        outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas)
        self.body.bind("<Configure>",
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._window_id = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._window_id, width=e.width))
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.win.protocol("WM_DELETE_WINDOW", self.close)

    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def close(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.app.cast_window = None

    def _initial_engine(self):
        candidate = self.app.settings.get("engine") or self.app._last_engine
        if ENGINE_AVAILABILITY.get(candidate):
            return candidate
        for name in ("espeak", "piper", "kokoro", "windows"):
            if ENGINE_AVAILABILITY.get(name):
                return name
        return "espeak"

    def _rebuild_model_area(self, engine):
        for child in self.model_area.winfo_children():
            child.destroy()
        if engine == "piper":
            row = ttk.Frame(self.model_area)
            row.pack(fill="x")
            ttk.Label(row, text="Model:").pack(side="left")
            ttk.Label(row, textvariable=self.app.piper_model_display_var).pack(side="left", padx=(4, 8))
            ttk.Button(row, text="Browse…", command=self._on_browse_piper_model).pack(side="left")
        elif engine == "kokoro":
            row1 = ttk.Frame(self.model_area)
            row1.pack(fill="x")
            ttk.Label(row1, text="Model:").pack(side="left")
            ttk.Label(row1, textvariable=self.app.kokoro_model_display_var).pack(side="left", padx=(4, 8))
            ttk.Button(row1, text="Browse…", command=self._on_browse_kokoro_model).pack(side="left")
            row2 = ttk.Frame(self.model_area)
            row2.pack(fill="x", pady=(4, 0))
            ttk.Label(row2, text="Voices:").pack(side="left")
            ttk.Label(row2, textvariable=self.app.kokoro_voices_display_var).pack(side="left", padx=(4, 8))
            ttk.Button(row2, text="Browse…", command=self._on_browse_kokoro_voices).pack(side="left")
        # espeak-ng and Windows Native have no model file -- nothing to show.

    def _on_browse_piper_model(self):
        self.app._on_browse_piper_model()
        self.app._save_settings()
        self.refresh()

    def _on_browse_kokoro_model(self):
        self.app._on_browse_kokoro_model()
        self.app._save_settings()
        self.refresh()

    def _on_browse_kokoro_voices(self):
        self.app._on_browse_kokoro_voices()
        self.app._save_settings()
        self.refresh()

    def refresh(self, highlight=None):
        if not self.alive():
            return
        profile = self.app.profile
        for child in self.body.winfo_children():
            child.destroy()
        self._rows = []
        if profile is None:
            ttk.Label(self.body, text="No profile loaded.").pack(anchor="w")
            return

        self.title_var.set(profile.name)

        engine = self.engine_var.get()
        self._rebuild_model_area(engine)
        key = gp.model_key(
            engine,
            piper_model=self.app.piper_model_var.get().strip() or None,
            kokoro_model=self.app.kokoro_model_var.get().strip() or None,
        )

        is_active = engine == (self.app.settings.get("engine") or self.app._last_engine)
        if engine in ("piper", "kokoro") and not self.app.piper_model_var.get().strip() and engine == "piper":
            hint = "Pick a Piper .onnx model above, then a voice (speaker number) for each character."
        elif engine == "kokoro" and not (self.app.kokoro_model_var.get().strip() and self.app.kokoro_voices_var.get().strip()):
            hint = "Pick Kokoro's model and voices files above, then a voice for each character."
        else:
            hint = ("Pick a voice (and, for Piper, a speaker number) for each character. Leave a "
                    "field at \"(system default)\" to keep sounding like this model's own default "
                    "until you decide otherwise. Test always works here, whether or not the reader "
                    "is running.")
            if not is_active:
                hint += " Speech is set to a different engine right now, so this isn't what Start will use yet."
        self.hint_var.set(hint)

        head = ttk.Frame(self.body)
        head.pack(fill="x", pady=(0, 4))
        ttk.Label(head, text="Character", width=22).pack(side="left")
        if engine == "piper":
            count = _piper_model_speaker_count(self.app.piper_model_var.get().strip())
            label = f"Speaker (0–{count - 1})" if count else "Speaker"
            ttk.Label(head, text=label, width=16).pack(side="left")
        else:
            ttk.Label(head, text="Voice", width=16).pack(side="left")
        ttk.Label(head, text="Speed", width=8).pack(side="left")

        for entry in profile.cast.entries:
            self._add_row(entry, key, engine, highlight)

        if len(profile.cast.entries) <= 1:
            ttk.Label(self.body, padding=(0, 10), justify="left", wraplength=460,
                      text=("Nobody met yet. Start the reader and play — characters land "
                            "here the first time they speak.")).pack(anchor="w")

    def _add_row(self, entry, key, engine, highlight=None):
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=1)

        label = entry["name"]
        if entry.get("provisional"):
            # Seen once. It gets a voice already, but isn't written to the
            # profile until it turns up again -- that's what keeps a one-off
            # OCR misread out of the file permanently.
            label += "  (unconfirmed)"
        name_label = tk.Label(row, text=label, width=24, anchor="w")
        if highlight and entry["name"] == highlight:
            name_label.config(bg=self.ROW_BG_NEW)
        name_label.pack(side="left")

        cast = self.app.profile.cast
        cfg = cast.get_model(entry["name"], key)
        primary_field = "speaker" if engine == "piper" else "voice"

        def field_text(c, f):
            v = c.get(f)
            return "" if v is None else str(v)

        # The voice/speaker field is a dropdown, not free text -- there's a
        # real, queryable list of what each engine actually has (or, for
        # Piper, a numeric range from the model's own sidecar file), so
        # offering exactly those options rules out a typo that would only
        # surface as a runtime error later. Speed stays free text below: it's
        # a number the user picks by ear, not something to enumerate.
        if engine == "espeak":
            primary_values = [EMPTY_VOICE_LABEL] + _get_espeak_voices()
            primary_state = "readonly"
        elif engine == "windows":
            primary_values = [EMPTY_VOICE_LABEL] + _get_windows_native_voices()
            primary_state = "readonly"
        elif engine == "kokoro":
            primary_values = [EMPTY_VOICE_LABEL] + _get_kokoro_voices(self.app.settings.get("kokoro_voices"))
            primary_state = "readonly"
        else:  # piper -- a speaker number, not a named voice
            count = _piper_model_speaker_count(self.app.piper_model_var.get().strip())
            if count:
                primary_values = [EMPTY_VOICE_LABEL] + [str(i) for i in range(count)]
                primary_state = "readonly"
            else:
                # Speaker count isn't knowable right now (no Piper model
                # chosen above yet, or its sidecar .json is missing) --
                # fall back to a freely-typed number rather than a dropdown
                # with nothing real to offer.
                primary_values = []
                primary_state = "normal"

        current_primary = field_text(cfg, primary_field)
        if not current_primary and primary_state == "readonly":
            current_primary = EMPTY_VOICE_LABEL
        primary_var = tk.StringVar(value=current_primary)
        primary_entry = ttk.Combobox(row, textvariable=primary_var, values=primary_values,
                                      width=14, state=primary_state)
        primary_entry.pack(side="left", padx=(0, 6))

        speed_var = tk.StringVar(value=field_text(cfg, "speed"))
        speed_entry = ttk.Entry(row, textvariable=speed_var, width=8)
        speed_entry.pack(side="left", padx=(0, 6))

        def commit(_evt=None, e=entry, pv=primary_var, sv=speed_var):
            profile = self.app.profile
            if profile is None:
                return
            current = profile.cast.get_model(e["name"], key)
            fields = {}

            raw = pv.get().strip()
            if raw == EMPTY_VOICE_LABEL:
                raw = ""
            if primary_field == "speaker":
                if raw == "":
                    fields["speaker"] = None
                else:
                    try:
                        fields["speaker"] = int(raw)
                    except ValueError:
                        messagebox.showerror("Invalid speaker", f'"{raw}" isn\'t a whole number.')
                        reverted = field_text(current, "speaker")
                        pv.set(reverted if reverted or primary_state == "normal" else EMPTY_VOICE_LABEL)
                        return
            else:
                fields["voice"] = raw or None

            raw_speed = sv.get().strip()
            if raw_speed == "":
                fields["speed"] = None
            else:
                try:
                    fields["speed"] = float(raw_speed)
                except ValueError:
                    messagebox.showerror("Invalid speed", f'"{raw_speed}" isn\'t a number.')
                    sv.set(field_text(current, "speed"))
                    return

            profile.cast.set_model(e["name"], key, **fields)
            profile.save_if_dirty()

        primary_entry.bind("<FocusOut>", commit)
        primary_entry.bind("<Return>", commit)
        speed_entry.bind("<FocusOut>", commit)
        speed_entry.bind("<Return>", commit)

        ttk.Button(row, text="Test", width=5,
                   command=lambda e=entry: self.app._speak_sample(e, key)).pack(side="left")

        # The Narrator is a reserved slot -- Cast.__init__ recreates it the
        # moment it's gone -- so removing it would only reappear, with
        # nothing assigned again. Simplest to just not offer it here.
        if entry["name"] != gp.NARRATOR:
            ttk.Button(row, text="Remove", width=7,
                       command=lambda e=entry: self._remove(e)).pack(side="left")

        self._rows.append((entry, primary_var, speed_var))

    def _remove(self, entry):
        """Delete a cast member the detectors got wrong -- a page number, a
        chapter heading, a mid-sentence OCR fragment that got confirmed
        before it could be filtered out. Confirms first: this can't be
        undone from here, and the name disappearing from the *next* line it
        actually speaks (if it's real) is the only way back."""
        profile = self.app.profile
        if profile is None:
            return
        label = entry["name"]
        if not messagebox.askyesno(
            "Remove character",
            f'Remove "{label}" from the cast?\n\n'
            "If this was a real character, they'll be re-added -- as a new, "
            "unconfirmed entry -- the next time they speak.",
        ):
            return
        if profile.cast.remove(label):
            profile.save_if_dirty()
            self.refresh()


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

        self.profile = None
        self.cast_window = None
        self._live_speaker = None   # set while running, for the reader's own speech
        self._preview_speakers = {}  # _preview_key(engine) -> Speaker, for Cast's Test button (see _speak_sample)
        self._preview_loading = set()  # _preview_key(engine) values currently being built, to dedupe clicks

        self._build_ui()
        self._load_active_profile()
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
            "piper_model": self.piper_model_var.get().strip(),
            "kokoro_model": self.kokoro_model_var.get().strip(),
            "kokoro_voices": self.kokoro_voices_var.get().strip(),
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

        # Everything that's specific to one GAME -- the region, the timing,
        # how it marks who's speaking, and the cast -- belongs to the profile
        # picked here, so switching games is one dropdown instead of
        # re-dragging the box and re-tuning everything. See game_profile.py.
        game_frame = ttk.LabelFrame(self.root, text="Game")
        game_frame.pack(fill="x", **pad)
        game_row = ttk.Frame(game_frame)
        game_row.pack(fill="x", padx=6, pady=6)
        ttk.Label(game_row, text="Profile:").pack(side="left")
        self.profile_var = tk.StringVar(value="")
        self.profile_combo = ttk.Combobox(game_row, textvariable=self.profile_var,
                                          values=[], width=24, state="readonly")
        self.profile_combo.pack(side="left", padx=(4, 6))
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)
        self._add_info_button(
            game_row,
            "A profile holds everything specific to one game: the dialogue region, the "
            "polling/similarity settings, how that game marks who's speaking, and the "
            "cast of characters it has met.\n\n"
            "Profiles live in the 'profiles' folder, one .json each, and are meant to be "
            "shared — they deliberately contain no file paths and no voice names, only "
            "'this character is male #0'. Your own settings say what male #0 sounds like "
            "on the engine you run, so a profile still works for someone using a "
            "different engine entirely.",
            title="Game profiles", side="right")
        ttk.Button(game_row, text="Cast…", command=self._open_cast_window).pack(side="right", padx=(0, 4))
        ttk.Button(game_row, text="New…", command=self._on_new_profile).pack(side="right", padx=(0, 4))

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

        # espeak-ng and Windows Native have no engine-wide Rate/Voice fields
        # here anymore, and Piper/Kokoro have no Model fields here either --
        # all of that moved to the Cast window (see CastWindow), where it
        # sits right next to the per-character voices it used to just be a
        # fallback for. Narrator is always present in Cast, so setting a
        # voice there IS the default now; a second "default voice" control
        # here would just be a second place to lose track of. This tab's
        # only remaining job for a real engine is picking WHICH one runs --
        # see _update_engine_widgets(). Kokoro's CPU-thread cap stays here
        # deliberately: it's a performance knob for the machine, not a
        # voice, and has nothing to do with any particular character.
        #
        # Model paths are still owned by App (not CastWindow) since
        # _collect_settings()/_on_start() need them whether or not the Cast
        # window has ever been opened this session -- these StringVars are
        # exactly what CastWindow's Model/Browse widgets read and write.
        self.piper_model_var = tk.StringVar(value=self.settings["piper_model"])
        self.piper_model_display_var = tk.StringVar(value=_basename_or_placeholder(self.settings["piper_model"]))
        self.kokoro_model_var = tk.StringVar(value=self.settings["kokoro_model"])
        self.kokoro_model_display_var = tk.StringVar(value=_basename_or_placeholder(self.settings["kokoro_model"]))
        self.kokoro_voices_var = tk.StringVar(value=self.settings["kokoro_voices"])
        self.kokoro_voices_display_var = tk.StringVar(value=_basename_or_placeholder(self.settings["kokoro_voices"]))

        self.kokoro_frame = ttk.Frame(speech_frame)
        kokoro_row4 = ttk.Frame(self.kokoro_frame)
        kokoro_row4.pack(fill="x")
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
        # Kokoro's CPU-thread cap is the only engine-specific control left on
        # this tab (see _build_ui) -- everything else moved to Cast.
        if self.engine_var.get() == "kokoro":
            self.kokoro_frame.pack(fill="x", padx=6, pady=(0, 6))
        else:
            self.kokoro_frame.pack_forget()

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
        """Purely display. Shows the ACTIVE PROFILE's region rather than
        region.json's, because that's what the reader will actually watch.

        Deliberately does NOT copy region.json into the profile -- that file
        holds whatever was selected last, which belongs to whichever game was
        open at the time. Adopting it here meant switching to a brand-new
        profile silently inherited the previous game's box. Adoption happens
        only in _adopt_selected_region(), on the paths where the user has just
        actually dragged one."""
        if self.profile is not None:
            region = self.profile.get("region")
            if region:
                self.region_status_label.config(
                    text=f"Region set: {region['x']},{region['y']}  {region['w']}x{region['h']}")
            else:
                self.region_status_label.config(text="Region: not set for this profile yet")
            return
        self.region_status_label.config(text=_region_status_text())

    def _adopt_selected_region(self):
        """Called after the user picks a region. The selection UI writes
        region.json (that path is shared with the CLI); this is where it
        becomes THIS game's region instead of a global one."""
        if self.profile is not None:
            try:
                if core.CONFIG_PATH.exists():
                    picked = json.loads(core.CONFIG_PATH.read_text())
                    if picked:
                        self.profile.set("region", picked)
                        self.profile.save_if_dirty()
            except Exception as e:
                self.log(f"[profile] Couldn't save the region to this profile: {e}")
        self._refresh_region_status()

    def _refresh_popup_status(self):
        self.popup_status_label.config(text=_popup_status_text())

    def _adopt_selected_popup_marker(self):
        if self.profile is not None:
            try:
                if core.POPUP_MARKER_PATH.exists():
                    picked = json.loads(core.POPUP_MARKER_PATH.read_text())
                    if picked:
                        self.profile.set("popup_marker", picked)
                        self.profile.save_if_dirty()
            except Exception as e:
                self.log(f"[profile] Couldn't save the popup marker to this profile: {e}")
        self._refresh_popup_status()

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
                elif kind == "new_speaker":
                    self._on_new_speaker_ui(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    def _set_actions_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for b in self._action_buttons:
            b.config(state=state)

    # ---------------- game profiles ----------------

    def _load_active_profile(self):
        """Pick up the profile named in settings, or make one.

        First run after this feature landed there won't be any profiles, but
        there very likely IS a region.json and maybe a popup_marker.json left
        from before. Those were always game-specific settings that simply had
        nowhere game-specific to live, so they get folded into a starter
        profile rather than abandoned."""
        gp.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        wanted = (self.settings.get("profile") or "").strip()
        if wanted:
            path = Path(wanted)
            if not path.is_absolute():
                path = gp.PROFILE_DIR / path
            if path.exists():
                try:
                    self.profile = gp.load_profile(path)
                    self._refresh_profile_list()
                    return
                except Exception as e:
                    self.log(f"[profile] Couldn't read {path.name}: {e}")

        existing = gp.list_profiles()
        if existing:
            try:
                self.profile = gp.load_profile(existing[0][1])
            except Exception as e:
                self.log(f"[profile] Couldn't read {existing[0][1].name}: {e}")
                self.profile = None
        else:
            region = popup = None
            try:
                if core.CONFIG_PATH.exists():
                    region = json.loads(core.CONFIG_PATH.read_text())
                if core.POPUP_MARKER_PATH.exists():
                    popup = json.loads(core.POPUP_MARKER_PATH.read_text())
            except Exception:
                pass
            try:
                self.profile = gp.migrate_legacy(self.settings, region=region,
                                                 popup_marker=popup, name="My Game")
                if region or popup:
                    self.log("[profile] Moved your existing region/marker settings into "
                             f"profiles/{self.profile.path.name} — they're per-game now.")
            except OSError as e:
                self.log(f"[profile] Couldn't create a starter profile: {e}")
                self.profile = None
        if self.profile is not None:
            self.settings["profile"] = self.profile.path.name
        self._refresh_profile_list()

    def _refresh_profile_list(self):
        if not hasattr(self, "profile_combo"):
            return
        self._profile_paths = {name: path for name, path in gp.list_profiles()}
        self.profile_combo["values"] = list(self._profile_paths)
        if self.profile is not None:
            self.profile_var.set(self.profile.name)

    def _on_profile_selected(self, _event=None):
        path = getattr(self, "_profile_paths", {}).get(self.profile_var.get())
        if path is None or (self.profile is not None and self.profile.path == path):
            return
        try:
            self.profile = gp.load_profile(path)
        except Exception as e:
            messagebox.showerror("Couldn't open profile", str(e))
            return
        self.settings["profile"] = self.profile.path.name
        self._save_settings()
        self.log(f"[profile] Switched to '{self.profile.name}'.")
        self._refresh_region_status()
        if self.cast_window is not None and self.cast_window.alive():
            self.cast_window.refresh()

    def _on_new_profile(self):
        from tkinter import simpledialog

        name = simpledialog.askstring("New game profile", "Name this game:", parent=self.root)
        if not name or not name.strip():
            return
        # The one real decision at creation time: should the reader try to
        # tell characters apart at all? "Yes" turns on the pattern/margin
        # detectors (the normal case -- new characters show up in Cast as
        # they speak, ready for their own voice). "No" leaves the profile
        # with no detectors, so every line is credited to the Narrator --
        # no per-character voices, no cast list to curate, just one voice
        # throughout. Nothing else about a fresh profile needs deciding: an
        # unassigned character (or the Narrator, until you set one) just
        # sounds like whatever the current engine's Speech settings say.
        auto_detect = messagebox.askyesno(
            "Auto-detect characters?",
            "Should new characters be detected automatically as they speak, "
            "so each one can get their own voice in Cast?\n\n"
            "Choose No for a game where everything should just be read in "
            "the Narrator's voice.",
            parent=self.root,
        )
        try:
            overrides = {} if auto_detect else {"detectors": []}
            self.profile = gp.create_profile(name.strip(), **overrides)
        except OSError as e:
            messagebox.showerror("Couldn't create profile", str(e))
            return
        self.settings["profile"] = self.profile.path.name
        self._save_settings()
        self._refresh_profile_list()
        detect_note = "" if auto_detect else " (no character auto-detection -- everything reads as Narrator)"
        self.log(f"[profile] Created '{self.profile.name}' (profiles/{self.profile.path.name}){detect_note}. "
                 f"Pick a region for it next.")
        self._refresh_region_status()

    def _on_new_speaker_ui(self, name):
        """A character has just spoken for the first time. Deliberately
        interrupts nothing -- they've already said that line in whatever this
        model's own default sounds like by the time this runs. Raising the
        Cast panel IS the prompt; answering it is optional and can wait."""
        self.log(f"[cast] New speaker: {name} — pick a voice for them in the Cast panel.")
        self._open_cast_window(highlight=name)

    def _open_cast_window(self, highlight=None):
        if self.profile is None:
            messagebox.showinfo("No profile", "Create or choose a game profile first.")
            return
        if self.cast_window is None or not self.cast_window.alive():
            self.cast_window = CastWindow(self)
        self.cast_window.refresh(highlight=highlight)

    def _preview_key(self, engine):
        """Identifies a specific, loadable speech configuration for
        previewing -- not just the engine name, since Piper/Kokoro need to
        know exactly which files back them. Used to cache built Speaker
        instances across Test clicks (see _speak_sample) and, implicitly, to
        invalidate that cache the moment Browse points an engine at a
        different file: a changed path is just a different key."""
        if engine == "piper":
            return ("piper", self.piper_model_var.get().strip())
        if engine == "kokoro":
            return ("kokoro", self.kokoro_model_var.get().strip(),
                     self.kokoro_voices_var.get().strip(), self.kokoro_cpu_threads_var.get().strip())
        return (engine,)

    def _speak_sample(self, entry, key):
        """Audition one character's configured voice -- independent of
        whether the reader is actually running. Deliberately does NOT reuse
        self._live_speaker: the reader's own Speaker cuts off whatever it's
        mid-saying the moment new dialogue arrives (see Speaker._utterance_id
        in game_text_speaker.py), so sharing it here would let a manual Test
        click silently swallow a real line, or a real line cut off a Test
        click -- worth a second loaded copy of the model to avoid. Instead,
        builds (once per engine+model, then caches) a Speaker used only for
        previews; Piper/Kokoro's first Test click for a given model pays the
        model's real load time (a moment or two), every click after that is
        instant. `key` is the model bucket to read this character's voice
        from -- see CastWindow.refresh()."""
        if self.profile is None:
            return
        engine = key.split(":", 1)[0]
        cfg = self.profile.cast.get_model(entry["name"], key)
        sample = f"{entry['name']}. This is how this character will sound."

        pkey = self._preview_key(engine)
        speaker = self._preview_speakers.get(pkey)
        if speaker is not None:
            threading.Thread(target=lambda: speaker.say(
                sample, voice=cfg.get("voice"), speaker_id=cfg.get("speaker"), speed=cfg.get("speed"),
            ), daemon=True).start()
            return

        if pkey in self._preview_loading:
            return  # already loading this exact engine+model -- don't pile up duplicate loads
        self._preview_loading.add(pkey)
        if engine in ("piper", "kokoro"):
            self.log(f"[cast] Loading {ENGINE_LABELS.get(engine, engine)} for preview…")

        # Read every Tk variable the build needs HERE, on the main thread --
        # Tkinter variables aren't safe to touch from a background thread
        # (modern Tcl/Tk raises "main thread is not in main loop" if you
        # try), so the thread below gets plain, already-extracted values,
        # exactly the same way _on_start() hands its worker thread a plain
        # argparse.Namespace rather than the widgets themselves.
        piper_model = self.piper_model_var.get().strip() or None
        kokoro_model = self.kokoro_model_var.get().strip() or None
        kokoro_voices = self.kokoro_voices_var.get().strip() or None
        kokoro_cpu_threads = self._as_int(self.kokoro_cpu_threads_var.get(), None)

        def build_and_speak():
            try:
                built = core.Speaker(
                    engine=engine, rate=DEFAULT_RATE, voice="",
                    piper_model=piper_model, piper_speaker=None, piper_length_scale=None,
                    kokoro_model=kokoro_model, kokoro_voices=kokoro_voices,
                    kokoro_voice=None, kokoro_speed=None,
                    kokoro_cpu_threads=kokoro_cpu_threads,
                    log=self.log,
                )
            except (SystemExit, Exception) as e:
                self.log(f"[cast] Couldn't load {ENGINE_LABELS.get(engine, engine)} for preview: {e}")
                self._preview_loading.discard(pkey)
                return
            self._preview_speakers[pkey] = built
            self._preview_loading.discard(pkey)
            built.say(sample, voice=cfg.get("voice"), speaker_id=cfg.get("speaker"), speed=cfg.get("speed"))

        threading.Thread(target=build_and_speak, daemon=True).start()

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
                self.log_queue.put(("action_done", self._adopt_selected_region))

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
        self._adopt_selected_region()

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
                self.log_queue.put(("action_done", self._adopt_selected_popup_marker))

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
            # Each per-character Voice dropdown in Cast re-queries this file
            # itself on every refresh() (see CastWindow._add_row), so there's
            # no separate dropdown widget here to keep in sync anymore.

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

        # No more engine-wide Speaker ID / Speed / Voice fields to validate
        # here -- Piper's speaker and every engine's speed now come purely
        # from whichever character is talking (Cast), with Narrator standing
        # in as the default for anyone else. Passing None for all of them
        # lets each engine fall back to its own built-in default (Piper: no
        # speaker override; Kokoro: "af_heart" / 1.0x, both already built
        # into Speaker.__init__) whenever a line's cast entry doesn't say
        # otherwise -- see game_text_speaker.py's Speaker class.
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

        # Timing and similarity are per-game too -- a fast visual novel and a
        # slow tactics game want different poll intervals -- so the profile
        # wins over the global setting whenever one is loaded.
        prof = self.profile
        def from_profile(key, fallback):
            return prof.get(key, fallback) if prof is not None else fallback

        args = argparse.Namespace(
            ignore_popups=settings["ignore_popups"],
            popup_threshold=from_profile("popup_threshold", settings["popup_threshold"]),
            interval=from_profile("interval", settings["interval"]),
            ocr_engine=settings["ocr_engine"],
            lang=settings["lang"],
            ocr_min_confidence=settings["ocr_min_confidence"],
            speaker_name_mode=settings["speaker_name_mode"],
            engine=settings["engine"],
            rate=DEFAULT_RATE,
            voice="",
            piper_model=settings["piper_model"],
            piper_speaker=None,
            piper_length_scale=None,
            kokoro_model=settings["kokoro_model"],
            kokoro_voices=settings["kokoro_voices"],
            kokoro_voice=None,
            kokoro_speed=None,
            kokoro_cpu_threads=kokoro_cpu_threads,
            kokoro_lang="en-us",
            cpu_affinity=settings["cpu_affinity"],
            quiet=settings["quiet"],
            similarity=from_profile("similarity", settings["similarity"]),
            pause_key=settings["pause_key"],
        )

        self.stop_event = threading.Event()
        self.status_label.config(text="Running…")
        self.stop_button.config(state="normal")
        self._set_actions_enabled(False)  # disables Start + selection buttons; Stop isn't in that list

        stop_event = self.stop_event

        def on_pause_change(paused):
            self.log_queue.put(("pause_state", paused))

        profile = self.profile

        # Both of these are called from the reader's own thread, so they only
        # hand work to the Tk thread through the queue rather than touching
        # widgets directly -- Tkinter is not thread-safe, and a cast panel
        # built from the wrong thread is a crash waiting for a busy scene.
        def on_new_speaker(entry, line):
            self.log_queue.put(("new_speaker", entry["name"]))

        def on_speaker_ready(speaker):
            self._live_speaker = speaker

        def worker():
            try:
                core.run(args, stop_event=stop_event, log=self.log,
                         on_pause_change=on_pause_change, profile=profile,
                         on_new_speaker=on_new_speaker,
                         on_speaker_ready=on_speaker_ready)
            except (SystemExit, Exception) as e:
                self.log(f"Error: {e}")
            finally:
                self._live_speaker = None
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
