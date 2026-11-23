"""Cinder Wisp Macro: reads Deepwoken ritual cast letters off the screen and types them."""
import configparser
import logging
import os
import platform
import queue
import random
import sys
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import NamedTuple

import cv2 as cv
import mss
import numpy as np
import pywinctl
from PIL import Image, ImageDraw, ImageTk
from pynput import keyboard

IS_WINDOWS = platform.system() == "Windows"
if IS_WINDOWS:
    import pydirectinput as pd
else:
    import pyautogui as pd  # fallback for macOS/Linux

# Built-in wait after each key press. Raise it if the game starts missing keys.
pd.PAUSE = 0.015

APP_NAME = "CinderWispMacro"
WINDOW_TITLE = "Cinder Wisp Macro"

# --- Detection tuning ---
# The ritual cast letters are drawn at a fixed pixel size and position: centred
# horizontally and a fixed distance below the top of the Roblox window, whatever the
# resolution. So the search box is a fixed size, placed relative to the window.
CAST_OFFSET_X = -116         # left edge, in pixels from the window's horizontal centre
CAST_OFFSET_Y = 170          # top edge, in pixels below the top of the window's content
CAST_WIDTH = 307             # split into 4 equal slots, one letter each
CAST_HEIGHT = 190
BINARIZE_THRESH = 140
MATCH_THRESHOLD = 0.5
# A letter after an empty slot (e.g. the last one left on screen) needs a surer match, since
# there's no letter before it to confirm a cast is showing. Real letters score ~0.8-0.9;
# letter-sized patches of the game background top out around 0.5.
GAP_MATCH_THRESHOLD = 0.7
PURPLE_BLUE_OVER_GREEN = 40  # purple letters measure ~88 more blue than green, white ones ~0
SLOTS = 4                    # a ritual cast shows 1-4 letters
# If the exact same letters are still showing right after typing them, the game may just not have
# updated yet, so wait this long before assuming the keys didn't register and typing them again
REPEAT_GUARD_SECONDS = 0.2
RESCAN_DELAY = 0.02          # when a scan finds under 4 letters, look again this soon (fading in)

# --- Human mode timing (seconds): (average, spread, shortest, longest) ---
# A good player already knows the ritual's normal letters, so those flow quickly.
# Only the purple letters are random and need reading.
HUMAN_START = (0.04, 0.015, 0.02, 0.08)         # before the first key, when it's a normal letter
HUMAN_GAP = (0.055, 0.018, 0.03, 0.11)          # between keys
HUMAN_READ_PURPLE = (0.15, 0.04, 0.09, 0.26)    # before a purple letter, instead of the gap
HUMAN_HOLD = (0.045, 0.012, 0.025, 0.08)        # how long each key is held down
HUMAN_HESITATE_CHANCE = 0.04                    # occasionally pause a little longer
HUMAN_HESITATE = (0.04, 0.10)
TEMPLATE_EXTENSIONS = (".png", ".jpg", ".jpeg")

MACRO_KEYS = {"c", "v", "x", "z"}  # keys the macro presses itself
# Older settings files stored Tk key names; map them to pynput names
KEY_ALIASES = {
    "escape": "esc",
    "return": "enter",
    "prior": "page_up",
    "next": "page_down",
    "control_l": "ctrl_l",
    "control_r": "ctrl_r",
    "print": "print_screen",
}

log = logging.getLogger(APP_NAME)


