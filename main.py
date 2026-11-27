"""AutoWisp: reads Deepwoken ritual cast letters off the screen and types them."""
import configparser
import json
import logging
import os
import platform
import queue
import random
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from tkinter import messagebox
from pathlib import Path
from typing import NamedTuple

import cv2 as cv
import mss
import numpy as np
import pywinctl
from PIL import Image, ImageDraw, ImageFont, ImageTk
from pynput import keyboard

try:
    import pystray  # tray icon; the macro still works without it
except Exception:
    pystray = None

IS_WINDOWS = platform.system() == "Windows"
if IS_WINDOWS:
    import pydirectinput as pd
else:
    import pyautogui as pd  # fallback for macOS/Linux

# Built-in wait after each key press. Raise it if the game starts missing keys.
pd.PAUSE = 0.015

APP_NAME = "AutoWisp"
WINDOW_TITLE = "AutoWisp"
APP_VERSION = "1.4.0"  # bump this for each GitHub release (tagged v1.4.0 etc.)
RELEASES_API = "https://api.github.com/repos/irefusetosleep/autowisp_v2/releases/latest"
RELEASES_PAGE = "https://github.com/irefusetosleep/autowisp_v2/releases"

# --- Detection tuning ---
# The ritual cast letters are drawn at a fixed pixel size and position: centred
# horizontally and a fixed distance below the top of the Roblox window, whatever the
# resolution. So the search box is a fixed size, placed relative to the window.
CAST_OFFSET_X = -116         # left edge, in pixels from the window's horizontal centre
CAST_OFFSET_Y = 170          # top edge, in pixels below the top of the window's content
CAST_WIDTH = 307             # split into 4 equal slots, one letter each
CAST_HEIGHT = 190
BINARIZE_THRESH = 140
SLOTS = 4                    # a ritual cast shows 1-4 letters

# --- Reading the letters ---
# The cast shows the player's own 4 cast keys, so each letter on screen only has to be told
# apart from those 4. It's compared with each key drawn in a spread of fonts (and the game's own
# letter shapes where we have them). Scores are shape similarity, 1.0 = identical.
DEFAULT_CAST_KEYS = ("c", "v", "x", "z")
# Keys a cast can be bound to: anything typed without Shift whose symbol is letter-sized on
# screen. (Small marks like , . ' - would be indistinguishable from specks in the scenery.)
CAST_KEY_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789[]"
# Learned shapes are saved as <name>_<n>.png; [ and ] have special meanings in file searches
KEY_FILE_NAMES = {"[": "lbracket", "]": "rbracket"}
GLYPH_SIZE = 32              # letters are compared at this size (px square)
RATIO_PENALTY = 0.5          # how much a different width/height ratio counts against a match
FONT_FOLDERS = ("C:/Windows/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
                "/usr/share/fonts/truetype/dejavu")
# Serif faces only, like the game's cast font. (Sans-serif I and l are plain bars, which any
# thin bright line in the scenery would match.)
REFERENCE_FONTS = ("georgiab.ttf", "georgia.ttf", "timesbd.ttf", "times.ttf", "palab.ttf", "pala.ttf",
                   "cambriab.ttf", "cambriab.ttc", "constan.ttf", "constanb.ttf", "sylfaen.ttf",
                   "Georgia Bold.ttf", "Georgia.ttf", "Times New Roman Bold.ttf", "Times New Roman.ttf",
                   "DejaVuSerif-Bold.ttf", "DejaVuSerif.ttf")
# A letter on screen is a bright shape of about this size (real cast letters are ~19-24 x 23-25px;
# wide letters like M and W reach ~40px in serif faces, Q/J tails add up to ~10px of height, and
# the game's [ and ] are 7 x 24px)
GLYPH_MIN_HEIGHT, GLYPH_MAX_HEIGHT = 20, 34
GLYPH_MIN_WIDTH, GLYPH_MAX_WIDTH = 5, 42
# ...and one of two colours: near-white (measured ~225,230,237 BGR, channels within ~12 of each
# other) or the cast's purple (~227,139,151 BGR: ~88 more blue than green). Bluish fog, sky and
# scenery that happen to be letter-shaped are neither.
LETTER_MIN_BRIGHTNESS = 190  # brightest channel, averaged over the letter
WHITE_MAX_SPREAD = 25        # max difference between channels for "white"
GLYPH_MIN_AREA = 25
GLYPH_MARGIN = 4             # px around a letter checked for being dark
MATCH_THRESHOLD = 0.75       # a full cast of 4: each letter, against the fonts or game shapes
MIN_LEAD = 0.02              # how far ahead of the next-best key the best one must be
# A read of fewer than 4 letters (a letter left on its own, or a cast still fading in) has no
# full cast around it to confirm it, and is where false positives come from. So each letter
# must closely match the game's own shape of that key: real letters score 0.96-0.98 against it.
GAME_MATCH_THRESHOLD = 0.9
# Learning the game's shapes for a new key: only from a full cast, and only letters that are a
# sure match (real ones score 0.87+ against the fonts; letters that aren't the player's keys
# ≤0.83) and clearly ahead of the other keys (similar pairs like E/F wait for a clearer read)
LEARN_THRESHOLD = 0.86
LEARN_MIN_LEAD = 0.05
LEARN_SKIP_IF_MATCHES = 0.95  # already have a shape this close: don't store another
LEARN_MAX_PER_KEY = 4         # e.g. white and purple versions
# A real letter is a bright shape on a darker backdrop, so almost none of the pixels around it
# are lit (measured 0%). Bright fog, sky and edges that happen to look letter-shaped light up
# 25%+ of them, so a shape lighting more than this share is rejected.
MAX_BACKGROUND_LIT = 0.12
# Purple letters measure ~88 more blue than green, white ones ~0, bluish fog/sky up to ~50
PURPLE_BLUE_OVER_GREEN = 60
# If the exact same letters are still showing right after typing them, the game may just not have
# updated yet, so wait this long before assuming the keys didn't register and typing them again
REPEAT_GUARD_SECONDS = 0.2
RESCAN_DELAY = 0.02          # when a scan finds under 4 letters, look again this soon (fading in)

# --- Ping-based delay ---
# Roblox servers don't answer pings, so the player enters the ping Roblox shows them.
# Each key has to reach the server and the updated cast come back (a full ping), plus the
# game needs a fixed amount of time to handle the key whatever the ping.
# delay = base + ping * factor. Measured: at 33ms ping the fastest working delay is 0.1s.
PING_DELAY_BASE = 0.07
PING_DELAY_FACTOR = 1.0
MAX_PING_MS = 1000

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


OLD_APP_NAME = "CinderWispMacro"  # the macro's settings folder before it was renamed AutoWisp


