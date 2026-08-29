#!/usr/bin/env python3
"""
setup.py — one-command setup for game-text-speaker.

Run it with:

    python3 setup.py          (Linux)
    python setup.py           (Windows)

That single command:

  1. Creates a venv/ next to this script if one doesn't exist yet, and
     re-launches itself inside it -- so there's no separate "create and
     activate a venv" step to remember or document.
  2. Opens a small window where you check off which OCR engine(s), speech
     engine(s), and extras (pause hotkey, CPU affinity) you actually want,
     then installs exactly those pip packages -- nothing you don't select
     ever gets installed. Two people running this can end up with
     completely different sets of packages depending on what they checked,
     and that's the point: this replaces "add packages one at a time until
     you have them all" with picking from a list up front.
  3. On Windows only, has a "build .exe" checkbox that, if checked, wraps
     PyInstaller right after installing -- the same way build_windows.py
     used to as a separate script and a separate click. That script is
     now retired and that click is gone too: one button does both.

Re-run this any time to add more later (e.g. you started with just
espeak-ng and now want Kokoro too) -- it remembers what you picked last
time (setup_settings.json, next to this script) and only installs whatever
new boxes you check; nothing here uninstalls or removes packages.

This does NOT install things that aren't pip packages at all -- the
tesseract-ocr/espeak-ng programs themselves, downloaded Piper/Kokoro voice
models, or a Windows OCR language pack. Those need a real installer, a
download, or an OS package manager, which this script has no business
running on your system without you watching it happen -- so instead, the
GUI's info (ⓘ) button next to anything that needs one of those tells you
exactly what to do, and the same is written out in README.md.

This project is developed primarily on Linux, so if a build fails or the
resulting .exe misbehaves, see the .exe-building section's comments below
for the most likely things to check.
"""
import json
import os
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_DIR = HERE / "venv"
REQUIREMENTS_PATH = HERE / "requirements.txt"
SETUP_SETTINGS_PATH = HERE / "setup_settings.json"


def _venv_python() -> Path:
    # Same two layouts venv/PyInstaller/every other tool in this project
    # already assumes: Scripts\ + .exe on Windows, bin/ with no extension
    # elsewhere.
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _running_in_our_venv() -> bool:
    try:
        return Path(sys.executable).resolve() == _venv_python().resolve()
    except Exception:
        return False


def _bootstrap():
    """Creates venv/ (if it doesn't exist) using whatever Python launched
    this script, then re-launches THIS SAME SCRIPT using the venv's own
    Python -- from that point on (a fresh process), _running_in_our_venv()
    is True and this function is never reached again. This is what makes
    "python3 setup.py" a complete, self-contained first step: no separate
    "python -m venv venv && source venv/bin/activate" for anyone to type,
    forget, or get wrong.

    On Windows, os.execv() doesn't truly replace the process the way it
    does on Linux/Mac (Windows has no real exec() syscall) -- Python
    emulates it by spawning the new process, waiting for it, and exiting
    with its return code. Functionally the same result (this script's
    output continues seamlessly in the same terminal), just implemented
    differently under the hood; nothing to configure differently here."""
    if not VENV_DIR.exists():
        print(f"Creating a virtual environment in {VENV_DIR} ...")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
        # Best-effort only -- an old bundled pip failing to self-upgrade
        # isn't worth aborting setup over; worst case, a later real install
        # fails with a clearer pip-versioning error the user can act on.
        try:
            subprocess.run(
                [str(_venv_python()), "-m", "pip", "install", "--upgrade", "pip"],
                check=False,
            )
        except Exception:
            pass

    py = _venv_python()
    if not py.exists():
        sys.exit(
            f"Expected a venv Python at {py} but it's not there -- something went wrong creating the "
            f"venv. Delete the venv{os.sep} folder and run this again."
        )
    print("Re-launching setup inside the venv...\n")
    os.execv(str(py), [str(py), str(Path(__file__).resolve())] + sys.argv[1:])


if not _running_in_our_venv():
    _bootstrap()
    sys.exit(0)  # unreachable in practice -- _bootstrap() never returns

# --------------------------------------------------------------------------
# Everything below this point is guaranteed to be running inside venv/.
# --------------------------------------------------------------------------

