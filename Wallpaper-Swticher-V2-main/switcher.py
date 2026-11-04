"""
switcher.py — Wallpaper-Engine-style quick switcher bar (Alt+W).

A borderless, topmost, rounded floating bar with a search field and a
horizontal row of wallpaper cards. Click a card (or press Enter on the
selected one) to apply the wallpaper through the native API.

Performance notes:
- filtering is debounced (rebuilds once, not on every keystroke)
- thumbnails are cropped-to-fill with a fast resampling filter and cached
- selection highlight only repaints the two affected cards
- the mouse wheel scrolls the slider from anywhere over the bar with a
  smooth ease-out glide (animated at ~60 fps), not a jumpy per-notch step
"""

from __future__ import annotations

import math
import os
import queue
import random
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

import fonts

try:
    from PIL import Image, ImageOps, ImageTk
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

if HAVE_PIL:
    try:
        RESAMPLE = Image.Resampling.BILINEAR
    except AttributeError:  # Pillow < 9.1
        RESAMPLE = Image.BILINEAR


class _ThumbWorker(threading.Thread):
    """Decodes wallpapers to thumbnail-sized PIL images off the Tk thread.

    PIL's decode never touches Tk state, so it is safe to run here. We hand
    back a PIL Image; the Tk thread does the cheap ImageTk.PhotoImage
    conversion, which *is* a Tk call and must happen on the main thread.
    """

    def __init__(self, requests: queue.Queue, results: queue.Queue):
        super().__init__(daemon=True)
        self._requests = requests
        self._results = results
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._requests.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return
            path, size = item
            try:
                with Image.open(path) as im:
                    im.draft("RGB", size)
                    im = ImageOps.fit(im.convert("RGB"), size, RESAMPLE)
                self._results.put((path, im))
            except Exception:
                self._results.put((path, None))

    def stop(self) -> None:
        self._stop.set()
        try:
            self._requests.put_nowait(None)
        except queue.Full:
            pass