def app_data_dir() -> Path:
    base = Path(os.getenv("APPDATA") or Path.home() / ".config")
    folder = base / APP_NAME
    if not folder.exists():
        _migrate_old_settings(base / OLD_APP_NAME, folder)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _migrate_old_settings(old: Path, new: Path):
    """Carry settings (and logs) over from the old CinderWispMacro folder, once."""
    if not old.is_dir():
        return
    try:
        old.rename(new)  # moves everything
    except OSError:
        # The old folder is in use (e.g. an old copy is still running), so just copy the settings
        try:
            new.mkdir(parents=True, exist_ok=True)
            if (old / "settings.ini").exists():
                (new / "settings.ini").write_bytes((old / "settings.ini").read_bytes())
        except OSError:
            pass


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
        self._load()

    def reset(self):
        """Put every setting back to its default."""
        self.config.remove_section("SETTINGS")
        self.config.add_section("SETTINGS")
        self._load()

    def _load(self):
        section = self.config["SETTINGS"]
        self.keybind = normalize_key(section.get("keybind", "")) or self.DEFAULTS["keybind"]
        try:
            self.delay = max(0.0, float(section.get("delay", self.DEFAULTS["delay"])))
        except ValueError:
            self.delay = float(self.DEFAULTS["delay"])
        self.debug = self._bool(section, "debug", False)
        self.human_mode = self._bool(section, "human_mode", False)
        self.sound = self._bool(section, "sound", True)  # False = muted
        try:
            self.volume = min(100, max(0, int(float(section.get("volume", "60")))))
        except ValueError:
            self.volume = 60
        self.tray = self._bool(section, "minimize_to_tray", False)
        self.start_minimized = self._bool(section, "start_minimized", False)
        keys = [c for c in section.get("cast_keys", "").strip().lower() if c in CAST_KEY_CHARS]
        valid = len(keys) == SLOTS and len(set(keys)) == SLOTS
        self.cast_keys = keys if valid else list(DEFAULT_CAST_KEYS)
        self.delay_mode = section.get("delay_mode", "fixed").strip().lower()
        if self.delay_mode not in ("fixed", "ping"):
            self.delay_mode = "fixed"
        try:
            self.ping_ms = min(MAX_PING_MS, max(0, int(float(section.get("ping_ms", "100")))))
        except ValueError:
            self.ping_ms = 100
        self.overlay = self._bool(section, "overlay", False)
        self.overlay_locked = self._bool(section, "overlay_locked", False)
        try:
            self.overlay_pos = (int(section["overlay_x"]), int(section["overlay_y"]))
        except (KeyError, ValueError):
            self.overlay_pos = None  # use the default spot
        self.save()

    def effective_delay(self):
        """Seconds to wait between keys: the fixed value, or worked out from the player's ping."""
        if self.delay_mode == "ping":
            return round(PING_DELAY_BASE + self.ping_ms / 1000 * PING_DELAY_FACTOR, 3)
        return self.delay

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
        section["sound"] = "true" if self.sound else "false"
        section["volume"] = str(self.volume)
        section["minimize_to_tray"] = "true" if self.tray else "false"
        section["start_minimized"] = "true" if self.start_minimized else "false"
        section["cast_keys"] = "".join(self.cast_keys)
        section["delay_mode"] = self.delay_mode
        section["ping_ms"] = str(self.ping_ms)
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


def letter_colour(pixels):
    """'white' or 'purple' if these pixels are a cast letter's colour, otherwise None.
    `pixels` are BGR(A) colours from the letter's shape."""
    if len(pixels) < 10:
        return None
    mean = pixels[:, :3].astype(float).mean(axis=0)
    if mean.max() < LETTER_MIN_BRIGHTNESS:
        return None
    if mean[0] - mean[1] > PURPLE_BLUE_OVER_GREEN:
        return "purple"
    if mean.max() - mean.min() <= WHITE_MAX_SPREAD:
        return "white"
    return None


def window_frame(window):
    """(left, top, width, height) of the window's content area, without the title bar."""
    try:
        frame = window.getClientFrame()
        return frame.left, frame.top, frame.right - frame.left, frame.bottom - frame.top
    except Exception:
        return window.left, window.top, window.width, window.height