import importlib.util  # noqa: E402
import queue  # noqa: E402
import threading  # noqa: E402

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, scrolledtext
except ImportError:
    sys.exit(
        "Missing tkinter. Install it with:  sudo apt install python3-tk   (Linux -- the python.org "
        "Windows installer already includes it, so this shouldn't come up there)."
    )


def _build_features() -> list:
    """Returns the checkbox list this run of setup.py should offer,
    resolved once for the current platform rather than branching on
    sys.platform scattered through the UI code below. Each entry:
      id       -- stable key, used for persistence and the checked-set
      section  -- "ocr" / "speech" / "extras", which LabelFrame it lands in
      label    -- checkbox text
      packages -- pip package names to install if this is checked (can be
                  empty -- e.g. espeak-ng itself isn't a pip package at all)
      note     -- text for the info (ⓘ) button, or None if there's truly
                  nothing else to do beyond the pip install"""
    win = sys.platform == "win32"
    features = []

    features.append({
        "id": "tesseract", "section": "ocr", "label": "Tesseract OCR",
        "packages": ["pytesseract"],
        "note": "Also needs the tesseract-ocr program itself (a separate, non-pip install):\n\n"
                + (
                    "https://github.com/UB-Mannheim/tesseract/wiki -- the UB Mannheim .exe installer "
                    "is the standard Windows build. If OCR then comes back empty, its install folder "
                    "probably isn't on PATH -- add it (typically C:\\Program Files\\Tesseract-OCR) via "
                    "Windows' \"Edit environment variables\" dialog."
                    if win else
                    "sudo apt install tesseract-ocr"
                ),
    })
    if win:
        features.append({
            "id": "winocr", "section": "ocr", "label": "Windows OCR (built-in)",
            "packages": ["winocr"],
            "note": "Uses Windows' own OCR engine (Windows.Media.Ocr -- the same one PowerToys' Text "
                    "Extractor and the Snipping Tool use) -- nothing else to install, no PATH entry. If "
                    "a language's OCR pack isn't installed yet, running the app prints the exact "
                    "PowerShell command to add it.",
        })

    features.append({
        "id": "espeak", "section": "speech", "label": "espeak-ng (robotic)",
        "packages": [],
        "note": "Needs the espeak-ng program itself (a separate, non-pip install -- this checkbox has "
                "nothing for pip to do, it's just here so 'espeak-ng' shows up as a choice):\n\n"
                + (
                    "https://github.com/espeak-ng/espeak-ng/releases -- grab the .msi installer from "
                    "the latest release."
                    if win else
                    "sudo apt install espeak-ng"
                ),
    })
    features.append({
        "id": "piper", "section": "speech", "label": "Piper (natural neural voice)",
        "packages": ["piper-tts"] + (["sounddevice"] if win else []),
        "note": "Also needs a downloaded voice model (.onnx) -- see README.md's \"Better voices\" "
                "section for where to get one, then point --piper-model / the GUI's Model field at it."
                + (
                    " (sounddevice, included automatically above, is what plays the audio on Windows --"
                    " Linux instead uses aplay/paplay, already on most systems.)"
                    if win else ""
                ),
    })
    features.append({
        "id": "kokoro", "section": "speech", "label": "Kokoro (most natural neural voice)",
        "packages": ["kokoro-onnx"] + (["sounddevice"] if win else []),
        "note": "Also needs two downloaded model files (kokoro-v1.0.onnx, voices-v1.0.bin) -- see "
                "README.md's \"Better voices\" section for where to get them."
                + (
                    " (sounddevice, included automatically above, is what plays the audio on Windows --"
                    " Linux instead uses aplay/paplay, already on most systems.)"
                    if win else ""
                ),
    })
    if win:
        features.append({
            "id": "windows_sapi", "section": "speech", "label": "Windows SAPI5 (built-in)",
            "packages": ["pyttsx3"],
            "note": None,  # genuinely nothing else to do
        })

    features.append({
        "id": "hotkey", "section": "extras", "label": "Pause hotkey (pause narration with a keypress)",
        "packages": ["keyboard"] if win else ["evdev"],
        "note": None if win else (
            "Also needs read access to /dev/input -- usually: sudo usermod -aG input $USER, then log "
            "all the way out and back in for it to take effect."
        ),
    })
    # No CPU affinity checkbox on Windows: gui.py's own CPU affinity field
    # was removed from the Windows build (it's now Linux-only there too),
    # so there'd be nothing left in the app that could ever use psutil for
    # this -- installing it here would just be dead weight. Linux already
    # gets CPU affinity for free either way (os.sched_setaffinity, standard
    # library, no package needed), which is why it never had a checkbox
    # here to begin with.

    return features


