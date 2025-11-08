import os
import sys
import cv2 as cv
import pyautogui
import numpy as np
from time import sleep
from pathlib import Path
import pywinctl
import platform
import configparser
import threading
import tkinter as tk
from tkinter import ttk, messagebox

# --- Handle file paths in both source and PyInstaller EXE/app ---
def resource_path(relative_path: str) -> Path:
    """Get absolute path to resource (works for dev and PyInstaller)."""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / relative_path  # Inside PyInstaller bundle
    return Path(os.path.abspath(".")) / relative_path

# --- Cross-platform key input handling ---
if platform.system() == "Windows":
    import pydirectinput as pd
else:
    pd = pyautogui  # fallback for macOS/Linux

# --- Config handling (persistent settings) ---
def get_settings_path():
    app_dir = Path(os.getenv('APPDATA') or Path.home() / ".config") / "CinderWispMacro"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir / "settings.ini"

config = configparser.ConfigParser()
settings_path = get_settings_path()

if settings_path.exists():
    config.read(settings_path)
else:
    config["SETTINGS"] = {"keybind": "F8"}
    with open(settings_path, "w") as f:
        config.write(f)

# --- Global vars ---
letter_folder = resource_path("Original")
template_folder = resource_path("Letters")
binarize_thresh = 140
delay = 0
running = False
pd.PAUSE = 0.05

# --- Core functions ---
def get_window():
    window = pywinctl.getActiveWindow()
    return window.title if window else "No window"

def split_into_4(img):
    h, w = img.shape[:2]
    slice_w = w // 4
    return [img[:, i * slice_w:(i + 1) * slice_w] for i in range(4)]

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
            matches[template_path.stem.replace("_purple", "").lower()] = max_val

        if matches:
            best_match = max(matches, key=matches.get)
            confidence = matches[best_match]
            if confidence < 0.5:
                return []
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

def macro_loop():
    global running
    while running:
        window = get_window()
        if "roblox" not in window.lower():
            sleep(0.1)
            continue

        ss = screenshot()
        letters = get_letters(ss)
        if len(letters) == 0:
            sleep(0.1)
            continue
        for letter in letters:
            pd.press(letter)
            sleep(delay)
    print("Macro stopped.")

# --- GUI ---
def toggle_macro():
    global running
    running = not running
    if running:
        start_button.config(text="Stop", bg="red", activebackground="red")
        threading.Thread(target=macro_loop, daemon=True).start()
    else:
        start_button.config(text="Start", bg="green", activebackground="green")

def change_keybind():
    def on_key_press(event):
        new_key = event.keysym
        config["SETTINGS"]["keybind"] = new_key
        with open(settings_path, "w") as f:
            config.write(f)
        keybind_label.config(text=f"Current Keybind: {new_key}")
        bind_window.destroy()

    bind_window = tk.Toplevel(root)
    bind_window.title("Set Keybind")
    bind_window.geometry("300x150")
    bind_window.configure(bg="#1c1c1c")
    tk.Label(bind_window, text="Press a new key...", fg="orange", bg="#1c1c1c", font=("Segoe UI", 12)).pack(expand=True)
    bind_window.bind("<Key>", on_key_press)

def on_key_press_global(event):
    key = config["SETTINGS"]["keybind"]
    if event.keysym.lower() == key.lower():
        toggle_macro()

# --- Tkinter Window ---
root = tk.Tk()
root.title("Cinder Wisp Macro")
root.geometry("350x250")
root.configure(bg="#1c1c1c")

icon_path = resource_path("icon.ico")
try:
    root.iconbitmap(icon_path)
except Exception as e:
    print("Icon load failed:", e)  # safe fallback for macOS

tk.Label(root, text="Cinder Wisp Macro", fg="orange", bg="#1c1c1c", font=("Segoe UI", 18, "bold")).pack(pady=20)

start_button = tk.Button(root, text="Start", command=toggle_macro, bg="green", fg="white",
                         activebackground="green", relief="flat", width=12, height=2)
start_button.pack(pady=10)

tk.Button(root, text="Change Keybind", command=change_keybind, bg="#333", fg="white",
          activebackground="#555", relief="flat").pack(pady=10)

keybind_label = tk.Label(root, text=f"Current Keybind: {config['SETTINGS']['keybind']}",
                         fg="white", bg="#1c1c1c", font=("Segoe UI", 10))
keybind_label.pack(pady=5)

root.bind("<Key>", on_key_press_global)
root.mainloop()

