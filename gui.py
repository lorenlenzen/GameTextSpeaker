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
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, scrolledtext
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

# Small black circle-i icon for _add_info_button(), embedded as base64 PNG
# rather than drawn from a Unicode glyph (both the earlier "u2139" and the
# "u24d8" it replaced rendered inconsistently -- thin, tiny, or a fallback
# box -- depending on what the running system's default font happened to
# ship for that codepoint). Tk has no real SVG support even in 8.6, so this
# is the practical equivalent: a tiny raster, built once with Pillow (a
# 16x16 circle+"i" drawn at 16x scale and downsampled with a BOX/area-
# average filter -- not LANCZOS, which rings/overshoots at a hard edge and
# was showing up as a faint halo outside the circle) and embedded here so
# there's no extra asset file to ship or go missing -- it looks identical
# on every platform this app runs on, unlike a font glyph.
_INFO_ICON_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAA30lEQVR4nK2TTaqDMBSFvz6ciBCc"
    "S8alZC/lIZ25yeIanHRm4U7cQncQKLmdaIkSpX8HMkhyzpfc/MCX2iXGMuAfOAL7cWwAWuAM3LeA"
    "B6AHFFDnnDrndOqPc4et8G0yW2vVe6/ee7XWxpBbCpLFKwNqjFERURFRY0wMmHaSxYDTwvBKOwH8"
    "jYBjqqY8zymKYq3kWeaSWqXrOhWRtR1cptpXVVUVZVluWZ4lDJuutIYY0H4AaGPAGbguHSEEQgip"
    "8HXMzDR7SIDWda1N0ywPL/mQYkjP+t33y/DPP9PbegBlpnLplRx2xQAAAABJRU5ErkJggg=="
)

_info_icon_photo = None


def _info_icon() -> "tk.PhotoImage":
    """Lazily creates and caches the info-icon PhotoImage. Cached at module
    level (not per-call) because Tk garbage-collects a PhotoImage the
    instant nothing references it, which would otherwise blank a button's
    icon out from under it the next time Tk redraws."""
    global _info_icon_photo
    if _info_icon_photo is None:
        _info_icon_photo = tk.PhotoImage(data=_INFO_ICON_PNG_B64, format="png")
    return _info_icon_photo


def _show_dialog(parent, title: str, message: str, kind: str = "info", extra=None):
    """Stand-in for tkinter.messagebox's showinfo/showerror/showwarning --
    same one-button, modal, blocks-until-dismissed shape, but placed next
    to wherever the mouse actually was when it popped up instead of
    wherever Tk (or the window manager) feels like putting a bare
    messagebox with no `parent=` -- which is what every call site in this
    file used to be, since a plain tk_messageBox has no notion of "near
    the button that opened it" at all. With several independent windows
    open at once (main window, Cast, Transcript), centering on the wrong
    one is exactly what read as "popping up in unexpected places."

    No icon is drawn here -- there used to be one (a Unicode glyph picked
    by `kind`), but that carries the same font-rendering-quality risk that
    turned out badly for the small info-button icon (see _info_icon()):
    plain text, no image asset, so it's at the mercy of whatever glyph
    coverage happens to be installed. `kind` is kept as a parameter anyway
    since show_info/show_warning/show_error already pass it, and it's a
    reasonable hook if this ever wants a real per-kind icon (an embedded
    PNG, the same way _info_icon() works) instead of none at all.

    `extra`, if given, is an (label, command) pair for one additional
    button shown to the left of OK, invoked, then this dialog closes. Not
    currently used by any call site, but kept as a general capability.

    `parent` only has to be some live widget in the window that triggered
    this -- winfo_toplevel() finds the actual window from there, and
    winfo_pointerx/y() finds the mouse regardless of which screen or
    window it's over. transient() keeps this grouped with that window on
    most window managers; "-topmost" is what actually guarantees it can't
    end up buried behind it on the ones where transient() alone doesn't."""
    toplevel = parent.winfo_toplevel()
    win = tk.Toplevel(toplevel)
    win.title(title)
    win.resizable(False, False)
    win.transient(toplevel)

    body = ttk.Frame(win, padding=16)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=message, justify="left", wraplength=360).pack(fill="both", expand=True)

    btn_row = ttk.Frame(body)
    btn_row.pack(fill="x", pady=(14, 0))
    ok = ttk.Button(btn_row, text="OK", width=8, command=win.destroy)
    ok.pack(side="right")
    if extra is not None:
        extra_label, extra_command = extra

        def _run_extra(cmd=extra_command):
            win.destroy()
            cmd()

        ttk.Button(btn_row, text=extra_label, command=_run_extra).pack(side="right", padx=(0, 6))
    win.bind("<Return>", lambda _e: win.destroy())
    win.bind("<Escape>", lambda _e: win.destroy())
    win.protocol("WM_DELETE_WINDOW", win.destroy)

    # Sized to its own content first (update_idletasks forces the geometry
    # manager to run without waiting for the event loop), then placed near
    # the pointer and clamped so a click near a screen edge can't push it
    # partly off-screen.
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    x = min(max(toplevel.winfo_pointerx() + 12, 0), max(sw - w, 0))
    y = min(max(toplevel.winfo_pointery() + 12, 0), max(sh - h, 0))
    win.geometry(f"{w}x{h}+{x}+{y}")

    win.attributes("-topmost", True)
    win.grab_set()
    ok.focus_set()
    win.wait_window()


def show_info(parent, title: str, message: str, extra=None):
    _show_dialog(parent, title, message, kind="info", extra=extra)


def show_warning(parent, title: str, message: str):
    _show_dialog(parent, title, message, kind="warning")


def show_error(parent, title: str, message: str):
    _show_dialog(parent, title, message, kind="error")


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

