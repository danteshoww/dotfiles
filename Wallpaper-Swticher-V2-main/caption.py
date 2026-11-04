"""caption.py — shared spec for the switcher's caption pill.

The filename/count pill under the cards ("wallpaper.png   •   3 / 41")
exists in both switchers: the Qt gallery draws it in
`switcher_flow._CardsView._draw_caption`, the Tk Grid Cards bar draws it
in `switcher.SwitcherBar`. The rendering API differs (QPainter vs Tk
canvas items) so each side owns its drawing code, but the text format,
truncation limit and spacing come from here so the pill looks and
behaves identically in both.
"""

from __future__ import annotations

# Shared geometry (pixels). Qt uses these directly; Tk pads its font
# metrics with the same values.
PILL_PAD_X = 16
PILL_PAD_Y = 7
PILL_BOTTOM_GAP = 10
PILL_MAX_NAME = 46

# Colours, as (r, g, b, a). Each switcher maps these to its own API
# (QColor vs canvas hex fills).
PILL_BG = (14, 14, 18, 190)
PILL_FG = (230, 230, 235, 235)

PILL_FONT_PT = 9.5
ELLIPSIS = "\u2026"


def pill_caption(name: str, position: int, count: int,
                 max_chars: int = PILL_MAX_NAME) -> str:
    """Truncate `name` and append the "•  position / count" suffix."""
    if len(name) > max_chars:
        name = name[:max_chars - 1] + ELLIPSIS
    return f"{name}   \u2022   {position} / {count}"
