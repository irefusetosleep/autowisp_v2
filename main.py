import cv2 as cv
import pyautogui
import numpy as np
import pydirectinput as pd
from time import sleep
import pywinctl
from pathlib import Path
import threading
import tkinter as tk
import configparser
import keyboard
import sys, os

# ------------------------
# Resource path helper (PyInstaller)
# ------------------------
def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS # this error is fine
    except AttributeError:
        base_path = os.path.abspath(".")
    return Path(os.path.join(base_path, relative_path))

# ------------------------
# Settings Path helper
# ------------------------

def get_settings_path():
    app_dir = Path(os.getenv("APPDATA") or Path.home() / ".config") / "autowisp_v2"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir / "settings.ini"

# ------------------------
# Config
# ------------------------
letter_folder = resource_path("Original")
template_folder = resource_path("Letters")
settings_file = get_settings_path()

binarize_thresh = 140
delay = 0
pd.PAUSE = 0.05
running = False
toggle_key = "f2"
hotkey_handler = None

# ------------------------
# Save / Load window state
# ------------------------
def load_window_settings():
    config = configparser.ConfigParser()
    if settings_file.exists():
        config.read(settings_file)
        if "Window" in config:
            x = config.getint("Window", "x", fallback=100)
            y = config.getint("Window", "y", fallback=100)
            w = config.getint("Window", "w", fallback=400)
            h = config.getint("Window", "h", fallback=300)
            k = config.get("Window", "keybind", fallback="f2")
            return x, y, w, h, k
    return 100, 100, 400, 300, "f2"

def save_window_settings(x, y, w, h, keybind):
    config = configparser.ConfigParser()
    config["Window"] = {"x": x, "y": y, "w": w, "h": h, "keybind": keybind}
    with open(settings_file, "w") as f:
        config.write(f)

def get_window():
    window = pywinctl.getActiveWindow()
    return window.title if window else "No window"

def split_into_4(img):
    h, w = img.shape[:2]
    slice_w = w // 4
    return [
        img[:, 0:slice_w],
        img[:, slice_w:2*slice_w],
        img[:, 2*slice_w:3*slice_w],
        img[:, 3*slice_w:w],
    ]

def get_letters(screenshot):
    found_letters = []
    templates = list(template_folder.glob("*.[jp][pn]g"))
    quadrants = split_into_4(screenshot)
    for screenshot in quadrants:
        matches = {}
        for template_path in templates:
            template = cv.imread(str(template_path), cv.IMREAD_GRAYSCALE)
            if template is None:
                continue
            match_result = cv.matchTemplate(screenshot, template, cv.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv.minMaxLoc(match_result)
            matches[str(template_path.stem.replace("_purple", "").lower())] = max_val
        if matches:
            best_match = max(matches, key=matches.get)
            confidence = matches[best_match]
            if confidence < 0.5:
                return found_letters
            found_letters.append(best_match)
    return found_letters

def screenshot():
    screen_width, screen_height = pyautogui.size()
    top = screen_height // 6
    left = int(screen_width * 0.44)
    width = int(screen_width * 0.16)
    height = screen_height // 6
    ss = pyautogui.screenshot(region=(left, top, width, height))
    ss = cv.cvtColor(np.array(ss), cv.COLOR_RGB2BGR)
    ss = cv.cvtColor(ss, cv.COLOR_BGR2GRAY)
    _, ss = cv.threshold(ss, binarize_thresh, 255, cv.THRESH_BINARY)
    return ss

# ------------------------
# Macro logic
# ------------------------
def run_macro():
    global running
    while running:
        window = get_window()
        if "roblox" not in window.lower():
            sleep(0.1)
            continue
        ss = screenshot()
        letters = get_letters(ss)
        if not letters:
            sleep(0.1)
            continue
        for letter in letters:
            if not running:
                break
            pd.press(letter)
        sleep(0.05)

def toggle_macro():
    global running
    if running:
        running = False
    else:
        running = True
        threading.Thread(target=run_macro, daemon=True).start()

# ------------------------
# GUI
# ------------------------
x, y, w, h, saved_key = load_window_settings()
toggle_key = saved_key

root = tk.Tk()
root.title("Cinder Wisp Macro")
root.configure(bg="#1e1e1e")
root.geometry(f"{w}x{h}+{x}+{y}")
root.iconbitmap(resource_path("icon.ico"))

title_label = tk.Label(root, text="Cinder Wisp Macro", fg="orange", bg="#1e1e1e", font=("Arial", 16, "bold"))
title_label.pack(pady=10)

# Start/Stop button
start_button = tk.Button(root, text="Start", command=toggle_macro,
                         fg="white", font=("Arial", 12, "bold"),
                         width=12, relief=tk.FLAT, bd=0)
start_button.pack(pady=10)

def update_button_appearance():
    if running:
        start_button.configure(bg="red", text="Stop")
    else:
        start_button.configure(bg="green", text="Start")
    root.after(200, update_button_appearance)

# Keybind input
hotkey_handler = keyboard.add_hotkey(toggle_key, toggle_macro)

def set_new_key():
    global toggle_key, hotkey_handler
    new_key = key_entry.get().lower()
    if not new_key:
        return
    # Remove previous hotkey
    if hotkey_handler:
        keyboard.remove_hotkey(hotkey_handler)
    toggle_key = new_key
    key_label.config(text=f"Current Keybind: {toggle_key.upper()}")
    # Register new hotkey
    hotkey_handler = keyboard.add_hotkey(toggle_key, toggle_macro)

key_frame = tk.Frame(root, bg="#1e1e1e")
key_frame.pack(pady=10)

key_label = tk.Label(key_frame, text=f"Current Keybind: {toggle_key.upper()}", fg="white", bg="#1e1e1e")
key_label.pack(side=tk.LEFT, padx=5)

key_entry = tk.Entry(key_frame, width=10)
key_entry.pack(side=tk.LEFT, padx=5)

key_button = tk.Button(key_frame, text="Set Keybind", command=set_new_key)
key_button.pack(side=tk.LEFT, padx=5)

# Status label
status_label = tk.Label(root, text="Status: Idle", fg="white", bg="#1e1e1e", font=("Arial", 10))
status_label.pack(pady=10)

def update_status():
    if running:
        status_label.config(text=f"Status: Running (Press {toggle_key.upper()} to stop)", fg="lime")
    else:
        status_label.config(text=f"Status: Idle (Press {toggle_key.upper()} to start)", fg="white")
    root.after(200, update_status)

# ------------------------
# Save window state on close
# ------------------------
def on_close():
    geo = root.geometry()
    wh, xy = geo.split("+", 1)
    w, h = map(int, wh.split("x"))
    x, y = map(int, xy.split("+"))
    save_window_settings(x, y, w, h, toggle_key)
    root.destroy()

root.protocol("WM_DELETE_WINDOW", on_close)

# ------------------------
# Launch GUI updates
# ------------------------
update_status()
update_button_appearance()
root.mainloop()

