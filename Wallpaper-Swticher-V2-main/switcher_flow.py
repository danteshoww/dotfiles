"""switcher_flow.py — multi-layout Qt gallery (YASB-inspired).

Four layouts share one QWidget, one decode pool and one pump loop:

  * Strips      — accordion: the active strip grows, neighbours share the rest
  * Hearthstone — cards fanned out, active card upright and largest
  * Drift       — staggered grid with a small alternating tilt
  * Gallery     — plain grid, no tilt

Background: the window is fully transparent (WA_TranslucentBackground) —
the real desktop shows straight through instead of a blurred stand-in.
DWM does have to composite this window every frame because of that, so
`_CardsView._paint_and_hit` drops antialiasing/smooth scaling for the
duration of a card animation (motion hides the loss of crispness anyway)
to keep the extra compositing cost from showing up as stutter, especially
on the rotated layouts (Hearthstone, Drift) where it matters most.

Image decoding uses QImageReader + QThreadPool (Qt releases the GIL), so
thumbnails never stall the UI thread.
"""

from __future__ import annotations

import glob
import math
import os
from collections import OrderedDict
from typing import NamedTuple

from PyQt6.QtCore import (
    QEasingCurve, QObject, QPointF, QRunnable, QRectF, QSize, Qt,
    QThreadPool, QVariantAnimation, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QImage, QImageReader, QPainter, QPainterPath, QPen, QPixmap,
    QTransform,
)
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QPushButton, QWidget,
)

import caption

HAVE_QT = True
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}

# Cross-open thumbnail cache, keyed by (path, decode_w, decode_h).
#
# `StripGallery` used to be built fresh on every open and destroyed on
# every close, so its `_cache` died with it and each Alt+W re-decoded
# every visible thumbnail from disk. (The Tk "Grid Cards" bar keeps its
# thumbnail cache on the long-lived SwitcherBar, which is exactly why it
# never showed this first-open delay.) Keying on the requested decode
# size means a cache entry stays valid when the window is rebuilt for a
# layout with different card dimensions.
_SHARED_THUMBS: "OrderedDict[tuple, QPixmap]" = OrderedDict()
SHARED_THUMB_LIMIT = 96

PUMP_MS = 12
CARD_ANIMATION_DURATION = 280
CARD_ANIMATION_EASING = QEasingCurve.Type.OutCubic

LAYOUT_LABELS = {
    "grid":        "Grid Cards",
    "strip":       "Strips",
    "hearthstone": "Hearthstone",
    "drift":       "Drift",
    "gallery":     "Gallery",
}

# "Grid Cards" (key "grid") isn't one of this Qt gallery's own layout
# strategies — it's the *other* switcher entirely (switcher.py's plain
# Tkinter floating bar). It's still offered as a pill tab so you don't
# have to dig into the tray menu to reach it, but picking it closes this
# Qt window and asks app.py to swap the whole switcher over, same as the
# tray menu's "Switcher Layout" submenu does.
NATIVE_LAYOUT_KEYS = {"grid"}


# ─────────────────────────────────────────────────────────────────────────────
#  Image loader
# ─────────────────────────────────────────────────────────────────────────────

class _ImgSignals(QObject):
    # path/w/h ride along so the decoder can publish into the shared
    # cache under the same key `_load_around()` looks up.
    loaded = pyqtSignal(int, QImage, str, int, int)


class _ImageLoader(QRunnable):
    def __init__(self, path, w, h, index, gallery):
        super().__init__()
        self._path, self._w, self._h = path, w, h
        self._index, self._gallery = index, gallery
        self._signals = _ImgSignals()

    def run(self):
        reader = QImageReader(self._path)
        reader.setAutoTransform(True)
        original = reader.size()
        tw, th = self._w, self._h
        if not original.isValid() or original.height() == 0:
            reader.setScaledSize(QSize(tw, th))
        else:
            oa = original.width() / original.height()
            ta = tw / th if th else 1.0
            if oa > ta:
                sh, sw = th, int(th * oa)
            else:
                sw, sh = tw, int(tw / oa) if oa else th
            reader.setScaledSize(QSize(max(1, sw), max(1, sh)))
        img = reader.read()
        self._signals.loaded.emit(self._index, img, self._path,
                                  self._w, self._h)


# ─────────────────────────────────────────────────────────────────────────────
#  Layout primitives
# ─────────────────────────────────────────────────────────────────────────────

class CardSlot(NamedTuple):
    x: float
    y: float
    width: float
    height: float
    angle: float = 0.0
    z: int = 0
    # Rotation around the card's vertical axis, in degrees (3D cover
    # flow). Rendered as a perspective QTransform — see `_paint_and_hit`.
    rot_y: float = 0.0


class _BaseLayout:
    """Strategy that turns (indices, active, w, h) into {index: CardSlot}."""

    wraps = True
    max_visible = 15
    # Whether selection changes tween card positions (animated scroll) or
    # cards jump straight to the scroll position (Hearthstone's fan moves
    # in real time with the scroll, nothing easing after it stops).
    animated = True
    # Tween length for this layout's snap (short = each scroll notch lands
    # crisply on its card instead of gliding past several).
    animation_duration = CARD_ANIMATION_DURATION
    animation_easing = CARD_ANIMATION_EASING

    def __init__(self, gallery: "StripGallery"):
        self.gallery = gallery

    def visible_indices(self, center: int, count: int) -> list[int]:
        raise NotImplementedError

    def layout(self, indices, active, width, height) -> dict[int, CardSlot]:
        raise NotImplementedError

    def focus_strength(self, slot: CardSlot,
                       active_slot: CardSlot | None) -> float:
        return 1.0


class AccordionLayout(_BaseLayout):
    """Strips — narrow bands; the active one grows to a wide card."""

    active_fraction = 0.42
    min_strip = 100
    wraps = True
    max_visible = 17

    def visible_indices(self, center, count):
        if count == 0:
            return []
        n = min(self.gallery.visible_count, count)
        half = n // 2
        return [((center - half + i) % count) for i in range(n)]

    def layout(self, indices, active, width, height):
        n = len(indices)
        if n == 0 or width <= 0:
            return {}
        has_active = active in indices
        active_w = width * self.active_fraction if has_active else 0.0
        others = n - (1 if has_active else 0)
        other_w = (width - active_w) / others if others else 0.0
        result: dict[int, CardSlot] = {}
        x = 0.0
        for index in indices:
            w = active_w if index == active else other_w
            result[index] = CardSlot(x=x, y=0.0, width=w, height=height)
            x += w
        return result

    def focus_strength(self, slot, active_slot):
        if active_slot is None or active_slot.width <= 0:
            return 0.4
        return min(1.0, slot.width / active_slot.width)