# Shown in the Speech engine ⓘ info button (see App._engine_info_text()) --
# the radios themselves show just the short name now (ENGINE_LABELS), same
# horizontal layout as the Cast window's own engine picker.
ENGINE_DESCRIPTIONS = {
    "windows": "Built-in, zero setup.",
    "espeak": "Robotic, needs espeak-ng installed separately.",
    "piper": "Natural, needs a model.",
    "kokoro": "Most natural, bigger download.",
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

# Shown in the OCR engine ⓘ info button (see App._ocr_engine_info_text()).
OCR_ENGINE_DESCRIPTIONS = {
    "windows": "Built-in, zero setup.",
    "tesseract": "Needs the tesseract-ocr binary installed separately.",
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
        # Passing app.root as the master above only makes it this window's
        # LOGICAL Tk parent -- it does NOT set the WM_TRANSIENT_FOR hint a
        # window manager actually uses to treat this as owned by the main
        # window (grouped with it in the taskbar/switcher, raised and
        # minimized together, kept above it). transient() is what sets
        # that hint; see _show_dialog()'s own use of it for the same reason.
        self.win.transient(app.root)
        self.win.title("Cast")
        self.win.geometry("760x480")
        self.win.minsize(640, 280)
        self._rows = []

        header = ttk.Frame(self.win, padding=(10, 8, 10, 4))
        header.pack(fill="x")
        self.title_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.title_var, font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Button(header, text="Refresh", command=self.refresh).pack(side="right")

        # Defaults ON: this window's main reason to exist mid-playthrough is
        # assigning a voice to a character who just showed up, and a game
        # running borderless/windowed fullscreen (the mode this app's OCR
        # capture actually needs -- see README) will otherwise cover this
        # window right back up the moment you click into the game again.
        # "-topmost" keeps it visible above the game without needing a full
        # alt-tab each time; it has no effect over a TRUE exclusive-fullscreen
        # game, since nothing but that game is allowed to render at all then.
        #
        # Saved per-profile (see _on_keep_on_top_changed()/refresh() below),
        # since one game might run exclusive-fullscreen (nothing to cover)
        # while another runs borderless windowed right under this window --
        # not something everyone wants the same answer to every time. Stays
        # enabled and usable with no profile loaded too, though there's
        # nowhere to save the choice until one is.
        self.on_top_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(header, text="Keep on top", variable=self.on_top_var,
                         command=self._on_keep_on_top_changed).pack(side="right", padx=(0, 10))
        self._apply_on_top()

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

        # Also per-profile -- see game_profile.Cast.observe()'s `freeze`
        # parameter. Set from refresh() (below), not here: there's no
        # profile yet at __init__ time, and setting a BooleanVar doesn't
        # fire its own command, so that's a safe place to sync it without
        # triggering a spurious save.
        freeze_row = ttk.Frame(self.win, padding=(10, 0, 10, 4))
        freeze_row.pack(fill="x")
        self.freeze_var = tk.BooleanVar(value=False)
        self.freeze_check = ttk.Checkbutton(
            freeze_row, text="Freeze adding new cast members",
            variable=self.freeze_var, command=self._on_freeze_changed)
        self.freeze_check.pack(side="left")
        self.app._add_info_button(
            freeze_row,
            "When checked, a name nobody's met yet is never added to the cast -- that line just "
            "falls back to the default voice, the same as a line with no detected speaker at all. "
            "Characters already in the cast are unaffected; this only stops new arrivals.",
            title="Freeze adding new cast members", side="left")


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

    def _apply_on_top(self):
        try:
            self.win.attributes("-topmost", bool(self.on_top_var.get()))
        except tk.TclError:
            pass  # window already gone -- nothing to do

    def _on_keep_on_top_changed(self):
        self._apply_on_top()
        profile = self.app.profile
        if profile is None:
            return
        profile.set("keep_on_top", bool(self.on_top_var.get()))
        profile.save_if_dirty()

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
            ttk.Button(row, text="Browse", command=self._on_browse_piper_model).pack(side="left")
        elif engine == "kokoro":
            row1 = ttk.Frame(self.model_area)
            row1.pack(fill="x")
            ttk.Label(row1, text="Model:").pack(side="left")
            ttk.Label(row1, textvariable=self.app.kokoro_model_display_var).pack(side="left", padx=(4, 8))
            ttk.Button(row1, text="Browse", command=self._on_browse_kokoro_model).pack(side="left")
            row2 = ttk.Frame(self.model_area)
            row2.pack(fill="x", pady=(4, 0))
            ttk.Label(row2, text="Voices:").pack(side="left")
            ttk.Label(row2, textvariable=self.app.kokoro_voices_display_var).pack(side="left", padx=(4, 8))
            ttk.Button(row2, text="Browse", command=self._on_browse_kokoro_voices).pack(side="left")
        # espeak-ng and Windows Native have no model file -- nothing to show.

    def _on_browse_piper_model(self):
        self.app._on_browse_piper_model(parent=self.win)
        self.app._save_settings()
        self.refresh()

    def _on_browse_kokoro_model(self):
        self.app._on_browse_kokoro_model(parent=self.win)
        self.app._save_settings()
        self.refresh()

    def _on_browse_kokoro_voices(self):
        self.app._on_browse_kokoro_voices(parent=self.win)
        self.app._save_settings()
        self.refresh()

    def _on_freeze_changed(self):
        profile = self.app.profile
        if profile is None:
            return
        profile.set("freeze_cast", bool(self.freeze_var.get()))
        profile.save_if_dirty()

    def refresh(self, highlight=None):
        if not self.alive():
            return
        profile = self.app.profile
        for child in self.body.winfo_children():
            child.destroy()
        self._rows = []
        if profile is None:
            ttk.Label(self.body, text="No profile loaded.").pack(anchor="w")
            self.freeze_check.config(state="disabled")
            return

        self.title_var.set(profile.name)
        self.freeze_check.config(state="normal")
        self.freeze_var.set(bool(profile.get("freeze_cast", False)))
        self.on_top_var.set(bool(profile.get("keep_on_top", True)))
        self._apply_on_top()

        engine = self.engine_var.get()
        self._rebuild_model_area(engine)
        key = gp.model_key(
            engine,
            piper_model=self.app.piper_model_var.get().strip() or None,
            kokoro_voices=self.app.kokoro_voices_var.get().strip() or None,
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
                        show_error(self.win, "Invalid speaker", f'"{raw}" isn\'t a whole number.')
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
                    show_error(self.win, "Invalid speed", f'"{raw_speed}" isn\'t a number.')
                    sv.set(field_text(current, "speed"))
                    return

            profile.cast.set_model(e["name"], key, **fields)
            profile.save_if_dirty()

        primary_entry.bind("<FocusOut>", commit)
        primary_entry.bind("<Return>", commit)
        speed_entry.bind("<FocusOut>", commit)
        speed_entry.bind("<Return>", commit)

        ttk.Button(row, text="🔊", width=3, style="Compact.TButton",
                   command=lambda e=entry: self.app._speak_sample(e, key)).pack(side="left")

        # The Narrator is a reserved slot -- Cast.__init__ recreates it the
        # moment it's gone -- so removing it would only reappear, with
        # nothing assigned again. Simplest to just not offer it here.
        if entry["name"] != gp.NARRATOR:
            ttk.Button(row, text="✕", width=2, style="Compact.TButton",
                       command=lambda e=entry: self._remove(e)).pack(side="left", padx=(6, 0))

        self._rows.append((entry, primary_var, speed_var))

    def _remove(self, entry):
        """Delete a cast member detection got wrong -- a page number, a
        chapter heading, a mid-sentence OCR fragment that got confirmed
        before it could be filtered out. No confirmation prompt: this can't
        be undone from here, but it isn't destructive in any lasting way
        either -- if this was a real character, they're simply re-added, as
        a new unconfirmed entry, the next time they speak."""
        profile = self.app.profile
        if profile is None:
            return
        if profile.cast.remove(entry["name"]):
            profile.save_if_dirty()
            self.refresh()


class OcrCorrectionsWindow:
    """Editor for a profile's "ocr_corrections" -- fixes for an OCR glyph
    confusion specific to THIS game's own font (see
    game_profile.apply_ocr_corrections() and the module docstring's note
    on why this is a per-GAME concern). Deliberately a separate window
    from Cast: these rules apply to the raw OCR'd text before speaker
    detection even runs, so they're just as relevant with no cast (or no
    name region at all) configured -- they aren't casting, and living in
    a window titled "Cast" would say otherwise."""

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.transient(app.root)
        self.win.title("OCR Corrections")
        self.win.geometry("620x440")
        self.win.minsize(520, 320)
        self._rows = []  # list of (row_frame, pattern_var, replace_var)

        header = ttk.Frame(self.win, padding=(10, 8, 10, 4))
        header.pack(fill="x")
        self.title_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.title_var, font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.app._add_info_button(
            header,
            "Fixes for an OCR glyph confusion specific to THIS game's font -- e.g. an opening "
            "quote mark fused against a capital \"I\" that reads as a bare \"T\". Each rule is a "
            "regex matched against one whole OCR'd word at a time, not a substring search, so a "
            "fix aimed at one misread can't accidentally clip a letter out of an unrelated real "
            "word. These apply to the raw OCR text before speaker detection runs -- they aren't "
            "part of the cast, and apply even with no cast configured at all. Use \\1, \\2 etc. in "
            "Replace to refer back to a parenthesized group in Pattern.",
            title="OCR corrections", side="right")

        cols = ttk.Frame(self.win, padding=(10, 4, 10, 0))
        cols.pack(fill="x")
        ttk.Label(cols, text="Pattern (regex)", width=30).pack(side="left")
        ttk.Label(cols, text="Replace").pack(side="left", padx=(8, 0))

        outer = ttk.Frame(self.win, padding=(10, 0, 10, 4))
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

        add_row = ttk.Frame(self.win, padding=(10, 0, 10, 4))
        add_row.pack(fill="x")
        ttk.Button(add_row, text="+ Add rule", command=lambda: self._add_row()).pack(side="left")

        test_frame = ttk.LabelFrame(self.win, text="Try it", padding=(8, 6))
        test_frame.pack(fill="x", padx=10, pady=(0, 4))
        self.test_in_var = tk.StringVar(value="")
        ttk.Entry(test_frame, textvariable=self.test_in_var).pack(fill="x")
        self.test_in_var.trace_add("write", lambda *_: self._update_test())
        self.test_out_var = tk.StringVar(value="")
        ttk.Label(test_frame, textvariable=self.test_out_var, foreground="#555").pack(
            fill="x", anchor="w", pady=(4, 0))

        btn_row = ttk.Frame(self.win, padding=(10, 0, 10, 10))
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Save", command=self._on_save).pack(side="right")
        ttk.Button(btn_row, text="Close", command=self.close).pack(side="right", padx=(0, 6))

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
        self.app.ocr_corrections_window = None

    def _add_row(self, pattern="", replace=""):
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=2)
        pattern_var = tk.StringVar(value=pattern)
        replace_var = tk.StringVar(value=replace)
        ttk.Entry(row, textvariable=pattern_var, width=30).pack(side="left")
        ttk.Entry(row, textvariable=replace_var).pack(side="left", fill="x", expand=True, padx=(8, 0))
        pattern_var.trace_add("write", lambda *_: self._update_test())
        replace_var.trace_add("write", lambda *_: self._update_test())
        ttk.Button(row, text="✕", width=2, style="Compact.TButton",
                   command=lambda: self._remove_row(row)).pack(side="left", padx=(6, 0))
        self._rows.append((row, pattern_var, replace_var))

    def _remove_row(self, row):
        self._rows = [r for r in self._rows if r[0] is not row]
        row.destroy()
        self._update_test()

    def refresh(self):
        if not self.alive():
            return
        profile = self.app.profile
        for row, _, _ in self._rows:
            row.destroy()
        self._rows = []
        if profile is None:
            self.title_var.set("No profile loaded")
            return
        self.title_var.set(profile.name)
        for c in profile.get("ocr_corrections") or []:
            if isinstance(c, dict):
                self._add_row(c.get("pattern", ""), c.get("replace", ""))
        self._update_test()

    def _current_corrections(self):
        out = []
        for _, pattern_var, replace_var in self._rows:
            pattern, replace = pattern_var.get(), replace_var.get()
            if pattern.strip() or replace.strip():
                out.append({"pattern": pattern, "replace": replace})
        return out

    def _update_test(self):
        sample = self.test_in_var.get()
        if not sample:
            self.test_out_var.set("")
            return
        try:
            result = gp.apply_ocr_corrections(sample, self._current_corrections(), log=lambda *_: None)
        except Exception as e:
            result = f"(error: {e})"
        self.test_out_var.set(f"→ {result}")

    def _on_save(self):
        profile = self.app.profile
        if profile is None:
            show_warning(self.win, "OCR corrections", "No profile loaded.")
            return
        corrections = self._current_corrections()
        bad = []
        for c in corrections:
            try:
                re.compile(c["pattern"])
            except re.error as e:
                bad.append((c["pattern"], str(e)))
        if bad:
            lines = "\n".join(f"- {pattern!r}: {err}" for pattern, err in bad[:5])
            show_error(self.win, "OCR corrections",
                       f"Couldn't save -- {len(bad)} pattern(s) don't compile as a regex:\n{lines}")
            return
        profile.set("ocr_corrections", corrections)
        profile.save_if_dirty()
        show_info(self.win, "OCR corrections", "Saved.")


class TranscriptWindow:
    """A second, deliberately plain window that shows nothing but the
    detected/spoken dialogue -- no timestamps, no "[speech] loading model"
    or "[cast] new speaker" chatter. It exists for people who already run
    their own TTS or screen reader and just want this app's OCR+detection
    output as clean text to point that tool at, instead of picking through
    the operational Log for the lines that matter (see
    game_text_speaker.run()'s on_transcript hook, which is what feeds this
    instead of the main log once this window -- or any -- has ever been
    opened this session).

    Same "Keep on top" idea as CastWindow, for the same reason: this is
    meant to sit visible over a borderless/windowed game, not get buried
    the moment you click back into it.

    "Stream to clipboard" is the other half of the accessibility story: a
    plain Tk Text widget doesn't participate in the OS accessibility tree
    (Tk has no real UI Automation/AT-SPI support), so a generic screen
    reader can't actually see this window's content change on its own --
    it would have to be manually re-read after every line. Plenty of
    lightweight external TTS/screen-reading tools work off the clipboard
    instead of the accessibility tree, though, so this checkbox copies
    each new line there the moment it's spoken. Off by default, since it
    takes over the system clipboard while it's running -- turning it on
    means anything you manually copy elsewhere gets overwritten by the
    next line."""

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        # See CastWindow.__init__'s comment on the same call: app.root above
        # only makes it this window's logical Tk parent, not what the
        # window manager treats as its owner -- transient() is what actually
        # sets that (WM_TRANSIENT_FOR), and this window was already meant to
        # behave like one of CastWindow's owned utility windows (see the
        # "Keep on top" note in this class's docstring above).
        self.win.transient(app.root)
        self.win.title("Transcript")
        self.win.geometry("520x420")
        self.win.minsize(320, 200)

        header = ttk.Frame(self.win, padding=(10, 8, 10, 4))
        header.pack(fill="x")
        ttk.Label(header, text="Transcript", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Button(header, text="Clear", command=self._clear).pack(side="right")
        self.on_top_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(header, text="Keep on top", variable=self.on_top_var,
                         command=self._apply_on_top).pack(side="right", padx=(0, 10))
        self.clipboard_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(header, text="Stream to clipboard", variable=self.clipboard_var).pack(
            side="right", padx=(0, 10))
        self._apply_on_top()

        ttk.Label(
            self.win, padding=(10, 0, 10, 4), justify="left", wraplength=460,
            text="Plain spoken-line output -- nothing else lands here. Point your own "
                 "screen reader or TTS tool at this window if you'd rather it read the "
                 "lines than this app's own speech engine (turn on Quiet in Speech to "
                 "stop this app from also speaking them itself). If your tool reads the "
                 "clipboard instead of a window, turn on \"Stream to clipboard\" -- each "
                 "new line replaces whatever's on it, since the clipboard only ever holds "
                 "one thing at a time.",
        ).pack(fill="x")

        text_frame = ttk.Frame(self.win, padding=(10, 0, 10, 10))
        text_frame.pack(fill="both", expand=True)
        self.text = scrolledtext.ScrolledText(text_frame, state="disabled", wrap="word")
        self.text.pack(fill="both", expand=True)

        # Backfill whatever was already spoken before this window existed,
        # so opening it mid-playthrough doesn't start on a blank page.
        for who, spoken in self.app._transcript_lines:
            self._append(who, spoken)

    def _apply_on_top(self):
        try:
            self.win.attributes("-topmost", bool(self.on_top_var.get()))
        except tk.TclError:
            pass  # window already gone -- nothing to do

    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def _clear(self):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")

    def append(self, who, spoken):
        """Called on the Tk main thread only (see App._drain_log_queue), for
        a line arriving live while this window is open -- as opposed to
        _append(), also used to backfill history on open (see __init__),
        which deliberately does NOT touch the clipboard: replaying old
        lines onto it the instant the window opens would just stomp
        whatever the user had copied, for lines nobody's currently reading."""
        self._append(who, spoken)
        if self.clipboard_var.get():
            line = f"{who}: {spoken}" if who else spoken
            try:
                self.win.clipboard_clear()
                self.win.clipboard_append(line)
            except tk.TclError:
                pass  # clipboard briefly unavailable/owned elsewhere -- just skip this line

    def _append(self, who, spoken):
        line = f"{who}: {spoken}" if who else spoken
        self.text.config(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.config(state="disabled")


class LicensesWindow:
    """Read-only viewer for THIRD_PARTY_LICENSES.md, opened from the
    "Licenses" button in the main window's Game row (see App._build_ui's
    game_row and _open_licenses_window). Exists so the licensing info this
    project
    needs to make available travels with the app itself, not just as a
    file someone has to go find in the repo -- especially important for a
    built .exe handed to someone who never sees the source tree at all
    (see that file's "GPL-3.0 components" section for why that matters).

    Deliberately a plain, undecorated text viewer -- no editing, no "Keep
    on top" (this isn't meant to sit over a running game the way
    CastWindow/OcrCorrectionsWindow/TranscriptWindow are), just something
    to open, read, and close."""

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.transient(app.root)
        self.win.title("Licenses")
        self.win.geometry("640x520")
        self.win.minsize(360, 260)

        header = ttk.Frame(self.win, padding=(10, 8, 10, 4))
        header.pack(fill="x")
        ttk.Label(header, text="Licenses", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Button(header, text="Close", command=self.win.destroy).pack(side="right")

        text_frame = ttk.Frame(self.win, padding=(10, 0, 10, 10))
        text_frame.pack(fill="both", expand=True)
        self.text = scrolledtext.ScrolledText(text_frame, state="normal", wrap="word")
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0", self._load_text())
        self.text.config(state="disabled")

    def _load_text(self) -> str:
        # Same Path(__file__).with_name(...) convention gui_settings.json
        # already relies on elsewhere in this file -- resolves next to
        # gui.py in a normal checkout, and next to the built .exe once
        # frozen, which is exactly where the README tells people to copy
        # these two files alongside the .exe before sharing it.
        notices_path = Path(__file__).with_name("THIRD_PARTY_LICENSES.md")
        license_path = Path(__file__).with_name("LICENSE")
        parts = []
        try:
            parts.append(notices_path.read_text(encoding="utf-8"))
        except OSError as exc:
            parts.append(
                "Couldn't read THIRD_PARTY_LICENSES.md next to this program "
                f"({exc}).\n\nIf this is a built .exe, make sure that file was "
                "copied alongside it -- see the README's \"Building a "
                "standalone .exe\" section."
            )
        if not license_path.exists():
            parts.append(
                "\n\n---\n\nNote: LICENSE (this project's own GPL-3.0 text) "
                "wasn't found alongside this program either."
            )
        return "\n".join(parts)

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


class App:
    def __init__(self, root):
        self.root = root
        root.title("Game Text Speaker")
        root.geometry("620x700")
        root.minsize(520, 560)

        # ttk's default button padding makes even a tiny icon/glyph-only
        # button (an info bubble, a per-row "✕" delete, Cast's "🔊" test
        # button) noticeably taller than the Entry/Combobox fields it sits
        # beside in the same row. This style trims that padding down so
        # those small buttons line up at the same height instead of
        # sticking up above the row.
        ttk.Style(root).configure("Compact.TButton", padding=0)

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
        self.transcript_window = None
        self.ocr_corrections_window = None
        self.licenses_window = None
        self._live_speaker = None   # set while running, for the reader's own speech
        self._preview_speakers = {}  # _preview_key(engine) -> Speaker, for Cast's Test button (see _speak_sample)
        self._preview_loading = set()  # _preview_key(engine) values currently being built, to dedupe clicks
        self._transcript_lines = []  # (who, spoken) history, so opening the Transcript window late still shows it

        self._build_ui()
        self._load_active_profile()
        self._refresh_region_status()
        self._refresh_name_region_status()
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

    def _add_info_button(self, parent, text: str, title: str = "Info", side: str = "left", extra=None):
        """Small circle-i button that pops up `text` in a dialog when
        clicked. Used instead of an always-visible wrapped hint label under
        a field -- keeps the explanation one click away without it
        permanently taking up window height. `side` controls which edge of
        `parent` it packs against, so it can either sit right next to the
        field it explains (the default) or be pinned to a corner (e.g. a
        section's top-right). `extra`, if given, is an (label, command)
        pair added as a second button on the popup -- see _show_dialog()."""
        padx = (4, 0) if side == "left" else (0, 4)
        btn = ttk.Button(parent, image=_info_icon(), style="Compact.TButton",
                          command=lambda: show_info(parent, title, text, extra=extra))
        btn.image = _info_icon()  # keep a reference so Tk doesn't GC it (see _info_icon())
        btn.pack(side=side, padx=padx)

    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        # Everything that's specific to one GAME -- the region, the timing,
        # how it marks who's speaking, and the cast -- belongs to the profile
        # picked here, so switching games is one dropdown instead of
        # re-dragging the box and re-tuning everything. See game_profile.py.
        game_frame = ttk.LabelFrame(self.root, text="Game")
        game_frame.pack(fill="x", **pad)
        game_row = ttk.Frame(game_frame)
        game_row.pack(fill="x", padx=6, pady=(6, 4))
        ttk.Label(game_row, text="Profile:").pack(side="left")
        self.profile_var = tk.StringVar(value="")
        self.profile_combo = ttk.Combobox(game_row, textvariable=self.profile_var,
                                          values=[], width=40, state="readonly")
        self.profile_combo.pack(side="left", padx=(4, 6), fill="x", expand=True)
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)
        ttk.Button(game_row, text="Licenses", command=self._open_licenses_window).pack(side="right")

        # Second row, just for the profile action buttons -- keeps this
        # LabelFrame from getting too wide for the window at default size
        # now that there are five of them alongside the dropdown above.
        # Left-aligned (unlike the rest of this app's rows) so they read as
        # a toolbar right under the dropdown they act on, in plain
        # left-to-right order: New, Import, Export, Transcript, Cast.
        actions_row = ttk.Frame(game_frame)
        actions_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(actions_row, text="New", command=self._on_new_profile).pack(side="left", padx=(0, 4))
        ttk.Button(actions_row, text="Import", command=self._on_import_shared).pack(side="left", padx=(0, 4))
        ttk.Button(actions_row, text="Export", command=self._on_export_shared).pack(side="left", padx=(0, 4))
        ttk.Button(actions_row, text="Transcript", command=self._open_transcript_window).pack(side="left", padx=(0, 4))
        ttk.Button(actions_row, text="Cast", command=self._open_cast_window).pack(side="left", padx=(0, 4))

        region_frame = ttk.LabelFrame(self.root, text="1. Dialogue region")
        region_frame.pack(fill="x", **pad)

        # Own row, deliberately -- packing these three straight into
        # region_frame (as this used to, back when it was the only row)
        # would let the two side="right" buttons each claim a FULL-HEIGHT
        # column on the right of the whole LabelFrame, not just this row,
        # squeezing every row added below (Name region) into whatever
        # narrow sliver was left between them and the label. Confining
        # left/right packing to its own row is what keeps each row
        # independent and full-width.
        region_row = ttk.Frame(region_frame)
        region_row.pack(fill="x")
        self.region_status_label = ttk.Label(region_row, text="")
        self.region_status_label.pack(side="left", padx=6, pady=6)
        btn = ttk.Button(region_row, text="From Screenshot", command=self._on_select_region_from_image)
        btn.pack(side="right", padx=6, pady=6)
        self._action_buttons.append(btn)
        btn = ttk.Button(region_row, text="Select Region", command=self._on_select_region)
        btn.pack(side="right", padx=6, pady=6)
        self._action_buttons.append(btn)

        # Name region: a SECOND, independently-watched box for whoever's
        # speaking -- see game_profile.py's detect_speaker(). Optional and
        # per-profile; leaving it unset means every line reads as the
        # Narrator, same as before this existed.
        ttk.Separator(region_frame, orient="horizontal").pack(fill="x", padx=6, pady=(0, 4))

        name_region_row = ttk.Frame(region_frame)
        name_region_row.pack(fill="x", padx=6, pady=(0, 4))
        self.name_region_status_label = ttk.Label(name_region_row, text="")
        self.name_region_status_label.pack(side="left")
        btn = ttk.Button(name_region_row, text="From Screenshot", command=self._on_select_name_region_from_image)
        btn.pack(side="right")
        self._action_buttons.append(btn)
        btn = ttk.Button(name_region_row, text="Select Name Region", command=self._on_select_name_region)
        btn.pack(side="right", padx=(0, 4))
        self._action_buttons.append(btn)

        popup_frame = ttk.LabelFrame(self.root, text="2. Ignore popups/overlays (optional)")
        popup_frame.pack(fill="x", **pad)
        top_row = ttk.Frame(popup_frame)
        top_row.pack(fill="x", padx=6, pady=(6, 0))
        self.popup_status_label = ttk.Label(top_row, text="")
        self.popup_status_label.pack(side="left")
        btn = ttk.Button(top_row, text="Select Popup Marker", command=self._on_select_popup_marker)
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
        engine_choices = [("quiet", "Quiet")]
        if sys.platform == "win32":
            # Listed first among the real engines on Windows: it's the
            # zero-setup option there (no separate install, unlike
            # espeak-ng/Piper/Kokoro) and the default engine on this
            # platform -- see DEFAULT_SETTINGS.
            engine_choices.append(("windows", ENGINE_LABELS["windows"]))
        engine_choices += [
            ("espeak", ENGINE_LABELS["espeak"]),
            ("piper", ENGINE_LABELS["piper"]),
            ("kokoro", ENGINE_LABELS["kokoro"]),
        ]
        # Disabling every unavailable engine is only useful when at least
        # one real engine remains selectable -- if something about this
        # install is stripped-down enough that NONE of them are available,
        # disabling all of them would leave the selector with only "Quiet"
        # reachable and no way to actually hear anything. In that unlikely
        # case, every option is left enabled and _on_start()'s own checks
        # are what surface the real problem once the user hits Start. What
        # each one actually is/needs lives in the ⓘ info button now, not a
        # label here -- see _engine_info_text().
        any_available = any(ENGINE_AVAILABILITY.get(name) for name, _ in engine_choices if name != "quiet")
        self.engine_radios = {}
        engine_row = ttk.Frame(engine_col)
        engine_row.pack(fill="x")
        ttk.Label(engine_row, text="Engine:").pack(side="left")
        for name, label in engine_choices:
            if name == "quiet":
                state = "normal"
            else:
                state = "normal" if (not any_available or ENGINE_AVAILABILITY.get(name)) else "disabled"
            rb = ttk.Radiobutton(engine_row, text=label, variable=self.engine_var,
                                  value=name, command=self._on_engine_selected, state=state)
            rb.pack(side="left", padx=(6, 0))
            self.engine_radios[name] = rb
        self._add_info_button(engine_row, self._engine_info_text(), title="Speech engine", side="right")

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
            [("windows", OCR_ENGINE_LABELS["windows"])] if sys.platform == "win32" else []
        ) + [("tesseract", OCR_ENGINE_LABELS["tesseract"])]
        # Same reasoning as the speech engine radios above: disabling every
        # unavailable OCR engine is only useful when at least one remains
        # selectable -- if neither is available, every option is left
        # enabled instead and _on_start() surfaces the real problem. What
        # each one actually needs lives in the ⓘ info button now, not a
        # label here -- see _ocr_engine_info_text().
        any_ocr_available = any(OCR_ENGINE_AVAILABILITY.get(name) for name, _ in ocr_engine_choices)
        self.ocr_engine_radios = {}
        ttk.Label(ocr_engine_row, text="Engine:").pack(side="left")
        for name, label in ocr_engine_choices:
            state = "normal" if (not any_ocr_available or OCR_ENGINE_AVAILABILITY.get(name)) else "disabled"
            rb = ttk.Radiobutton(ocr_engine_row, text=label, variable=self.ocr_engine_var,
                                  value=name, command=self._update_ocr_engine_widgets, state=state)
            rb.pack(side="left", padx=(6, 0))
            self.ocr_engine_radios[name] = rb
        self._add_info_button(ocr_engine_row, self._ocr_engine_info_text(), title="OCR engine", side="right")

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
        # A profile-specific glyph-confusion fix -- e.g. an opening quote
        # fused against a capital "I" into what a particular game's font
        # reads as a bare "T" -- applies to the raw OCR text before speaker
        # detection even runs, so it lives here with the rest of the OCR
        # pipeline's settings, deliberately NOT in the Cast window: it has
        # nothing to do with casting and applies even with no cast at all.
        # See game_profile.apply_ocr_corrections().
        ttk.Button(self.speaker_name_row, text="OCR Corrections",
                   command=self._open_ocr_corrections_window).pack(side="right")

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
        lines = ["Quiet — log recognized text, don't speak it."]
        for name in shown:
            ok = ENGINE_AVAILABILITY.get(name)
            line = f"{ENGINE_LABELS[name]} — {ENGINE_DESCRIPTIONS[name]} {'Installed.' if ok else 'NOT installed.'}"
            if not ok:
                line += f"\n    -> {ENGINE_INSTALL_HINT[name]}"
            lines.append(line)
        return "\n\n".join(lines)

    def _ocr_engine_info_text(self) -> str:
        shown = (["windows"] if sys.platform == "win32" else []) + ["tesseract"]
        lines = []
        for name in shown:
            ok = OCR_ENGINE_AVAILABILITY.get(name)
            line = (f"{OCR_ENGINE_LABELS[name]} — {OCR_ENGINE_DESCRIPTIONS[name]} "
                    f"{'Installed.' if ok else 'NOT installed.'}")
            if not ok:
                line += f"\n    -> {OCR_ENGINE_INSTALL_HINT[name]}"
            lines.append(line)
        return "\n\n".join(lines)

    # ---------------- status labels ----------------

    def _refresh_region_status(self):
        """Purely display. self.profile always exists by the time the main
        window is up (see _load_active_profile()) except on the rare path
        where even the starter profile couldn't be created -- guarded here
        rather than everywhere that calls this."""
        if self.profile is None:
            self.region_status_label.config(text="Region: (no profile loaded)")
            return
        region = self.profile.get("region")
        if region:
            self.region_status_label.config(
                text=f"Region set: {region['x']},{region['y']}  {region['w']}x{region['h']}")
        else:
            self.region_status_label.config(text="Region: not set for this profile yet")

    def _adopt_selected_region(self, picked=None):
        """Called after the user picks a region, with the picker's return
        value passed straight through -- nothing round-trips through a file
        anymore, so there's nothing here to clobber between games."""
        if picked and self.profile is not None:
            self.profile.set("region", picked)
            self.profile.save_if_dirty()
        self._refresh_region_status()

    def _refresh_name_region_status(self):
        """Purely display -- same idea as _refresh_region_status(), but for
        the profile's name_region (see game_profile.py's detect_speaker())."""
        if self.profile is None:
            self.name_region_status_label.config(text="Name region: (no profile loaded)")
            return
        region = self.profile.get("name_region")
        if region:
            self.name_region_status_label.config(
                text=f"Name region set: {region['x']},{region['y']}  {region['w']}x{region['h']}")
        else:
            self.name_region_status_label.config(
                text="Name region: not set (every line reads as Narrator)")

    def _adopt_selected_name_region(self, picked=None):
        """Called after the user picks a name region -- mirrors
        _adopt_selected_region()."""
        if picked and self.profile is not None:
            self.profile.set("name_region", picked)
            self.profile.save_if_dirty()
        self._refresh_name_region_status()

    def _refresh_popup_status(self):
        if self.profile is None:
            self.popup_status_label.config(text="Marker: (no profile loaded)")
            return
        marker = self.profile.get("popup_marker")
        if marker:
            self.popup_status_label.config(
                text=f"Marker set: {marker['x']},{marker['y']}  {marker['w']}x{marker['h']}  "
                     f"color {marker['ref_color']}")
        else:
            self.popup_status_label.config(text="Marker: not set")

    def _adopt_selected_popup_marker(self, picked=None):
        if picked and self.profile is not None:
            self.profile.set("popup_marker", picked)
            self.profile.save_if_dirty()
        self._refresh_popup_status()

    # ---------------- logging (thread-safe: workers push, main thread drains) ----------------

    def log(self, msg):
        self.log_queue.put(("log", msg))

    def _append_log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", str(msg) + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # Capped so a very long playthrough can't grow this without bound just
    # to backfill a Transcript window that might never get opened this run.
    TRANSCRIPT_MAX_LINES = 500

    def _on_transcript(self, who, spoken):
        """Called from the reader's own thread (see the on_transcript= wiring
        in _on_start's worker()) -- only ever hands work to the Tk thread
        through the queue, same as on_new_speaker/on_pause_change above."""
        self.log_queue.put(("transcript", (who, spoken)))

    def _append_transcript(self, who, spoken):
        self._transcript_lines.append((who, spoken))
        del self._transcript_lines[:-self.TRANSCRIPT_MAX_LINES]
        if self.transcript_window is not None and self.transcript_window.alive():
            self.transcript_window.append(who, spoken)

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
                elif kind == "transcript":
                    self._append_transcript(*payload)
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

        First run after this feature landed there won't be any profiles yet,
        so a "Default" one gets created automatically -- there's nothing
        older to fold in anymore now that region/marker selections never
        touch disk outside of a profile."""
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
            try:
                self.profile = gp.create_profile("Default")
                self.log(f"[profile] Created a starter profile: profiles/{self.profile.path.name}.")
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
            show_error(self.root, "Couldn't open profile", str(e))
            return
        self.settings["profile"] = self.profile.path.name
        self._save_settings()
        self.log(f"[profile] Switched to '{self.profile.name}'.")
        self._refresh_region_status()
        self._refresh_name_region_status()
        if self.cast_window is not None and self.cast_window.alive():
            self.cast_window.refresh()

    def _on_new_profile(self):
        from tkinter import simpledialog

        name = simpledialog.askstring("New game profile", "Name this game:", parent=self.root)
        if not name or not name.strip():
            return
        # Nothing else about a fresh profile needs deciding up front. It
        # starts with no Name region set, which means every line is
        # credited to the Narrator -- no per-character voices, no cast list
        # to curate, just one voice throughout, exactly like before. Drawing
        # a Name region (see the Dialogue region section) is what turns
        # per-character detection on, whenever you're ready for it -- an
        # unassigned character (or the Narrator) just sounds like whatever
        # the current engine's Speech settings say in the meantime.
        try:
            self.profile = gp.create_profile(name.strip())
        except OSError as e:
            show_error(self.root, "Couldn't create profile", str(e))
            return
        self.settings["profile"] = self.profile.path.name
        self._save_settings()
        self._refresh_profile_list()
        self.log(f"[profile] Created '{self.profile.name}' (profiles/{self.profile.path.name}). "
                 f"Pick a region for it next -- and a Name region too, if you want characters "
                 f"detected and given their own voices.")
        self._refresh_region_status()
        self._refresh_name_region_status()

    def _shared_dir(self):
        d = gp.PROFILE_DIR / gp.SHARED_EXPORT_DIRNAME
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _on_export_shared(self):
        if self.profile is None:
            show_warning(self.root, "Export", "No profile loaded.")
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export cast + OCR corrections",
            initialdir=str(self._shared_dir()),
            initialfile=f"{gp.slugify(self.profile.name)}-shared.json",
            defaultextension=".json",
            filetypes=[("Shared profile files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self.profile.export_shared(path)
        except OSError as e:
            show_error(self.root, "Export", f"Couldn't write {path}:\n{e}")
            return
        cast_count = len(self.profile.cast.to_json())
        corr_count = len(self.profile.get("ocr_corrections") or [])
        show_info(self.root, "Export",
                  f"Exported {cast_count} character(s) and {corr_count} OCR correction(s) to:\n{path}")

    def _on_import_shared(self):
        if self.profile is None:
            show_warning(self.root, "Import", "No profile loaded.")
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Import cast + OCR corrections",
            initialdir=str(self._shared_dir()),
            filetypes=[("Shared profile files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            cast_added, cast_updated, corr_added = self.profile.import_shared(path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            show_error(self.root, "Import", f"Couldn't import {path}:\n{e}")
            return
        self.profile.save_if_dirty()
        if self.cast_window is not None and self.cast_window.alive():
            self.cast_window.refresh()
        if self.ocr_corrections_window is not None and self.ocr_corrections_window.alive():
            self.ocr_corrections_window.refresh()
        show_info(self.root, "Import",
                  f"Added {cast_added} new character(s), updated {cast_updated} existing. "
                  f"Added {corr_added} new OCR correction(s).")

    def _on_new_speaker_ui(self, name):
        """A character has just spoken for the first time. Deliberately
        interrupts nothing -- they've already said that line in whatever this
        model's own default sounds like by the time this runs. Raising the
        Cast panel IS the prompt; answering it is optional and can wait."""
        self.log(f"[cast] New speaker: {name} — pick a voice for them in the Cast panel.")
        self._open_cast_window(highlight=name)

    def _open_cast_window(self, highlight=None):
        if self.profile is None:
            show_info(self.root, "No profile", "Create or choose a game profile first.")
            return
        if self.cast_window is None or not self.cast_window.alive():
            self.cast_window = CastWindow(self)
        self.cast_window.refresh(highlight=highlight)

    def _open_transcript_window(self):
        # No profile required here specifically -- this window just shows
        # whatever's been said so far, which self.profile being None (a
        # failed profile creation, see _load_active_profile()) doesn't
        # prevent.
        if self.transcript_window is None or not self.transcript_window.alive():
            self.transcript_window = TranscriptWindow(self)

    def _open_licenses_window(self):
        # No profile required -- this is app-wide info, not tied to any
        # particular game (see LicensesWindow's docstring).
        if self.licenses_window is None or not self.licenses_window.alive():
            self.licenses_window = LicensesWindow(self)

    def _open_ocr_corrections_window(self):
        if self.profile is None:
            show_info(self.root, "No profile", "Create or choose a game profile first.")
            return
        if self.ocr_corrections_window is None or not self.ocr_corrections_window.alive():
            self.ocr_corrections_window = OcrCorrectionsWindow(self)
        self.ocr_corrections_window.refresh()

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
            picked = None
            try:
                picked = core.select_region(log=self.log)
            except (SystemExit, Exception) as e:
                self.log(f"Error: {e}")
            finally:
                self.log_queue.put(("action_done", lambda: self._adopt_selected_region(picked)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_select_region_from_image(self):
        path = filedialog.askopenfilename(
            title="Select a screenshot with the dialogue visible",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")],
        )
        if not path:
            return
        picked = None
        try:
            picked = core.select_region_from_image(path, master=self.root, log=self.log)
        except Exception as e:
            self.log(f"Error: {e}")
            show_error(self.root, "Selection failed", str(e))
        self._adopt_selected_region(picked)

    def _on_select_name_region(self):
        self._set_actions_enabled(False)
        self.log("Select Name Region: click the button, then Alt+Tab back to the game — "
                  "drag a box around wherever the speaker's name shows (its own nameplate, "
                  "or the dialogue box itself if the name runs into the text there).")

        def worker():
            picked = None
            try:
                picked = core.select_region(log=self.log)
            except (SystemExit, Exception) as e:
                self.log(f"Error: {e}")
            finally:
                self.log_queue.put(("action_done", lambda: self._adopt_selected_name_region(picked)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_select_name_region_from_image(self):
        path = filedialog.askopenfilename(
            title="Select a screenshot with the character's name visible",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")],
        )
        if not path:
            return
        picked = None
        try:
            picked = core.select_region_from_image(path, master=self.root, log=self.log)
        except Exception as e:
            self.log(f"Error: {e}")
            show_error(self.root, "Selection failed", str(e))
        self._adopt_selected_name_region(picked)

    def _on_select_popup_marker(self):
        self._set_actions_enabled(False)
        self.log("Select Popup Marker: get the popup showing, click the button, then Alt+Tab back "
                  "and drag a small box over a spot unique to the popup.")

        def worker():
            picked = None
            try:
                picked = core.select_popup_marker(log=self.log)
            except (SystemExit, Exception) as e:
                self.log(f"Error: {e}")
            finally:
                self.log_queue.put(("action_done", lambda: self._adopt_selected_popup_marker(picked)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_browse_piper_model(self, parent=None):
        # `parent` ties the native file dialog to whichever window its
        # Browse button actually lives in -- currently always the Cast
        # window (see CastWindow._on_browse_piper_model()), which passes
        # its own self.win. Without an explicit parent, tkinter falls back
        # to the hidden default root, which has no fixed screen position of
        # its own -- so the dialog would show up wherever that happens to
        # land instead of near the button that opened it. Defaulting to
        # self.root here just means "the main window" if this is ever
        # called with no parent at all.
        path = filedialog.askopenfilename(
            parent=parent or self.root,
            title="Select a Piper voice model",
            filetypes=[("Piper voice model", "*.onnx"), ("All files", "*.*")],
        )
        if path:
            self.piper_model_var.set(path)
            self.piper_model_display_var.set(_basename_or_placeholder(path))

    def _on_browse_kokoro_model(self, parent=None):
        path = filedialog.askopenfilename(
            parent=parent or self.root,
            title="Select Kokoro's model file (kokoro-v1.0.onnx)",
            filetypes=[("Kokoro model", "*.onnx"), ("All files", "*.*")],
        )
        if path:
            self.kokoro_model_var.set(path)
            self.kokoro_model_display_var.set(_basename_or_placeholder(path))

    def _on_browse_kokoro_voices(self, parent=None):
        path = filedialog.askopenfilename(
            parent=parent or self.root,
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
        if self.profile is None:
            show_warning(self.root, "No profile", "No game profile is loaded -- see the log above "
                         "for why the starter profile couldn't be created.")
            return
        if not self.profile.get("region"):
            show_warning(self.root, "No region set", "Select a dialogue region first (step 1).")
            return

        settings = self._collect_settings()
        self._save_settings()

        if settings["ignore_popups"] and not self.profile.get("popup_marker"):
            show_warning(
                self.root, "No popup marker",
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
            show_warning(
                self.root, "Piper model required",
                "Select a Piper .onnx model file first, or switch to espeak-ng.",
            )
            return

        if not settings["quiet"] and settings["engine"] == "kokoro" and not (settings["kokoro_model"] and settings["kokoro_voices"]):
            show_warning(
                self.root, "Kokoro model required",
                "Select both Kokoro's model (.onnx) and voices (.bin) files first, or switch engines.",
            )
            return

        if not settings["quiet"] and settings["engine"] == "windows" and not ENGINE_AVAILABILITY.get("windows"):
            # The Radiobutton for this is normally disabled when pyttsx3
            # isn't installed (see ENGINE_AVAILABILITY), but a persisted
            # gui_settings.json from before it was uninstalled -- or before
            # this was even Windows -- can still leave engine_var pointing
            # at "windows" underneath a disabled, still-"selected" radio.
            show_warning(
                self.root, "Windows Native not available",
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
                show_warning(
                    self.root, "Invalid CPU threads",
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
                         on_speaker_ready=on_speaker_ready,
                         on_transcript=self._on_transcript)
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
