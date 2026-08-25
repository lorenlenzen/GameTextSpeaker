"""
platform_adapter.py — the one place OS differences live.

game_text_speaker.py needs four things that Linux and Windows do
differently: letting the user drag a box around the screen, grabbing pixels
from that box repeatedly, playing back synthesized speech audio, and
watching for a hotkey press system-wide. Rather than sprinkling
`sys.platform == "win32"` checks through the main script, each of those
four things is defined as a small abstract interface here (the "strategy"
pattern), with one concrete implementation per OS. game_text_speaker.py
asks `get_platform_adapter()` for the right one once, then calls the same
four methods regardless of which OS it's actually running on.

LinuxAdapter wraps exactly the same slop/slurp/mss/grim/aplay/paplay/evdev/
sched_setaffinity code this project has always used — nothing about how it
behaves on Linux changes. WindowsAdapter is new: it has not been run on an
actual Windows machine, since this project is developed on Linux. If
something in it doesn't work, that's the place to look — see the README's
Windows section for what to try.

Linux also has its own internal split this file preserves: X11 (slop to
pick a region, mss to grab pixels) vs. Wayland (slurp, grim) — see
is_wayland() below. That split is a Linux desktop-server detail, not a
second "platform" alongside Windows, so it stays nested inside
LinuxAdapter rather than becoming a third top-level adapter.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from abc import ABC, abstractmethod
from pathlib import Path


def subprocess_no_window_kwargs() -> dict:
    """Extra kwargs for subprocess.Popen/run, so spawning a console-mode
    child process (espeak-ng.exe, piper.exe) doesn't flash its own console
    window on screen. This app has no console UI of its own to inherit, so
    Windows creating one from scratch for every single child process --
    once per utterance, i.e. constantly while dialogue is being read -- is
    pure visual noise, not an error. subprocess.CREATE_NO_WINDOW only
    exists on Windows; this is a no-op {} everywhere else, where spawning a
    subprocess never opens a visible window in the first place."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def exe_name(base: str) -> str:
    """'piper' -> 'piper.exe' on Windows, unchanged elsewhere. Windows'
    PATH search adds this suffix automatically for shutil.which(), but not
    when we build a path ourselves (Path(sys.executable).parent / 'piper',
    used to prefer a venv-local binary over PATH — see Speaker.__init__)."""
    return base + ".exe" if sys.platform == "win32" else base


def check_dependency(cmd: str, install_hint: str) -> None:
    if shutil.which(cmd) is None:
        sys.exit(f"Missing required command '{cmd}'.\n{install_hint}\n(see README.md for the full dependency list)")


# --------------------------------------------------------------------------
# Screen capture — one small class per capture backend. Region selection
# (below) and Speaker's audio playback (further below) also each have one
# implementation per backend, following the same shape.
# --------------------------------------------------------------------------

class MssCapturer:
    """Grabs a screen region via the 'mss' package. Used on Linux/X11 and
    on Windows alike — mss's cross-platform backend covers both."""

    def __init__(self, region: dict):
        self.region = region
        try:
            import mss  # noqa: F401
        except ImportError:
            sys.exit("Missing python package 'mss'. Install with: pip install mss")
        import mss as _mss
        self._mss = _mss.mss()

    def grab(self, region: dict = None):
        from PIL import Image

        r = region or self.region
        box = {"left": r["x"], "top": r["y"], "width": r["w"], "height": r["h"]}
        shot = self._mss.grab(box)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


class GrimCapturer:
    """Grabs a screen region via the Wayland 'grim' command-line tool.
    Linux/Wayland only — mss can't read a Wayland compositor's buffers
    directly the way it can X11's."""

    def __init__(self, region: dict):
        self.region = region
        check_dependency("grim", "Install it with:  sudo apt install grim")

    def grab(self, region: dict = None):
        from PIL import Image

        r = region or self.region
        geometry = f"{r['x']},{r['y']} {r['w']}x{r['h']}"
        proc = subprocess.run(["grim", "-g", geometry, "-"], capture_output=True, check=True)
        return Image.open(io.BytesIO(proc.stdout)).convert("RGB")


