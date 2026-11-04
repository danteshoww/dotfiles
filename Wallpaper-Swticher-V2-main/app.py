"""
Youcifer Wallpaper — a tray-resident Windows wallpaper switcher.

No main window: press Alt+W for the quick switcher bar, and use the
system tray icon (right-click menu) to choose the wallpaper folder,
fit style, monitor and slideshow. Changes the REAL Windows wallpaper
(visible in Settings > Personalization).

Run:  pythonw app.py   (or python app.py)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import random
import sys
import threading
import tkinter as tk
from tkinter import filedialog
import winreg

from hotkey import GlobalHotkey
from switcher import SwitcherBar

try:
    import switcher_flow
    from switcher_flow import FlowBar, HAVE_QT
    FLOW_OK = HAVE_QT
except (ImportError, Exception):
    FLOW_OK = False

from tray import TrayIcon
from wallpaper_api import POSITION_NAMES, POSITIONS, WallpaperManager
import fonts

fonts.register()  # load bundled Alexandria TTFs into this process

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
LOG_PATH = os.path.join(APP_DIR, "app.log")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
SKIP_DIRS = {"__pycache__", ".git", ".svn", "$RECYCLE.BIN",
             "System Volume Information", "node_modules", ".cache"}
MAX_THUMBS = 2000          # was 400 — see note below


def _setup_logging() -> None:
    """File logging for the whole process.

    Running via `pythonw.exe` (no console, e.g. the "Start with Windows"
    autostart entry) means anything logged only to stderr vanishes into
    nowhere. Rotate so the file can't grow forever.
    """
    root = logging.getLogger()
    if root.handlers:
        return  # already configured (e.g. re-imported under a test runner)
    root.setLevel(logging.INFO)
    try:
        handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    except OSError:
        pass  # e.g. read-only install dir — logging just goes nowhere


_setup_logging()

STYLES = list(POSITION_NAMES.keys())
INTERVALS = (5, 10, 15, 30, 60)
LAYOUT_NAMES = {
    "grid":        "Grid Cards",
    "strip":       "Strips",
    "hearthstone": "Hearthstone",
    "drift":       "Drift",
    "gallery":     "Gallery",
}
QT_LAYOUTS = {"strip", "hearthstone", "drift", "gallery"}


def default_folder() -> str:
    pics = os.path.join(os.path.expanduser("~"), "Pictures")
    return pics if os.path.isdir(pics) else os.path.expanduser("~")


class WallpaperApp:
    """Headless controller: global hotkey + tray icon + switcher bar."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.withdraw()  # no main window — tray only
        self.manager = WallpaperManager()

        self.settings = self._load_settings()
        self.folder = self.settings.get("folder", default_folder())
        self.history: list[str] = list(self.settings.get("history", []))
        self.last_applied = self.history[0] if self.history else ""
        self.style = self.settings.get("style", "fill")
        if self.style not in STYLES:
            self.style = "fill"
        self.monitor = self.settings.get("monitor")   # None = all monitors
        self.interval = int(self.settings.get("interval", 15))
        self.shuffle = bool(self.settings.get("shuffle", True))
        self.layout = self.settings.get("layout", "grid")
        if self.layout == "flow":          # legacy value from settings.json
            self.layout = "strip"
        if self.layout not in LAYOUT_NAMES:
            self.layout = "grid"
        # exposed for the switchers' layout tabs (both the Qt `_Toolbar`
        # and the Tk bar's toolbar read labels from here — single source)
        self.layout_names = LAYOUT_NAMES

        self.images: list[str] = []
        self._slideshow_running = False
        self._slide_job = None
        self._slide_index = 0

        self.monitors: list[dict] = []
        try:
            self.monitors = self.manager.monitors()
        except OSError:
            self.monitors = []

        # quick switcher bar (child of the hidden root)
        self._make_switcher()

        # global hotkey + tray icon, both marshal commands via one queue
        self._events: queue.Queue = queue.Queue()
        self._poll_job = None
        self.hotkey = GlobalHotkey(
            self.settings.get("hotkey", "alt+w"),
            lambda: self._events.put("toggle"))
        self.tray = TrayIcon(
            notify=self._events.put, menu_provider=self._tray_menu,
            tooltip="Youcifer Wallpaper — Alt+W to switch")
        self.hotkey.start()
        self.tray.start()
        self._poll_events()

        self._load_folder(self.folder)

    # -- switcher layout -----------------------------------------------------------

    def _make_switcher(self) -> None:
        if FLOW_OK and self.layout in QT_LAYOUTS:
            self.switcher = FlowBar(self, layout=self.layout)
            self._schedule_qt_prewarm(self.switcher.prewarm)
        else:
            self.switcher = SwitcherBar(self)
            # Warm Qt even while the Tk switcher is the active one, so
            # switching to a Qt layout later (toolbar tab or tray menu)
            # doesn't pay the one-time font-database init on the spot.
            # prewarm_qt() is process-global and idempotent — a second
            # call after a layout switch is a cheap no-op, not a re-block
            # of the UI thread right after the new bar appears.
            self._schedule_qt_prewarm(self._prewarm_qt)

    def _schedule_qt_prewarm(self, fn) -> None:
        try:
            self.root.after(300, fn)
        except Exception:
            pass

    def _prewarm_qt(self) -> None:
        if FLOW_OK:
            switcher_flow.prewarm_qt()


    # -- tray menu ---------------------------------------------------------------

    def _tray_menu(self) -> list:
        fit = [(name, f"style:{key}", key == self.style)
               for key, name in POSITION_NAMES.items()]
        mons = [("All monitors", "monitor:all", self.monitor is None)]
        for m in self.monitors:
            w, h = m["size"]
            mons.append((f"Monitor {m['index']}  ({w}x{h})",
                         f"monitor:{m['index']}",
                         self.monitor == m["index"]))
        slide = [("Turn Off Slideshow" if self._slideshow_running
                  else "Start Slideshow", "slide", False), ("-",)]
        slide += [(f"Every {n} min", f"interval:{n}", self.interval == n)
                  for n in INTERVALS]
        layout = [(name, f"layout:{key}", self.layout == key)
                  for key, name in LAYOUT_NAMES.items()]
        return [
            ("Open Switcher  (Alt+W)", "toggle", False),
            ("Random Wallpaper", "random", False),
            ("Choose Folder…", "folder", False),
            ("-",),
            ("Fit Style", fit),
            ("Monitor", mons),
            ("Slideshow", slide),
            ("Switcher Layout", layout),
            ("Start with Windows", "startup", self._startup_enabled()),
            ("-",),
            ("Exit", "exit", False),
        ]

    # -- event pump (hotkey + tray commands) --------------------------------------

    def _poll_events(self) -> None:
        try:
            if self.hotkey.failed and not getattr(
                    self, "_hotkey_warned", False):
                self._hotkey_warned = True
                self._status(f"Hotkey {self.hotkey.combo} could not be "
                             f"registered (already in use by another app)")
            while True:
                event = self._events.get_nowait()
                if isinstance(event, tuple):
                    kind = event[0]
                    if kind == "folder_scanned":
                        self._on_folder_scanned(event[1], event[2])
                    elif kind == "folder_scan_error":
                        self._status(f"Cannot read folder: {event[1]}")
                    continue
                if event == "toggle":
                    self.switcher.toggle()
                elif event == "folder":
                    self._choose_folder()
                elif event == "random":
                    self._random()
                elif event == "slide":
                    self._toggle_slideshow()
                elif event == "startup":
                    self._toggle_startup()
                elif event == "exit":
                    self._on_close()
                    return
                elif event.startswith("style:"):
                    self.style = event[6:]
                    self._save_settings()
                    self._status(f"Fit style: {POSITION_NAMES[self.style]}")
                elif event.startswith("monitor:"):
                    value = event[8:]
                    self.monitor = None if value == "all" else int(value)
                    self._save_settings()
                    self._status("Monitor: "
                                 + ("all" if self.monitor is None else value))
                elif event.startswith("interval:"):
                    self.interval = int(event[9:])
                    self._save_settings()
                    self._status(f"Slideshow interval: {self.interval} min")
                elif event.startswith("layout:"):
                    value = event[7:]
                    if value != self.layout:
                        self.layout = value
                        self._save_settings()
                        self.switcher.destroy()
                        self._make_switcher()
                    self._status(f"Switcher layout: {LAYOUT_NAMES[value]}")
        except queue.Empty:
            pass
        except tk.TclError:
            return  # window already destroyed
        try:
            self._poll_job = self.root.after(120, self._poll_events)
        except tk.TclError:
            pass

    def _toggle_switcher(self) -> None:
        self.switcher.toggle()

    # -- status (shown as the tray tooltip) -----------------------------------------

    def _status(self, text: str) -> None:
        try:
            self.tray.set_tooltip(text[:127])
        except Exception:
            pass

    # -- folder & images ---------------------------------------------------------

    def _choose_folder(self) -> None:
        """Handler for the tray's "Choose Folder…" item.

        NOTE: this method was called from `_poll_events()` but never defined
        anywhere in the app — the only trace of it was the unused
        `filedialog` import at the top of the file. Clicking "Choose
        Folder…" was a guaranteed crash.
        """
        folder = filedialog.askdirectory(
            title="Choose a wallpaper folder", initialdir=self.folder,
            parent=self.root)
        if folder:
            self._load_folder(folder)

    def _load_folder(self, folder: str) -> None:
        """Kick off a background scan of `folder`; results land in
        `_poll_events()` via the same event queue used for hotkey/tray
        commands, so nothing here touches Tk/Qt state off the main thread.

        This used to walk the whole folder synchronously, right here, on
        the main thread — and at startup that ran *before* `root.mainloop()`
        even started, so the global hotkey and tray were technically
        "started" but nothing was pumping their queued events yet. On a
        big or slow (network/external drive) wallpaper folder that made
        the very first Alt+W after launch feel like it hung for several
        seconds: it wasn't the switcher being slow, the app just hadn't
        reached its event loop yet. Scanning on a thread means mainloop()
        starts immediately and Alt+W works right away; the switcher simply
        has no images until the scan reports back a moment later.
        """
        folder = os.path.normpath(folder)
        if not os.path.isdir(folder):
            self._status(f"Folder not found: {folder}")
            return
        self.folder = folder
        self._status(f"Scanning {folder}\u2026")
        threading.Thread(target=self._scan_folder_worker,
                         args=(folder,), daemon=True).start()

    def _scan_folder_worker(self, folder: str) -> None:
        """Runs off the main thread. Only touches local variables and the
        thread-safe event queue — never `self.images`, Tk, or Qt directly.
        """
        files: list[str] = []
        try:
            for root_dir, dirs, names in os.walk(folder):
                # prune in place so os.walk never descends into them
                dirs[:] = [d for d in dirs
                           if d not in SKIP_DIRS and not d.startswith(".")]
                for name in names:
                    if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                        files.append(os.path.join(root_dir, name))
        except OSError as exc:
            self._events.put(("folder_scan_error", str(exc)))
            return
        # normcase+realpath dedupe; sort so the order is stable across scans
        seen: set[str] = set()
        uniq: list[str] = []
        for p in files:
            key = os.path.normcase(os.path.realpath(p))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(p)
        uniq.sort()
        if MAX_THUMBS:
            uniq = uniq[:MAX_THUMBS]
        self._events.put(("folder_scanned", folder, uniq))

    def _on_folder_scanned(self, folder: str, uniq: list[str]) -> None:
        if folder != self.folder:
            return  # a newer "Choose Folder…" pick already superseded this
        self.images = uniq
        self.switcher.on_images_changed()
        self._save_settings()
        self._status(f"{len(uniq)} wallpapers — press Alt+W to switch")

    # -- applying -----------------------------------------------------------------

    def _random(self) -> None:
        if self.images:
            self._apply_path(random.choice(self.images))

    def _apply_path(self, path: str) -> None:
        if not os.path.isfile(path):
            self._status(f"File not found: {path}")
            return
        self.last_applied = path
        self._set_wallpaper_now(path)

    def _set_wallpaper_now(self, path: str) -> None:
        try:
            self.manager.set_wallpaper(path, monitor_index=self.monitor,
                                       style=self.style)
        except Exception as exc:
            self._status(f"Could not set wallpaper: {exc}")
            return
        self._add_history(path)
        self._save_settings()
        target = "all monitors" if self.monitor is None \
            else f"monitor {self.monitor}"
        self._status(f"Wallpaper set to {os.path.basename(path)} "
                     f"({self.style}, {target})")

    def _add_history(self, path: str) -> None:
        if path in self.history:
            self.history.remove(path)
        self.history.insert(0, path)
        del self.history[30:]

    # -- start with Windows (HKCU Run key) ----------------------------------------

    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    RUN_NAME = "YouciferWallpaper"

    def _startup_command(self) -> str:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pythonw if os.path.isfile(pythonw) else sys.executable
        return f'"{exe}" "{os.path.join(APP_DIR, "app.py")}"'

    def _startup_enabled(self) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.RUN_KEY) as k:
                value, _ = winreg.QueryValueEx(k, self.RUN_NAME)
                return bool(value)  # an empty leftover value counts as OFF
        except OSError:
            return False

    def _toggle_startup(self) -> None:
        try:
            if self._startup_enabled():
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER, self.RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, self.RUN_NAME)
                self._status("Start with Windows: OFF")
            else:
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER, self.RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, self.RUN_NAME, 0, winreg.REG_SZ,
                                      self._startup_command())
                self._status("Start with Windows: ON")
        except OSError as exc:
            self._status(f"Could not change startup setting: {exc}")

    # -- slideshow ------------------------------------------------------------------

    def _toggle_slideshow(self) -> None:
        if self._slideshow_running:
            self._stop_slideshow()
        else:
            self._slideshow_running = True
            self._status(f"Slideshow started — every {self.interval} min")
            self._slideshow_tick()

    def _stop_slideshow(self) -> None:
        self._slideshow_running = False
        if self._slide_job is not None:
            self.root.after_cancel(self._slide_job)
            self._slide_job = None
        self._status("Slideshow stopped")

    def _slideshow_tick(self) -> None:
        if not self._slideshow_running:
            return
        if not self.images:
            self._stop_slideshow()
            self._status("Slideshow stopped — no images in folder")
            return
        if self.shuffle:
            path = random.choice(self.images)
        else:
            path = self.images[self._slide_index % len(self.images)]
            self._slide_index += 1
        self._apply_path(path)
        delay = max(1, self.interval) * 60_000
        self._slide_job = self.root.after(delay, self._slideshow_tick)

    # -- settings ----------------------------------------------------------------------

    def _load_settings(self) -> dict:
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_settings(self) -> None:
        data = {
            "folder": self.folder,
            "style": self.style,
            "monitor": self.monitor,
            "interval": self.interval,
            "shuffle": self.shuffle,
            "layout": self.layout,
            "hotkey": self.settings.get("hotkey", "alt+w"),
            "history": self.history,
        }
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass

    # -- shutdown -------------------------------------------------------------------------

    def _on_close(self) -> None:
        # Idempotent: guard against re-entry if shutdown is slow.
        if getattr(self, "_closing", False):
            return
        self._closing = True

        # Stop background work first so nothing schedules new Tk callbacks
        # while we're tearing down.
        self._stop_slideshow()
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None

        if self.switcher is not None:
            try:
                self.switcher.destroy()
            except Exception:
                pass
            self.switcher = None

        self._save_settings()

        # Stop the tray + hotkey threads — their message loops will exit
        # once they receive the quit/close message.
        self.hotkey.stop()
        self.tray.stop()

        # Schedule the actual mainloop exit. We can't safely call
        # root.destroy() from inside an after-callback (it deadlocks Tk),
        # so defer it by a tick.
        try:
            self.root.after(20, self._shutdown_now)
        except tk.TclError:
            pass

        # Hard fallback: if Tk is wedged for any reason, force-exit after
        # a short delay so the process never hangs in the tray.
        try:
            self.root.after(1500, lambda: os._exit(0))
        except tk.TclError:
            os._exit(0)

    def _shutdown_now(self) -> None:
        """Actually tear down the Tk root. Called from a fresh after-callback."""
        try:
            self.root.quit()   # break out of mainloop
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def main() -> None:
    root = tk.Tk()
    WallpaperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