# --- Paths ---
def resource_path(relative_path: str) -> Path:
    """Absolute path to a bundled resource (works from source and from PyInstaller)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative_path


def app_data_dir() -> Path:
    folder = Path(os.getenv("APPDATA") or Path.home() / ".config") / APP_NAME
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def setup_logging(debug: bool):
    log.handlers.clear()
    formatter = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(message)s", "%H:%M:%S")
    file_handler = RotatingFileHandler(app_data_dir() / "macro.log", maxBytes=512_000,
                                       backupCount=1, encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    if sys.stderr is not None:  # no console in the windowed exe
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        log.addHandler(stream_handler)
    set_debug_logging(debug)


def set_debug_logging(debug: bool):
    log.setLevel(logging.DEBUG if debug else logging.INFO)


# --- Keys ---
def normalize_key(name: str) -> str:
    name = name.strip().lower()
    return KEY_ALIASES.get(name, name)


def display_key(name: str) -> str:
    name = normalize_key(name)
    return name.upper() if len(name) <= 3 else name.replace("_", " ").title()


def pynput_key_name(key):
    if isinstance(key, keyboard.KeyCode):
        return key.char.lower() if key.char else None
    return key.name.lower()


# --- Settings ---
class Settings:
    """Persistent settings that fall back to defaults when the file is missing or invalid."""

    DEFAULTS = {"keybind": "f8", "delay": "0.0", "debug": "false"}

    def __init__(self, path: Path):
        self.path = path
        self.config = configparser.ConfigParser()
        try:
            self.config.read(path)
        except configparser.Error as e:
            print("Settings file is corrupt, resetting to defaults:", e)
            self.config.clear()
        if not self.config.has_section("SETTINGS"):
            self.config.add_section("SETTINGS")
        section = self.config["SETTINGS"]

        self.keybind = normalize_key(section.get("keybind", "")) or self.DEFAULTS["keybind"]
        try:
            self.delay = max(0.0, float(section.get("delay", self.DEFAULTS["delay"])))
        except ValueError:
            self.delay = float(self.DEFAULTS["delay"])
        self.debug = self._bool(section, "debug", False)
        self.human_mode = self._bool(section, "human_mode", False)
        self.overlay = self._bool(section, "overlay", False)
        self.overlay_locked = self._bool(section, "overlay_locked", False)
        try:
            self.overlay_pos = (int(section["overlay_x"]), int(section["overlay_y"]))
        except (KeyError, ValueError):
            self.overlay_pos = None  # use the default spot
        self.save()

    @staticmethod
    def _bool(section, key, default):
        value = section.get(key, "").strip().lower()
        return value in ("1", "true", "yes", "on") if value else default

    def save(self):
        section = self.config["SETTINGS"]
        section["keybind"] = self.keybind
        section["delay"] = f"{self.delay:g}"
        section["debug"] = "true" if self.debug else "false"
        section["human_mode"] = "true" if self.human_mode else "false"
        section["overlay"] = "true" if self.overlay else "false"
        section["overlay_locked"] = "true" if self.overlay_locked else "false"
        if self.overlay_pos is not None:
            section["overlay_x"], section["overlay_y"] = map(str, self.overlay_pos)
        try:
            with open(self.path, "w") as f:
                self.config.write(f)
        except OSError as e:
            log.warning("Couldn't save settings: %s", e)


# --- Detection ---
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    def _process_name(hwnd):
        """Executable name of the process that owns a window (fast, unlike getAppName)."""
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = _kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not process:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(len(buffer))
            if _kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                return Path(buffer.value).name
            return ""
        finally:
            _kernel32.CloseHandle(process)

_roblox_cache = {}  # window handle -> belongs to Roblox (a window's owner never changes)


def is_roblox_handle(handle, window=None):
    if handle in _roblox_cache:
        return _roblox_cache[handle]
    if IS_WINDOWS:
        name = _process_name(handle)
    else:
        try:
            name = window.getAppName() or ""  # slow, hence the cache
        except Exception:
            name = window.title if window is not None else ""
    if len(_roblox_cache) > 256:
        _roblox_cache.clear()
    _roblox_cache[handle] = result = "roblox" in name.lower()
    return result


def is_roblox(window):
    return is_roblox_handle(window.getHandle(), window)


def find_roblox_window():
    """The active window if it belongs to the Roblox app, otherwise None."""
    window = pywinctl.getActiveWindow()
    return window if window is not None and is_roblox(window) else None


class Letter(NamedTuple):
    key: str
    purple: bool  # purple letters are the random ones a player has to read


def is_purple(pixels):
    """Normal letters are near-white; purple ones have far more blue than green.
    `pixels` are BGR(A) colours from where the letter was matched."""
    if len(pixels) < 10:
        return False
    blue, green = pixels[:, 0].astype(float).mean(), pixels[:, 1].astype(float).mean()
    return blue - green > PURPLE_BLUE_OVER_GREEN


def window_frame(window):
    """(left, top, width, height) of the window's content area, without the title bar."""
    try:
        frame = window.getClientFrame()
        return frame.left, frame.top, frame.right - frame.left, frame.bottom - frame.top
    except Exception:
        return window.left, window.top, window.width, window.height


