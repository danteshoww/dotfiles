# Youcifer Wallpaper V2

A lightweight Windows wallpaper **switcher** built with **Python** that
**actually changes the Windows wallpaper** — the same one you see in
*Settings → Personalization → Background*. No overlay windows, no
"covering up" the real wallpaper. It lives entirely in the **system tray**:
no main window.

| Image 1 | Image 2 |
| :---: | :---: |
| <img src="https://github.com/user-attachments/assets/b1de61a3-9dc2-4b87-8b38-54d4fc68e868" width="100%" alt="Screenshot 1"> | <img src="https://github.com/user-attachments/assets/7822f298-fb9f-432f-8200-110d33624887" width="100%" alt="Screenshot 2"> |
| <img src="https://github.com/user-attachments/assets/c24919a4-f061-4fc3-8274-e0b7597427ee" width="100%" alt="Screenshot 3"> | <img src="https://github.com/user-attachments/assets/63babb55-314b-4dfb-ab5e-4acf08fa58cd" width="100%" alt="Screenshot 4"> |
## How it's different from WinWallpaper / Wallpaper Engine

Those apps render a hidden window **behind your desktop icons** (the
`Progman`/`WorkerW` trick) that covers the real wallpaper — it's the only way
to show *animated/video* wallpapers, but it "overrides" rather than changes.

This app goes the native route instead:

- **Primary:** the `IDesktopWallpaper` COM interface (Windows 10/11) —
  the official API. Supports **per-monitor** wallpapers and fit styles.
- **Fallback:** `SystemParametersInfoW(SPI_SETDESKWALLPAPER)` + registry
  style keys (older systems, all monitors).

Because it changes the real wallpaper, everything is permanent: it survives
reboots, shows up in Windows Settings, and works even if this app is closed.

## Features

- 🖥️ **System tray icon** — left-click toggles the switcher; right-click
  opens the menu: `Open Switcher / Random Wallpaper / Choose Folder… /
  Fit Style / Monitor / Slideshow / Exit`. Status messages show as the
  tray tooltip.
- ⌨️ **Global hotkey `Alt+W`** — opens a quick switcher anywhere.
  **Five layouts**, selectable from the tray menu or the pill tab toolbar
  built into every switcher (`"layout"` in `settings.json`); picking a tab
  swaps switchers in place and reopens immediately:
  - **Grid Cards** (Tkinter) — floating rounded bar with a search field
    and a horizontal row of wallpaper cards (plus a "⇄ Random Wallpaper"
    card). Thumbnails are cached across opens, so it opens instantly.
  - **Strips** (Qt) — accordion: the active strip grows wide while its
    neighbours share the rest of the row.
  - **Hearthstone** (Qt) — hand-of-cards fan: portrait cards overlap
    heavily and lean outward along a downward arc; the active card is
    raised above the fan, upright and undimmed. Cards entering/leaving
    the fan appear/vanish instantly; the fan itself glides with a short
    smooth snap.
  - **Drift** (Qt) — 4×2 grid where rows read in alternating directions
    (serpentine/boustrophedon sweep), so it's visibly different from
    Gallery's plain grid.
  - **Gallery** (Qt) — dense 6×6 grid, nearly full-screen height.
  All four Qt layouts share one window, one decode pool and one painter:
  a filename/count caption pill sits under the cards, the selected card
  gets an accent outline with a soft glow, and thumbnails load in a
  background thread pool with a cross-open cache (no re-decode on
  reopen). `Esc` closes; the hotkey is changeable in `settings.json`
  (`"hotkey": "alt+w"` — supports `ctrl`, `shift`, `win` modifiers and
  `f1`–`f12` keys).
- 🚀 **Fast first open** — Qt (platform plugin + Windows font database)
  and thumbnails are warmed up shortly after startup, so the first
  Alt+W is as fast as every later one; the gallery window is kept alive
  between opens instead of being rebuilt each time.
