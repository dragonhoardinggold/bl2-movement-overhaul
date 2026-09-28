"""The stamina bar, in three styles meant to sit with BL2's own HUD.

BL2's HUD is built from slanted, cel-shaded shapes with heavy black outlines -
the health and shield bars are skewed parallelograms, not rectangles - so all
three styles are made from those:

- **Chevrons**: a row of slanted segments, one per charge.
- **Gauge**: one long slanted bar, notched into charges, filling continuously.
- **Diamonds**: outlined diamond pips that fill from the bottom up.

Canvas can only draw axis-aligned rectangles, so every slanted or pointed
shape is drawn as a stack of one-pixel rows, each shifted or narrowed a little.
`DrawTile` takes its colour as an argument (it ignores `Canvas.DrawColor`), and
`PostRender` keeps firing over menus, so the bar checks it's in play first.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from mods_base import get_pc
from unrealsdk import find_enum, find_object, make_struct

from . import options
from .legacy import stamina
from .common import Warn

Colour = tuple[int, int, int, int]

OUTLINE: Colour = (10, 10, 12, 255)
EMPTY: Colour = (28, 30, 36, 190)
FULL: Colour = (255, 205, 48, 255)
HIGHLIGHT: Colour = (255, 240, 170, 255)
REGEN: Colour = (196, 112, 28, 255)
FLASH: Colour = (255, 255, 255, 255)

FLASH_SECONDS: float = 0.18
OUTLINE_PX: int = 2


class Draw:
    white: ClassVar[Any] = None
    white_ul: ClassVar[float] = 1.0
    white_vl: ClassVar[float] = 1.0
    blend: ClassVar[int | None] = None
    colours: ClassVar[dict[Colour, Any]] = {}
    last_whole: ClassVar[int] = -1
    # Overall opacity, for fading the bar out once it's full.
    alpha: ClassVar[float] = 1.0
    last_frame: ClassVar[float] = 0.0
    flash_index: ClassVar[int] = -1
    flash_until: ClassVar[float] = 0.0


def _white() -> Any:
    if Draw.white is None:
        texture = find_object("Texture2D", "EngineResources.WhiteSquareTexture")
        Draw.white = texture
        # Sample the whole texture - UL/VL are in texels, and 1x1 reads one corner.
        Draw.white_ul = float(getattr(texture, "SizeX", 0) or 1)
        Draw.white_vl = float(getattr(texture, "SizeY", 0) or 1)
    return Draw.white


def _blend() -> int:
    if Draw.blend is None:
        try:
            Draw.blend = int(find_enum("EBlendMode").BLEND_Translucent)
        except Exception:  # noqa: BLE001
            Draw.blend = 2
    return Draw.blend


def _colour(rgba: Colour) -> Any:
    # Opacity is stepped in 64 levels so the colour cache stays small while fading.
    r, g, b, a = rgba
    key = (r, g, b, round(a * Draw.alpha / 4) * 4)
    colour = Draw.colours.get(key)
    if colour is None:
        colour = make_struct("LinearColor", R=r / 255.0, G=g / 255.0, B=b / 255.0, A=key[3] / 255.0)
        Draw.colours[key] = colour
    return colour


def rect(canvas: Any, x: float, y: float, w: float, h: float, rgba: Colour) -> None:
    x, y, w, h = round(x), round(y), round(w), round(h)
    if w <= 0 or h <= 0:
        return
    canvas.SetPos(x, y)
    canvas.DrawTile(_white(), w, h, 0.0, 0.0, Draw.white_ul, Draw.white_vl, _colour(rgba), False, _blend())


# --- shapes ------------------------------------------------------------------------


def slanted(
    canvas: Any,
    x: float,
    y: float,
    w: float,
    h: float,
    skew: float,
    rgba: Colour,
    fill: float = 1.0,
    rows: tuple[float, float] = (0.0, 1.0),
) -> None:
    """A parallelogram whose top edge sits `skew` pixels right of its bottom.

    `fill` draws only the left part of it, measured along its width. `rows`
    limits it to a band of its height, as fractions - used for highlights.
    """
    height = max(1, round(h))
    first, last = round(rows[0] * height), round(rows[1] * height)
    for r in range(first, last):
        shift = skew * (1.0 - (r + 0.5) / height)
        rect(canvas, x + shift, y + r, w * fill, 1, rgba)


def chevron(
    canvas: Any,
    x: float,
    y: float,
    w: float,
    h: float,
    point: float,
    rgba: Colour,
    fill: float = 1.0,
    rows: tuple[float, float] = (0.0, 1.0),
) -> None:
    """A right-pointing chevron: rows shift right towards the middle, like ">"."""
    height = max(2, round(h))
    first, last = round(rows[0] * height), round(rows[1] * height)
    for r in range(first, last):
        t = (r + 0.5) / height
        shift = point * (1.0 - abs(t * 2.0 - 1.0))
        rect(canvas, x + shift, y + r, w * fill, 1, rgba)


def diamond(canvas: Any, cx: float, cy: float, half: float, rgba: Colour, fill_from_bottom: float = 1.0) -> None:
    """A diamond centred on (cx, cy), optionally filled only from the bottom up."""
    size = max(2, round(half * 2))
    first = round(size * (1.0 - max(0.0, min(1.0, fill_from_bottom))))
    for r in range(first, size):
        t = (r + 0.5) / size
        width = half * 2 * (1.0 - abs(t * 2 - 1.0))
        rect(canvas, cx - width / 2, cy - half + r, width, 1, rgba)


# --- styles ------------------------------------------------------------------------


def _segment_colour(index: int, filled: float) -> Colour:
    if index == Draw.flash_index and time.monotonic() < Draw.flash_until:
        return FLASH
    return FULL if filled >= 1.0 else REGEN


def draw_chevrons(canvas: Any, cx: float, y: float, scale: float, total: int, charges: float) -> None:
    w, h, point, gap = 26 * scale, 16 * scale, 9 * scale, 6 * scale
    span = total * w + (total - 1) * gap + point
    x0 = cx - span / 2
    o = max(OUTLINE_PX, round(OUTLINE_PX * scale))
    for i in range(total):
        x = x0 + i * (w + gap)
        filled = max(0.0, min(1.0, charges - i))
        chevron(canvas, x - o, y - o, w + 2 * o, h + 2 * o, point, OUTLINE)
        if filled < 1.0:  # a full charge covers its background completely
            chevron(canvas, x, y, w, h, point, EMPTY)
        if filled > 0.0:
            chevron(canvas, x, y, w, h, point, _segment_colour(i, filled), fill=filled)
            if filled >= 1.0:
                chevron(canvas, x, y, w, h, point, HIGHLIGHT, rows=(0.0, 0.25))


def draw_gauge(canvas: Any, cx: float, y: float, scale: float, total: int, charges: float) -> None:
    w, h, skew = (46 * total) * scale, 12 * scale, 10 * scale
    x0 = cx - (w + skew) / 2
    o = max(OUTLINE_PX, round(OUTLINE_PX * scale))
    level = max(0.0, min(1.0, charges / total))
    whole = int(charges) / total
    slanted(canvas, x0 - o, y - o, w + 2 * o, h + 2 * o, skew, OUTLINE)
    slanted(canvas, x0, y, w, h, skew, EMPTY)
    if level > 0.0:
        slanted(canvas, x0, y, w, h, skew, REGEN, fill=level)
    if whole > 0.0:
        flashing = Draw.flash_index >= 0 and time.monotonic() < Draw.flash_until
        slanted(canvas, x0, y, w, h, skew, FLASH if flashing else FULL, fill=whole)
        slanted(canvas, x0, y, w, h, skew, HIGHLIGHT, fill=whole, rows=(0.0, 0.3))
    # Notches between charges, slanted to match the bar.
    for i in range(1, total):
        nx = x0 + w * i / total
        slanted(canvas, nx - 1 * scale, y, 2 * scale, h, skew, OUTLINE)


def draw_diamonds(canvas: Any, cx: float, y: float, scale: float, total: int, charges: float) -> None:
    half, gap = 11 * scale, 8 * scale
    o = max(OUTLINE_PX, round(OUTLINE_PX * scale)) + 1
    step = half * 2 + gap
    x0 = cx - (total - 1) * step / 2
    for i in range(total):
        dx = x0 + i * step
        dy = y + half
        filled = max(0.0, min(1.0, charges - i))
        diamond(canvas, dx, dy, half + o, OUTLINE)
        if filled < 1.0:
            diamond(canvas, dx, dy, half, EMPTY)
        if filled > 0.0:
            diamond(canvas, dx, dy, half, _segment_colour(i, filled), fill_from_bottom=filled)
            if filled >= 1.0:
                diamond(canvas, dx, dy - half * 0.35, half * 0.35, HIGHLIGHT)


STYLES = {
    "Chevrons": draw_chevrons,
    "Gauge": draw_gauge,
    "Diamonds": draw_diamonds,
}


# --- entry point -------------------------------------------------------------------


def in_gameplay() -> bool:
    try:
        pc = get_pc()
        if pc is None or pc.Pawn is None or pc.GetHUDMovie() is None:
            return False
    except Exception:  # noqa: BLE001
        return False
    return not any(getattr(pc, flag, False) for flag in ("bStatusMenuOpen", "bInMenu", "bIsPaused"))


def _track_flash(charges: float) -> None:
    """Flash a charge for a moment when it finishes recharging."""
    whole = int(charges)
    if Draw.last_whole >= 0 and whole > Draw.last_whole:
        Draw.flash_index = whole - 1
        Draw.flash_until = time.monotonic() + FLASH_SECONDS
    Draw.last_whole = whole


def draw(canvas: Any) -> None:
    if not options.show_bar.value or canvas is None or not in_gameplay():
        return
    try:
        render(canvas, stamina.State.charges)
    except Exception as ex:  # noqa: BLE001
        Warn.once("bar", f"stamina bar could not draw: {ex!r}")


# How the bar fades when "Hide bar when full" is on.
FADE_OUT_SECONDS: float = 1.0
FADE_IN_SECONDS: float = 0.12

# Where each position puts the bar, measured off BL2's HUD at 16:9: the centre
# of the bar (x) and its top edge (y), as fractions of a 16:9 frame. The HUD
# scales with screen height, so on wider screens the frame is centred.
POSITIONS: dict[str, tuple[float, float, str]] = {
    "Above XP bar": (0.5, 0.81, "center"),  # clears the icon on the XP bar
    "Below health and shields": (0.078, 0.935, "left"),
}


def _update_fade(charges: float, total: int) -> None:
    now = time.monotonic()
    delta = min(now - Draw.last_frame, 0.1) if Draw.last_frame else 0.0
    Draw.last_frame = now
    hide = bool(options.bar_hide_full.value) and charges >= total and now >= Draw.flash_until
    if hide:
        Draw.alpha = max(0.0, Draw.alpha - delta / FADE_OUT_SECONDS)
    else:
        Draw.alpha = min(1.0, Draw.alpha + delta / FADE_IN_SECONDS) if options.bar_hide_full.value else 1.0


def anchor(width: float, height: float, span: float) -> tuple[float, float]:
    """Centre x and top y for the bar, for the chosen position."""
    where = str(options.bar_position.value)
    if where not in POSITIONS:
        return width / 2.0, height * options.pct(options.bar_height)
    fx, fy, align = POSITIONS[where]
    frame_w = min(width, height * 16.0 / 9.0)
    left = (width - frame_w) / 2.0
    x = left + frame_w * fx
    if align == "left":
        x += span / 2.0
    return x, height * fy


SPANS = {
    "Chevrons": lambda total, scale: (total * 26 + (total - 1) * 6 + 9) * scale,
    "Gauge": lambda total, scale: (46 * total + 10) * scale,
    "Diamonds": lambda total, scale: (total * 22 + (total - 1) * 8) * scale,
}


def render(canvas: Any, charges: float) -> None:
    total = int(options.num(options.charges))
    width, height = canvas.ClipX, canvas.ClipY
    if total <= 0 or not width or not height:
        return
    _track_flash(charges)
    _update_fade(charges, total)
    if Draw.alpha <= 0.0:
        return
    # Scale with resolution, so the bar is the same size on screen at 1080p and 4K.
    scale = options.pct(options.bar_size) * (height / 1080.0)
    name = str(options.bar_style.value)
    style = STYLES.get(name, draw_chevrons)
    span = SPANS.get(name, SPANS["Chevrons"])(total, scale)
    cx, top = anchor(width, height, span)
    style(canvas, cx, top, scale, total, charges)