class Detector:
    def __init__(self, template_folder: Path):
        self.templates = []  # (letter, binary image) at the reference resolution
        for path in sorted(template_folder.iterdir()):
            if not (path.is_file() and path.suffix.lower() in TEMPLATE_EXTENSIONS):
                continue
            image = cv.imread(str(path), cv.IMREAD_GRAYSCALE)
            if image is None:
                log.warning("Template load failed: %s", path)
                continue
            letter = path.stem.replace("_purple", "").lower()
            self.templates.append((letter, image))
        if not self.templates:
            raise FileNotFoundError(f"No letter templates found in {template_folder}")
        self._local = threading.local()

    def _mss(self):
        # mss isn't thread-safe, so each thread gets its own instance
        if not hasattr(self._local, "mss"):
            self._local.mss = mss.MSS()
        return self._local.mss

    @staticmethod
    def capture_region(window):
        """Screen area where the cast letters appear, placed relative to the Roblox window."""
        left, top, width, _ = window_frame(window)
        return left + width // 2 + CAST_OFFSET_X, top + CAST_OFFSET_Y, CAST_WIDTH, CAST_HEIGHT

    def grab(self, region):
        """(binary image for matching, colour image for telling purple letters apart)"""
        left, top, width, height = region
        shot = np.asarray(self._mss().grab({"left": left, "top": top, "width": width, "height": height}))
        gray = cv.cvtColor(shot, cv.COLOR_BGRA2GRAY)
        _, binary = cv.threshold(gray, BINARIZE_THRESH, 255, cv.THRESH_BINARY)
        return binary, shot

    def read_letters(self, image, color=None):
        """Letters from left to right. Empty slots are skipped, so a letter left on its own in a
        later slot (after the ones before it were typed) is still found."""
        slot_width = image.shape[1] // SLOTS
        letters = []
        after_gap = False
        for i in range(SLOTS):
            x0 = i * slot_width
            slot = image[:, x0:x0 + slot_width]
            best = {}  # letter -> (score, location, template)
            for letter, template in self.templates:
                if template.shape[0] > slot.shape[0] or template.shape[1] > slot.shape[1]:
                    continue
                result = cv.matchTemplate(slot, template, cv.TM_CCOEFF_NORMED)
                _, score, _, loc = cv.minMaxLoc(result)
                # Normal and purple templates share a letter, so keep the better score
                if score > best.get(letter, (-1.0,))[0]:
                    best[letter] = (score, loc, template)
            if not best:
                after_gap = True
                continue
            key = max(best, key=lambda k: best[k][0])
            score, (lx, ly), template = best[key]
            log.debug("Slot %d scores: %s", i + 1,
                      ", ".join(f"{k}={v[0]:.2f}" for k, v in sorted(best.items())))
            if score < (GAP_MATCH_THRESHOLD if after_gap else MATCH_THRESHOLD):
                after_gap = True
                continue
            purple = False
            if color is not None:
                th, tw = template.shape[:2]
                pixels = color[ly:ly + th, x0 + lx:x0 + lx + tw][template > 0]
                purple = is_purple(pixels)
            letters.append(Letter(key, purple))
        return letters