- ✒️ **Alexandria font** across the switcher UI (bundled in `assets/fonts`,
  registered privately per-process for Tk and via `QFontDatabase` for
  Qt — nothing is installed system-wide)
- 🖼️ Crop-to-fill **thumbnails**, cached and loaded lazily in chunks —
  the switcher opens instantly and scrolling stays smooth
- 🖥️ **Per-monitor** wallpaper selection (or all monitors at once)
- 📐 Fit styles: Fill, Fit, Stretch, Tile, Center, Span
- ⏱️ **Slideshow** — auto-rotate the folder with shuffle, 5–60 min intervals
- 🕘 **History** of applied wallpapers (kept in `settings.json`)
- 🪶 Light dependencies — Python, Tkinter, Pillow, ctypes
  (and optionally PyQt6 for the four Qt layouts)

## Run

```
run.bat            (recommended — starts silently via pythonw)
```

or `pythonw app.py` (use `python app.py` to see errors in a console).

## Tray menu

| Item | Action |
|---|---|
| Open Switcher (Alt+W) | show/hide the quick switcher |
| Random Wallpaper | apply a random image from the folder |
| Choose Folder… | native folder picker — the switcher shows its images |
| Fit Style | Fill / Fit / Stretch / Tile / Center / Span |
| Monitor | all monitors or a specific one (with resolutions) |
| Slideshow | start / **turn off** + interval (5–60 min) |
| Switcher Layout | Grid Cards / Strips / Hearthstone / Drift / Gallery |
| Start with Windows | launch the app automatically at login (HKCU Run key, ✓ shows state) |
| Exit | clean shutdown, releases the hotkey and tray icon |

## Switcher layouts at a glance

| Layout | Toolkit | Look |
|---|---|---|
| Grid Cards | Tkinter | floating bar, search field, card row |
| Strips | PyQt6 | horizontal accordion of tall strips |
| Hearthstone | PyQt6 | fanned hand of cards, active card raised & centred |
| Drift | PyQt6 | 4×2 grid, serpentine reading order |
| Gallery | PyQt6 | dense 6×6 full-height grid |

Every switcher carries the same layout-toolbar pill so you can switch
layouts without touching the tray menu. Navigation in all layouts:
mouse wheel / ← → keys to browse, Enter (or click on the selected card)
to apply, Esc to close.

## Requirements

- Windows 10/11
- Python 3.8+ (standard library only)
- Pillow (for thumbnails): `pip install pillow`
- PyQt6 (for the Strips / Hearthstone / Drift / Gallery layouts):
  `pip install PyQt6` — optional; without it the switcher falls back to
  the Grid Cards layout, everything else still works

## Files

| File | Purpose |
|---|---|
| `app.py` | Headless controller: hotkey + tray + switcher wiring + Qt warm-up |
| `switcher.py` | Grid Cards switcher bar (Tkinter) + layout toolbar |
| `switcher_flow.py` | Qt gallery: Strips / Hearthstone / Drift / Gallery layouts, painter, thumbnail loader, shared caches |
| `caption.py` | Shared caption-pill spec (text format, spacing, colours) for both switchers |
| `tray.py` | System tray icon + popup menu (pure ctypes) |
| `hotkey.py` | Global hotkey registration (RegisterHotKey, pure ctypes) |
| `fonts.py` | Bundled Alexandria font loader (AddFontResourceExW, FR_PRIVATE) |
| `wallpaper_api.py` | Native Windows wallpaper API (ctypes COM wrapper + fallback) |
| `settings.json` | Persisted folder / style / layout / slideshow / hotkey / history |
| `run.bat` | Launcher (pythonw — no console window) |

## Notes / limitations

- Static images only — Windows has **no API** for video wallpapers; that's
  why Wallpaper Engine uses the overlay technique this app deliberately avoids.
- The slideshow only runs while the app is open (it can stay hidden in the tray).
- Each Qt layout tunes its own scroll animation (duration/easing) via
  constants at the top of its layout class in `switcher_flow.py`.