SECTION_TITLES = {
    "ocr": "OCR engine(s) — pick at least one",
    "speech": "Speech engine(s) — pick at least one",
    "extras": "Extras (optional)",
}


def _default_checked() -> set:
    """What's pre-checked the very first time this runs (before
    setup_settings.json exists) -- deliberately the same as this project's
    own runtime defaults (see gui.py's DEFAULT_SETTINGS and
    game_text_speaker.py's --engine/--ocr-engine argparse defaults), so the
    box someone gets by just accepting the defaults here matches the box
    the app itself will assume if you never touch --engine/--ocr-engine."""
    if sys.platform == "win32":
        return {"winocr", "windows_sapi"}
    return {"tesseract", "espeak"}


def _load_selection() -> set:
    if SETUP_SETTINGS_PATH.exists():
        try:
            data = json.loads(SETUP_SETTINGS_PATH.read_text())
            return set(data.get("checked", []))
        except Exception:
            pass
    return _default_checked()


def _save_selection(checked: set) -> None:
    try:
        SETUP_SETTINGS_PATH.write_text(json.dumps({"checked": sorted(checked)}, indent=2))
    except Exception:
        pass  # not worth bothering the user about


def _pip_installed(module_name: str) -> bool:
    """Used only for build-time decisions below (what to tell PyInstaller
    to collect/hidden-import), running unfrozen inside this venv --
    find_spec() is the right, cheap check here. This is deliberately NOT
    reused by gui.py's own at-runtime availability checks: those run
    inside the (potentially frozen) built .exe, where find_spec() can
    report a package "available" when a real import would actually fail
    (see gui.py's _package_available() for why it does a real import
    instead) -- a distinction that only matters once something is frozen,
    which nothing in this file ever is."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def _check_pythoncom(log=print) -> bool:
    """pyttsx3's SAPI5 driver depends on pywin32's `pythoncom` module.
    PyInstaller's own hook for it finds pythoncom's bundled DLL by
    importing it in a throwaway subprocess purely to read its __file__ --
    and wraps ANY failure there (a real ImportError, or anything else)
    in the same generic, unhelpful message:
        Failed to retrieve attribute __file__ from module pythoncom
    Reproducing that exact same "import it in a fresh subprocess of this
    venv's own Python" check here, before PyInstaller ever runs, surfaces
    the REAL underlying error instead -- almost always pywin32 being
    installed but never having completed its own post-install step (the
    thing that registers pythoncom's DLL with this Python install)."""
    if not _pip_installed("pyttsx3"):
        return True  # SAPI5 not in use -- pythoncom is irrelevant either way
    result = subprocess.run(
        [sys.executable, "-c", "import pythoncom"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True
    log(
        "\npythoncom (part of pywin32, needed by the Windows SAPI5 speech engine) is installed but "
        "won't import cleanly in this venv. PyInstaller would fail the same way, just with a more "
        "confusing \"Failed to retrieve attribute __file__ from module pythoncom\" error -- caught "
        "here first instead. The actual import error was:\n\n"
        + (result.stderr.strip() or "(no error output)") +
        "\n\nThis is almost always pywin32 not having finished its own setup. Try, in order, then "
        "click Build again:\n"
        "1. python -m pip install --upgrade --force-reinstall pywin32\n"
        "2. If it exists, run the file venv\\Scripts\\pywin32_postinstall.py directly with this "
        "venv's python: venv\\Scripts\\python.exe venv\\Scripts\\pywin32_postinstall.py -install\n"
        "3. Make sure only one of pywin32/pypiwin32 is installed -- "
        "python -m pip uninstall pypiwin32 -y, then reinstall pywin32 (step 1 above)."
    )
    return False


def _build_exe(onefile: bool, log=print) -> bool:
    """Ported from the now-retired build_windows.py -- see git history if
    you want that standalone script back, but the logic is unchanged, just
    moved here so it's reachable from this GUI's "Build standalone .exe"
    button instead of being a second script to separately document.

    Runs PyInstaller as a SEPARATE PROCESS (python -m PyInstaller ...)
    rather than importing PyInstaller.__main__ and calling run() directly
    in this thread. Two reasons:
      1. Streaming: like _run_pip() below, a subprocess lets us forward
         PyInstaller's own progress output into the Log box line-by-line
         as it happens, instead of the box going silent for the whole
         build -- PyInstaller.__main__.run() prints to this process's
         real stdout, which nothing here was ever reading. From inside
         the GUI, "no new lines for a couple minutes" looks exactly like
         "hung", especially once Windows itself marks the window (Not
         Responding) while it's busy.
      2. Isolation: several of PyInstaller's own error paths (bad script
         path, running from a disallowed cwd, Ctrl-C) are a bare
         `raise SystemExit(...)`, which is NOT a subclass of Exception --
         the try/except around the call to this function in _on_install's
         worker() wouldn't catch it, so it would silently kill this
         background thread with no log line and no way to ever
         re-enable the Install button again. A subprocess can exit however
         it likes; only its return code reaches us here.
    """
    args = [
        str(HERE / "gui.py"),
        "--name=game-text-speaker",
        "--windowed",   # no console window behind the GUI
        "--noconfirm",  # overwrite a previous build without asking
        "--clean",
    ]
    if onefile:
        args.insert(1, "--onefile")

    # kokoro-onnx ships a config.json (its phoneme/vocab table) sitting
    # next to its own .py files, loaded via
    # open(Path(__file__).parent / "config.json") the moment the package
    # is imported. PyInstaller's static analysis only follows Python
    # *imports*, not arbitrary non-.py files sitting in the same folder,
    # so without this, the frozen .exe fails as soon as --engine kokoro is
    # used, with something like:
    #   [Errno 2] No such file or directory: '...\\_internal\\kokoro_onnx\\config.json'
    if _pip_installed("kokoro_onnx"):
        args.append("--collect-data=kokoro_onnx")

    # winocr depends on `language-tags` to validate/normalize BCP-47
    # language codes, which ships its own bundled JSON language-subtag
    # registry the exact same way kokoro-onnx ships config.json above.
    # Without this, the frozen .exe fails the first time --ocr-engine
    # windows actually runs OCR, with something like:
    #   [Errno 2] No such file or directory: '...\\_internal\\language_tags\\data\\json/index.json'
    if _pip_installed("language_tags"):
        args.append("--collect-data=language_tags")

    # kokoro-onnx's phonemizer backend needs espeak-ng's own phoneme/
    # language data, which it gets via espeakng_loader -- a package that,
    # again, ships that data as a real directory (espeak-ng-data/) sitting
    # next to its .py files rather than as anything PyInstaller's import
    # scanning would notice. Without this, the frozen .exe fails the first
    # time --engine kokoro actually tries to speak, with:
    #   Failed to load Kokoro model: data path not exists at
    #   ...\_internal\espeakng_loader\espeak-ng-data
    if _pip_installed("espeakng_loader"):
        args.append("--collect-data=espeakng_loader")

    # Piper ships its OWN separate, private espeak-ng-data directory (piper
    # doesn't use espeakng_loader/kokoro's copy at all -- it bundles its
    # own, right inside the `piper` package) -- same non-Python-data-file
    # blind spot as above. Without this, the frozen .exe fails the first
    # time --engine piper actually tries to speak, with something like:
    #   [Errno 2] No such file or directory: '...\\_internal\\piper\\espeak-ng-data'
    if _pip_installed("piper"):
        args.append("--collect-data=piper")

    # pyttsx3 picks its platform driver (drivers/sapi5.py on Windows) with
    # an importlib.import_module() call built from a string at runtime,
    # which PyInstaller's static import-scanning can't see -- without
    # these, the frozen .exe fails the moment --engine windows is used,
    # with: ModuleNotFoundError: No module named 'pyttsx3.drivers.sapi5'
    # (a well-known PyInstaller+pyttsx3 packaging gap -- see PyInstaller
    # issue #3268 upstream -- not a bug in this project's own code).
    if _pip_installed("pyttsx3"):
        args.append("--hidden-import=pyttsx3.drivers")
        args.append("--hidden-import=pyttsx3.drivers.sapi5")
        args.append("--hidden-import=pyttsx3.drivers.dummy")

    # game_text_speaker.py defaults to --engine windows and --ocr-engine
    # windows on Windows specifically so a built .exe can run with zero
    # extra installs -- but only if pyttsx3/winocr were both in THIS venv
    # before this build ran. Doesn't block the build (you might genuinely
    # want a slimmer .exe and always pick a different engine yourself),
    # just makes that a choice instead of a surprise.
    gaps = []
    if not _pip_installed("pyttsx3"):
        gaps.append("pyttsx3 (backs the default speech engine)")
    if not _pip_installed("winocr"):
        gaps.append("winocr (backs the default OCR engine)")
    if gaps:
        log(
            "Note: " + " and ".join(gaps) + " not installed in this venv.\n"
            "The .exe below will still run, but its own DEFAULT settings will fail immediately unless "
            "you pick a different engine/OCR backend every time (the GUI's dropdowns) -- check the "
            "matching box above, Install, and rebuild to fix the default itself instead."
        )

    log("Running PyInstaller -- this can take a minute or two...")
    cmd = [sys.executable, "-m", "PyInstaller"] + args
    log(f"$ {' '.join(cmd)}")
    # cwd=HERE, always -- PyInstaller refuses to run at all with a cwd of
    # C:\Windows\System32 (a real check it does on Windows, not something
    # we're working around), and without this it just inherits whatever
    # directory the setup.py process itself happened to be started from --
    # System32 specifically is the default cwd for e.g. an elevated
    # ("Run as administrator") terminal, so this is easy to hit by
    # accident and has nothing to do with where the project actually
    # lives. Pinning it here means it no longer matters how someone
    # launched setup.py in the first place.
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=str(HERE)
    )
    for line in proc.stdout:
        log(line.rstrip())
    proc.wait()
    if proc.returncode != 0:
        log(f"\nPyInstaller exited with code {proc.returncode} -- see the output above for why.")
        return False

    # onefile and onedir put the finished .exe in genuinely different
    # places -- onedir COLLECTs everything into dist\game-text-speaker\
    # with game-text-speaker.exe inside it, while onefile's EXE() writes a
    # single game-text-speaker.exe straight into dist\ itself, no subfolder
    # at all. Reporting the wrong one of these isn't just a cosmetic typo:
    # someone who checked "Single-file .exe" and then goes looking in
    # dist\game-text-speaker\ (per the onedir wording) finds either nothing
    # or a stale exe left over from an earlier onedir build, which looks
    # exactly like "the build didn't work" even though it did.
    if onefile:
        log(
            "\nBuilt. The standalone app is dist\\game-text-speaker.exe -- a single file that runs "
            "without needing Python installed (no dist\\game-text-speaker\\ folder this time -- that's "
            "only for the non-single-file build).\nTesseract and espeak-ng (if you're using either) "
            "still need to be installed separately and on PATH -- see the (?) notes above."
        )
    else:
        log(
            "\nBuilt. The standalone app is in dist\\game-text-speaker\\ -- game-text-speaker.exe in there "
            "runs without needing Python installed.\nTesseract and espeak-ng (if you're using either) still "
            "need to be installed separately and on PATH -- see the (?) notes above."
        )
    return True


class SetupApp:
    def __init__(self, root):
        self.root = root
        root.title("game-text-speaker setup")
        root.geometry("640x680")
        root.minsize(560, 540)

        self.features = _build_features()
        self.by_id = {f["id"]: f for f in self.features}
        self.checked_ids = _load_selection()
        self.vars = {}

        self.log_queue = queue.Queue()
        self._build_ui()
        self.root.after(100, self._drain_log_queue)

    # ---------------- UI construction ----------------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        ttk.Label(
            self.root,
            text=(
                "Pick the OCR and speech engines (and extras) you want available, then click Install. "
                "Re-run this any time to add more -- it remembers what you picked and never removes "
                "anything."
                + (
                    " Check \"build .exe\" below to also build a standalone executable in the same step."
                    if sys.platform == "win32" else ""
                )
            ),
            wraplength=600, justify="left",
        ).pack(fill="x", **pad)

        base_frame = ttk.LabelFrame(self.root, text="Always installed")
        base_frame.pack(fill="x", **pad)
        ttk.Label(
            base_frame,
            text="Screen capture: mss, pillow — needed no matter what you pick below.",
        ).pack(anchor="w", padx=6, pady=4)

        for section_id in ("ocr", "speech", "extras"):
            frame = ttk.LabelFrame(self.root, text=SECTION_TITLES[section_id])
            frame.pack(fill="x", **pad)
            for feat in self.features:
                if feat["section"] != section_id:
                    continue
                var = tk.BooleanVar(value=feat["id"] in self.checked_ids)
                self.vars[feat["id"]] = var
                row = ttk.Frame(frame)
                row.pack(fill="x", padx=6, pady=2)
                ttk.Checkbutton(row, text=feat["label"], variable=var).pack(side="left")
                if feat["note"]:
                    # Pinned to the right edge of this row, same convention
                    # as gui.py's info buttons -- setup.py has one row per
                    # checkbox rather than gui.py's one shared row per
                    # section, so "the first row" there is just this row.
                    ttk.Button(
                        row, text="ⓘ", width=2,
                        command=lambda n=feat["note"], l=feat["label"]: messagebox.showinfo(l, n),
                    ).pack(side="right", padx=(4, 0))

        # Build options live in their own frame, but there's deliberately no
        # second button here -- just checkboxes that the single Install
        # button below reads at click time. One button doing "install, then
        # optionally build" is simpler to explain than two buttons where the
        # second one silently depends on what state the first one left
        # behind (see _on_install below).
        if sys.platform == "win32":
            build_frame = ttk.LabelFrame(self.root, text="Build a standalone .exe (optional)")
            build_frame.pack(fill="x", **pad)
            self.build_exe_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                build_frame,
                text="Also build game-text-speaker.exe, right after installing",
                variable=self.build_exe_var,
            ).pack(anchor="w", padx=6, pady=(4, 0))
            self.onefile_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                build_frame,
                text="Single-file .exe (slower to start each time; easier to hand to someone as one file)",
                variable=self.onefile_var,
            ).pack(anchor="w", padx=6)

            # Checking "Single-file .exe" on its own reads like a complete
            # request -- "I want a single-file build" -- but it's really
            # just a modifier for the checkbox above: _on_install() only
            # builds anything at all when build_exe_var is checked, and
            # onefile_var only matters when it is. Checking only this one
            # used to silently install packages and build NOTHING, no
            # warning, nothing in dist\ -- exactly what happened here. This
            # makes the dependency visible instead of a trap: checking
            # Single-file also checks (and shows checked) the box that
            # actually triggers a build.
            def _onefile_implies_build(*_args):
                if self.onefile_var.get():
                    self.build_exe_var.set(True)

            self.onefile_var.trace_add("write", _onefile_implies_build)
            ttk.Label(
                build_frame,
                text="Bundles whatever else is checked above -- so check everything you want included "
                     "before clicking Install.",
                wraplength=580, justify="left",
            ).pack(anchor="w", padx=6, pady=(0, 4))

        btn_row = ttk.Frame(self.root)
        btn_row.pack(fill="x", **pad)
        self.install_button = ttk.Button(btn_row, text="Install selected", command=self._on_install)
        self.install_button.pack(side="left")

        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=14, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

    # ---------------- logging (thread-safe: workers push, main thread drains) ----------------

    def _log(self, msg):
        self.log_queue.put(("log", msg))

    def _drain_log_queue(self):
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "log":
                    self.log_text.config(state="normal")
                    self.log_text.insert("end", str(payload) + "\n")
                    self.log_text.see("end")
                    self.log_text.config(state="disabled")
                elif kind == "enable_install":
                    self.install_button.config(state="normal")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    # ---------------- install ----------------

    def _run_pip(self, args) -> bool:
        cmd = [sys.executable, "-m", "pip"] + args
        self._log(f"$ {' '.join(cmd)}")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            self._log(line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            self._log(f"pip exited with code {proc.returncode} -- see the output above for why.")
        return proc.returncode == 0

    def _on_install(self):
        checked = {fid for fid, var in self.vars.items() if var.get()}
        if not any(self.by_id[fid]["section"] == "ocr" for fid in checked):
            messagebox.showwarning(
                "Pick an OCR engine",
                "Select at least one OCR engine (Tesseract" + (" or Windows OCR" if sys.platform == "win32" else "") + ").",
            )
            return
        if not any(self.by_id[fid]["section"] == "speech" for fid in checked):
            messagebox.showwarning("Pick a speech engine", "Select at least one speech engine.")
            return

        _save_selection(checked)

        packages = []
        for fid in checked:
            for pkg in self.by_id[fid]["packages"]:
                if pkg not in packages:
                    packages.append(pkg)

        # Read these on the main thread (Tkinter variables) before handing
        # off to the worker -- same reasoning as `checked`/`packages` above.
        # Checking either box means "build me something" -- the
        # onefile_var trace in _build_ui already keeps build_exe_var
        # checked in lockstep whenever onefile_var is, but this `or` is a
        # second, independent guarantee that checking Single-file alone
        # can never again silently build nothing.
        onefile = sys.platform == "win32" and self.onefile_var.get()
        build_exe = sys.platform == "win32" and (self.build_exe_var.get() or self.onefile_var.get())

        self.install_button.config(state="disabled")

        def worker():
            ok = True
            self._log("Installing base requirements (mss, pillow)...")
            ok = self._run_pip(["install", "-r", str(REQUIREMENTS_PATH)]) and ok
            if packages:
                self._log(f"\nInstalling selected extras: {', '.join(packages)}")
                ok = self._run_pip(["install"] + packages) and ok
            else:
                self._log("\nNo optional extras selected -- base requirements only.")

            notes = [self.by_id[fid]["note"] for fid in sorted(checked) if self.by_id[fid]["note"]]
            if notes:
                self._log("\nDone installing. Remaining manual steps for what you picked (click the "
                           "matching (?) button above for the full text):")
                for fid in sorted(checked):
                    if self.by_id[fid]["note"]:
                        self._log(f"- {self.by_id[fid]['label']}")
            else:
                self._log("\nDone -- nothing else to install manually for what you picked.")
            if not ok:
                self._log("\nOne or more installs failed -- scroll up for the pip error, fix it "
                           "(often just a network hiccup -- try Install again), then retry.")

            # Same click, second half: build the .exe from whatever was just
            # installed above, only if "build .exe" was checked. This used
            # to be a separate button with its own separate click -- folded
            # in here instead since there was never a reason to install and
            # build as two deliberately separate steps.
            if build_exe:
                if not ok:
                    self._log("\nSkipping the .exe build -- fix the install error above first, then "
                               "click Install again.")
                else:
                    self._log("\nBuilding game-text-speaker.exe...")
                    try:
                        import PyInstaller  # noqa: F401
                    except ImportError:
                        self._log("Installing pyinstaller...")
                        if not self._run_pip(["install", "pyinstaller"]):
                            self._log("Couldn't install pyinstaller -- see the error above.")
                            ok = False
                    if ok and _check_pythoncom(log=self._log):
                        try:
                            if not _build_exe(onefile=onefile, log=self._log):
                                self._log(
                                    "\n*** .exe build FAILED -- see the PyInstaller output above for "
                                    "why. Nothing was written to dist\\. ***"
                                )
                        except Exception as e:
                            self._log(f"\n*** .exe build FAILED: {e} ***")

            self._log("\nYou can now run:  python gui.py" if sys.platform == "win32" else
                       "\nYou can now run:  python3 gui.py")
            self.log_queue.put(("enable_install", None))

        threading.Thread(target=worker, daemon=True).start()


def main():
    root = tk.Tk()
    SetupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()