# --- Macro loop ---
class MacroRunner:
    """Runs the detect-and-type loop on a background thread."""

    def __init__(self, detector: Detector, get_delay, get_human_mode, on_event):
        self.detector = detector
        self.get_delay = get_delay
        self.get_human_mode = get_human_mode
        self.on_event = on_event  # called from the macro thread with (kind, data)
        self._stop_event = None
        self._thread = None

    @property
    def running(self):
        return self._stop_event is not None

    def start(self):
        if self.running:
            return
        self._stop_event = threading.Event()
        # Pass the previous thread so the new run waits for it to fully exit
        self._thread = threading.Thread(target=self._loop, args=(self._stop_event, self._thread),
                                        daemon=True)
        self._thread.start()

    def stop(self):
        if self._stop_event is not None:
            self._stop_event.set()
            self._stop_event = None

    def owns(self, stop_event):
        return stop_event is self._stop_event

    @staticmethod
    def _vary(timing):
        """A random duration that clusters around the average, like a person's timing does."""
        average, spread, shortest, longest = timing
        return min(longest, max(shortest, random.gauss(average, spread)))

    def _type_like_human(self, letters, stop_event):
        """Type like a player who knows the ritual: normal letters flow quickly with natural
        variation, and only purple (random) letters get a pause to read them."""
        for i, letter in enumerate(letters):
            if stop_event.is_set():
                break
            if letter.purple:
                wait = self._vary(HUMAN_READ_PURPLE)
            else:
                wait = self._vary(HUMAN_START if i == 0 else HUMAN_GAP)
            if i > 0:
                wait += self.get_delay()
                if random.random() < HUMAN_HESITATE_CHANCE:
                    wait += random.uniform(*HUMAN_HESITATE)
            stop_event.wait(wait)
            if stop_event.is_set():
                break
            pd.keyDown(letter.key, _pause=False)
            try:
                stop_event.wait(self._vary(HUMAN_HOLD))
            finally:
                pd.keyUp(letter.key, _pause=False)  # always release, even when stopping
        stop_event.wait(self.get_delay())

    def _loop(self, stop_event, previous_thread):
        if previous_thread is not None:
            previous_thread.join()
        log.info("Macro started")
        last_status = None
        last_typed, last_typed_at = None, 0.0

        def status(kind):
            nonlocal last_status
            if kind != last_status:
                last_status = kind
                self.on_event(kind, None)

        try:
            while not stop_event.is_set():
                window = find_roblox_window()
                if window is None:
                    status("waiting")
                    stop_event.wait(0.1)
                    continue
                status("scanning")

                region = self.detector.capture_region(window)
                letters = self.detector.read_letters(*self.detector.grab(region))

                if not letters:
                    last_typed = None  # screen cleared, so the same letters can be typed again
                    stop_event.wait(0.015)  # a scan takes ~10ms, so check again quickly
                    continue
                if len(letters) < SLOTS:
                    # The next letters may still be fading in. Look again so we don't type a
                    # partial batch and leave a letter behind.
                    stop_event.wait(RESCAN_DELAY)
                    again = self.detector.read_letters(*self.detector.grab(region))
                    if len(again) > len(letters) and again[:len(letters)] == letters:
                        log.debug("Rescan found more letters: %d -> %d", len(letters), len(again))
                        letters = again
                if letters == last_typed:
                    held = time.monotonic() - last_typed_at
                    if held < REPEAT_GUARD_SECONDS:
                        stop_event.wait(0.02)
                        continue
                    log.info("%s still showing %.0fms after typing; typing it again",
                             "".join(l.key.upper() for l in letters), held * 1000)

                # Purple (random) letters are shown in brackets, e.g. C X [V] Z
                log.info("Typing %s", " ".join(f"[{l.key.upper()}]" if l.purple else l.key.upper()
                                               for l in letters))
                if self.get_human_mode():
                    self._type_like_human(letters, stop_event)
                else:
                    for letter in letters:
                        if stop_event.is_set():
                            break
                        pd.press(letter.key)
                        stop_event.wait(self.get_delay())
                last_typed, last_typed_at = letters, time.monotonic()
                self.on_event("typed", letters)
        except Exception as e:
            log.exception("Macro crashed")
            self.on_event("error", (stop_event, f"{type(e).__name__}: {e}"))
        finally:
            log.info("Macro stopped")


# --- GUI ---
BG = "#1c1c1c"
SURFACE = "#252525"
SURFACE_HI = "#2f2f2f"
BORDER = "#383838"
ORANGE = "#ffa500"
ORANGE_HOVER = "#ffb733"
ORANGE_DIM = "#5c3d0a"
TEXT = "#f2f2f2"
MUTED = "#8f8f8f"
ERROR = "#e5534b"

if IS_WINDOWS:
    FONT, FONT_BOLD, FONT_MONO = "Segoe UI", "Segoe UI Semibold", "Consolas"
elif platform.system() == "Darwin":
    FONT, FONT_BOLD, FONT_MONO = "Helvetica Neue", "Helvetica Neue", "Menlo"
else:
    FONT, FONT_BOLD, FONT_MONO = "DejaVu Sans", "DejaVu Sans", "DejaVu Sans Mono"


class FlatButton(tk.Label):
    """A flat, hoverable button. tk.Button ignores colours on macOS, so this is a Label."""

    def __init__(self, master, text, command, bg, fg, hover_bg, font, padx=12, pady=6, **kw):
        super().__init__(master, text=text, bg=bg, fg=fg, font=font, padx=padx, pady=pady,
                         cursor="hand2", **kw)
        self.command = command
        self.colors = (bg, hover_bg)
        self.bind("<Enter>", lambda e: self.config(bg=self.colors[1]))
        self.bind("<Leave>", lambda e: self.config(bg=self.colors[0]))
        self.bind("<Button-1>", self._click)

    def _click(self, _event):
        self.focus_set()  # takes focus off the delay box so it saves
        self.command()

    def set_style(self, text, bg, fg, hover_bg):
        self.colors = (bg, hover_bg)
        hovered = self.winfo_containing(*self.winfo_pointerxy()) is self
        self.config(text=text, fg=fg, bg=hover_bg if hovered else bg)


