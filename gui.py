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
import json
import queue
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

SETTINGS_PATH = Path(__file__).with_name("gui_settings.json")

DEFAULT_SETTINGS = {
    "engine": "espeak",
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
        return {
            "engine": self.engine_var.get(),
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
            "lang": self.lang_var.get().strip() or "eng",
            "similarity": self._as_float(self.similarity_var.get(), DEFAULT_SETTINGS["similarity"]),
            "ocr_min_confidence": self._as_int(self.ocr_min_confidence_var.get(), DEFAULT_SETTINGS["ocr_min_confidence"]),
            "speaker_name_mode": self.speaker_name_mode_var.get(),
            "ignore_popups": self.ignore_popups_var.get(),
            "popup_threshold": self._as_float(self.popup_threshold_var.get(), DEFAULT_SETTINGS["popup_threshold"]),
            "quiet": self.quiet_var.get(),
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

    def _add_info_button(self, parent, text: str, title: str = "Info"):
        """Small "(?)" button that pops up `text` in a dialog when clicked.
        Used instead of an always-visible wrapped hint label under a field
        -- keeps the explanation one click away without it permanently
        taking up window height."""
        ttk.Button(parent, text="(?)", width=3,
                   command=lambda: messagebox.showinfo(title, text)).pack(side="left", padx=(4, 0))

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
        self.engine_var = tk.StringVar(value=self.settings["engine"])
        ttk.Radiobutton(engine_col, text="espeak-ng — robotic, zero setup", variable=self.engine_var,
                         value="espeak", command=self._update_engine_widgets).pack(anchor="w")
        ttk.Radiobutton(engine_col, text="Piper — natural, needs a model", variable=self.engine_var,
                         value="piper", command=self._update_engine_widgets).pack(anchor="w")
        ttk.Radiobutton(engine_col, text="Kokoro — most natural, bigger download", variable=self.engine_var,
                         value="kokoro", command=self._update_engine_widgets).pack(anchor="w")

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

        self._update_engine_widgets()

        ocr_frame = ttk.LabelFrame(self.root, text="4. OCR / timing")
        ocr_frame.pack(fill="x", **pad)
        row = ttk.Frame(ocr_frame)
        row.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(row, text="Language:").pack(side="left")
        self.lang_var = tk.StringVar(value=self.settings["lang"])
        ttk.Entry(row, textvariable=self.lang_var, width=6).pack(side="left")
        ttk.Label(row, text="  Interval:").pack(side="left")
        self.interval_var = tk.StringVar(value=str(self.settings["interval"]))
        ttk.Entry(row, textvariable=self.interval_var, width=6).pack(side="left")
        ttk.Label(row, text="  Similarity:").pack(side="left")
        self.similarity_var = tk.StringVar(value=str(self.settings["similarity"]))
        ttk.Entry(row, textvariable=self.similarity_var, width=6).pack(side="left")

        row1b = ttk.Frame(ocr_frame)
        row1b.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Label(row1b, text="OCR cleanup (min confidence):").pack(side="left")
        self.ocr_min_confidence_var = tk.StringVar(value=str(self.settings["ocr_min_confidence"]))
        ttk.Entry(row1b, textvariable=self.ocr_min_confidence_var, width=5).pack(side="left")
        self._add_info_button(
            row1b,
            "Drops low-confidence OCR results (0-100) before they're spoken -- raise this if "
            "screen artifacts are read aloud as stray punctuation or gibberish; lower it if real "
            "dialogue is getting dropped.",
            title="OCR cleanup",
        )

        row1c = ttk.Frame(ocr_frame)
        row1c.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Label(row1c, text="Speaker name:").pack(side="left")
        self.speaker_name_mode_var = tk.StringVar(value=self.settings["speaker_name_mode"])
        ttk.Combobox(row1c, textvariable=self.speaker_name_mode_var,
                     values=["off", "skip", "announce"], width=10, state="readonly").pack(side="left")
        self._add_info_button(
            row1c,
            "For boxes that show a character's name above their quoted line: 'skip' drops the "
            "name and speaks only the dialogue; 'announce' speaks the name with a pause before "
            "the dialogue instead of it running straight into the first word. 'off' speaks the "
            "text exactly as OCR'd.",
            title="Speaker name",
        )

        row2 = ttk.Frame(ocr_frame)
        row2.pack(fill="x", padx=6, pady=(4, 0))
        self.quiet_var = tk.BooleanVar(value=self.settings["quiet"])
        ttk.Checkbutton(row2, text="Quiet (log only, don't speak)", variable=self.quiet_var).pack(anchor="w")

        row3 = ttk.Frame(ocr_frame)
        row3.pack(fill="x", padx=6, pady=(4, 6))
        ttk.Label(row3, text="Pause key:").pack(side="left")
        self.pause_key_var = tk.StringVar(value=self.settings["pause_key"])
        ttk.Entry(row3, textvariable=self.pause_key_var, width=10).pack(side="left")
        self._add_info_button(row3, "Pauses narration even while the game is focused. Blank disables.",
                               title="Pause key")
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

    def _update_engine_widgets(self):
        engine = self.engine_var.get()
        frames = {"espeak": self.espeak_frame, "piper": self.piper_frame, "kokoro": self.kokoro_frame}
        for name, frame in frames.items():
            if name == engine:
                frame.pack(fill="x", padx=6, pady=(0, 6))
            else:
                frame.pack_forget()

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

        if settings["engine"] == "piper" and not settings["piper_model"]:
            messagebox.showwarning(
                "Piper model required",
                "Select a Piper .onnx model file first, or switch to espeak-ng.",
            )
            return

        if settings["engine"] == "kokoro" and not (settings["kokoro_model"] and settings["kokoro_voices"]):
            messagebox.showwarning(
                "Kokoro model required",
                "Select both Kokoro's model (.onnx) and voices (.bin) files first, or switch engines.",
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