class SwitcherBar:
    W = 1280
    H = 372
    R = 26                      # corner radius
    MAGIC = "#010203"           # transparent key color
    BG = "#202024"              # bar background
    INNER = "#17171a"           # search field background
    CARD = "#2a2a30"
    SEL_BG = "#cfe3f7"
    SEL_FG = "#1c2530"
    FG = "#e8e8ea"
    FG_DIM = "#8f9096"
    THUMB = (184, 152)
    CARD_W, CARD_H = 208, 236
    CARD_STEP = 220             # card width + 12px gap
    MAX_CARDS = 2000
    # Layout toolbar (mirrors `_Toolbar` in switcher_flow.py; its colours
    # are rgba() there — these are the same colours flattened onto the
    # bar's #202024 background, since Tk canvas items have no alpha)
    TOOLBAR_BG = "#191a21"
    TOOLBAR_BORDER = "#3d3e46"
    TAB_FG = "#9497a3"
    TAB_CHECKED_FG = "#eceef2"
    TAB_CHECKED_BG = "#303138"
    TAB_HOVER_BG = "#292a31"

    def __init__(self, app) -> None:
        self.app = app
        self.visible = False
        self.filtered: list[str] = []
        self.sel = 0
        self.query = ""
        self._thumb_cache: dict[str, object] = {}
        self._built_sig: tuple | None = None   # last rebuilt (query, paths)
        self._card_refs: list[tk.PhotoImage] = []
        self._card_widgets: list[dict] = []
        self._pending: list[tuple] = []
        self._thumb_requests: queue.Queue = queue.Queue()
        self._thumb_results: queue.Queue = queue.Queue()
        self._thumb_worker: _ThumbWorker | None = None
        self._thumb_poll_job = None
        # NOTE: `_rebuild_cards()` reads/cancels `self._thumb_job` on every
        # single card rebuild (every time the bar opens, you type a filter,
        # or the folder changes) but it was never initialized here, so that
        # was an instant AttributeError before a single card got drawn.
        self._thumb_job = None
        self._thumbs_in_flight = 0
        self._filter_job = None
        self._sel_prev = None
        self._scroll_pos = 0.0      # current scroll fraction (animated)
        self._scroll_target = 0.0   # where the wheel wants to be
        self._scroll_job = None
        self._last_tick: float | None = None
        self._placeholder_active = True

        self.top = tk.Toplevel(app.root)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.configure(bg=self.MAGIC)
        try:
            self.top.attributes("-transparentcolor", self.MAGIC)
        except tk.TclError:
            pass  # older Tk: square corners, still works

        self.canvas = tk.Canvas(self.top, bg=self.MAGIC,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._draw_shell()
        self._build_search()
        self._build_cards()
        self._build_layout_toolbar()

        self.top.bind("<Escape>", lambda e: self.hide())
        self.top.bind("<Return>", lambda e: self._apply_selected())
        self.top.bind("<Left>", lambda e: self._move(-1))
        self.top.bind("<Right>", lambda e: self._move(1))
        # the wheel scrolls the slider from anywhere over the bar
        # (binds on the toplevel so it fires over every child widget)
        self.top.bind("<MouseWheel>", self._on_wheel)
        self.top.bind("<Deactivate>", lambda e: self.hide())

    def _on_wheel(self, event) -> None:
        if not self.visible:
            return
        notches = event.delta / 120.0
        if not notches:
            notches = 1.0 if event.delta > 0 else -1.0
        # wheel up = toward the start, 120 px per notch; the bar then
        # glides smoothly to the target instead of jumping
        self._scroll_by(-notches * 120.0)

    # -- smooth scrolling (exponential ease-out at ~60 fps) ----------------------

    def _scroll_limits(self) -> tuple[float, float]:
        cc = self.cards_canvas
        view = max(1.0, float(cc.winfo_width()))
        content = max(1.0, float(self._content_w))
        return 0.0, max(0.0, (self._content_w - view) / content)

    def _scroll_reset(self) -> None:
        self._cancel_scroll_anim()
        self._scroll_pos = 0.0
        self._scroll_target = 0.0
        self.cards_canvas.xview_moveto(0)

    def _cancel_scroll_anim(self) -> None:
        if self._scroll_job is not None:
            self.top.after_cancel(self._scroll_job)
            self._scroll_job = None

    def _scroll_by(self, px: float) -> None:
        if self.cards_canvas.winfo_width() < 50:
            return  # not mapped yet
        frac = px / max(1.0, float(self._content_w))
        lo, hi = self._scroll_limits()
        self._scroll_target = min(hi, max(lo, self._scroll_target + frac))
        self._start_scroll_anim()

    def _scroll_to_px(self, px: float) -> None:
        if self.cards_canvas.winfo_width() < 50:
            return
        lo, hi = self._scroll_limits()
        frac = min(hi, max(lo, px / max(1.0, float(self._content_w))))
        self._scroll_target = frac
        self._start_scroll_anim()

    def _start_scroll_anim(self) -> None:
        if self._scroll_job is None:
            self._last_tick = None
            self._scroll_job = self.top.after(15, self._scroll_tick)

    def _scroll_tick(self) -> None:
        self._scroll_job = None
        if not self.visible:
            return
        cc = self.cards_canvas
        now = time.perf_counter()
        # frame-rate independent exponential ease-out (time constant 90ms):
        # fast start, soft landing, identical feel at any timer resolution
        dt = 0.016 if self._last_tick is None else min(0.05, now - self._last_tick)
        self._last_tick = now
        diff = self._scroll_target - self._scroll_pos
        if abs(diff) < 0.00035:
            self._scroll_pos = self._scroll_target
            cc.xview_moveto(self._scroll_pos)
            return
        self._scroll_pos += diff * (1.0 - math.exp(-dt / 0.09))
        cc.xview_moveto(self._scroll_pos)
        self._scroll_job = self.top.after(15, self._scroll_tick)

    # -- shell ---------------------------------------------------------------

    @staticmethod
    def _round_rect_pts(x1, y1, x2, y2, r):
        return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
                x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
                x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]

    def _draw_shell(self) -> None:
        c = self.canvas
        w, h, r = self.W, self.H, self.R
        c.create_polygon(
            self._round_rect_pts(6, 6, w - 6, h - 6, r),
            smooth=True, fill=self.BG, outline="#2f3036", width=1)

    def _build_search(self) -> None:
        wrap = tk.Frame(self.canvas, bg=self.BG)
        self.canvas.create_window((28, 24), anchor="nw", window=wrap,
                                  width=self.W - 56, height=48)
        bar = tk.Frame(wrap, bg=self.INNER)
        bar.pack(fill="both", expand=True)
        tk.Label(bar, text="Select Wallpaper", bg=self.INNER,
                 fg=self.FG_DIM, font=fonts.f(10),
                 padx=14).pack(side="left")
        self.entry = tk.Entry(
            bar, bg=self.INNER, fg=self.FG_DIM, relief="flat",
            insertbackground=self.FG, font=fonts.f(10),
            highlightthickness=0)
        self.entry.pack(side="left", fill="both", expand=True,
                        padx=(0, 12), ipady=7)
        self.entry.insert(0, "Type to filter…")
        self.entry.bind("<FocusIn>", self._entry_focus_in)
        self.entry.bind("<FocusOut>", self._entry_focus_out)
        self.entry.bind("<KeyRelease>", self._on_type)

    def _entry_focus_in(self, _e=None) -> None:
        if self._placeholder_active:
            self.entry.delete(0, "end")
            self.entry.config(fg=self.FG)
            self._placeholder_active = False

    def _entry_focus_out(self, _e=None) -> None:
        if not self.entry.get():
            self.entry.insert(0, "Type to filter…")
            self.entry.config(fg=self.FG_DIM)
            self._placeholder_active = True

    def _on_type(self, _e=None) -> None:
        # debounce: rebuild the card row once typing pauses, not per key
        if self._filter_job is not None:
            self.top.after_cancel(self._filter_job)
        self._filter_job = self.top.after(180, self._run_filter)

    def _run_filter(self) -> None:
        self._filter_job = None
        query = "" if self._placeholder_active else \
            self.entry.get().strip().lower()
        if query == self.query:
            return
        self.query = query
        self.refresh()

    # -- card row ---------------------------------------------------------------

    def _build_cards(self) -> None:
        self._card_bg_items: list[int] = []
        self._card_text_items: list[list[int]] = []
        self._content_w = 1
        # NOTE: no xscrollincrement — a positive value makes Tk snap all
        # scrolling to that many pixels, which kills smooth animation.
        self.cards_canvas = tk.Canvas(
            self.canvas, bg=self.BG, highlightthickness=0, bd=0)
        # height leaves a 44px bottom band (plus gap) free for the layout
        # toolbar — same reservation `_layout_children` makes in the Qt
        # gallery; the card row is CARD_H tall so nothing is clipped.
        self.canvas.create_window(
            (28, 84), anchor="nw", window=self.cards_canvas,
            width=self.W - 56, height=self.CARD_H)

    def refresh(self) -> None:
        """Rebuild the card row from the app's images filtered by query.

        Skips the (expensive) rebuild when the card set is unchanged —
        e.g. re-opening the bar with the same folder — and only repaints
        the selection."""
        q = self.query
        self.filtered = [p for p in self.app.images
                         if q in os.path.basename(p).lower()]
        self.filtered = self.filtered[:self.MAX_CARDS]
        self.sel = 0
        self._sel_prev = None
        sig = (q, tuple(self.filtered))
        if sig == self._built_sig:
            self._paint_selection()
            return
        self._built_sig = sig
        self._rebuild_cards()
    def on_images_changed(self) -> None:
        """Called by the app whenever the wallpaper folder is reloaded."""
        self._thumb_cache.clear()
        self._card_refs.clear()
        self._built_sig = None  # force rebuild (thumbnails were discarded)
        if self.visible:
            self.refresh()

    def _rebuild_cards(self) -> None:
        cc = self.cards_canvas
        if self._thumb_job is not None:
            self.top.after_cancel(self._thumb_job)
            self._thumb_job = None
        self._pending.clear()
        self._card_refs.clear()
        self._card_bg_items.clear()
        self._card_text_items.clear()
        self._card_widgets.clear()
        cc.delete("all")
        self._scroll_reset()
        n = len(self.filtered)
        if not n:
            self._content_w = 1
            cc.configure(scrollregion=(0, 0, 1, 1))
            msg = (f'No wallpapers match "{self.query}"'
                   if self.query else "No wallpapers found")
            cc.create_text(12, 90, anchor="w", text=msg,
                           fill=self.FG_DIM, font=fonts.f(11))
            return
        self._content_w = n * self.CARD_STEP
        cc.configure(scrollregion=(0, 0, self._content_w, self.CARD_H))
        # slot 0 is the "Random Wallpaper" card; images occupy slots 1..n
        self._draw_card(0, "")
        for i, path in enumerate(self.filtered, 1):
            self._draw_card(i, path)
        self._paint_selection()
        self._schedule_thumbs()

    def _draw_card(self, i: int, path: str) -> None:
        cc = self.cards_canvas
        x = i * self.CARD_STEP
        tag = f"card{i}"
        bg = cc.create_polygon(
            self._round_rect_pts(x + 2, 2, x + self.CARD_W - 2,
                                 self.CARD_H - 2, 14),
            smooth=True, fill=self.CARD, outline="", tags=(tag,))
        texts: list[int] = []
        if i == 0:
            texts.append(cc.create_text(
                x + self.CARD_W // 2, 104, text="\u21c4", fill=self.FG,
                font=fonts.f(42, "bold"), tags=(tag,)))
            texts.append(cc.create_text(
                x + self.CARD_W // 2, self.CARD_H - 40,
                text="Random Wallpaper", fill=self.FG,
                font=fonts.f(10, "bold"), width=self.CARD_W - 20,
                tags=(tag,)))
        else:
            # image item is filled in later by the lazy thumbnail loader
            cc.create_image(x + 12, 12, anchor="nw",
                            tags=(tag, f"img{i}"))
            name = os.path.basename(path)
            if len(name) > 24:
                name = name[:23] + "…"
            texts.append(cc.create_text(
                x + self.CARD_W // 2, self.CARD_H - 36, text=name,
                fill=self.FG, font=fonts.f(9), width=self.CARD_W - 16,
                tags=(tag,)))
            self._pending.append((i, path))
        cc.tag_bind(tag, "<Button-1>",
                    lambda _e, idx=i: self._on_card_click(idx))
        cc.tag_bind(tag, "<Enter>",
                    lambda _e: cc.configure(cursor="hand2"))
        cc.tag_bind(tag, "<Leave>",
                    lambda _e: cc.configure(cursor=""))
        self._card_bg_items.append(bg)
        self._card_text_items.append(texts)
        self._card_widgets.append({"index": i, "path": path, "tag": tag})

    # -- thumbnails (lazy, chunked so the bar never stutters) -------------------

    def _schedule_thumbs(self) -> None:
        if not HAVE_PIL or not self._pending:
            return
        if self._thumb_worker is None:
            self._thumb_worker = _ThumbWorker(
                self._thumb_requests, self._thumb_results)
            self._thumb_worker.start()
        for i, path in self._pending:
            cached = self._thumb_cache.get(path)
            if cached is not None:
                # Rebuild (search filter, folder change): the thumbnail is
                # already decoded — attach it to the new card item right
                # here. Previously only the thumbnail *loader* ever called
                # itemconfig, so every cached path stayed a blank card
                # after a rebuild.
                try:
                    self.cards_canvas.itemconfig(f"img{i}", image=cached)
                except tk.TclError:
                    pass
                continue
            self._thumb_requests.put((path, self.THUMB))
            self._thumbs_in_flight += 1
        self._pending.clear()
        if self._thumbs_in_flight and self._thumb_poll_job is None:
            self._thumb_poll_job = self.top.after(16, self._poll_thumbs)

    def _poll_thumbs(self) -> None:
        self._thumb_poll_job = None
        if not self.visible:
            return
        drained = 0
        try:
            while drained < 12:           # cap per tick so the UI stays live
                path, im = self._thumb_results.get_nowait()
                drained += 1
                self._thumbs_in_flight = max(0, self._thumbs_in_flight - 1)
                if im is None:
                    continue
                try:
                    photo = ImageTk.PhotoImage(im)
                except Exception:
                    continue
                if len(self._thumb_cache) > 1000:
                    self._thumb_cache.clear()
                self._thumb_cache[path] = photo
                self._card_refs.append(photo)
                for card in self._card_widgets:
                    if card["path"] == path:
                        try:
                            self.cards_canvas.itemconfig(
                                f"img{card['index']}", image=photo)
                        except tk.TclError:
                            pass
                        break
        except queue.Empty:
            pass
        if self._thumbs_in_flight or drained:
            self._thumb_poll_job = self.top.after(16, self._poll_thumbs)

    def _thumb(self, path: str):
        if path in self._thumb_cache:
            return self._thumb_cache[path]
        if not HAVE_PIL:
            return None
        try:
            with Image.open(path) as im:
                im.draft("RGB", self.THUMB)          # fast JPEG decode
                # crop-to-fill: every thumbnail is exactly THUMB size,
                # centered — no more stretched / inconsistent previews
                im = ImageOps.fit(im.convert("RGB"), self.THUMB, RESAMPLE)
            photo = ImageTk.PhotoImage(im)
        except Exception:
            return None
        if len(self._thumb_cache) > 800:
            self._thumb_cache.clear()
        self._thumb_cache[path] = photo
        return photo


    # -- layout toolbar ---------------------------------------------------------

    def _build_layout_toolbar(self) -> None:
        """The layout-switcher pill tabs, matching the Qt layouts' toolbar.

        Same design as `_Toolbar` in switcher_flow.py: a rounded dark
        container centred at the bottom of the window with one pill tab
        per switcher layout, the active one highlighted. Picking a
        different layout goes through the same app event path the Qt
        pill's "Grid Cards" tab uses (layout:<key>, then toggle), so
        switching is a full swap between the two switcher windows.
        """
        c = self.canvas
        self._layout_tabs: dict[str, dict] = {}

        labels = self.app.layout_names
        font = fonts.f(9)
        f = tkfont.Font(font=font)
        btn_pad_x, btn_h = 16, 32
        frame_pad, spacing = 8, 4
        widths = {k: f.measure(name) + btn_pad_x * 2
                  for k, name in labels.items()}
        total = sum(widths.values()) + spacing * (len(labels) - 1) \
            + frame_pad * 2
        th = btn_h + 12
        x = (self.W - total) / 2
        y0 = self.H - 8 - th

        c.create_polygon(
            self._round_rect_pts(x, y0, x + total, y0 + th, 16),
            smooth=True, fill=self.TOOLBAR_BG,
            outline=self.TOOLBAR_BORDER, width=1, tags=("layoutbar",))

        bx = x + frame_pad
        by = y0 + 6
        for key, name in labels.items():
            w = widths[key]
            tag = f"laytab_{key}"
            pill = c.create_polygon(
                self._round_rect_pts(bx, by, bx + w, by + btn_h, 9),
                smooth=True, fill="", outline="", tags=(tag,))
            text = c.create_text(bx + w / 2, by + btn_h / 2, text=name,
                                 fill=self.TAB_FG, font=font, tags=(tag,))
            c.tag_bind(tag, "<Button-1>",
                       lambda _e, k=key: self._on_layout_tab(k))
            c.tag_bind(tag, "<Enter>",
                       lambda _e, k=key: self._tab_hover(k, True))
            c.tag_bind(tag, "<Leave>",
                       lambda _e, k=key: self._tab_hover(k, False))
            self._layout_tabs[key] = {"pill": pill, "text": text, "w": w}
            bx += w + spacing
        self._toolbar_set_current(self.app.layout)

    def _tab_hover(self, key: str, on: bool) -> None:
        # Only the pill polygon's fill changes — the tab's text shares
        # its tag with the polygon, so itemconfig-ing the tag would
        # recolour (or blank) the text as well.
        if key == self.app.layout:
            return  # checked style wins over hover
        self.canvas.itemconfig(self._layout_tabs[key]["pill"],
                               fill=self.TAB_HOVER_BG if on else "")

    def _toolbar_set_current(self, key: str) -> None:
        for k, tab in self._layout_tabs.items():
            checked = k == key
            self.canvas.itemconfig(
                tab["pill"],
                fill=self.TAB_CHECKED_BG if checked else "")
            self.canvas.itemconfig(
                tab["text"], fill=self.TAB_CHECKED_FG if checked
                else self.TAB_FG)

    def _on_layout_tab(self, key: str) -> None:
        if key == self.app.layout:
            return
        # Same path the Qt pill uses to hand over between switchers:
        # swap the layout, then reopen the (new) switcher immediately.
        self.app._events.put(f"layout:{key}")
        self.app._events.put("toggle")

    # -- selection ----------------------------------------------------------------

    def _paint_selection(self) -> None:
        """Repaint only the two affected cards (previous + new selection)."""
        cc = self.cards_canvas
        targets = {self.sel}
        if self._sel_prev is not None:
            targets.add(self._sel_prev)
        for idx in targets:
            if not 0 <= idx < len(self._card_bg_items):
                continue
            on = idx == self.sel
            cc.itemconfig(self._card_bg_items[idx],
                          fill=self.SEL_BG if on else self.CARD)
            for t in self._card_text_items[idx]:
                cc.itemconfig(t, fill=self.SEL_FG if on else self.FG)
        self._sel_prev = self.sel

    def _move(self, delta: int) -> None:
        n = len(self.filtered)
        if not n:
            return
        self.sel = min(n - 1, max(0, self.sel + delta))
        self._paint_selection()
        self._see_card(self.sel)

    def _see_card(self, idx: int) -> None:
        cc = self.cards_canvas
        if cc.winfo_width() < 50:
            return  # not mapped yet; avoid bogus scrolling
        x0 = idx * self.CARD_STEP
        x1 = x0 + self.CARD_W
        left = self._scroll_pos * self._content_w
        right = left + cc.winfo_width()
        if x0 < left:
            self._scroll_to_px(x0 - 20)
        elif x1 > right:
            self._scroll_to_px(x1 - cc.winfo_width() + 20)

    # -- applying -----------------------------------------------------------------

    def _on_card_click(self, idx: int) -> None:
        self.sel = idx
        self._paint_selection()
        self._apply_selected()

    def _apply_selected(self) -> None:
        if not self.filtered:
            return
        if self.sel == 0:
            self._apply_random()
        else:
            self._apply_path(self.filtered[self.sel - 1])

    def _apply_random(self) -> None:
        if len(self.filtered) <= 1:
            self.app._status("No wallpapers to randomize")
            return
        self._apply_path(random.choice(self.filtered[1:]))

    def _apply_path(self, path: str) -> None:
        self.app._apply_path(path)
        self.hide()

    # -- show / hide ----------------------------------------------------------------

    def _reset_entry(self) -> None:
        self.entry.delete(0, "end")
        self.entry.config(fg=self.FG_DIM)
        self._placeholder_active = True
        self.query = ""

    def _preselect_current(self) -> None:
        """Highlight the card matching the current Windows wallpaper."""
        self.sel = 0
        try:
            cur = os.path.normpath(
                self.app.manager.get_wallpaper() or "").lower()
        except Exception:
            cur = ""
        if cur:
            for i, p in enumerate(self.filtered):
                if os.path.normpath(p).lower() == cur:
                    self.sel = i + 1  # slot 0 is the random card
                    break
        self._paint_selection()
        if self.sel:
            self._see_card(self.sel)

    def show(self) -> None:
        if self.visible:
            return
        self.visible = True
        self._reset_entry()
        self.refresh()
        self._preselect_current()
        self._scroll_reset()
        sw = self.top.winfo_screenwidth()
        sh = self.top.winfo_screenheight()
        w = min(self.W, sw - 40)
        h = min(self.H, sh - 80)
        x = (sw - w) // 2
        y = max(0, (sh - h) // 2 - 40)
        self.top.geometry(f"{w}x{h}+{x}+{y}")
        self.top.deiconify()
        self.top.lift()
        try:
            self.top.focus_force()
            self.entry.focus_set()
        except tk.TclError:
            pass
        # glide to the card of the currently applied wallpaper
        self.top.update_idletasks()
        if self.sel:
            self._see_card(self.sel)

    def hide(self) -> None:
        if not self.visible:
            return
        self.visible = False
        if self._thumb_poll_job is not None:
            try:
                self.top.after_cancel(self._thumb_poll_job)
            except tk.TclError:
                pass
            self._thumb_poll_job = None
        self._cancel_scroll_anim()
        self.top.withdraw()

    def toggle(self) -> None:
        self.hide() if self.visible else self.show()

    def destroy(self) -> None:
        for attr in ("_thumb_poll_job", "_filter_job", "_scroll_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.top.after_cancel(job)
                except tk.TclError:
                    pass
        if self._thumb_worker is not None:
            self._thumb_worker.stop()
            self._thumb_worker = None
        try:
            self.top.destroy()
        except tk.TclError:
            pass