class Toggle(tk.Label):
    """A small on/off switch. Tk's canvas shapes have jagged edges on Windows, so the
    switch is drawn with Pillow at 4x size and scaled down for smooth, anti-aliased edges."""

    WIDTH, HEIGHT, SUPERSAMPLE = 40, 22, 4
    _images = {}  # True/False -> PhotoImage, shared by every toggle

    def __init__(self, master, value, command):
        super().__init__(master, bg=SURFACE, bd=0, cursor="hand2")
        self.value = value
        self.command = command
        self.bind("<Button-1>", self._flip)
        self._draw()

    @classmethod
    def _image(cls, on):
        if on not in cls._images:
            s = cls.SUPERSAMPLE
            w, h = cls.WIDTH * s, cls.HEIGHT * s
            image = Image.new("RGB", (w, h), SURFACE)
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=ORANGE if on else BORDER)
            inset = 3 * s
            knob = h - 2 * inset
            x = w - inset - knob if on else inset
            draw.ellipse((x, inset, x + knob, inset + knob), fill=TEXT if on else MUTED)
            image = image.resize((cls.WIDTH, cls.HEIGHT), Image.LANCZOS)
            cls._images[on] = ImageTk.PhotoImage(image)
        return cls._images[on]

    def _flip(self, _event=None):
        self.value = not self.value
        self._draw()
        self.command(self.value)

    def _draw(self):
        self.config(image=self._image(self.value))


class StatusOverlay:
    """A small always-on-top badge showing whether the macro is on. Shown only over Roblox."""

    DEFAULT_POS = (24, 80)
    # Windows extended window styles
    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT = 0x20        # clicks pass through to the game
    WS_EX_TOOLWINDOW = 0x80         # no taskbar or Alt+Tab entry
    WS_EX_LAYERED = 0x80000
    WS_EX_NOACTIVATE = 0x08000000   # clicking it doesn't take focus from Roblox

    def __init__(self, root, settings, on_moved):
        self.settings = settings
        self.on_moved = on_moved
        self.locked = settings.overlay_locked
        self.visible = None
        self._drag_offset = None

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-alpha", 0.92)
        except tk.TclError:
            pass
        self.win.configure(bg=BORDER)  # shows as a 1px border around the inner frame
        inner = tk.Frame(self.win, bg=BG, padx=10, pady=4)
        inner.pack(padx=1, pady=1)
        self.dot = tk.Label(inner, text="●", bg=BG, font=(FONT, 10))
        self.dot.pack(side="left")
        self.label = tk.Label(inner, bg=BG, font=(FONT_BOLD, 10, "bold"))
        self.label.pack(side="left", padx=(5, 0))
        self.widgets = (self.win, inner, self.dot, self.label)
        for widget in self.widgets:
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._end_drag)

        x, y = self._valid_pos(settings.overlay_pos)
        self.win.geometry(f"+{x}+{y}")
        self.win.update_idletasks()

        self.hwnd = None
        if IS_WINDOWS:
            import ctypes
            self.user32 = ctypes.windll.user32
            self.hwnd = self.user32.GetAncestor(self.win.winfo_id(), 2)  # GA_ROOT
            # Keep the overlay out of recordings and screenshots (Medal, OBS, Print Screen, Snipping
            # Tool) while it stays visible on screen. Needs Windows 10 version 2004 or newer.
            if not self.user32.SetWindowDisplayAffinity(self.hwnd, 0x11):  # WDA_EXCLUDEFROMCAPTURE
                log.warning("Couldn't hide the overlay from screen capture (needs Windows 10 2004+)")
        self.set_running(False)
        self.set_locked(self.locked)
        self.set_visible(False)

    def _valid_pos(self, pos):
        """The saved position, or the default if it's missing or now off-screen."""
        if pos is None:
            return self.DEFAULT_POS
        if IS_WINDOWS:
            import ctypes
            metrics = ctypes.windll.user32.GetSystemMetrics
            left, top = metrics(76), metrics(77)  # virtual screen covering all monitors
            right, bottom = left + metrics(78), top + metrics(79)
            if not (left <= pos[0] < right - 20 and top <= pos[1] < bottom - 20):
                return self.DEFAULT_POS
        return pos

    def set_running(self, running):
        self.dot.config(fg=ORANGE if running else MUTED)
        self.label.config(text="Autowisp: On" if running else "Autowisp: Off", fg=TEXT if running else MUTED)
        self.win.configure(bg=ORANGE if running else BORDER)
        self.win.update_idletasks()  # redraw now rather than when Tk next gets round to it

    def set_locked(self, locked):
        self.locked = locked
        for widget in self.widgets:
            widget.config(cursor="" if locked else "fleur")
        if self.hwnd:
            style = self.user32.GetWindowLongW(self.hwnd, self.GWL_EXSTYLE)
            style |= self.WS_EX_LAYERED | self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE
            if locked:
                style |= self.WS_EX_TRANSPARENT
            else:
                style &= ~self.WS_EX_TRANSPARENT
            self.user32.SetWindowLongW(self.hwnd, self.GWL_EXSTYLE, style)

    def set_visible(self, visible):
        if visible == self.visible:
            return
        self.visible = visible
        if self.hwnd:
            # Show without activating, so Roblox keeps keyboard focus
            if visible:
                self.user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
                # HWND_TOPMOST, flags: SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
                self.user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)
            else:
                self.user32.ShowWindow(self.hwnd, 0)  # SW_HIDE
        elif visible:
            self.win.deiconify()
            self.win.lift()
        else:
            self.win.withdraw()

    def _start_drag(self, event):
        if not self.locked:
            self._drag_offset = (event.x_root - self.win.winfo_x(), event.y_root - self.win.winfo_y())

    def _drag(self, event):
        if self._drag_offset is not None:
            dx, dy = self._drag_offset
            self.win.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")
            self.win.update_idletasks()

    def _end_drag(self, _event):
        if self._drag_offset is not None:
            self._drag_offset = None
            self.on_moved((self.win.winfo_x(), self.win.winfo_y()))


