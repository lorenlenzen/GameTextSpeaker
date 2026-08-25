#!/usr/bin/env python3
"""
build_windows.py — builds a standalone Windows .exe of the GUI with
PyInstaller. Run this ON Windows, inside your activated venv, after:

    pip install pyinstaller

then:

    python build_windows.py

The result lands in dist\\game-text-speaker\\ (a folder — see ONEFILE
below to get a single .exe instead). Nothing this produces is committed to
the repo — build/ and dist/ are in .gitignore, same as this project
doesn't commit a venv/. That's normal for a PyInstaller-based project:
everyone builds their own .exe from source rather than a binary living in
git or being shipped alongside it. This script (source, a few KB) is what
gets committed; the .exe it produces (tens of MB, opaque, goes stale the
moment the source changes) does not.

This does NOT bundle Tesseract or espeak-ng — those remain separate
system-level installs (see README's Windows section) that need to be on
PATH, exactly like running from source. What this DOES bundle is the
Python interpreter itself plus every pip-installed dependency actually
present in the venv you run it from (pytesseract, mss, Pillow always;
piper-tts, kokoro-onnx, keyboard, sounddevice, psutil if you've installed
them) — so someone running the built .exe doesn't need Python installed
at all.

UNTESTED on real Windows hardware, like the rest of this project's Windows
support — this is developed on Linux. If the build fails or the resulting
.exe errors on startup, that's expected territory to debug; the comments
below point at the most likely culprits.
"""
import sys

if sys.platform != "win32":
    sys.exit(
        "This builds a Windows .exe and only makes sense to run on Windows.\n"
        "PyInstaller isn't a cross-compiler -- it bundles whatever OS it's actually\n"
        "run on, so a Linux/Mac run of this script would produce a Linux/Mac binary,\n"
        "not a .exe. Run it on the Windows machine you want the .exe for."
    )

try:
    import PyInstaller.__main__
except ImportError:
    sys.exit("Missing 'pyinstaller'. Install it with:  pip install pyinstaller   (inside your venv)")

# Flip to True for a single .exe file instead of a folder. A single file is
# more convenient to hand someone, but PyInstaller has to unpack itself to a
# temp directory on every launch, making startup noticeably slower --
# --onedir (the default here) starts faster and is easier to debug if a
# dependency turns out to be missing, at the cost of being a folder instead
# of one file.
ONEFILE = False

args = [
    "gui.py",
    "--name=game-text-speaker",
    "--windowed",   # no console window behind the GUI
    "--noconfirm",  # overwrite a previous build without asking
    "--clean",
]
if ONEFILE:
    args.insert(1, "--onefile")

# PyInstaller finds most imports via static analysis of the source, but
# onnxruntime (a Kokoro dependency) in particular loads some of its pieces
# dynamically in a way that analysis can miss. If the built .exe runs but
# fails specifically when --engine kokoro is used, with an error like "No
# module named onnxruntime.something", add the missing dotted name here and
# rebuild -- that's a PyInstaller packaging gap to work around, not a bug
# in this project's own code. Left empty until/unless that's actually hit.
HIDDEN_IMPORTS = []
for name in HIDDEN_IMPORTS:
    args.append(f"--hidden-import={name}")

# platform_adapter.py imports evdev, keyboard, sounddevice, and psutil
# lazily (inside functions, only on the branch that actually needs them) --
# PyInstaller's analysis still finds these import statements by scanning
# the whole file, so it'll warn about whichever ones aren't installed in
# THIS venv (evdev, being Linux-only, always will be). That warning is
# expected and harmless: those code paths never run on Windows anyway
# (they're behind `sys.platform == "win32"` checks), so there's nothing
# for PyInstaller to actually bundle or for the .exe to be missing.

PyInstaller.__main__.run(args)

print(
    "\nBuilt. The standalone app is in dist\\game-text-speaker\\ -- "
    "game-text-speaker.exe in there runs gui.py without needing Python installed.\n"
    "Tesseract and espeak-ng still need to be installed separately and on PATH "
    "(see README.md's Windows section)."
)