# --------------------------------------------------------------------------
# Region selection from a still image — shared by every platform. Used as
# the --select-from-image fallback everywhere, and internally by
# WindowsAdapter.select_region() (see below) as the *only* way to pick a
# region on Windows, since there's no slop/slurp equivalent there.
# --------------------------------------------------------------------------

def pick_region_from_image(img, master=None, log=print) -> dict:
    """Opens `img` (a PIL Image) in a click-and-drag Tkinter window and
    returns the selected {x, y, w, h} in the image's own pixel coordinates.
    Raises RuntimeError if `master` is given (an existing Tk app, e.g. the
    GUI) and something goes wrong; calls sys.exit otherwise (standalone
    CLI use). See select_region_from_image()/WindowsAdapter.select_region()
    for the two callers."""
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

    img = img.convert("RGB")
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

    log(f"Image was {img_w}x{img_h}px.")
    return result


# --------------------------------------------------------------------------
# PCM audio playback — Speaker (in game_text_speaker.py) hands raw 16-bit
# mono PCM samples to whatever this returns, without needing to know how
# they actually get to speakers. Both engines that need this (Piper and
# Kokoro) already produce audio as either "a subprocess whose stdout is raw
# PCM" (Piper) or "numpy sample chunks in this process" (Kokoro) — the
# two open_*() methods below match those two shapes.
#
# Every PcmPlayer duck-types the handful of subprocess.Popen members
# Speaker's stop/pause/error-reporting logic already relies on (poll(),
# .returncode, terminate()), so that logic — _check_finished(),
# _stop_current() — didn't need to change at all when this was introduced.
# --------------------------------------------------------------------------

class PcmPlayer(ABC):
    @abstractmethod
    def write(self, data: bytes) -> None:
        """Feed more raw PCM bytes. Only meaningful for a player opened via
        open_pcm_player(); a player opened via open_piped_player() gets its
        data from the piped source stream instead and ignores this."""

    @abstractmethod
    def close_stdin(self) -> None:
        """Signal that no more audio is coming, once the caller's done
        writing. Safe to call more than once."""

    @abstractmethod
    def poll(self):
        """None while still playing, else a returncode (0 = finished
        cleanly, matching subprocess.Popen.poll())."""

    @property
    @abstractmethod
    def returncode(self):
        ...

    @abstractmethod
    def terminate(self) -> None:
        """Stop playback immediately (used to cut off a superseded or
        paused utterance) — mirrors subprocess.Popen.terminate()."""

    @abstractmethod
    def read_stderr(self) -> str:
        """Whatever error text is available, for _check_finished()'s
        '[speech] ... exited with code N: ...' log line. Empty string if
        there's nothing to report or nothing failed."""


