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
import queue
import traceback
import tkinter as tk
from tkinter import ttk, messagebox
from pynput import keyboard

# --- Handle file paths in both source and PyInstaller EXE/app ---
def resource_path(relative_path: str) -> Path:
    """Get absolute path to resource (works for dev and PyInstaller)."""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / relative_path  # Inside PyInstaller bundle
    else:
        return Path(__file__).resolve().parent / relative_path

# --- Cross-platform key input handling ---
if platform.system() == "Windows":
    import pydirectinput as pd
else:
    pd = pyautogui  # fallback for macOS/Linux

# --- Config handling (persistent settings) ----
def get_settings_path():
    app_dir = Path(os.getenv('APPDATA') or Path.home() / ".config") / "CinderWispMacro"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir / "settings.ini"

config = configparser.ConfigParser()
settings_path = get_settings_path()

# Default settings
default_keybind = "F8"
default_delay = 0.0

def save_settings():
    with open(settings_path, "w") as f:
        config.write(f)

def load_settings():
    """Load settings, falling back to defaults for anything missing or invalid."""
    try:
        config.read(settings_path)
    except configparser.Error as e:
        print("Settings file is corrupt, resetting to defaults:", e)
        config.clear()

    if not config.has_section("SETTINGS"):
        config.add_section("SETTINGS")
    settings = config["SETTINGS"]

    if not settings.get("keybind", "").strip():
        settings["keybind"] = default_keybind

    try:
        loaded_delay = max(0.0, float(settings.get("delay", default_delay)))
    except ValueError:
        loaded_delay = default_delay
    settings["delay"] = str(loaded_delay)

    save_settings()
    return loaded_delay

# --- Global vars ---
letter_folder = resource_path("./Original")
template_folder = resource_path("./Letters")
binarize_thresh = 140
delay = load_settings()
pd.PAUSE = 0.05

# Stop signal for the currently running macro thread (None when stopped)
current_stop_event = None
macro_thread = None