def glyph_vector(binary):
    """A letter shape as a fixed-size, unit-length vector (tight crop, fitted into a square
    keeping its proportions, lightly blurred), plus its width/height ratio."""
    ys, xs = np.nonzero(binary)
    if len(xs) == 0:
        return None, 1.0
    crop = binary[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = crop.shape
    scale = (GLYPH_SIZE - 2) / max(h, w)
    crop = cv.resize(crop, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv.INTER_AREA)
    square = np.zeros((GLYPH_SIZE, GLYPH_SIZE), np.float32)
    y0, x0 = (GLYPH_SIZE - crop.shape[0]) // 2, (GLYPH_SIZE - crop.shape[1]) // 2
    square[y0:y0 + crop.shape[0], x0:x0 + crop.shape[1]] = crop / 255.0
    vector = cv.GaussianBlur(square, (3, 3), 0).ravel()
    norm = np.linalg.norm(vector)
    return (vector / norm if norm else None), w / h


def render_letter(letter, font):
    """A letter drawn white on black in one font, thresholded like a screen capture."""
    image = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(image).text((12, 6), letter, font=font, fill=255)
    return (np.asarray(image) > BINARIZE_THRESH).astype(np.uint8) * 255


def reference_fonts():
    """Fonts to draw reference letters in: a spread of serif faces, so a letter is recognised
    whatever the exact shape of the game's font."""
    fonts = []
    for folder in FONT_FOLDERS:
        for name in REFERENCE_FONTS:
            path = Path(folder) / name
            if path.exists():
                try:
                    fonts.append(ImageFont.truetype(str(path), 30))
                except OSError:
                    pass
    if not fonts:
        try:
            fonts.append(ImageFont.load_default(size=30))
        except TypeError:  # older Pillow without scalable default font
            pass
    return fonts


class Detector:
    """Reads the cast's letters. The player's 4 cast keys are the only letters that can appear,
    so each letter on screen is identified by which of those 4 it looks most like.

    Two kinds of reference shapes are used: each key drawn in a spread of fonts (works for any
    letter straight away), and the game's own letter shapes, which match far more closely. The
    game shapes for C/V/X/Z ship with the app; for other keys they're learned from the first
    casts that are read with confidence, and saved so they're there next time."""

    def __init__(self, template_folder: Path, keys, learned_folder: Path = None):
        self.builtin_shapes = self._load_shapes(template_folder)
        self.learned_folder = learned_folder
        self.learned_shapes = self._load_shapes(learned_folder) if learned_folder else {}
        self.fonts = reference_fonts()
        self._font_cache = {}  # key -> font reference vectors (drawing them is the slow part)
        self._lock = threading.Lock()
        log.info("Letter references: %d fonts; game shapes for %s; learned for %s", len(self.fonts),
                 " ".join(sorted(self.builtin_shapes)).upper() or "none",
                 " ".join(sorted(self.learned_shapes)).upper() or "none")
        self._local = threading.local()
        self.set_keys(keys)

    @staticmethod
    def _load_shapes(folder):
        shapes = {}
        if folder is None or not folder.is_dir():
            return shapes
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.suffix.lower() in TEMPLATE_EXTENSIONS:
                image = cv.imread(str(path), cv.IMREAD_GRAYSCALE)
                if image is not None:
                    name = path.stem.split("_")[0].lower()
                    letter = {v: k for k, v in KEY_FILE_NAMES.items()}.get(name, name)
                    shapes.setdefault(letter, []).append(image)
        return shapes

    def set_keys(self, keys):
        """Build the reference shapes for the player's cast keys. Learned shapes for letters
        that are no longer cast keys are forgotten."""
        keys = [k.lower() for k in keys]
        with self._lock:
            for letter in list(self.learned_shapes):
                if letter not in keys:
                    del self.learned_shapes[letter]
                    self._delete_learned_files(letter)
            vectors, ratios, owners, is_game = [], [], [], []
            for index, key in enumerate(keys):
                if key not in self._font_cache:
                    self._font_cache[key] = [glyph_vector(render_letter(key.upper(), font))
                                             for font in self.fonts]
                refs = [(v, r, False) for v, r in self._font_cache[key]]
                game = self.builtin_shapes.get(key, []) + self.learned_shapes.get(key, [])
                refs += [(*glyph_vector(shape), True) for shape in game]
                for vector, ratio, game_shape in refs:
                    if vector is not None:
                        vectors.append(vector)
                        ratios.append(ratio)
                        owners.append(index)
                        is_game.append(game_shape)
            if not vectors:
                raise RuntimeError("Couldn't build any letter references")
            # Swap in all at once, so the macro thread never sees a half-built set
            self._refs = (keys, np.array(vectors), np.log(np.array(ratios)), np.array(owners),
                          np.array(is_game))

    def classify(self, glyph):
        """(key, score, lead over the next-best key, score against the game's own shapes of
        that key, or 0 if we have none yet) for a letter shape."""
        keys, vectors, log_ratios, owners, is_game = self._refs
        vector, ratio = glyph_vector(glyph)
        if vector is None:
            return None, 0.0, 0.0, 0.0
        scores = vectors @ vector - RATIO_PENALTY * np.abs(log_ratios - np.log(ratio))
        per_key = np.full(len(keys), -9.0)
        np.maximum.at(per_key, owners, scores)
        order = np.argsort(per_key)[::-1]
        best = order[0]
        lead = per_key[best] - per_key[order[1]] if len(keys) > 1 else 1.0
        game_scores = scores[(owners == best) & is_game]
        game_score = float(game_scores.max()) if len(game_scores) else 0.0
        return keys[best], float(per_key[best]), float(lead), game_score

    def _learn(self, found):
        """Remember the game's shapes from a confidently read full cast: (key, glyph, game_score)
        for each letter. Only shapes unlike any we already have are kept."""
        changed = False
        with self._lock:
            for key, glyph, game_score in found:
                have = self.builtin_shapes.get(key, []) + self.learned_shapes.get(key, [])
                if game_score >= LEARN_SKIP_IF_MATCHES or len(self.learned_shapes.get(key, [])) >= LEARN_MAX_PER_KEY:
                    continue
                self.learned_shapes.setdefault(key, []).append(glyph.copy())
                changed = True
                log.info("Learned the game's %s (%d shape%s now)", key.upper(), len(have) + 1,
                         "" if not have else "s")
                if self.learned_folder is not None:
                    try:
                        self.learned_folder.mkdir(parents=True, exist_ok=True)
                        n = len(self.learned_shapes[key])
                        name = KEY_FILE_NAMES.get(key, key)
                        cv.imwrite(str(self.learned_folder / f"{name}_{n}.png"), glyph)
                    except Exception:
                        log.exception("Couldn't save a learned letter shape")
        if changed:
            self.set_keys(self._refs[0])

    def _delete_learned_files(self, letter):
        if self.learned_folder is None or not self.learned_folder.is_dir():
            return
        name = KEY_FILE_NAMES.get(letter, letter)
        for path in self.learned_folder.iterdir():
            if path.name.lower().startswith(f"{name}_") and path.suffix.lower() == ".png":
                try:
                    path.unlink()
                except OSError:
                    pass

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
        letters, details = [], []  # details: (glyph, score, lead, game score) per letter
        for i in range(SLOTS):
            x0 = i * slot_width
            slot = image[:, x0:x0 + slot_width]
            found = self._find_glyph(slot)
            if found is None:
                continue
            glyph_mask, (gx, gy, gw, gh), background_lit = found
            if background_lit > MAX_BACKGROUND_LIT:
                log.debug("Slot %d: rejected a shape - %.0f%% of its surroundings are lit",
                          i + 1, background_lit * 100)
                continue
            glyph = glyph_mask[gy:gy + gh, gx:gx + gw]
            purple = False
            if color is not None:
                kind = letter_colour(color[gy:gy + gh, x0 + gx:x0 + gx + gw][glyph > 0])
                if kind is None:
                    log.debug("Slot %d: rejected a shape - not a cast letter colour", i + 1)
                    continue
                purple = kind == "purple"
            key, score, lead, game_score = self.classify(glyph)
            log.debug("Slot %d: looks like %s (%.2f, lead %.2f, game shape %.2f)",
                      i + 1, key, score, lead, game_score)
            if score < MATCH_THRESHOLD or lead < MIN_LEAD:
                continue
            letters.append(Letter(key, purple))
            details.append((glyph, score, lead, game_score))
        if len(letters) < SLOTS:
            # Fewer than 4 letters is where false positives come from (a full cast is 4), so
            # each one must closely match the game's own shape of that letter. Letters still
            # fading in are picked up on the next scan.
            kept = [(l, d) for l, d in zip(letters, details) if d[3] >= GAME_MATCH_THRESHOLD]
            return [l for l, _ in kept]
        # A full cast: learn the game's shape of each letter that was read with real certainty
        sure = [(l.key, d[0], d[3]) for l, d in zip(letters, details)
                if d[1] >= LEARN_THRESHOLD and d[2] >= LEARN_MIN_LEAD]
        if sure:
            self._learn(sure)
        return letters

    @staticmethod
    def _find_glyph(slot):
        """The letter-sized bright shape in a slot, as (mask of just that shape, its box,
        share of lit pixels around it), or None. Pieces of one letter that the threshold split
        apart (thin strokes of purple letters) are joined back together."""
        count, labels, stats, _ = cv.connectedComponentsWithStats(slot, connectivity=8)
        candidates = [k for k in range(1, count)
                      if stats[k, cv.CC_STAT_AREA] >= GLYPH_MIN_AREA
                      and GLYPH_MIN_HEIGHT <= stats[k, cv.CC_STAT_HEIGHT] <= GLYPH_MAX_HEIGHT
                      and stats[k, cv.CC_STAT_WIDTH] <= GLYPH_MAX_WIDTH]
        if not candidates:
            return None
        main_part = max(candidates, key=lambda k: stats[k, cv.CC_STAT_AREA])
        x, y, w, h = stats[main_part, :4]
        x0, y0, x1, y1 = x, y, x + w, y + h
        parts = [main_part]
        for k in range(1, count):
            if k == main_part or stats[k, cv.CC_STAT_AREA] < 3:
                continue
            kx, ky, kw, kh = stats[k, :4]
            if kx <= x1 + 2 and kx + kw >= x0 - 2 and ky <= y1 + 2 and ky + kh >= y0 - 2:
                nx0, ny0, nx1, ny1 = min(x0, kx), min(y0, ky), max(x1, kx + kw), max(y1, ky + kh)
                if ny1 - ny0 <= GLYPH_MAX_HEIGHT and nx1 - nx0 <= GLYPH_MAX_WIDTH:
                    x0, y0, x1, y1 = nx0, ny0, nx1, ny1
                    parts.append(k)
        if x1 - x0 < GLYPH_MIN_WIDTH:
            return None  # a thin line, not a letter (even a serif I is wider)
        mask = np.isin(labels, parts).astype(np.uint8) * 255
        # How much is lit around the letter that isn't the letter: real cast letters sit on a
        # darker backdrop, bright scenery and text don't
        m = GLYPH_MARGIN
        ax0, ay0 = max(0, x0 - m), max(0, y0 - m)
        ax1, ay1 = min(slot.shape[1], x1 + m), min(slot.shape[0], y1 + m)
        around = (slot[ay0:ay1, ax0:ax1] > 0) & (mask[ay0:ay1, ax0:ax1] == 0)
        ring_area = (ax1 - ax0) * (ay1 - ay0) - (x1 - x0) * (y1 - y0)
        background_lit = around.sum() / max(1, ring_area)
        return mask, (x0, y0, x1 - x0, y1 - y0), float(background_lit)


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
                    # Look again: the next letters may still be fading in (so we don't type a
                    # partial batch and leave a letter behind), and a partial read has to still
                    # be there, since a real cast stays on screen but a passing patch of
                    # background that looked like a letter for one frame doesn't.
                    stop_event.wait(RESCAN_DELAY)
                    again = self.detector.read_letters(*self.detector.grab(region))
                    if len(again) > len(letters) and again[:len(letters)] == letters:
                        log.debug("Rescan found more letters: %d -> %d", len(letters), len(again))
                        letters = again
                    elif again != letters:
                        log.debug("Ignored %s: gone on the second look",
                                  " ".join(l.key.upper() for l in letters))
                        continue
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
BORDER_TEXT = "#5f5f5f"  # very low-key text, e.g. the version number
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


class VolumeSlider(tk.Label):
    """A smooth horizontal slider (0-100), drawn with Pillow like the toggles.
    Drag or click to set it, or use the mouse wheel."""

    WIDTH, HEIGHT, SUPERSAMPLE = 104, 20, 4

    def __init__(self, master, value, enabled, on_change, on_release):
        super().__init__(master, bg=SURFACE, bd=0, cursor="hand2")
        self.value, self.enabled = value, enabled
        self.on_change, self.on_release = on_change, on_release
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda e: self.on_release(self.value))
        self.bind("<MouseWheel>", self._wheel)
        self._draw()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self._draw()

    def _drag(self, event):
        knob = self.HEIGHT // 2
        fraction = (event.x - knob) / (self.WIDTH - 2 * knob)
        self._set(round(min(1.0, max(0.0, fraction)) * 100))

    def _wheel(self, event):
        self._set(min(100, max(0, self.value + (5 if event.delta > 0 else -5))))
        self.on_release(self.value)

    def _set(self, value):
        if value != self.value:
            self.value = value
            self._draw()
            self.on_change(value)

    def _draw(self):
        s = self.SUPERSAMPLE
        w, h = self.WIDTH * s, self.HEIGHT * s
        image = Image.new("RGB", (w, h), SURFACE)
        draw = ImageDraw.Draw(image)
        radius = h // 2 - 2 * s  # knob
        left, right = h // 2, w - h // 2
        track_h = 4 * s
        top = h // 2 - track_h // 2
        x = left + (right - left) * self.value / 100
        fill = ORANGE if self.enabled else MUTED
        draw.rounded_rectangle((left - track_h // 2, top, right + track_h // 2, top + track_h),
                               radius=track_h // 2, fill=BORDER)
        if self.value > 0:
            draw.rounded_rectangle((left - track_h // 2, top, x, top + track_h), radius=track_h // 2, fill=fill)
        draw.ellipse((x - radius, h // 2 - radius, x + radius, h // 2 + radius),
                     fill=TEXT if self.enabled else MUTED)
        self._image = ImageTk.PhotoImage(image.resize((self.WIDTH, self.HEIGHT), Image.LANCZOS))
        self.config(image=self._image)


class SpeakerButton(tk.Label):
    """A speaker icon that mutes / unmutes. Shows a cross when muted."""

    SIZE, SUPERSAMPLE = 20, 4
    _images = {}

    def __init__(self, master, muted, command):
        super().__init__(master, bg=SURFACE, bd=0, cursor="hand2")
        self.muted = muted
        self.command = command
        self.bind("<Button-1>", self._click)
        self._draw()

    @classmethod
    def _image(cls, muted):
        if muted not in cls._images:
            s = cls.SUPERSAMPLE
            n = cls.SIZE * s
            image = Image.new("RGB", (n, n), SURFACE)
            d = ImageDraw.Draw(image)
            color = MUTED if muted else TEXT
            u = n / 20  # one unit on a 20x20 grid
            d.polygon([(2 * u, 7 * u), (6 * u, 7 * u), (11 * u, 3 * u), (11 * u, 17 * u),
                       (6 * u, 13 * u), (2 * u, 13 * u)], fill=color)
            if muted:
                d.line((13.5 * u, 7 * u, 18.5 * u, 13 * u), fill=ERROR, width=int(1.8 * u))
                d.line((13.5 * u, 13 * u, 18.5 * u, 7 * u), fill=ERROR, width=int(1.8 * u))
            else:
                d.arc((8 * u, 6 * u, 15 * u, 14 * u), -50, 50, fill=ORANGE, width=int(1.6 * u))
                d.arc((8 * u, 3 * u, 19 * u, 17 * u), -50, 50, fill=ORANGE, width=int(1.6 * u))
            cls._images[muted] = ImageTk.PhotoImage(image.resize((cls.SIZE, cls.SIZE), Image.LANCZOS))
        return cls._images[muted]

    def _click(self, _event):
        self.muted = not self.muted
        self._draw()
        self.command(self.muted)

    def _draw(self):
        self.config(image=self._image(self.muted))


class SegmentedSwitch(tk.Frame):
    """A two-way (or more) pill switch: the selected option is highlighted orange."""

    def __init__(self, master, options, value, command):
        super().__init__(master, bg=BORDER, padx=1, pady=1)
        self.command = command
        self.value = value
        self.buttons = {}
        for key, text in options:
            button = tk.Label(self, text=text, font=(FONT_BOLD, 9, "bold"), padx=9, pady=2, cursor="hand2")
            button.pack(side="left")
            button.bind("<Button-1>", lambda e, k=key: self.select(k))
            self.buttons[key] = button
        self._draw()

    def select(self, key):
        if key != self.value:
            self.value = key
            self._draw()
            self.command(key)

    def _draw(self):
        for key, button in self.buttons.items():
            on = key == self.value
            button.config(bg=ORANGE if on else SURFACE_HI, fg=BG if on else MUTED)


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
        self.win.configure(bg=BG)  # shows as a 1px border around the inner frame (orange while on)
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
        self.label.config(text="AutoWisp: On" if running else "AutoWisp: Off", fg=TEXT if running else MUTED)
        # Orange outline while on; when off the 1px edge matches the fill, so no outline shows
        self.win.configure(bg=ORANGE if running else BG)
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
    def __init__(self, launched_at_startup=False):
        self.settings = Settings(app_data_dir() / "settings.ini")
        setup_logging(self.settings.debug)
        # Started by Windows at sign-in with "Start minimized" on: open straight to the tray/taskbar
        self._start_hidden = launched_at_startup and self.settings.start_minimized

        self.events = queue.Queue()  # work handed from background threads to the Tk thread
        self.capturing_keybind = False
        self.capturing_cast_key = None  # which of the 4 cast-key buttons is waiting for a key
        self.update_info = None   # (version, url) once a newer release is found
        self.tray_icon = None

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        if self._start_hidden:
            self.root.attributes("-alpha", 0.0)  # invisible until it's tucked away, so no flash
        try:
            self.root.iconbitmap(resource_path("AutoWisp_Icon.ico"))
        except Exception as e:
            log.debug("Icon load failed: %s", e)  # expected on macOS

        try:
            self.detector = Detector(resource_path("Letters"), self.settings.cast_keys,
                                     app_data_dir() / "letter_shapes")
        except Exception as e:
            log.exception("Couldn't load templates")
            self._show_fatal(f"Couldn't load the letter templates.\n\n{e}")
            return

        self.runner = MacroRunner(self.detector, self.settings.effective_delay,
                                  lambda: self.settings.human_mode,
                                  lambda kind, data: self.events.put((kind, data)))
        self.hwnd = None
        if IS_WINDOWS:
            self._build_title_strip()
        self._build_ui()
        if IS_WINDOWS:
            self._go_borderless()
            self.root.bind("<Map>", self._on_map, add="+")
        try:
            self.overlay = StatusOverlay(self.root, self.settings, self._overlay_moved)
        except Exception:
            self.overlay = None  # the macro still works without it
            log.exception("Couldn't create the overlay")
        self._start_hotkey_listener()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Unmap>", self._on_unmap)
        self._poll_events()
        self._overlay_loop()
        self._instance_loop()
        threading.Thread(target=self._check_updates, daemon=True).start()
        if self._start_hidden:
            # Wait for the borderless set-up (which re-shows the window) to finish first
            self.root.after(150, self._finish_hidden_start)

    def _finish_hidden_start(self):
        log.info("Started with Windows; starting minimized")
        self.minimize()  # to the tray if "Minimize to tray" is on, otherwise the taskbar
        # Make it visible again once it's out of sight, so restoring shows it normally
        self.root.after(400, lambda: self.root.attributes("-alpha", 1.0))

    # ----- custom title bar (Windows) -----
    def _build_title_strip(self):
        """Our own minimize / close buttons, replacing the Windows title bar. They sit in the
        window's top-right corner, over the header's spare space, so there's no empty bar."""
        self.root.configure(bg=BORDER, padx=1, pady=1)  # 1px edge so the window stands out
        buttons = self.window_buttons = tk.Frame(self.root, bg=BG)
        close = FlatButton(buttons, "✕", self._on_close, bg=BG, fg=MUTED, hover_bg="#c42b1c",
                           font=(FONT, 10), padx=9, pady=3)
        close.pack(side="right")
        close.bind("<Enter>", lambda e: close.config(bg="#c42b1c", fg="white"), add="+")
        close.bind("<Leave>", lambda e: close.config(bg=BG, fg=MUTED), add="+")
        FlatButton(buttons, "–", self.minimize, bg=BG, fg=MUTED, hover_bg=SURFACE_HI,
                   font=(FONT, 11), padx=9, pady=1).pack(side="right")

    def _place_window_buttons(self):
        if getattr(self, "window_buttons", None) is not None:
            self.window_buttons.place(relx=1.0, x=0, y=0, anchor="ne")
            self.window_buttons.lift()  # keep them above the rebuilt content

    def _go_borderless(self):
        """Use Tk's own borderless mode, so Tk knows the window's real frame and moves it
        cleanly. On its own that mode drops the taskbar button, so add that back."""
        self.root.overrideredirect(True)
        self.root.update_idletasks()
        user32 = ctypes.windll.user32
        self.hwnd = user32.GetParent(self.root.winfo_id())
        style = user32.GetWindowLongW(self.hwnd, -20)  # GWL_EXSTYLE
        style = (style & ~0x80) | 0x40000  # -WS_EX_TOOLWINDOW, +WS_EX_APPWINDOW (taskbar button)
        user32.SetWindowLongW(self.hwnd, -20, style)
        # The taskbar only notices the change when the window is shown again
        self.root.withdraw()
        self.root.after(10, self.root.deiconify)

    def _on_map(self, event):
        # Coming back from being minimized: drop the Windows frame we put back to minimize
        if event.widget is self.root and getattr(self, "_restoring", False):
            self._restoring = False
            self.root.after(10, self._go_borderless)

    def _make_draggable(self, *widgets):
        """Let the window be dragged by these widgets. Done purely in Tk: handing the drag to
        Windows (WM_NCLBUTTONDOWN) runs a modal loop that re-enters Python and crashes it.
        Mouse events come in far faster than the screen refreshes, so the window is moved at
        most once per redraw, to the latest position."""
        if not IS_WINDOWS:
            return
        self._drag_offset = None
        self._drag_target = None

        def start(event):
            self._drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

        def apply_move():
            target, self._drag_target = self._drag_target, None
            if target is not None:
                self.root.geometry(f"+{target[0]}+{target[1]}")

        def move(event):
            if self._drag_offset is None:
                return
            dx, dy = self._drag_offset
            first = self._drag_target is None
            self._drag_target = (event.x_root - dx, event.y_root - dy)
            if first:
                self.root.after_idle(apply_move)

        def end(_event):
            apply_move()
            self._drag_offset = None

        for widget in widgets:
            widget.bind("<ButtonPress-1>", start)
            widget.bind("<B1-Motion>", move)
            widget.bind("<ButtonRelease-1>", end)

    def minimize(self):
        if self.settings.tray and pystray is not None:
            self.root.withdraw()
            self._start_tray()
        elif IS_WINDOWS:
            # Tk can't minimize a borderless window, so give it its frame back just to minimize;
            # _on_map takes it off again when the window is restored. Switching the frame back
            # on re-shows the window, so wait for that before minimizing.
            self.root.overrideredirect(False)
            self.root.update_idletasks()

            def iconify():
                self.root.iconify()
                self._restoring = True  # the next time it's shown is a restore

            self.root.after(30, iconify)
        else:
            self.root.iconify()

    # ----- layout -----
    def _build_ui(self):
        # On Windows the title shares the top row with the – / ✕ buttons, so almost no top padding
        outer = self.outer = tk.Frame(self.root, bg=BG, padx=20, pady=0 if IS_WINDOWS else 18)
        outer.pack(fill="both", expand=True)
        self._make_draggable(outer)  # empty background drags the window too

        # Header
        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x", pady=(2, 0) if IS_WINDOWS else 0)
        title_box = tk.Frame(header, bg=BG)
        title_box.pack(side="left")
        title = tk.Label(title_box, text="AutoWisp", fg=ORANGE, bg=BG, font=(FONT_BOLD, 20, "bold"))
        title.pack(anchor="w")
        self._make_draggable(header, title_box, title)

        # Status as plain text (coloured dot + word) beside the title
        self.status_pill = tk.Frame(header, bg=BG)
        if IS_WINDOWS:
            # The – / ✕ buttons take the top-right corner, so the status sits beside the title
            self.status_pill.pack(side="left", padx=(8, 0), pady=(5, 0))
        else:
            self.status_pill.pack(side="right")
        self.status_dot = tk.Label(self.status_pill, text="●", bg=BG, fg=MUTED, font=(FONT, 9))
        self.status_dot.pack(side="left")
        self.status_label = tk.Label(self.status_pill, text="Idle", bg=BG, fg=MUTED, font=(FONT, 9))
        self.status_label.pack(side="left", padx=(3, 0))
        self._make_draggable(self.status_pill, self.status_dot, self.status_label)

        # Start / stop
        self.start_button = FlatButton(outer, "Start", self.toggle_macro, bg=ORANGE, fg=BG,
                                       hover_bg=ORANGE_HOVER, font=(FONT_BOLD, 13, "bold"), pady=10)
        self.start_button.pack(fill="x", pady=(18, 0))
        self.detail_label = tk.Label(outer, text="Press Start, then focus Roblox.", fg=MUTED, bg=BG,
                                     font=(FONT, 9), wraplength=250)  # fits the window's width
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

        row = self._setting_row(settings, "Ritual cast keys")
        self.cast_key_buttons = []
        for index in reversed(range(SLOTS)):  # packed right to left
            button = FlatButton(row, self.settings.cast_keys[index].upper(),
                                lambda i=index: self.begin_cast_key_capture(i),
                                bg=SURFACE_HI, fg=ORANGE, hover_bg=BORDER,
                                font=(FONT_MONO, 10, "bold"), padx=7, pady=3,
                                highlightthickness=1, highlightbackground=BORDER)
            button.pack(side="right", padx=(4, 0))
            self.cast_key_buttons.insert(0, button)

        row = self._setting_row(settings, "Delay between keys")
        self.delay_switch = SegmentedSwitch(row, [("fixed", "Fixed"), ("ping", "Ping")],
                                            self.settings.delay_mode, self.set_delay_mode)
        self.delay_switch.pack(side="right")

        controls = tk.Frame(settings, bg=SURFACE)
        controls.pack(fill="x", pady=(6, 0))
        self.delay_info = tk.Label(controls, fg=MUTED, bg=SURFACE, font=(FONT, 9))
        self.delay_info.pack(side="left")
        self.delay_var = tk.StringVar(value=f"{self.settings.delay:g}")
        self.fixed_controls, self.delay_entry = self._stepper(
            controls, self.delay_var, "sec", self.apply_delay, lambda: self.nudge_delay(0.05),
            lambda: self.nudge_delay(-0.05))
        self.ping_var = tk.StringVar(value=str(self.settings.ping_ms))
        self.ping_controls, self.ping_entry = self._stepper(
            controls, self.ping_var, "ms", self.apply_ping, lambda: self.nudge_ping(10),
            lambda: self.nudge_ping(-10))
        self._show_delay_controls()

        row = self._setting_row(settings, "Human mode")
        Toggle(row, self.settings.human_mode, self.set_human_mode).pack(side="right")

        row = self._setting_row(settings, "Sound")
        self.volume_label = tk.Label(row, width=4, anchor="e", bg=SURFACE, font=(FONT, 9))
        self.volume_label.pack(side="right")
        self.volume_slider = VolumeSlider(row, self.settings.volume, self.settings.sound,
                                          self._volume_changed, self._volume_released)
        self.volume_slider.pack(side="right", padx=(4, 0))
        self.speaker_button = SpeakerButton(row, not self.settings.sound, self.set_muted)
        self.speaker_button.pack(side="right")
        self._update_volume_label()

        row = self._setting_row(settings, "Status overlay in Roblox")
        Toggle(row, self.settings.overlay, self.set_overlay).pack(side="right")

        row = self._setting_row(settings, "Lock overlay position")
        Toggle(row, self.settings.overlay_locked, self.set_overlay_locked).pack(side="right")

        if pystray is not None:
            row = self._setting_row(settings, "Minimize to tray")
            Toggle(row, self.settings.tray, self.set_tray).pack(side="right")

        if IS_WINDOWS:
            row = self._setting_row(settings, "Launch on Windows startup")
            Toggle(row, get_launch_on_startup(), self.set_startup).pack(side="right")

            row = self._setting_row(settings, "Start minimized")
            Toggle(row, self.settings.start_minimized, self.set_start_minimized).pack(side="right")

        row = self._setting_row(settings, "Debug logging")
        Toggle(row, self.settings.debug, self.set_debug).pack(side="right")
        self._small_button(row, "Open logs", self.open_logs).pack(side="right", padx=(0, 8))

        buttons = tk.Frame(settings, bg=SURFACE)
        buttons.pack(fill="x", pady=(14, 0))
        self._small_button(buttons, "Show scan area", self.show_scan_area).pack(side="left")
        self._small_button(buttons, "Reset settings", self.reset_settings).pack(side="right")

        # Footer
        self.hint_label = tk.Label(outer, fg=MUTED, bg=BG, font=(FONT, 8))
        self.hint_label.pack(pady=(12, 0))
        self._update_hint()
        self.version_label = tk.Label(outer, text=f"v{APP_VERSION}", fg=BORDER_TEXT, bg=BG, font=(FONT, 8))
        self.version_label.pack()
        if IS_WINDOWS:
            tk.Frame(outer, bg=BG, height=16).pack()  # bottom padding (outer has none on Windows)
        self._show_update()
        self._sync_running_ui()
        self._place_window_buttons()

    def _small_button(self, parent, text, command):
        return FlatButton(parent, text, command, bg=SURFACE_HI, fg=TEXT, hover_bg=BORDER,
                          font=(FONT, 9), padx=8, pady=2)

    def _card(self, parent, pady):
        card = tk.Frame(parent, bg=SURFACE, padx=16, pady=14,
                        highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x", pady=pady)
        return card

    def _stepper(self, parent, var, unit, apply, increase, decrease):
        """[−] [value] [+] unit — returns (frame, entry)."""
        frame = tk.Frame(parent, bg=SURFACE)
        tk.Label(frame, text=unit, fg=MUTED, bg=SURFACE, font=(FONT, 9)).pack(side="right", padx=(6, 0))
        FlatButton(frame, "+", increase, bg=SURFACE_HI, fg=TEXT, hover_bg=BORDER,
                   font=(FONT_BOLD, 10, "bold"), padx=8, pady=1).pack(side="right")
        entry = tk.Entry(frame, textvariable=var, width=5, justify="center", bg=SURFACE_HI, fg=TEXT,
                         insertbackground=ORANGE, relief="flat", font=(FONT_MONO, 10),
                         highlightthickness=1, highlightbackground=BORDER, highlightcolor=ORANGE)
        entry.pack(side="right", padx=4, ipady=3)
        entry.bind("<Return>", lambda e: apply())
        entry.bind("<FocusOut>", lambda e: apply())
        FlatButton(frame, "−", decrease, bg=SURFACE_HI, fg=TEXT, hover_bg=BORDER,
                   font=(FONT_BOLD, 10, "bold"), padx=8, pady=1).pack(side="right")
        return frame, entry

    def _show_delay_controls(self):
        ping = self.settings.delay_mode == "ping"
        self.fixed_controls.pack_forget()
        self.ping_controls.pack_forget()
        (self.ping_controls if ping else self.fixed_controls).pack(side="right")
        self.delay_info.config(text=f"= {self.settings.effective_delay():g}s" if ping else "")
        if self.delay_switch.value != self.settings.delay_mode:
            self.delay_switch.value = self.settings.delay_mode
            self.delay_switch._draw()

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
        self.status_label.config(text=text, fg=color)
        self.status_dot.config(fg=color)

    def _update_hint(self):
        self.hint_label.config(text=f"Press {display_key(self.settings.keybind)} anywhere to start or stop")

    # ----- actions -----
    def toggle_macro(self):
        if self.runner.running:
            self.runner.stop()
            self.detail_label.config(text="Stopped.", fg=MUTED)
        else:
            self.runner.start()
        self._sync_running_ui()
        if self.overlay is not None:
            self.overlay.set_running(self.runner.running)
        if self.settings.sound:
            play_toggle_sound(self.runner.running, self.settings.volume, self.root)

    def _sync_running_ui(self):
        if self.runner.running:
            self.start_button.set_style("Stop", SURFACE_HI, ORANGE, BORDER)
            self.set_status("Running", ORANGE)
        else:
            self.start_button.set_style("Start", ORANGE, BG, ORANGE_HOVER)
            self.set_status("Idle", MUTED)

    def set_muted(self, muted):
        self.settings.sound = not muted
        self.settings.save()
        self.volume_slider.set_enabled(not muted)
        self._update_volume_label()
        if not muted:
            play_toggle_sound(True, self.settings.volume, self.root)  # a sample of the volume

    def _volume_changed(self, value):
        self.settings.volume = value
        self._update_volume_label()

    def _volume_released(self, value):
        self.settings.volume = value
        if value > 0 and not self.settings.sound:  # moving the slider unmutes, like most apps
            self.settings.sound = True
            self.speaker_button.muted = False
            self.speaker_button._draw()
            self.volume_slider.set_enabled(True)
        self.settings.save()
        self._update_volume_label()
        play_toggle_sound(True, value if self.settings.sound else 0, self.root)

    def _update_volume_label(self):
        muted = not self.settings.sound
        self.volume_label.config(text="Off" if muted else f"{self.settings.volume}%",
                                 fg=MUTED if muted else TEXT)

    def set_tray(self, enabled):
        self.settings.tray = enabled
        self.settings.save()
        if enabled:
            self.detail_label.config(text="Minimizing the window now hides it in the system tray.", fg=MUTED)

    def set_startup(self, enabled):
        try:
            set_launch_on_startup(enabled)
        except OSError as e:
            log.exception("Couldn't change the startup entry")
            self.detail_label.config(text=f"Couldn't change launch on startup: {e}", fg=ERROR)
            return
        if enabled:
            self.detail_label.config(text="AutoWisp will open when you sign in to Windows.", fg=MUTED)

    def set_start_minimized(self, enabled):
        self.settings.start_minimized = enabled
        self.settings.save()
        if enabled:
            where = "the system tray" if self.settings.tray and pystray is not None else "the taskbar"
            self.detail_label.config(text=f"When it opens with Windows, it goes straight to {where}.",
                                     fg=MUTED)

    def open_logs(self):
        try:
            open_folder(app_data_dir())
        except Exception as e:
            self.detail_label.config(text=f"Couldn't open the log folder: {e}", fg=ERROR)

    def show_scan_area(self):
        window = find_roblox_window_any()
        if window is None:
            self.detail_label.config(text="Open Roblox first, then press Show scan area.", fg=ERROR)
            return
        try:
            window.activate()  # bring the game forward so the outline is drawn over it
        except Exception:
            pass
        region = self.detector.capture_region(window)
        ScanAreaPreview(self.root, region)
        self.detail_label.config(text="The orange box in Roblox is where letters are read from.", fg=MUTED)

    def reset_settings(self):
        if not messagebox.askyesno("Reset settings", "Put every setting back to its default?",
                                   parent=self.root):
            return
        self.capturing_keybind = False
        self.capturing_cast_key = None  # the buttons are rebuilt below
        self.settings.reset()
        self.detector.set_keys(self.settings.cast_keys)
        set_debug_logging(self.settings.debug)
        if IS_WINDOWS:
            try:
                set_launch_on_startup(False)  # the default is off
            except OSError:
                log.exception("Couldn't remove the startup entry")
        if self.overlay is not None:
            self.overlay.set_locked(self.settings.overlay_locked)
            self.overlay.win.geometry("+{}+{}".format(*StatusOverlay.DEFAULT_POS))
        self.outer.destroy()
        self._build_ui()
        self._update_overlay()
        self.detail_label.config(text="Settings reset to defaults.", fg=MUTED)

    # ----- tray, single instance, updates -----
    def show_window(self):
        self._stop_tray()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_unmap(self, event):
        if (event.widget is self.root and self.settings.tray and pystray is not None
                and self.root.state() == "iconic"):
            self.root.withdraw()
            self._start_tray()

    def _start_tray(self):
        if self.tray_icon is not None:
            return
        try:
            image = Image.open(resource_path("AutoWisp_Icon.ico"))
        except Exception:
            image = Image.new("RGB", (64, 64), ORANGE)
        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda: self.events.put(("tray_show", None)), default=True),
            pystray.MenuItem(lambda item: "Stop macro" if self.runner.running else "Start macro",
                             lambda: self.events.put(("tray_toggle", None))),
            pystray.MenuItem("Quit", lambda: self.events.put(("tray_quit", None))),
        )
        self.tray_icon = pystray.Icon(APP_NAME, image, WINDOW_TITLE, menu)
        try:
            self.tray_icon.run_detached()
        except Exception:
            log.exception("Couldn't create the tray icon")
            self.tray_icon = None
            self.root.deiconify()  # don't leave the window hidden with no way back

    def _stop_tray(self):
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None

    def _instance_loop(self):
        if show_requested():
            self.show_window()  # another copy was opened; bring this one forward instead
        self.root.after(250, self._instance_loop)

    def _check_updates(self):
        try:
            result = check_for_update()
        except Exception as e:
            log.debug("Update check failed: %s", e)
            return
        if result:
            self.events.put(("update", result))

    def _show_update(self):
        if self.update_info is None:
            return
        version, url = self.update_info
        self.version_label.config(text=f"v{APP_VERSION} · Update available: {version}", fg=ORANGE,
                                  cursor="hand2")
        self.version_label.bind("<Button-1>", lambda e: webbrowser.open(url))

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
        self.end_cast_key_capture()
        self.capturing_keybind = True
        self.keybind_button.config(text="Press a key…", highlightbackground=ORANGE)
        self.detail_label.config(text="Press the new hotkey, or Esc to cancel.", fg=MUTED)

    def end_keybind_capture(self):
        self.capturing_keybind = False
        self.keybind_button.config(text=display_key(self.settings.keybind), highlightbackground=BORDER)

    def begin_cast_key_capture(self, index):
        if self.capturing_keybind:
            self.end_keybind_capture()
        self.end_cast_key_capture()
        self.capturing_cast_key = index
        self.cast_key_buttons[index].config(text="…", highlightbackground=ORANGE)
        self.detail_label.config(text="Press the key this cast slot is bound to in Deepwoken, or Esc "
                                      "to cancel.", fg=MUTED)

    def end_cast_key_capture(self):
        index, self.capturing_cast_key = self.capturing_cast_key, None
        if index is not None:
            self.cast_key_buttons[index].config(text=self.settings.cast_keys[index].upper(),
                                                highlightbackground=BORDER)

    def set_cast_key(self, index, name):
        keys = list(self.settings.cast_keys)
        if name in keys and keys.index(name) != index:
            # Already one of the other cast keys: swap them, so all 4 stay different
            other = keys.index(name)
            keys[other] = keys[index]
        keys[index] = name
        self.settings.cast_keys = keys
        self.settings.save()
        self.detector.set_keys(keys)
        for button, key in zip(self.cast_key_buttons, keys):
            button.config(text=key.upper())
        log.info("Cast keys set to %s", " ".join(k.upper() for k in keys))

    def handle_key(self, name):
        if self.capturing_cast_key is not None:
            index = self.capturing_cast_key
            if name == "esc":
                self.end_cast_key_capture()
                self.detail_label.config(text="Cast key unchanged.", fg=MUTED)
            elif len(name) != 1 or name not in CAST_KEY_CHARS:
                self.detail_label.config(text="Cast keys can be A-Z, 0-9, [ or ]. Press one of those, or "
                                              "Esc to cancel.", fg=ERROR)
            elif name == self.settings.keybind:
                self.detail_label.config(text=f"{display_key(name)} is your start/stop hotkey. "
                                              "Pick another key.", fg=ERROR)
            else:
                self.end_cast_key_capture()
                self.set_cast_key(index, name)
                self.detail_label.config(text="Cast keys: " + " ".join(k.upper() for k in self.settings.cast_keys)
                                         + ". New letters are learned from your first casts.", fg=MUTED)
            return
        if self.capturing_keybind:
            if name == "esc":
                self.end_keybind_capture()
                self.detail_label.config(text="Hotkey unchanged.", fg=MUTED)
            elif name in self.settings.cast_keys:
                self.detail_label.config(text=f"{display_key(name)} is typed by the macro. Pick another key.",
                                         fg=ERROR)
            else:
                self.settings.keybind = name
                self.settings.save()
                self.end_keybind_capture()
                self._update_hint()
                self.detail_label.config(text=f"Hotkey set to {display_key(name)}.", fg=MUTED)
            return
        if self.root.focus_get() in (self.delay_entry, self.ping_entry):
            return  # typing a value, not pressing the hotkey
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

    def apply_ping(self):
        try:
            value = min(MAX_PING_MS, max(0, round(float(self.ping_var.get()))))
        except ValueError:
            value = self.settings.ping_ms
            self.detail_label.config(text="Ping must be a number of milliseconds.", fg=ERROR)
        self.settings.ping_ms = value
        self.settings.save()
        self.ping_var.set(str(value))
        self._show_delay_controls()

    def nudge_ping(self, step):
        self.apply_ping()
        self.ping_var.set(str(max(0, self.settings.ping_ms + step)))
        self.apply_ping()

    def set_delay_mode(self, mode):
        self.apply_delay()
        self.apply_ping()
        self.settings.delay_mode = mode
        self.settings.save()
        self._show_delay_controls()
        if mode == "ping":
            self.detail_label.config(
                text=f"Enter the ping Roblox shows you (Shift+F3). "
                     f"Delay = {PING_DELAY_BASE * 1000:g}ms + your ping.", fg=MUTED)

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
        elif kind == "update":
            self.update_info = data
            self._show_update()
        elif kind == "tray_show":
            self.show_window()
        elif kind == "tray_toggle":
            self.toggle_macro()
        elif kind == "tray_quit":
            self._on_close()
        elif not self.runner.running and kind != "error":
            return  # late update from a run that was already stopped
        elif kind == "waiting":
            self.set_status("Waiting", MUTED)  # short to fit beside the title; the line below explains
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
        self._stop_tray()
        self.runner.stop()
        if getattr(self, "listener", None) is not None:
            self.listener.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# --- Single instance ---
_instance_handles = []  # kept open for the life of the process


def claim_single_instance():
    """True if no other copy is running. Otherwise asks that copy to show its window and
    returns False, since two copies would both type every letter and break the cast."""
    if IS_WINDOWS:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.OpenEventW.restype = wintypes.HANDLE
        mutex = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            event = kernel32.OpenEventW(0x0002, False, f"Local\\{APP_NAME}Show")  # EVENT_MODIFY_STATE
            if event:
                kernel32.SetEvent(event)
                kernel32.CloseHandle(event)
            return False
        # Auto-reset event that a second copy sets to ask us to come to the front
        event = kernel32.CreateEventW(None, False, False, f"Local\\{APP_NAME}Show")
        _instance_handles.extend([mutex, event])
        return True
    import fcntl
    lock = open(app_data_dir() / "instance.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _instance_handles.append(lock)
    return True


def show_requested():
    """True if another copy asked this one to show its window (Windows only)."""
    if IS_WINDOWS and len(_instance_handles) == 2:
        return ctypes.windll.kernel32.WaitForSingleObject(wintypes.HANDLE(_instance_handles[1]), 0) == 0
    return False


# --- Small helpers ---
_tone_cache = {}


CHIME_NOTES = (659.25, 880.0)  # E5 and A5: a soft, friendly interval


def _chime_wav(on, volume, rate=44100):
    """A gentle two-note chime as WAV bytes: rising for on, falling for off.
    Each note fades in softly and dies away like a bell. winsound.Beep has no volume
    control, so we make our own sound."""
    key = (on, volume)
    if key not in _tone_cache:
        import io, wave
        notes = CHIME_NOTES if on else CHIME_NOTES[::-1]
        note_gap, note_len = 0.09, 0.32  # second note starts while the first is still ringing
        n = int(rate * (note_gap + note_len))
        t = np.arange(n) / rate
        signal = np.zeros(n)
        for i, freq in enumerate(notes):
            start = i * note_gap
            local = t - start
            active = (local >= 0) & (local < note_len)
            attack = np.clip(local / 0.012, 0, 1)            # 12ms soft start
            decay = np.exp(-local / 0.09)                    # bell-like die-away
            tone = np.sin(2 * np.pi * freq * local) + 0.12 * np.sin(4 * np.pi * freq * local)  # touch of warmth
            signal += np.where(active, tone * attack * decay, 0.0)
        signal[-int(rate * 0.01):] *= np.linspace(1, 0, int(rate * 0.01))  # end on silence
        signal /= np.abs(signal).max()
        amplitude = (volume / 100) ** 2 * 0.55  # squared so the slider feels even to the ear
        samples = (signal * amplitude * 32767).astype("<i2")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(samples.tobytes())
        _tone_cache[key] = buffer.getvalue()
    return _tone_cache[key]


def play_toggle_sound(on: bool, volume: int, root=None):
    """A soft rising chime for on, a falling one for off, at the given volume (0-100).
    Plays in the background, and a new chime cuts off one that's still playing, so spamming
    the toggle never leaves a queue of sounds."""
    if IS_WINDOWS:
        import winsound
        if volume <= 0:
            winsound.PlaySound(None, 0)  # just stop anything still playing
            return
        # Windows can only play asynchronously (and cut off the previous sound) from a file,
        # not from memory, so each chime is written once to a small temp file.
        winsound.PlaySound(str(_chime_file(on, volume)),
                           winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    elif root is not None and volume > 0:
        root.bell()  # no volume control here


def _chime_file(on, volume):
    folder = Path(tempfile.gettempdir()) / APP_NAME
    path = folder / f"chime_{'on' if on else 'off'}_{volume}.wav"
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_chime_wav(on, volume))
    return path


def open_folder(path: Path):
    if IS_WINDOWS:
        os.startfile(str(path))
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def version_tuple(version: str):
    return tuple(int(part) for part in version.strip().lstrip("vV").split(".") if part.isdigit())


def check_for_update():
    """(newer version, release page URL) if GitHub has a newer release, otherwise None."""
    request = urllib.request.Request(RELEASES_API, headers={"User-Agent": APP_NAME,
                                                            "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=6) as response:
        release = json.load(response)
    tag = release.get("tag_name", "")
    if version_tuple(tag) > version_tuple(APP_VERSION):
        return tag, release.get("html_url") or RELEASES_PAGE
    return None


def find_roblox_window_any():
    """The main Roblox window even if it isn't focused (the biggest visible one)."""
    best = None
    for window in pywinctl.getAllWindows():
        try:
            if window.isVisible and window.width > 200 and is_roblox(window):
                if best is None or window.width * window.height > best.width * best.height:
                    best = window
        except Exception:
            continue
    return best


class ScanAreaPreview:
    """Briefly outlines the area the macro reads letters from, split into its 4 slots.
    Click-through and hidden from recordings, like the status overlay."""

    KEY = "#ff00fe"  # drawn transparent

    def __init__(self, root, region, seconds=4.0):
        x, y, w, h = region
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-transparentcolor", self.KEY)
        except tk.TclError:
            self.win.attributes("-alpha", 0.6)  # no colour-key transparency on this platform
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        canvas = tk.Canvas(self.win, width=w, height=h, bg=self.KEY, highlightthickness=0)
        canvas.pack()
        canvas.create_rectangle(1, 1, w - 2, h - 2, outline=ORANGE, width=2)
        for i in range(1, SLOTS):
            sx = i * (w // SLOTS)
            canvas.create_line(sx, 4, sx, h - 4, fill=ORANGE, dash=(4, 4))
        canvas.create_text(6, 4, text="Scan area", anchor="nw", fill=ORANGE, font=(FONT_BOLD, 9, "bold"))
        self.win.update_idletasks()
        if IS_WINDOWS:
            user32 = ctypes.windll.user32
            hwnd = user32.GetAncestor(self.win.winfo_id(), 2)
            style = user32.GetWindowLongW(hwnd, -20)
            # layered + click-through + no taskbar entry + never takes focus
            user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x08000000)
            user32.SetWindowDisplayAffinity(hwnd, 0x11)  # hide from recordings
            user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)
        else:
            self.win.deiconify()
        self.win.after(int(seconds * 1000), self.win.destroy)


# --- Launch on Windows startup ---
# A per-user entry under the registry's Run key: Windows starts it at sign-in, no admin needed.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_ARG = "--startup"  # tells the app it was started by Windows, not by the user


def startup_command():
    """The command Windows should run at sign-in: this exe, or pythonw + this script."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {STARTUP_ARG}'
    pythonw = Path(sys.executable).with_name("pythonw.exe")  # no console window
    python = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{python}" "{Path(__file__).resolve()}" {STARTUP_ARG}'


def get_launch_on_startup():
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except OSError:
        return False


def set_launch_on_startup(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def refresh_launch_on_startup():
    """If startup is on, point it at wherever the app is now (e.g. after the exe was moved)."""
    try:
        if get_launch_on_startup():
            set_launch_on_startup(True)
    except OSError as e:
        log.warning("Couldn't update the startup entry: %s", e)


def main():
    if not claim_single_instance():
        return  # the copy that's already running brings its window to the front
    refresh_launch_on_startup()
    App(launched_at_startup=STARTUP_ARG in sys.argv[1:]).run()


if __name__ == "__main__":
    main()