class HearthstoneLayout(_BaseLayout):
    """Hand-of-cards fan (like the Hearthstone game hand): cards overlap
    heavily and fan out from the centre along a downward arc — the active
    card stands upright, larger, raised above the fan and undimmed in
    the middle, while cards farther out lean progressively outward
    (in-plane rotation), shrink slightly, drop toward the bottom of the
    arc, and darken."""

    wraps = True
    animated = True
    # Smooth, unhurried snap — every card (including the outgoing centre
    # card) glides to its new slot together; no snapping/teleporting and
    # no bounce.
    animation_duration = 260
    animation_easing = CARD_ANIMATION_EASING
    max_visible = 11
    # Active card as a fraction of the window — compact portrait ratio
    # like the reference hand-of-cards fan
    active_w = 0.115
    active_h = 0.62
    # Centre-to-centre distance between neighbouring cards, as a fraction
    # of the active width — small enough that cards overlap heavily and
    # only an edge strip of each side card shows
    spread = 0.40
    # Outward lean per card away from the centre, in degrees
    rotation_step = 8.0
    # Downward arc: drop grows with the square of a card's horizontal
    # distance so the fan curves like a held hand of cards
    arc = 0.08
    # The active card sits above the fan (like the reference): raised by
    # this fraction of its height, while side cards keep the downward arc
    active_raise = 0.14
    scale_step = 0.05
    min_scale = 0.70

    def visible_indices(self, center, count):
        if count == 0:
            return []
        n = min(self.max_visible, count)
        if n % 2 == 0:
            n = max(1, n - 1)
        half = n // 2
        return [((center - half + i) % count) for i in range(n)]

    def layout(self, indices, active, width, height):
        n = len(indices)
        if n == 0 or width <= 0:
            return {}
        w0 = width * self.active_w
        h0 = height * self.active_h
        cx = width / 2.0
        cy = height / 2.0
        center = n // 2
        result: dict[int, CardSlot] = {}
        for i, index in enumerate(indices):
            offset = i - center
            aoff = abs(offset)
            scale = max(self.min_scale, 1.0 - aoff * self.scale_step)
            w = w0 * scale
            h = h0 * scale
            x_off = offset * w0 * self.spread
            if offset == 0:
                # active card rides above the fan (like the reference)
                y_off = -h0 * self.active_raise
            else:
                # arc: outer cards sit lower, growing quadratically so
                # the fan curves like a held hand of cards
                y_off = self.arc * (offset * self.spread) ** 2 * w0
            result[index] = CardSlot(x=cx + x_off - w / 2.0,
                                     y=cy + y_off - h / 2.0,
                                     width=w, height=h,
                                     angle=offset * self.rotation_step,
                                     z=100 - aoff,
                                     rot_y=0.0)
        return result

    def focus_strength(self, slot, active_slot):
        # Progressive depth dimming: cards farther from the centre are
        # much darker (the fan keeps only the active card at full
        # brightness). Width tracks distance in this layout, so the
        # cubed ratio gives a strong, smooth depth ramp.
        if active_slot is None or active_slot.width <= 0:
            return 0.35
        if slot.width >= active_slot.width:
            return 1.0
        return max(0.16, (slot.width / active_slot.width) ** 3)