# Work handed from background threads to the Tk main thread
ui_queue = queue.Queue()

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
    templates = [
        p for p in template_folder.iterdir()
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg")
    ]

    quadrants = split_into_4(screenshot)
    for screenshot in quadrants:
        matches = {}
        for template_path in templates:
            template = cv.imread(str(template_path), cv.IMREAD_GRAYSCALE)
            if template is None:
                print("Template load failed:", template_path)
                continue

            match_result = cv.matchTemplate(screenshot, template, cv.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv.minMaxLoc(match_result)
            print(f"Matching {template_path.stem} yielded confidence {max_val:.2f}")
            # Normal and purple templates share a letter, so keep the better score of the two
            letter = template_path.stem.replace("_purple", "").lower()
            matches[letter] = max(matches.get(letter, -1.0), max_val)

        if matches:
            best_match = max(matches, key=matches.get)
            confidence = matches[best_match]
            print(f"Best match for quadrant: {best_match} with confidence {confidence:.2f}")
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

def macro_loop(stop_event, previous_thread):
    # Never let two loops press keys at once: wait for the last run to fully exit
    if previous_thread is not None:
        previous_thread.join()

    try:
        while not stop_event.is_set():
            window = get_window()
            if "roblox" not in window.lower():
                stop_event.wait(0.1)
                continue

            ss = screenshot()
            letters = get_letters(ss)

            print("Detected letters:", letters)

            if len(letters) == 0:
                stop_event.wait(0.1)
                continue
            for letter in letters:
                if stop_event.is_set():
                    break
                pd.press(letter)
                stop_event.wait(delay)
    except Exception as e:
        traceback.print_exc()
        message = f"The macro stopped because of an error:\n\n{type(e).__name__}: {e}"
        ui_queue.put(lambda: on_macro_crashed(stop_event, message))
    finally:
        print("Macro stopped.")

# --- GUI Logic ---
def set_button_running(is_running):
    if is_running:
        start_button.config(text="Stop", bg="red", activebackground="red")
    else:
        start_button.config(text="Start", bg="green", activebackground="green")

def toggle_macro():
    global current_stop_event, macro_thread
    if current_stop_event is None:
        current_stop_event = threading.Event()
        macro_thread = threading.Thread(
            target=macro_loop, args=(current_stop_event, macro_thread), daemon=True
        )
        macro_thread.start()
        set_button_running(True)
    else:
        current_stop_event.set()
        current_stop_event = None
        set_button_running(False)

def on_macro_crashed(stop_event, message):
    global current_stop_event
    # Only reset the UI if the crashed run is still the active one
    if stop_event is current_stop_event:
        stop_event.set()
        current_stop_event = None
        set_button_running(False)
        messagebox.showerror("Macro Error", message)

# --- Global hotkey handling ---
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
MACRO_KEYS = {"c", "v", "x", "z"}  # keys the macro presses itself
keybind_window = None

def normalize_key(name):
    name = name.strip().lower()
    return KEY_ALIASES.get(name, name)

def display_key(name):
    name = normalize_key(name)
    return name.upper() if len(name) <= 3 else name.replace("_", " ").title()

def pynput_key_name(key):
    if isinstance(key, keyboard.KeyCode):
        return key.char.lower() if key.char else None
    return key.name.lower()

def on_global_key_press(key):
    # Runs on pynput's listener thread, so hand off to the Tk thread
    name = pynput_key_name(key)
    if name is not None:
        ui_queue.put(lambda: handle_hotkey(name))

def handle_hotkey(name):
    if keybind_window is not None:
        set_keybind(name)
    elif name == normalize_key(config["SETTINGS"]["keybind"]):
        toggle_macro()

def set_keybind(name):
    if name in MACRO_KEYS:
        keybind_prompt.config(text=f"{display_key(name)} is used by the macro.\nPress a different key...")
        return
    config["SETTINGS"]["keybind"] = name
    save_settings()
    keybind_label.config(text=f"Current Keybind: {display_key(name)}")
    close_keybind_window()

def close_keybind_window():
    global keybind_window
    if keybind_window is not None:
        keybind_window.destroy()
        keybind_window = None

def change_keybind():
    global keybind_window, keybind_prompt
    if keybind_window is not None:
        keybind_window.lift()
        return

    keybind_window = tk.Toplevel(root)
    keybind_window.title("Set Keybind")
    keybind_window.geometry("300x150")
    keybind_window.configure(bg="#1c1c1c")
    keybind_window.protocol("WM_DELETE_WINDOW", close_keybind_window)
    keybind_prompt = tk.Label(keybind_window, text="Press a new key...", fg="orange", bg="#1c1c1c",
                              font=("Segoe UI", 12))
    keybind_prompt.pack(expand=True)

def poll_ui_queue():
    while True:
        try:
            callback = ui_queue.get_nowait()
        except queue.Empty:
            break
        callback()
    root.after(30, poll_ui_queue)

def update_delay():
    global delay
    try:
        val = float(delay_entry.get())
        delay = max(0.0, val)
        config["SETTINGS"]["delay"] = str(delay)
        save_settings()
    except ValueError:
        messagebox.showerror("Invalid Input", "Delay must be a number (seconds).")

# --- Tkinter Window ---
root = tk.Tk()
root.title("Cinder Wisp Macro")
root.geometry("350x300")
root.configure(bg="#1c1c1c")

icon_path = resource_path("Cinder_Icon.ico")

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

keybind_label = tk.Label(root, text=f"Current Keybind: {display_key(config['SETTINGS']['keybind'])}",
                         fg="white", bg="#1c1c1c", font=("Segoe UI", 10))
keybind_label.pack(pady=5)

# --- Delay Input ---
delay_frame = tk.Frame(root, bg="#1c1c1c")
delay_frame.pack(pady=10)
tk.Label(delay_frame, text="Delay (seconds):", fg="white", bg="#1c1c1c", font=("Segoe UI", 10)).pack(side="left", padx=5)
delay_entry = tk.Entry(delay_frame, width=8, justify="center")
delay_entry.insert(0, str(delay))
delay_entry.pack(side="left")
tk.Button(delay_frame, text="Apply", command=update_delay, bg="#333", fg="white",
          activebackground="#555", relief="flat").pack(side="left", padx=6)

# Listen for the hotkey system-wide, so it works while Roblox is focused
try:
    keyboard.Listener(on_press=on_global_key_press, daemon=True).start()
except Exception as e:
    messagebox.showwarning(
        "Hotkey Unavailable",
        f"Couldn't listen for the global hotkey ({e}).\nUse the Start/Stop button instead.",
    )

poll_ui_queue()
root.mainloop()