class _SubprocessPcmPlayer(PcmPlayer):
    """Wraps a real subprocess.Popen (aplay/paplay) — Linux's PcmPlayer.
    This is the exact same object the pre-adapter code built directly; it's
    just accessed through the PcmPlayer interface now instead of Speaker
    reaching into a raw Popen itself."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc

    def write(self, data: bytes) -> None:
        try:
            self._proc.stdin.write(data)
        except (BrokenPipeError, OSError):
            pass

    def close_stdin(self) -> None:
        try:
            self._proc.stdin.close()
        except (BrokenPipeError, OSError, AttributeError):
            pass

    def poll(self):
        return self._proc.poll()

    @property
    def returncode(self):
        return self._proc.returncode

    def terminate(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()

    def read_stderr(self) -> str:
        try:
            if self._proc.stderr:
                return self._proc.stderr.read().decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        return ""


class _SoundDevicePcmPlayer(PcmPlayer):
    """Plays raw 16-bit mono PCM through a sounddevice.RawOutputStream —
    Windows's PcmPlayer, since there's no aplay/paplay there. Used both for
    direct writes (Kokoro's chunks) and, via the reader thread started by
    WindowsAdapter.open_piped_player(), for Piper's piped stdout.

    UNTESTED on real Windows hardware — this project is developed on
    Linux. sounddevice.RawOutputStream.write() blocks until there's buffer
    room, the same way writing to a subprocess's stdin pipe blocks when the
    OS pipe fills up, so it should be a drop-in behavioral match — but if
    Piper/Kokoro audio stutters, cuts out, or errors on Windows, this class
    is the first place to look.
    """

    def __init__(self, sample_rate: int, channels: int = 1):
        import sounddevice as sd

        self._stream = sd.RawOutputStream(samplerate=sample_rate, channels=channels, dtype="int16")
        self._stream.start()
        self._error = None
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        try:
            self._stream.write(data)
        except Exception as e:
            self._error = str(e)

    def close_stdin(self) -> None:
        pass  # nothing to close -- terminate()/going idle ends the stream

    def poll(self):
        if self._error is not None:
            return 1
        if self._closed or not self._stream.active:
            return 0
        return None

    @property
    def returncode(self):
        return self.poll()

    def terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.abort()  # stop immediately, discarding anything buffered
            self._stream.close()
        except Exception:
            pass

    def read_stderr(self) -> str:
        return self._error or ""


def _pump_stream_into_player(source_stream, player: PcmPlayer, chunk_size: int = 4096) -> None:
    """Background-thread target for WindowsAdapter.open_piped_player():
    repeatedly reads from `source_stream` (a subprocess's stdout, e.g.
    Piper's) and forwards each chunk into `player`. On Linux this whole
    function is unnecessary — the OS pipes Piper's stdout straight into
    aplay/paplay's stdin with no Python involved at all (see
    LinuxAdapter.open_piped_player()); this thread exists only because
    sounddevice has no equivalent of "hand it a file descriptor"."""
    try:
        while True:
            chunk = source_stream.read(chunk_size)
            if not chunk:
                break
            player.write(chunk)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        player.close_stdin()


# --------------------------------------------------------------------------
# Global pause hotkey
# --------------------------------------------------------------------------

class HotkeyWatcher(ABC):
    """`available` is False (checked by callers before start()) whenever
    the hotkey couldn't be set up for any reason — missing package,
    unrecognized key name, no permission to read input. Every failure mode
    logs a note and disables itself rather than raising, since losing the
    pause hotkey shouldn't take down OCR/speech with it."""

    available: bool = False

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...


# A few friendlier spellings for common evdev key names (Linux).
_EVDEV_KEY_ALIASES = {
    "space": "KEY_SPACE",
    "spacebar": "KEY_SPACE",
    "scrolllock": "KEY_SCROLLLOCK",
    "scroll_lock": "KEY_SCROLLLOCK",
    "scroll-lock": "KEY_SCROLLLOCK",
    "pause": "KEY_PAUSE",
    "break": "KEY_PAUSE",
}


class LinuxHotkeyWatcher(HotkeyWatcher):
    """Listens for a key press anywhere on the system — not just while this
    app's own window has focus — and calls `on_toggle()` each time it's
    pressed, so the narrator can be paused/resumed while a fullscreen game
    has keyboard focus instead of this app.

    Reads raw keyboard events straight from the kernel (/dev/input) via the
    'evdev' package, rather than through a display-server-level "global
    hotkey" hook: those typically only work under X11 and silently do
    nothing under Wayland — the same X11/Wayland split this file already
    handles for screen capture. Reading straight from the kernel works
    under either.

    Deliberately does NOT grab() the input device exclusively — it only
    *observes* key events alongside however the game/desktop already
    handles them, so the pause key still does whatever it always did in
    the game too. See README for why, and how to pick a key that avoids
    stepping on something the game already uses.
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
        attr_name = _EVDEV_KEY_ALIASES.get(key_name.strip().lower(), f"KEY_{key_name.strip().upper()}")
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
        import glob
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


class WindowsHotkeyWatcher(HotkeyWatcher):
    """Windows equivalent of LinuxHotkeyWatcher, using the 'keyboard'
    package's low-level global keyboard hook instead of reading /dev/input
    — Windows has no X11-vs-Wayland split to work around, so a standard
    global-hotkey-style library works fine here (unlike on Linux, where
    that approach silently fails under Wayland).

    UNTESTED on real Windows hardware. One known difference from the Linux
    behavior: 'keyboard' fires its callback on Windows' own key-repeat
    events too if the key is held down, not just the initial press (evdev
    lets us filter to just the initial press via event.value == 1; this
    package doesn't expose that distinction as directly). In practice this
    should rarely matter, since run()'s existing 0.3s debounce (originally
    added for duplicate-input-device presses) also collapses rapid repeats
    from a held key — but a very long press could in principle toggle more
    than once. Worth testing with an actual held keypress on Windows.
    """

    def __init__(self, key_name: str, on_toggle, log=print):
        self.log = log
        self.on_toggle = on_toggle
        self.key_name = key_name
        self.available = False
        self._hook = None
        self._keyboard = None

        try:
            import keyboard
        except ImportError:
            self.log(
                f"Note: pause hotkey ('{key_name}') disabled — the 'keyboard' package isn't installed. "
                f"Install it with: pip install keyboard   (inside your venv — see README)."
            )
            return
        self._keyboard = keyboard
        self.available = True  # 'keyboard' validates the key name lazily, in start()

    def start(self):
        if not self.available:
            return
        try:
            self._hook = self._keyboard.on_press_key(self.key_name, lambda e: self.on_toggle())
        except (ValueError, ImportError) as e:
            self.log(
                f"Note: pause hotkey disabled — '{self.key_name}' isn't a recognized key name for "
                f"the 'keyboard' package ({e}). Try 'space', 'f9', 'scroll lock', ..."
            )
            self.available = False
        except Exception as e:
            # The 'keyboard' package needs administrator privileges on some
            # Windows setups to install a global low-level hook -- if this
            # fires, that's the first thing to try.
            self.log(f"Note: pause hotkey disabled — couldn't start the keyboard hook: {e}")
            self.available = False

    def stop(self):
        if self._hook is not None:
            try:
                self._keyboard.unhook(self._hook)
            except Exception:
                pass
            self._hook = None


# --------------------------------------------------------------------------
# Platform adapters
# --------------------------------------------------------------------------

class PlatformAdapter(ABC):
    """The interface game_text_speaker.py actually calls. Get one via
    get_platform_adapter() — never instantiate LinuxAdapter/WindowsAdapter
    directly outside of tests, so the rest of the code never needs its own
    sys.platform check."""

    @abstractmethod
    def select_region(self, log=print) -> dict:
        """Interactively let the user drag a box around the screen; return
        {x, y, w, h} in screen pixel coordinates."""

    @abstractmethod
    def make_capturer(self, region: dict):
        """Return an object with .grab(region=None) -> PIL.Image, for
        repeatedly grabbing that (or another) region."""

    @abstractmethod
    def open_pcm_player(self, sample_rate: int, channels: int = 1) -> PcmPlayer:
        """A PcmPlayer the caller will .write() raw PCM chunks into
        directly (Kokoro's synthesis path)."""

    @abstractmethod
    def open_piped_player(self, source_stream, sample_rate: int, channels: int = 1) -> PcmPlayer:
        """A PcmPlayer fed from an existing readable byte stream (Piper's
        subprocess stdout) instead of explicit write() calls."""

    @abstractmethod
    def resolve_player(self, engine_label: str, log=print) -> None:
        """Called once by Speaker.__init__ (for the piper/kokoro engines)
        before any open_*_player() call, to do one-time setup/checks and
        log which audio backend will be used."""

    @abstractmethod
    def make_hotkey_watcher(self, key_name: str, on_toggle, log=print) -> HotkeyWatcher:
        ...

    @abstractmethod
    def set_cpu_affinity(self, affinity: str, log=print) -> None:
        """Pin this process to specific CPU cores, e.g. "4,5,6,7". A no-op
        if `affinity` is empty."""


def is_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE") == "wayland"


class LinuxAdapter(PlatformAdapter):
    """Wraps this project's original, proven-working Linux implementation
    unchanged — including its own internal X11-vs-Wayland split."""

    def select_region(self, log=print) -> dict:
        if is_wayland():
            check_dependency("slurp", "Install it with:  sudo apt install slurp")
            out = subprocess.run(["slurp"], capture_output=True, text=True, check=True).stdout.strip()
            m = re.match(r"(\d+),(\d+)\s+(\d+)x(\d+)", out)
            if not m:
                sys.exit(f"Couldn't parse slurp output: {out!r}")
            x, y, w, h = map(int, m.groups())
        else:
            check_dependency("slop", "Install it with:  sudo apt install slop")
            out = subprocess.run(
                ["slop", "-f", "%x %y %w %h"], capture_output=True, text=True, check=True
            ).stdout.strip()
            x, y, w, h = map(int, out.split())
        return {"x": x, "y": y, "w": w, "h": h}

    def make_capturer(self, region: dict):
        return GrimCapturer(region) if is_wayland() else MssCapturer(region)

    def open_pcm_player(self, sample_rate: int, channels: int = 1) -> PcmPlayer:
        proc = subprocess.Popen(
            self._player_cmd(sample_rate, channels), stdin=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return _SubprocessPcmPlayer(proc)

    def open_piped_player(self, source_stream, sample_rate: int, channels: int = 1) -> PcmPlayer:
        proc = subprocess.Popen(
            self._player_cmd(sample_rate, channels), stdin=source_stream, stderr=subprocess.PIPE
        )
        return _SubprocessPcmPlayer(proc)

    def _player_cmd(self, sample_rate: int, channels: int) -> list:
        player = self.player_cmd_base
        if "paplay" in player:
            return [player, "--raw", f"--rate={sample_rate}", "--format=s16le", f"--channels={channels}"]
        return [player, "-q", "-r", str(sample_rate), "-f", "S16_LE", "-t", "raw", "-c", str(channels), "-"]

    def resolve_player(self, engine_label: str, log=print) -> None:
        """Finds paplay/aplay once and remembers it as self.player_cmd_base
        — called by Speaker.__init__ before any open_*_player() call, same
        as the original code did inline."""
        self.player_cmd_base = shutil.which("paplay")
        if not self.player_cmd_base:
            aplay = shutil.which("aplay")
            if not aplay:
                sys.exit(f"Need 'paplay' or 'aplay' to play {engine_label}'s audio. Install with: sudo apt install alsa-utils")
            self.player_cmd_base = aplay
            log(
                f"Note: using aplay ({aplay}) to play {engine_label}'s audio — paplay isn't installed. "
                f"aplay talks to ALSA directly rather than through PulseAudio/PipeWire, which on some "
                f"systems means it 'succeeds' silently without any audible output (wrong/dummy default "
                f"device) even though nothing errors. If {engine_label} stays silent with no error here, "
                f"try `sudo apt install pulseaudio-utils` for paplay instead."
            )
        else:
            log(f"{engine_label} audio playback: {self.player_cmd_base}")

    def make_hotkey_watcher(self, key_name: str, on_toggle, log=print) -> HotkeyWatcher:
        return LinuxHotkeyWatcher(key_name, on_toggle, log=log)

    def set_cpu_affinity(self, affinity: str, log=print) -> None:
        """Pins this whole process (OCR loop + Tesseract calls + Kokoro/
        Piper synthesis, since affinity is inherited by every thread) to
        specific CPU cores. On a busy system — a demanding game hogging
        every core — this reserves real, uncontended CPU time for the
        narrator instead of leaving the OS scheduler to time-share it with
        everything else, which is what causes long, unpredictable pauses
        before a line starts playing."""
        if not affinity:
            return
        if not hasattr(os, "sched_setaffinity"):
            log("[cpu-affinity] Not supported on this OS — ignoring --cpu-affinity.")
            return
        try:
            cores = {int(c.strip()) for c in affinity.split(",") if c.strip() != ""}
            if not cores:
                return
            os.sched_setaffinity(0, cores)
            log(f"[cpu-affinity] Pinned this process to CPU core(s): {sorted(cores)}")
        except Exception as e:
            log(f"[cpu-affinity] Couldn't set CPU affinity {affinity!r}: {e}")


class WindowsAdapter(PlatformAdapter):
    """New, and UNTESTED on real Windows hardware — see the module
    docstring. Every method here has a Linux counterpart above doing the
    same job with different tools; that's the pairing to check first if
    something behaves differently on Windows than on Linux."""

    def select_region(self, log=print) -> dict:
        # No slop/slurp equivalent on Windows, so instead: take a live
        # screenshot of the whole screen right now, show it full-size in a
        # Tkinter window, and let the user drag a box on that image with no
        # time pressure -- the same mechanism --select-from-image already
        # uses for the "can't alt-tab over a fullscreen game" case on
        # Linux, just fed a fresh screenshot instead of a saved file.
        log("Taking a screenshot to drag a box on...")
        import mss
        from PIL import Image
        with mss.mss() as sct:
            mon = sct.monitors[1]  # the full virtual screen across all monitors
            shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        result = pick_region_from_image(img, log=log)
        # pick_region_from_image()'s coordinates are relative to the
        # screenshot, which itself starts at (mon["left"], mon["top"]) --
        # normally (0, 0), but not guaranteed with multi-monitor setups
        # where a monitor sits to the left of/above the primary one.
        result["x"] += mon["left"]
        result["y"] += mon["top"]
        return result

    def make_capturer(self, region: dict):
        return MssCapturer(region)

    def open_pcm_player(self, sample_rate: int, channels: int = 1) -> PcmPlayer:
        return _SoundDevicePcmPlayer(sample_rate, channels)

    def open_piped_player(self, source_stream, sample_rate: int, channels: int = 1) -> PcmPlayer:
        player = _SoundDevicePcmPlayer(sample_rate, channels)
        threading.Thread(target=_pump_stream_into_player, args=(source_stream, player), daemon=True).start()
        return player

    def resolve_player(self, engine_label: str, log=print) -> None:
        try:
            import sounddevice  # noqa: F401
        except ImportError:
            sys.exit(
                f"Missing required package 'sounddevice', needed to play {engine_label}'s audio on Windows.\n"
                f"Install it with:  pip install sounddevice   (inside your venv)"
            )
        log(f"{engine_label} audio playback: sounddevice")

    def make_hotkey_watcher(self, key_name: str, on_toggle, log=print) -> HotkeyWatcher:
        return WindowsHotkeyWatcher(key_name, on_toggle, log=log)

    def set_cpu_affinity(self, affinity: str, log=print) -> None:
        if not affinity:
            return
        try:
            import psutil
        except ImportError:
            log(
                "[cpu-affinity] 'psutil' isn't installed — ignoring --cpu-affinity. "
                "Install it with: pip install psutil   (inside your venv)"
            )
            return
        try:
            cores = sorted({int(c.strip()) for c in affinity.split(",") if c.strip() != ""})
            if not cores:
                return
            psutil.Process().cpu_affinity(cores)
            log(f"[cpu-affinity] Pinned this process to CPU core(s): {cores}")
        except Exception as e:
            log(f"[cpu-affinity] Couldn't set CPU affinity {affinity!r}: {e}")


def get_platform_adapter() -> PlatformAdapter:
    return WindowsAdapter() if sys.platform == "win32" else LinuxAdapter()