class _GridBasedLayout(_BaseLayout):
    cols = 4
    rows = 2
    gap = 12
    tilt = 0.0
    # Which index alternates the tilt direction: "col" (checkerboard, each
    # card differs from its neighbours) or "row" (every card in a row leans
    # the same way, the next row leans the opposite way).
    tilt_axis = "col"
    # Reading order across rows. False: every row fills left-to-right like
    # a plain grid. True: rows alternate direction (row 0 left-to-right,
    # row 1 right-to-left, row 2 left-to-right, ...) — a "boustrophedon"
    # sweep, no rotation involved, just which column each card lands in.
    serpentine = False

    def _page_size(self) -> int:
        return self.cols * self.rows

    @property
    def max_visible(self) -> int:
        # The grid always shows exactly cols×rows cards — unlike the
        # cover-flow layouts' fixed constant, this must track whichever
        # subclass's cols/rows are in play (Drift's 4×2 vs Grid's 6×6),
        # otherwise `_load_around()`'s preload radius (driven by
        # `gallery.visible_count`, which mirrors this) undershoots and
        # cards at the edges of a dense grid stay blank until scrolled to.
        return self._page_size()

    @max_visible.setter
    def max_visible(self, _value: int) -> None:
        # `StripGallery.fit_to_view()` unconditionally does
        # `self.layout_strategy.max_visible = n` after reading it. The
        # grid's page size is fixed by cols×rows, not by how many images
        # exist, so that write is a no-op here rather than an error.
        pass

    def visible_indices(self, center, count):
        if count == 0:
            return []
        per_page = self._page_size()
        start = max(0, center - per_page // 2)
        start = min(max(0, count - per_page), start)
        return list(range(start, min(count, start + per_page)))

    def layout(self, indices, active, width, height):
        n = len(indices)
        if n == 0 or width <= 0:
            return {}
        cols = self.cols
        rows = max(1, (n + cols - 1) // cols)
        gap = self.gap
        cell_w = (width - gap * (cols + 1)) / cols
        cell_h = (height - gap * (rows + 1)) / rows
        result: dict[int, CardSlot] = {}
        for i, index in enumerate(indices):
            col_in_row = i % cols
            row = i // cols
            reversed_row = self.serpentine and (row % 2 == 1)
            col = (cols - 1 - col_in_row) if reversed_row else col_in_row
            x = gap + col * (cell_w + gap)
            y = gap + row * (cell_h + gap)
            angle = 0.0
            if self.tilt:
                parity = row if self.tilt_axis == "row" else col
                angle = (self.tilt if (parity % 2) else -self.tilt)
            result[index] = CardSlot(x=x, y=y, width=cell_w, height=cell_h,
                                     angle=angle, z=100 - i)
        return result

    def focus_strength(self, slot, active_slot):
        return 0.55


class DriftLayout(_GridBasedLayout):
    """Two rows, no rotation — the top row reads left-to-right, the
    bottom row reads right-to-left (a serpentine/boustrophedon sweep),
    so it's visibly different from Gallery's plain uniform grid."""
    cols = 4
    rows = 2
    tilt = 0.0
    serpentine = True


class GalleryLayout(_GridBasedLayout):
    """The dense "Grid" view — many same-size cards, no tilt, plain
    left-to-right/top-to-bottom reading order, filling as much of the
    screen as it can (see `StripGallery.FULL_HEIGHT_LAYOUTS`) instead of
    a single thin filmstrip band."""
    cols = 6
    rows = 6
    gap = 6
    tilt = 0.0


_LAYOUT_CLASSES = {
    "strip":       AccordionLayout,
    "hearthstone": HearthstoneLayout,
    "drift":       DriftLayout,
    "gallery":     GalleryLayout,
}


# ─────────────────────────────────────────────────────────────────────────────
#  Bottom toolbar
# ─────────────────────────────────────────────────────────────────────────────

class _Toolbar(QWidget):
    BUTTON_CSS = """
        QPushButton {
            background: transparent; color: #9497a3;
            border: none; padding: 8px 16px; border-radius: 9px;
            font-family: 'Alexandria', 'Segoe UI', sans-serif;
            font-size: 12px;
        }
        QPushButton:hover {
            background: rgba(255, 255, 255, 0.07); color: #e8e9ee;
        }
        QPushButton:checked {
            background: rgba(255, 255, 255, 0.10); color: #eceef2;
        }
    """

    layout_changed = pyqtSignal(str)

    def __init__(self, current: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        frame = QFrame(self)
        frame.setObjectName("layoutBar")
        frame.setStyleSheet("""
            #layoutBar {
                background: rgba(24, 25, 33, 0.88);
                border: 1px solid rgba(255, 255, 255, 0.14);
                border-radius: 16px;
            }
        """ + self.BUTTON_CSS)
        inner = QHBoxLayout(frame)
        inner.setContentsMargins(8, 6, 8, 6)
        inner.setSpacing(4)

        self._buttons: dict[str, QPushButton] = {}
        for key, label in LAYOUT_LABELS.items():
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(key == current)
            btn.clicked.connect(
                lambda _checked, k=key: self.layout_changed.emit(k))
            inner.addWidget(btn)
            self._buttons[key] = btn
        outer.addWidget(frame, alignment=Qt.AlignmentFlag.AlignCenter)

    def set_current(self, key: str) -> None:
        for k, btn in self._buttons.items():
            btn.setChecked(k == key)


# ─────────────────────────────────────────────────────────────────────────────
#  Gallery
# ─────────────────────────────────────────────────────────────────────────────

class StripGallery(QWidget):
    border = 3
    dim = 0.55
    corner_radius = 10
    # Was pure white, which is why the selected card's outline looked flat
    # and generic next to a photo — a cool accent reads as more "app-like"
    # and still pops against every wallpaper.
    accent = "#5ec8ff"
    # Layouts that should use nearly the whole screen height instead of a
    # thin filmstrip band. Only "Grid" wants this: a dense multi-row grid
    # needs the vertical room, whereas Strips/Hearthstone/Drift are meant
    # to read as a single horizontal band.
    FULL_HEIGHT_LAYOUTS = {"gallery"}

    def __init__(self, folder: str, current: str = "",
                 layout: str = "strip", backdrop_path: str = "",
                 image_files: list[str] | None = None):
        super().__init__()
        self._backdrop_path = backdrop_path
        # True transparency: the real desktop (wallpaper, icons, whatever's
        # behind it) shows straight through instead of a fake blurred-photo
        # panel. This does mean Windows' compositor (DWM) has to composite
        # this window every frame instead of us handing it an opaque
        # bitmap — see the animation-smoothness fix in `_paint_and_hit`
        # below, which is what keeps that from costing extra stutter.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # `app.py` already scans the folder once at startup (and again
        # whenever it changes) and keeps the result in `app.images` — that
        # list is passed in here. Re-walking the whole folder from scratch
        # on *every single open* (the old behaviour) was the single biggest
        # cause of the switcher feeling slow to appear, especially on a
        # large or slow (network/external) wallpaper folder. Only fall back
        # to scanning here if the caller genuinely has nothing cached yet
        # (e.g. standalone/test use).
        self.image_files = (list(image_files) if image_files is not None
                            else self._scan(folder))
        self.is_closing = False
        self.pending_apply: str | None = None
        self.pending_close = False
        # Set instead of pending_apply when the pill's "Grid Cards" tab is
        # picked: app.py needs to swap this Qt window out for the other
        # (Tkinter) switcher entirely, not apply a wallpaper.
        self.pending_native_layout: str | None = None

        self.layout_name = layout if layout in _LAYOUT_CLASSES else "strip"
        self.layout_strategy: _BaseLayout = _LAYOUT_CLASSES[self.layout_name](self)

        screen = QApplication.primaryScreen().geometry()
        card_h = int(screen.height() * 0.46)
        card_w = max(int(card_h * 9 / 16), int(screen.width() * 0.45))
        self.card_size = (card_w, card_h)
        self.visible_count = 0
        self.visible_strips = 0

        self._cache: OrderedDict[int, QPixmap] = OrderedDict()
        self._pending: set[int] = set()
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max(2, min(6, os.cpu_count() or 4)))

        # Was: a pre-blurred, darkened copy of the wallpaper drawn as an
        # opaque backdrop. Removed per request — the window is fully
        # transparent now, so the real desktop shows through directly.

        self.view = _CardsView(self)
        self.view.setParent(self)

        self._preselect = 0
        if current:
            norm = os.path.normpath(current).lower()
            for i, p in enumerate(self.image_files):
                if os.path.normpath(p).lower() == norm:
                    self._preselect = i
                    break

        self._toolbar = _Toolbar(self.layout_name, self)
        self._toolbar.layout_changed.connect(self._on_layout_changed)

        self.fit_to_view(screen.width(), card_h + 40)
        self._layout_children(screen.width(), card_h + 40)
        self.view.go_to(self._preselect, animate=False)
        self.on_focus_changed(self._preselect)

    def _scan(self, folder: str) -> list[str]:
        files: list[str] = []
        if not os.path.isdir(folder):
            return files
        skip = {"__pycache__", ".git", "$RECYCLE.BIN",
                "System Volume Information", "node_modules"}
        for root_dir, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs
                       if d not in skip and not d.startswith(".")]
            for name in names:
                if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                    files.append(os.path.join(root_dir, name))
        files.sort()
        return files

    def fit_to_view(self, width: int, height: int) -> None:
        count = len(self.image_files)
        if count == 0:
            self.visible_count = 0
            self.visible_strips = 0
            return
        if isinstance(self.layout_strategy, AccordionLayout):
            other = max(1, width) * (1.0 - self.layout_strategy.active_fraction)
            max_by_width = 1 + int(other // self.layout_strategy.min_strip)
            n = max(1, min(self.layout_strategy.max_visible,
                           max_by_width, count))
        else:
            n = max(1, min(self.layout_strategy.max_visible, count))
        self.visible_count = n
        self.visible_strips = n
        self.layout_strategy.max_visible = n

    def _on_layout_changed(self, key: str) -> None:
        if key == self.layout_name:
            return
        if key in NATIVE_LAYOUT_KEYS:
            # Not one of this window's own layout strategies — hand off
            # to app.py to swap in the other switcher and close this one
            # (mirrors Esc: set is_closing so nothing else tries to touch
            # this window again before it's torn down).
            if not self.is_closing:
                self.is_closing = True
                self.pending_native_layout = key
                self.pending_close = True
            return
        self.layout_name = key
        self.layout_strategy = _LAYOUT_CLASSES[key](self)
        self._toolbar.set_current(key)
        x, y, w, h = self._target_geometry()
        if (w, h) != (self.width(), self.height()) or (x, y) != (
                self.x(), self.y()):
            # setGeometry triggers resizeEvent, which already re-runs
            # _layout_children/fit_to_view/go_to for the new layout.
            self.setGeometry(x, y, w, h)
        else:
            self.fit_to_view(self.width(), self.view.height())
            self.view.go_to(self.view.index, animate=False)
            self.view.update()

    def _target_geometry(self) -> tuple[int, int, int, int]:
        """Where this window should sit for the *current* layout.

        Cover-flow style layouts (Strips/Hearthstone/Drift) are a thin
        horizontal band sized off the tall cover-flow card height. Grid
        wants to be tall and dense instead, so it gets nearly the full
        screen height.
        """
        screen = QApplication.primaryScreen().geometry()
        if self.layout_name in self.FULL_HEIGHT_LAYOUTS:
            margin = 24
            h = screen.height() - margin * 2
            y = margin
        else:
            h = self.card_size[1] + 100
            y = (screen.height() - h) // 2
        return 0, y, screen.width(), h

    def window_indices(self, centre: int) -> list[int]:
        return self.layout_strategy.visible_indices(
            centre, len(self.image_files))

    def flex_layout(self, indices, active, width):
        height = float(self.view.height()) if self.view else 100.0
        return self.layout_strategy.layout(indices, active, width, height)

    def on_focus_changed(self, index: int) -> None:
        if self.image_files:
            self._load_around(index)

    def reselect(self, current: str) -> None:
        """Point the (still-alive) window at the current wallpaper again.

        The gallery now survives between opens, so re-showing it means
        re-running just the preselection `__init__` used to do — not
        rebuilding the widget tree or re-decoding thumbnails.
        """
        if not self.image_files:
            return
        index = 0
        norm = os.path.normpath(current).lower()
        for i, p in enumerate(self.image_files):
            if os.path.normpath(p).lower() == norm:
                index = i
                break
        self._preselect = index
        self.view.go_to(index, animate=False)

    def selected_image(self):
        if not self.image_files:
            return None
        return self.image_files[self.view.index]

    def pixmap_for(self, index: int):
        pixmap = self._cache.get(index)
        if pixmap is not None:
            self._cache.move_to_end(index)
        return pixmap

    def _load_around(self, index: int) -> None:
        count = len(self.image_files)
        if not count:
            return
        radius = max(3, self.visible_count // 2 + 3)
        wanted = {(index + o) % count for o in range(-radius, radius + 1)}
        card_w, card_h = self._thumb_target_size()
        for target in sorted(wanted,
                             key=lambda t: abs(self.view.relative_distance(t))):
            if target in self._cache or target in self._pending:
                continue
            key = (self.image_files[target], card_w, card_h)
            shared = _SHARED_THUMBS.get(key)
            if shared is not None:
                _SHARED_THUMBS.move_to_end(key)
                self._cache[target] = shared
                continue
            self._pending.add(target)
            loader = _ImageLoader(self.image_files[target],
                                  card_w, card_h, target, self)
            loader._signals.loaded.connect(self._on_image_loaded)
            self._pool.start(loader)
        limit = 2 * radius + 8
        for cached in [i for i in self._cache if i not in wanted]:
            if len(self._cache) <= limit:
                break
            del self._cache[cached]

    def _thumb_target_size(self) -> tuple[int, int]:
        """Decode resolution to request for the current layout's cards.

        Grid-based layouts (Grid, Drift) show many small cells at once —
        decoding each at the tall cover-flow card size (`self.card_size`)
        would waste time and memory on detail that gets thrown away at
        render time. Use the actual on-screen cell size instead.
        """
        strategy = self.layout_strategy
        if isinstance(strategy, _GridBasedLayout):
            cols = max(1, strategy.cols)
            rows = max(1, strategy.rows)
            w = max(1, self.width() // cols)
            h = max(1, self.height() // rows)
            return (w, h)
        return self.card_size

    def _on_image_loaded(self, index: int, image: QImage, path: str,
                         w: int, h: int) -> None:
        self._pending.discard(index)
        if self.is_closing or index >= len(self.image_files):
            return
        try:
            if image.isNull():
                return
            pixmap = QPixmap.fromImage(image)
            key = (path, w, h)
            _SHARED_THUMBS[key] = pixmap
            _SHARED_THUMBS.move_to_end(key)
            while len(_SHARED_THUMBS) > SHARED_THUMB_LIMIT:
                _SHARED_THUMBS.popitem(last=False)
            self._cache[index] = pixmap
            if abs(self.view.relative_distance(index)) <= self.visible_count:
                self.view.update()
        except RuntimeError:
            pass

    def apply_wallpaper(self, image_path: str) -> None:
        if self.is_closing:
            return
        self.is_closing = True
        self.pending_apply = image_path

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if not self.is_closing:
                self.is_closing = True
                self.pending_close = True
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            path = self.selected_image()
            if path:
                self.apply_wallpaper(path)
        elif key == Qt.Key.Key_Left:
            self.view.move_by(-1)
        elif key == Qt.Key.Key_Right:
            self.view.move_by(1)
        elif key == Qt.Key.Key_Home:
            self.view.go_to(0)
        elif key == Qt.Key.Key_End:
            self.view.go_to(len(self.image_files) - 1)
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_children(self.width(), self.height())
        self.fit_to_view(self.width(), self.height())
        if self.image_files:
            self.view.go_to(self.view.index, animate=False)

    def _layout_children(self, w: int, h: int) -> None:
        toolbar_h = 44
        self.view.setGeometry(0, 0, w, max(1, h - toolbar_h - 8))
        self._toolbar.setGeometry(0, h - toolbar_h, w, toolbar_h)


# ─────────────────────────────────────────────────────────────────────────────
#  The painting view
# ─────────────────────────────────────────────────────────────────────────────

class _CardsView(QWidget):
    def __init__(self, gallery: StripGallery):
        super().__init__(gallery)
        self.gallery = gallery
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # No longer WA_OpaquePaintEvent: this view is on a transparent
        # window now, so it must not claim to paint every pixel opaquely —
        # the whole point is the desktop showing through the gaps.
        self.setMouseTracking(True)
        self.index = 0
        self.hovered: int | None = None
        self.layout: dict[int, CardSlot] = {}
        # Per-card dim/border focus, snapshotted from the from/to layouts
        # and lerped alongside the geometry. Deriving it per-frame from
        # the *interpolated* slots made the accordion flash fully bright
        # at the start of every scroll step (the incoming active strip is
        # still neighbour-sized at t=0, so the width-ratio formula —
        # which decides the dim level — briefly read 1.0 for everyone).
        self.focus: dict[int, float] = {}
        self._focus_from: dict[int, float] = {}
        self._focus_to: dict[int, float] = {}
        self.hit_areas: list[tuple[int, QRectF, float]] = []
        self._anim_from: dict[int, CardSlot] = {}
        self._anim_to: dict[int, CardSlot] = {}
        self._animating = False
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(CARD_ANIMATION_DURATION)  # default
        self.animation.setEasingCurve(CARD_ANIMATION_EASING)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.valueChanged.connect(self._on_animation_value)
        self.animation.finished.connect(self._on_animation_finished)

    @property
    def count(self) -> int:
        return len(self.gallery.image_files)

    def relative_distance(self, index: int) -> float:
        count = self.count
        distance = index - self.index
        if self.gallery.layout_strategy.wraps and count > 1:
            distance = ((distance + count / 2.0) % count) - count / 2.0
        return distance

    @staticmethod
    def _lerp(a: CardSlot, b: CardSlot, t: float) -> CardSlot:
        return CardSlot(
            x=a.x + (b.x - a.x) * t,
            y=a.y + (b.y - a.y) * t,
            width=a.width + (b.width - a.width) * t,
            height=a.height + (b.height - a.height) * t,
            angle=a.angle + (b.angle - a.angle) * t,
            z=b.z,
            rot_y=a.rot_y + (b.rot_y - a.rot_y) * t,
        )

    def _compute_focus(self, layout: dict[int, CardSlot],
                       active: int) -> dict[int, float]:
        """Dim/border strength per card for a settled layout snapshot."""
        strategy = self.gallery.layout_strategy
        active_slot = layout.get(active)
        return {i: strategy.focus_strength(slot, active_slot)
                for i, slot in layout.items()}

    def _edge_slot(self) -> CardSlot:
        return CardSlot(x=float(self.width()), y=0.0, width=0.0,
                        height=float(self.height()), angle=0.0, z=0,
                        rot_y=0.0)

    @staticmethod
    def _rot_y_transform(rot_y: float) -> QTransform:
        """Perspective rotate-Y around the card's vertical axis.

        A local point (x, y) maps to (x·cosθ, y) with a perspective
        divide of 1 + x·sinθ/d, d = viewer distance — the standard 3D
        projection expressed as a Qt projective QTransform (the raster
        engine renders pixmaps correctly through it).
        """
        rad = math.radians(rot_y)
        distance = 1500.0
        return QTransform(
            math.cos(rad), 0.0, math.sin(rad) / distance,
            0.0, 1.0, 0.0,
            0.0, 0.0, 1.0,
        )

    @staticmethod
    def draw_cover(painter, pixmap, rect: QRectF) -> None:
        size = pixmap.deviceIndependentSize()
        if size.width() <= 0 or size.height() <= 0:
            return
        scale = max(rect.width() / size.width(),
                    rect.height() / size.height())
        w, h = size.width() * scale, size.height() * scale
        c = rect.center()
        target = QRectF(c.x() - w / 2, c.y() - h / 2, w, h)
        painter.drawPixmap(target, pixmap,
                           QRectF(0, 0, pixmap.width(), pixmap.height()))

    def paintEvent(self, event) -> None:
        try:
            self._paint_and_hit(event)
        except Exception:
            import traceback
            traceback.print_exc()

    def _paint_and_hit(self, _event) -> None:
        painter = QPainter(self)
        # Rotated cards (Hearthstone's fan, Drift's tilt) force Qt onto a
        # much slower per-pixel-antialiased blit path than Gallery's plain
        # axis-aligned rects — that mismatch, not anything about the
        # layouts themselves, is why Gallery looked smooth and the others
        # didn't. Antialiasing only matters for a settled, static frame
        # (crisp edges); while a card is actually moving, motion hides the
        # jaggies anyway, so it's dropped for the duration of the tween —
        # same idea as SmoothPixmapTransform being skipped below.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing,
                              not self._animating)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,
                              not self._animating)

        gallery = self.gallery

        # Fully transparent background: the real desktop shows straight
        # through instead of a fake blurred-photo panel.
        # CompositionMode_Source (rather than a plain fillRect, which would
        # just alpha-blend onto whatever was drawn last frame) is what
        # actually erases the previous frame back to transparent instead of
        # leaving ghosting behind moving cards.
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver)

        border = gallery.border
        # Layouts can override the corner radius (Hearthstone's PathView
        # reference uses 15px cards) and the dim strength (its opacity
        # ramp needs a stronger overlay to hit brightness 0.4 at the
        # edges).
        radius = getattr(gallery.layout_strategy, "corner_radius",
                         gallery.corner_radius)
        self.hit_areas = []

        items = sorted(self.layout.items(),
                       key=lambda kv: (kv[1].z, kv[0]))

        for index, slot in items:
            if slot.width <= 0.5 or slot.height <= 0.5:
                continue
            area = QRectF(slot.x, slot.y, slot.width, slot.height)
            if area.right() < 0 or area.left() > self.width():
                continue
            if area.bottom() < 0 or area.top() > self.height():
                continue

            focus = self.focus.get(index, 0.4)
            strength = 1.0 if index == self.hovered else focus ** 3

            # One save/translate/rotate for the whole card (image + border)
            # instead of two separate ones — halves the per-card transform
            # overhead, which is exactly where the rotated layouts
            # (Hearthstone, Drift) were paying extra vs. Gallery's
            # zero-rotation cards.
            painter.save()
            painter.translate(area.center())
            painter.rotate(slot.angle)
            if slot.rot_y:
                # 3D cover flow: rotate the card around its vertical axis
                # with a perspective divide (Hearthstone layout). Row-
                # vector order `persp * painterT` applies the rotate-Y in
                # card-local space first, then the translate/rotate.
                # Smooth sampling stays on for the perspective blit —
                # without it the projected edges are visibly jaggy.
                painter.setRenderHint(
                    QPainter.RenderHint.SmoothPixmapTransform, True)
                painter.setTransform(
                    self._rot_y_transform(slot.rot_y)
                    * painter.transform())
            local = QRectF(-area.width() / 2, -area.height() / 2,
                           area.width(), area.height())

            clip = QPainterPath()
            if radius > 0:
                clip.addRoundedRect(local, radius, radius)
            else:
                clip.addRect(local)
            painter.setClipPath(clip)

            pixmap = gallery.pixmap_for(index)
            if pixmap is not None:
                self.draw_cover(painter, pixmap, local)
            else:
                painter.fillRect(local, QColor(255, 255, 255, 18))

            dim = getattr(gallery.layout_strategy, "dim", gallery.dim)
            shade = int(255 * dim * (1.0 - focus))
            if shade:
                painter.fillRect(local, QColor(0, 0, 0, shade))

            if border and strength > 0.01 and area.width() > border:
                painter.setClipping(False)
                inset = border / 2.0
                edge = local.adjusted(inset, inset, -inset, -inset)
                accent = QColor(gallery.accent)

                # Soft outer glow behind the crisp border on the focused
                # card — a plain single-pixel outline (the old behaviour)
                # reads as a thin technical rectangle; a faint halo behind
                # it is what makes the selected card actually pop the way
                # it does in a polished gallery UI instead of looking flat.
                if strength > 0.6:
                    glow_strength = (strength - 0.6) / 0.4
                    glow = QColor(accent)
                    glow.setAlpha(int(70 * glow_strength))
                    glow_pen = QPen(glow)
                    glow_pen.setWidthF(border * 3.0)
                    painter.setPen(glow_pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    if radius > 0:
                        painter.drawRoundedRect(
                            edge, max(0.0, radius - inset),
                            max(0.0, radius - inset))
                    else:
                        painter.drawRect(edge)

                accent.setAlpha(int(255 * strength))
                pen = QPen(accent)
                pen.setWidthF(border)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                if radius > 0:
                    painter.drawRoundedRect(
                        edge, max(0.0, radius - inset),
                        max(0.0, radius - inset))
                else:
                    painter.drawRect(edge)

            painter.restore()
            self.hit_areas.append((index, area, slot.angle))

        self._draw_caption(painter, gallery)
        painter.end()

    def _draw_caption(self, painter, gallery) -> None:
        """Small "filename  •  3 / 41" pill under the cards.

        Purely cosmetic — it's the detail that made the reference gallery
        feel like a finished app instead of a bare card row: you always
        know what you're looking at and how far you are through the set.
        """
        count = self.count
        if not count:
            return
        name = os.path.basename(gallery.image_files[self.index])
        text = caption.pill_caption(name, self.index + 1, count)

        font = painter.font()
        font.setPointSizeF(caption.PILL_FONT_PT)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text_w = metrics.horizontalAdvance(text)
        pad_x, pad_y = caption.PILL_PAD_X, caption.PILL_PAD_Y
        pill_w = text_w + pad_x * 2
        pill_h = metrics.height() + pad_y * 2
        x = (self.width() - pill_w) / 2.0
        y = self.height() - pill_h - caption.PILL_BOTTOM_GAP

        painter.setPen(Qt.PenStyle.NoPen)
        bg = QColor(*caption.PILL_BG[:3])
        bg.setAlpha(caption.PILL_BG[3])
        painter.setBrush(bg)
        painter.drawRoundedRect(QRectF(x, y, pill_w, pill_h), pill_h / 2, pill_h / 2)

        fg = QColor(*caption.PILL_FG[:3])
        fg.setAlpha(caption.PILL_FG[3])
        painter.setPen(fg)
        painter.drawText(QRectF(x, y, pill_w, pill_h),
                         Qt.AlignmentFlag.AlignCenter, text)

    def card_at(self, position: QPointF):
        for index, area, angle in reversed(self.hit_areas):
            cx, cy = area.center().x(), area.center().y()
            dx, dy = position.x() - cx, position.y() - cy
            rad = math.radians(-angle)
            lx = dx * math.cos(rad) - dy * math.sin(rad)
            ly = dx * math.sin(rad) + dy * math.cos(rad)
            if (-area.width() / 2 <= lx <= area.width() / 2
                    and -area.height() / 2 <= ly <= area.height() / 2):
                return index
        return None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.gallery.fit_to_view(self.width(), self.height())
        if self.count:
            self.go_to(self.index, animate=False)

    def _on_animation_value(self, t) -> None:
        t = float(t)
        union = self._anim_from.keys() | self._anim_to.keys()
        self.layout = {
            i: self._lerp(self._anim_from.get(i, self._edge_slot()),
                          self._anim_to.get(i, self._edge_slot()), t)
            for i in union
        }
        focus = {}
        for i in union:
            f1 = self._focus_to.get(i, 0.4)
            f0 = self._focus_from.get(i, f1)
            focus[i] = f0 + (f1 - f0) * t
        self.focus = focus
        self.update()

    def _on_animation_finished(self) -> None:
        self._animating = False
        self.layout = {i: s for i, s in self._anim_to.items()
                       if s.width > 0.5}
        self.focus = dict(self._focus_to)
        self._anim_from = {}
        self._anim_to = {}
        self._focus_from = {}
        self._focus_to = {}
        self.update()

    def go_to(self, index: int, animate: bool = True) -> None:
        count = self.count
        if not count:
            return
        new_index = index % count
        delta = new_index - self.index
        if self.gallery.layout_strategy.wraps and count:
            delta = ((delta + count / 2) % count) - count / 2

        old_layout = self.layout
        self.index = new_index
        self.gallery.on_focus_changed(new_index)

        new_indices = self.gallery.window_indices(new_index)
        new_layout = self.gallery.layout_strategy.layout(
            new_indices, new_index,
            float(self.width()), float(self.height()))
        new_focus = self._compute_focus(new_layout, new_index)

        self.animation.stop()
        if not animate:
            self.layout = new_layout
            self.focus = new_focus
            self._animating = False
            self.update()
            return

        # Only cards that REMAIN in the fan tween (scroll slide). Cards
        # entering the fan appear at their slot and cards leaving it
        # vanish immediately — no entrance/exit animation: the tween's
        # old fallback sent leaving cards sliding into a zero-width edge
        # slot, which read as a card flying off to the right.
        self._anim_from = {i: old_layout.get(i) or new_layout[i]
                           for i in new_layout}
        self._anim_to = dict(new_layout)
        # Dim/border strengths tween between the settled snapshots —
        # never re-derived from the moving geometry (see __init__ note).
        self._focus_from = dict(self.focus)
        self._focus_to = new_focus
        # per-layout snap length and easing
        strategy = self.gallery.layout_strategy
        self.animation.setDuration(strategy.animation_duration)
        self.animation.setEasingCurve(strategy.animation_easing)
        self._animating = True
        self.animation.start()
        # Apply the t=0 frame immediately — otherwise the old layout
        # (with the leaving card still shown, entering card missing)
        # stays on screen until the tween's first tick, a visible glitch.
        self._on_animation_value(0.0)

    def move_by(self, delta: int) -> None:
        count = self.count
        if not count:
            return
        animate = self.gallery.layout_strategy.animated
        if self.gallery.layout_strategy.wraps:
            self.go_to(self.index + delta, animate=animate)
        else:
            self.go_to(max(0, min(count - 1, self.index + delta)),
                       animate=animate)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if not delta or not self.count:
            event.ignore()
            return
        self.move_by(-1 if delta > 0 else 1)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        hovered = self.card_at(event.position())
        if hovered != self.hovered:
            self.hovered = hovered
            self.update()

    def leaveEvent(self, _event) -> None:
        if self.hovered is not None:
            self.hovered = None
            self.update()

    def mousePressEvent(self, event) -> None:
        event.accept()
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.card_at(event.position())
            if index is None:
                return
            if isinstance(self.gallery.layout_strategy, _GridBasedLayout):
                # A flat grid (Drift, Gallery) has no "centred" card the
                # way cover flow does — requiring a first click to select
                # a cell and a second, separate click in that exact same
                # spot to actually apply it just meant the wallpaper kept
                # slipping out of your hands between the two clicks (any
                # tiny cursor drift landed the second click on a neighbour
                # instead). One click on a grid cell now applies it
                # directly, same as it always could once you landed the
                # second click.
                self.gallery.apply_wallpaper(self.gallery.image_files[index])
            elif index != self.index:
                self.go_to(index, animate=self.gallery.layout_strategy.animated)
            else:
                self.gallery.apply_wallpaper(self.gallery.image_files[index])


# ─────────────────────────────────────────────────────────────────────────────
#  Tk-side controller
# ─────────────────────────────────────────────────────────────────────────────

# Qt process-wide warm state. These are module-level on purpose: the
# warm-up is once-per-process (font database, platform plugin), and a
# module reference also keeps the QApplication alive — PyQt destroys the
# Qt application when its last Python reference is dropped, so the app
# must never rely on an instance attribute (e.g. a FlowBar that gets
# replaced on a layout switch) to hold it.
_qapp = None
_qt_prewarmed = False


def prewarm_qt() -> None:
    """Initialise everything Qt initialises lazily, ahead of need.

    Called via a Tk `after` from app.py shortly after startup. Everything
    here otherwise runs the first time a font is needed — *inside the
    first show()* — and on this machine it costs seconds: the Windows
    font backend (DirectWrite) enumerates every installed font family —
    with per-file disk I/O — and resolves the toolbar's font stack on
    first use. Measured ~5.5 s on the first Alt+W open. It must run only
    once per process: `_qt_prewarmed` is global, so a later layout switch
    that builds a fresh FlowBar (or app.py's Tk-side prewarmer) is a
    cheap no-op instead of re-blocking the UI thread mid-transition.
    """
    global _qapp, _qt_prewarmed
    if _qt_prewarmed:
        return
    _qapp = QApplication.instance() or QApplication([])
    _qt_prewarmed = True
    try:
        from PyQt6.QtGui import QFontDatabase
        # fonts.register() loads the bundled Alexandria TTFs for GDI
        # (Tk) only — DirectWrite never sees private GDI fonts, so the
        # toolbar's 'Alexandria' family silently fell through to a
        # fallback scan. Register them with Qt too.
        font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "assets", "fonts")
        for path in glob.glob(os.path.join(font_dir, "*.ttf")):
            font_id = QFontDatabase.addApplicationFont(path)
            QFontDatabase.applicationFontFamilies(font_id)
        # Populate the font database (expensive first call, cached
        # afterwards) and force the exact styled-button metrics path
        # the toolbar uses on first layout — rendered offscreen via
        # grab(), so nothing flashes on screen.
        QFontDatabase.families()
        probe = QPushButton("Strips")
        probe.setStyleSheet(_Toolbar.BUTTON_CSS)
        probe.grab()
    except Exception:
        pass


def qt_app() -> "QApplication | None":
    """The process-wide QApplication, creating (and warming) it if needed."""
    prewarm_qt()
    return _qapp


class FlowBar:
    """Tk-side controller.

    The gallery is built once and *kept alive* between opens: hide() only
    withdraws the window (and lets in-flight thumbnail decodes finish into
    the shared cache), so pressing Alt+W again is a cheap re-show instead
    of a full widget-tree rebuild plus thumbnail re-decode — the delay the
    Tk "Grid Cards" bar never had because its window and thumb cache are
    persistent from startup.
    """

    def __init__(self, app, layout: str = "strip") -> None:
        self.app = app
        self.layout_name = layout
        self.visible = False
        self._gallery: StripGallery | None = None
        self._gallery_key: tuple | None = None
        self._job = None

    def prewarm(self) -> None:
        """Kept for app.py's startup hook — see `prewarm_qt()`."""
        prewarm_qt()

    def show(self) -> None:
        if self.visible or not HAVE_QT:
            return
        self._stop_pump()
        self.prewarm()
        current = (self.app.last_applied
                   or (self.app.manager.get_wallpaper() or ""))
        key = (self.layout_name, tuple(self.app.images))
        gallery = self._gallery
        if gallery is None or self._gallery_key != key or gallery.is_closing:
            try:
                gallery = StripGallery(
                    self.app.folder, current, layout=self.layout_name,
                    backdrop_path=current, image_files=self.app.images)
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "StripGallery creation failed")
                gallery = None
            if gallery is None:
                self._gallery = None
                self._gallery_key = None
                return
            from PyQt6.QtCore import Qt as _Qt
            gallery.setWindowFlags(
                _Qt.WindowType.FramelessWindowHint
                | _Qt.WindowType.WindowStaysOnTopHint
                | _Qt.WindowType.Tool)
            # WA_TranslucentBackground is set on the gallery itself (in
            # StripGallery.__init__) so the desktop shows through.
            self._gallery = gallery
            self._gallery_key = key
        else:
            # Reused window: clear whatever close path ran last time and
            # re-point it at the currently applied wallpaper.
            gallery.is_closing = False
            gallery.pending_apply = None
            gallery.pending_close = False
            gallery.pending_native_layout = None
            gallery.reselect(current)

        x, y, w, h = self._gallery._target_geometry()
        self._gallery.setGeometry(x, y, w, h)
        self._gallery.show()
        self._gallery.setFocus()
        self._gallery.activateWindow()
        self.visible = True
        self._pump()

    def hide(self) -> None:
        self._stop_pump()
        self.visible = False
        gallery = self._gallery
        if gallery is None:
            return
        # An apply/close is pending: mark the gallery closing so no late
        # decode touches it and the next show() rebuilds fresh. A plain
        # toggle (Alt+W while open) keeps it alive with its cache warm.
        if gallery.pending_apply is not None or gallery.pending_close:
            gallery.is_closing = True
        try:
            gallery.hide()
        except Exception:
            pass

    def toggle(self) -> None:
        self.hide() if self.visible else self.show()

    def destroy(self) -> None:
        gallery, self._gallery = self._gallery, None
        self._gallery_key = None
        self._stop_pump()
        self.visible = False
        if gallery is None:
            return
        gallery.is_closing = True
        try:
            gallery._pool.clear()
        except Exception:
            pass
        try:
            gallery.close()
        except Exception:
            pass
        try:
            gallery.deleteLater()
        except Exception:
            pass

    def on_images_changed(self) -> None:
        # The folder rescan produced a new image list. The kept-alive
        # window is built around the old one, so it must be rebuilt — but
        # if the switcher is open right now (e.g. the startup scan landing
        # while the user already has it up), swap it for a fresh one
        # instead of tearing it down and leaving nothing on screen.
        gallery = self._gallery
        if gallery is not None and self.visible:
            self._gallery = None
            self._gallery_key = None
            # Drop the "visible" flag first so the show() below actually
            # rebuilds instead of early-returning, and stop the pump loop
            # so it isn't left polling the deleted widget.
            self.visible = False
            self._stop_pump()
            gallery.is_closing = True
            try:
                gallery.close()
            except Exception:
                pass
            try:
                gallery.deleteLater()
            except Exception:
                pass
            self.show()
            return
        self.destroy()

    def _pump(self) -> None:
        if not self.visible or self._gallery is None:
            return
        try:
            _qapp.processEvents()
        except Exception:
            self.hide()
            return
        gallery = self._gallery
        if gallery is not None:
            if gallery.pending_apply is not None:
                path = gallery.pending_apply
                gallery.pending_apply = None
                self.hide()
                self.app._apply_path(path)
                return
            if gallery.pending_close:
                gallery.pending_close = False
                native_layout = gallery.pending_native_layout
                self.hide()
                if native_layout:
                    # Same event path the tray menu's "Switcher Layout"
                    # submenu uses — swap the switcher over, then reopen
                    # it immediately so picking "Grid Cards" from the pill
                    # feels like switching views, not like the switcher
                    # just closed on you.
                    self.app._events.put(f"layout:{native_layout}")
                    self.app._events.put("toggle")
                return
        self._job = self.app.root.after(PUMP_MS, self._pump)

    def _stop_pump(self) -> None:
        if self._job is not None:
            try:
                self.app.root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