class App:
    def __init__(self):
        self.settings = Settings(app_data_dir() / "settings.ini")
        setup_logging(self.settings.debug)

        self.events = queue.Queue()  # work handed from background threads to the Tk thread
        self.capturing_keybind = False

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        try:
            self.root.iconbitmap(resource_path("Cinder_Icon.ico"))
        except Exception as e:
            log.debug("Icon load failed: %s", e)  # expected on macOS

        try:
            self.detector = Detector(resource_path("Letters"))
        except Exception as e:
            log.exception("Couldn't load templates")
            self._show_fatal(f"Couldn't load the letter templates.\n\n{e}")
            return

        self.runner = MacroRunner(self.detector, lambda: self.settings.delay,
                                  lambda: self.settings.human_mode,
                                  lambda kind, data: self.events.put((kind, data)))
        self._build_ui()
        try:
            self.overlay = StatusOverlay(self.root, self.settings, self._overlay_moved)
        except Exception:
            self.overlay = None  # the macro still works without it
            log.exception("Couldn't create the overlay")
        self._start_hotkey_listener()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_events()
        self._overlay_loop()

    # ----- layout -----
    def _build_ui(self):
        outer = tk.Frame(self.root, bg=BG, padx=20, pady=18)
        outer.pack(fill="both", expand=True)

        # Header
        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x")
        title_box = tk.Frame(header, bg=BG)
        title_box.pack(side="left")
        tk.Label(title_box, text="Cinder Wisp", fg=ORANGE, bg=BG,
                 font=(FONT_BOLD, 20, "bold")).pack(anchor="w")
        tk.Label(title_box, text="RITUAL CAST MACRO", fg=MUTED, bg=BG,
                 font=(FONT, 8, "bold")).pack(anchor="w")

        self.status_pill = tk.Frame(header, bg=SURFACE, padx=10, pady=4)
        self.status_pill.pack(side="right", anchor="n", pady=(6, 0))
        self.status_dot = tk.Label(self.status_pill, text="●", bg=SURFACE, fg=MUTED, font=(FONT, 9))
        self.status_dot.pack(side="left")
        self.status_label = tk.Label(self.status_pill, text="Idle", bg=SURFACE, fg=TEXT, font=(FONT, 9))
        self.status_label.pack(side="left", padx=(4, 0))

        # Start / stop
        self.start_button = FlatButton(outer, "Start", self.toggle_macro, bg=ORANGE, fg=BG,
                                       hover_bg=ORANGE_HOVER, font=(FONT_BOLD, 13, "bold"), pady=10)
        self.start_button.pack(fill="x", pady=(18, 0))
        self.detail_label = tk.Label(outer, text="Press Start, then focus Roblox.", fg=MUTED, bg=BG,
                                     font=(FONT, 9), wraplength=280)
        self.detail_label.pack(pady=(8, 0))

        # Settings card
        settings = self._card(outer, pady=(12, 0))
        tk.Label(settings, text="SETTINGS", fg=MUTED, bg=SURFACE, font=(FONT, 8, "bold")).pack(anchor="w")

        row = self._setting_row(settings, "Start / stop hotkey")
        self.keybind_button = FlatButton(row, display_key(self.settings.keybind), self.begin_keybind_capture,
                                         bg=SURFACE_HI, fg=ORANGE, hover_bg=BORDER,
                                         font=(FONT_MONO, 10, "bold"), padx=10, pady=3,
                                         highlightthickness=1, highlightbackground=BORDER)
        self.keybind_button.pack(side="right")

        row = self._setting_row(settings, "Delay between keys")
        tk.Label(row, text="sec", fg=MUTED, bg=SURFACE, font=(FONT, 9)).pack(side="right", padx=(6, 0))
        FlatButton(row, "+", lambda: self.nudge_delay(0.05), bg=SURFACE_HI, fg=TEXT, hover_bg=BORDER,
                   font=(FONT_BOLD, 10, "bold"), padx=8, pady=1).pack(side="right")
        self.delay_var = tk.StringVar(value=f"{self.settings.delay:g}")
        self.delay_entry = tk.Entry(row, textvariable=self.delay_var, width=5, justify="center",
                                    bg=SURFACE_HI, fg=TEXT, insertbackground=ORANGE, relief="flat",
                                    font=(FONT_MONO, 10), highlightthickness=1,
                                    highlightbackground=BORDER, highlightcolor=ORANGE)
        self.delay_entry.pack(side="right", padx=4, ipady=3)
        self.delay_entry.bind("<Return>", lambda e: self.apply_delay())
        self.delay_entry.bind("<FocusOut>", lambda e: self.apply_delay())
        FlatButton(row, "−", lambda: self.nudge_delay(-0.05), bg=SURFACE_HI, fg=TEXT, hover_bg=BORDER,
                   font=(FONT_BOLD, 10, "bold"), padx=8, pady=1).pack(side="right")

        row = self._setting_row(settings, "Human mode")
        Toggle(row, self.settings.human_mode, self.set_human_mode).pack(side="right")

        row = self._setting_row(settings, "Status overlay in Roblox")
        Toggle(row, self.settings.overlay, self.set_overlay).pack(side="right")

        row = self._setting_row(settings, "Lock overlay position")
        Toggle(row, self.settings.overlay_locked, self.set_overlay_locked).pack(side="right")

        row = self._setting_row(settings, "Debug logging")
        Toggle(row, self.settings.debug, self.set_debug).pack(side="right")

        # Footer
        self.hint_label = tk.Label(outer, fg=MUTED, bg=BG, font=(FONT, 8))
        self.hint_label.pack(pady=(12, 0))
        self._update_hint()

    def _card(self, parent, pady):
        card = tk.Frame(parent, bg=SURFACE, padx=16, pady=14,
                        highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x", pady=pady)
        return card

    def _setting_row(self, parent, label):
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x", pady=(10, 0))
        tk.Label(row, text=label, fg=TEXT, bg=SURFACE, font=(FONT, 10)).pack(side="left")
        return row

    def _show_fatal(self, message):
        tk.Label(self.root, text=message, fg=ERROR, bg=BG, font=(FONT, 10), wraplength=320,
                 justify="left", padx=20, pady=20).pack()

    # ----- status display -----
    def set_status(self, text, color):
        self.status_label.config(text=text)
        self.status_dot.config(fg=color)

    def _update_hint(self):
        self.hint_label.config(text=f"Press {display_key(self.settings.keybind)} anywhere to start or stop")

    # ----- actions -----
    def toggle_macro(self):
        if self.runner.running:
            self.runner.stop()
            self.start_button.set_style("Start", ORANGE, BG, ORANGE_HOVER)
            self.set_status("Idle", MUTED)
            self.detail_label.config(text="Stopped.", fg=MUTED)
        else:
            self.runner.start()
            self.start_button.set_style("Stop", SURFACE_HI, ORANGE, BORDER)
            self.set_status("Starting", ORANGE)
        if self.overlay is not None:
            self.overlay.set_running(self.runner.running)

    def set_human_mode(self, enabled):
        self.settings.human_mode = enabled
        self.settings.save()
        self.detail_label.config(
            text="Human mode on: varied reaction time, key gaps and key holds." if enabled
            else "Human mode off: fastest, fixed timing.", fg=MUTED)

    def set_overlay(self, enabled):
        self.settings.overlay = enabled
        self.settings.save()
        self._update_overlay()

    def set_overlay_locked(self, locked):
        self.settings.overlay_locked = locked
        self.settings.save()
        if self.overlay is not None:
            self.overlay.set_locked(locked)

    def _overlay_moved(self, pos):
        self.settings.overlay_pos = pos
        self.settings.save()

    def _update_overlay(self):
        """Show the overlay only while it's enabled and Roblox (or the overlay) is focused."""
        if self.overlay is None:
            return
        show = False
        if self.settings.overlay:
            try:
                if IS_WINDOWS:
                    handle = _user32.GetForegroundWindow()
                    show = bool(handle) and (handle == self.overlay.hwnd or is_roblox_handle(handle))
                else:
                    active = pywinctl.getActiveWindow()
                    show = active is not None and is_roblox(active)
            except Exception:
                log.debug("Couldn't check the active window", exc_info=True)
        self.overlay.set_visible(show)

    def _overlay_loop(self):
        self._update_overlay()
        self.root.after(50, self._overlay_loop)

    def begin_keybind_capture(self):
        self.capturing_keybind = True
        self.keybind_button.config(text="Press a key…", highlightbackground=ORANGE)
        self.detail_label.config(text="Press the new hotkey, or Esc to cancel.", fg=MUTED)

    def end_keybind_capture(self):
        self.capturing_keybind = False
        self.keybind_button.config(text=display_key(self.settings.keybind), highlightbackground=BORDER)

    def handle_key(self, name):
        if self.capturing_keybind:
            if name == "esc":
                self.end_keybind_capture()
                self.detail_label.config(text="Hotkey unchanged.", fg=MUTED)
            elif name in MACRO_KEYS:
                self.detail_label.config(text=f"{display_key(name)} is typed by the macro. Pick another key.",
                                         fg=ERROR)
            else:
                self.settings.keybind = name
                self.settings.save()
                self.end_keybind_capture()
                self._update_hint()
                self.detail_label.config(text=f"Hotkey set to {display_key(name)}.", fg=MUTED)
            return
        if self.root.focus_get() is self.delay_entry:
            return  # typing a delay value, not pressing the hotkey
        if name == self.settings.keybind:
            self.toggle_macro()

    def apply_delay(self):
        try:
            value = min(5.0, max(0.0, float(self.delay_var.get())))
        except ValueError:
            value = self.settings.delay
            self.detail_label.config(text="Delay must be a number of seconds.", fg=ERROR)
        self.settings.delay = round(value, 3)
        self.settings.save()
        self.delay_var.set(f"{self.settings.delay:g}")

    def nudge_delay(self, step):
        self.apply_delay()
        self.delay_var.set(f"{max(0.0, round(self.settings.delay + step, 3)):g}")
        self.apply_delay()

    def set_debug(self, enabled):
        self.settings.debug = enabled
        self.settings.save()
        set_debug_logging(enabled)
        log.info("Debug logging %s", "on" if enabled else "off")

    # ----- background events -----
    def _start_hotkey_listener(self):
        def on_press(key):
            name = pynput_key_name(key)
            if name is not None:
                self.events.put(("key", name))
        try:
            self.listener = keyboard.Listener(on_press=on_press, daemon=True)
            self.listener.start()
        except Exception as e:
            self.listener = None
            log.exception("Hotkey listener failed")
            self.hint_label.config(text=f"Global hotkey unavailable ({e}). Use the button.", fg=ERROR)

    def _poll_events(self):
        while True:
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(kind, data)
        self.root.after(10, self._poll_events)

    def _handle_event(self, kind, data):
        if kind == "key":
            self.handle_key(data)
        elif not self.runner.running and kind != "error":
            return  # late update from a run that was already stopped
        elif kind == "waiting":
            self.set_status("Waiting for Roblox", MUTED)
            self.detail_label.config(text="Focus the Roblox window to begin.", fg=MUTED)
        elif kind == "scanning":
            self.set_status("Running", ORANGE)
            self.detail_label.config(text="Watching for cast letters…", fg=MUTED)
        elif kind == "error":
            stop_event, message = data
            if self.runner.owns(stop_event):  # ignore errors from runs already replaced
                self.toggle_macro()
                self.set_status("Error", ERROR)
                self.detail_label.config(text=message, fg=ERROR)

    def _on_close(self):
        self.runner.stop()
        if getattr(self, "listener", None) is not None:
            self.listener.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()
