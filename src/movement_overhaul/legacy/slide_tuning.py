# Originally the standalone `slide_tuning` mod, now part of Movement Overhaul.
# Its registration, links to the other movement mods and option objects were
# rewired (see the bottom of this file), and v1.2 added dash momentum carrying
# into slides.

"""Tunes slide speed, distance and slide-jump momentum without editing Sliding.

Sliding keeps slide speed in `Pawn.CrouchedPct`: it starts at `SLIDE_SPEED_DEFAULT`,
bleeds off at a fixed rate per second, and the slide ends once it falls back to
`CROUCHED_PCT_DEFAULT`. Distance is the area under that decay curve, which for a
linear bleed from S to E at rate k is `(S**2 - E**2) / 2k`.

That makes speed and distance separable. Pick the start speed you want, then solve
for the k that yields the distance you want:

    k = (S**2 - E**2) / (2 * target_distance)

So the two sliders below don't fight each other - raising the initial speed makes
the slide decay faster rather than making it travel further.

Nothing here edits Sliding. Start speed is applied by a POST hook on the crouch
input, after Sliding's PRE hook has begun the slide; the decay adjustment is a
POST hook on the move function, after Sliding has applied its own bleed for the
frame; and the jump boost scales the velocity Sliding has already stashed.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any

from mods_base import SpinnerOption, build_mod, hook
from unrealsdk.hooks import Type

from .bridge import peer  # noqa: F401

if TYPE_CHECKING:
    from unrealsdk import unreal


# These mirror constants and literals inside sliding's `slide()`. If Sliding ever
# changes them, the numbers below stop being accurate - recheck after updating it.
STOCK_START: float = 2.2
STOCK_EXIT: float = 0.5
STOCK_DECAY: float = 0.7

STOCK_DISTANCE: float = (STOCK_START**2 - STOCK_EXIT**2) / (2 * STOCK_DECAY)

# Fast finish. A linear bleed all the way down to STOCK_EXIT spends its last
# ~1.8s (at 140% / 300%) slower than a sprint, which is what made slides feel
# slow. With fast finish on, the configured distance is covered on the way down
# to sprint speed, then the rest bleeds off at TAIL_DECAY so the slide drops out
# in ~0.2s. Sliding still ends it the usual way, at STOCK_EXIT.
# 1.35 = sprint (~594 u/s) over the ground speed a slide runs at (~440 u/s),
# both measured in-game on 2026-09-24.
SPRINT_PCT: float = 1.35
TAIL_DECAY: float = 4.0

# A slide jump banks its speed so the next slide can pick it back up. Sliding only
# starts a slide from `bInSprintState`, which hasn't come back yet in the moment
# you land, so chaining otherwise depends on out-running that gap.
CARRY_FRAMES: int = 240  # ~4s ceiling, in case a jump never lands
CARRY_GROUND_GRACE: int = 45  # ~0.75s on the ground to still count as a chain


class MomentumIn:
    """Dash momentum a slide started with, so the slide opens at that speed.

    Slide speed is `CrouchedPct x GroundSpeed`, and GroundSpeed is contended
    around a slide start (the dash hands it back, the sprint drops out), so the
    conversion is re-done every frame for a short window rather than once.
    `ceiling` lifts the usual start-speed clamp for this slide only.
    """

    speed: float = 0.0
    window: float = 0.0
    ceiling: float = 0.0

    @classmethod
    def clear(cls) -> None:
        cls.speed = cls.window = cls.ceiling = 0.0


# How long after a slide starts its opening speed is re-matched to the momentum.
MOMENTUM_IN_SECONDS: float = 0.15


def match_momentum(pawn: unreal.UObject) -> None:
    """Raise CrouchedPct so the slide moves at least as fast as the momentum it took in."""
    ground = float(getattr(pawn, "GroundSpeed", 0.0) or 0.0)
    if ground <= 0.0 or MomentumIn.speed <= 0.0:
        return
    want = MomentumIn.speed / ground
    MomentumIn.ceiling = max(MomentumIn.ceiling, want)
    if pawn.CrouchedPct < want:
        pawn.CrouchedPct = want


def carry_in(pawn: unreal.UObject, speed: float) -> None:
    """Called by stamina when a slide starts with dash momentum still live."""
    if speed <= 0.0:
        return
    MomentumIn.speed = speed
    MomentumIn.window = MOMENTUM_IN_SECONDS
    match_momentum(pawn)


class Carry:
    """Speed banked by the last slide jump."""

    speed: float = 0.0
    frames: int = 0
    ground: int = 0

    @classmethod
    def bank(cls, speed: float) -> None:
        cls.speed = speed
        cls.frames = CARRY_FRAMES
        cls.ground = CARRY_GROUND_GRACE

    @classmethod
    def clear(cls) -> None:
        cls.speed = 0.0
        cls.frames = 0
        cls.ground = 0

    @classmethod
    def available(cls) -> bool:
        return cls.frames > 0 and cls.speed > STOCK_EXIT


def steps(low: float, high: float, step: float = 0.05) -> list[str]:
    """Exact multiples of `step` as display strings.

    The mod menu renders a SpinnerOption from a list of strings and writes back
    the chosen index, so every value is exactly what's shown. Its slider widget
    can't do this - the engine hands the callback an int, so a fractional slider
    silently collapses to whole numbers.
    """
    count = round((high - low) / step)
    return [f"{low + i * step:.2f}" for i in range(count + 1)]


speed = SpinnerOption(
    "Initial speed",
    "1.50",
    steps(1.0, 2.0),
    description=(
        "How fast a slide starts, as a multiple of stock speed. Distance stays"
        " wherever Slide distance puts it, so raising this alone makes the slide"
        " FASTER BUT SHORTER. Raise Slide distance too to keep its length."
    ),
)

distance = SpinnerOption(
    "Slide distance",
    "2.00",
    steps(1.0, 3.0),
    description=(
        "How far a slide carries, as a multiple of stock distance. Independent of"
        " initial speed."
    ),
)

jump_momentum = SpinnerOption(
    "Slide jump momentum",
    "1.30",
    steps(1.0, 2.0),
    description=(
        "Horizontal speed kept when you jump out of a slide, as a multiple of what"
        " the slide would normally carry. 1.00 is stock."
    ),
)


def amount(option: SpinnerOption) -> float:
    """The numeric value behind one of the spinners above."""
    try:
        return float(option.value)
    except (TypeError, ValueError):
        return 1.0


def get_sliding() -> ModuleType | None:
    """The Sliding mod's module, or None if it isn't loaded."""
    return peer("sliding")


def is_sliding(sliding: ModuleType) -> bool:
    try:
        return bool(sliding.OWN_SLIDE_STATE.is_sliding)
    except AttributeError:
        return False


def start_speed() -> float:
    return STOCK_START * amount(speed)


def decay_rate() -> float:
    """The per-second bleed that lands the configured speed on the configured distance."""
    target = STOCK_DISTANCE * amount(distance)
    floor = SPRINT_PCT if fast_finish.value and start_speed() > SPRINT_PCT else STOCK_EXIT
    return (start_speed() ** 2 - floor**2) / (2 * target)


def frame_decay_rate(current: float) -> float:
    """The bleed for this frame: the solved rate, or the quick finish below sprint speed."""
    if fast_finish.value and current <= SPRINT_PCT:
        return TAIL_DECAY
    return decay_rate()


@hook("WillowGame.WillowPlayerInput:DuckPressed", Type.POST)
def duck_pressed(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Set a new slide's opening speed, chaining off a slide jump where we can."""
    sliding = get_sliding()
    if sliding is None:
        return

    pc = obj.Outer
    if pc is None:
        return
    pawn = pc.Pawn
    if pawn is None:
        return

    opening = min(Carry.speed, start_speed()) if Carry.available() else start_speed()

    if is_sliding(sliding):
        # Sliding started it for us. Only touch a slide that just began - a fresh
        # one sits exactly at the stock start speed.
        if pawn.CrouchedPct >= sliding.SLIDE_SPEED_DEFAULT - 0.01:
            pawn.CrouchedPct = opening
            Carry.clear()
        return

    # Sliding declined, because it only ever starts a slide from `bInSprintState`
    # and that hasn't come back yet after landing. If the last slide jump banked
    # speed and we're back on the ground, carry it into a new slide ourselves.
    if not Carry.available() or not pawn.IsOnGroundOrShortFall():
        return

    try:
        sliding.enter_slide(pc)
    except Exception:  # noqa: BLE001 - never break the crouch input
        Carry.clear()
        return

    if is_sliding(sliding):
        pawn.CrouchedPct = opening
    Carry.clear()


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove", Type.POST)
def player_move(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Correct this frame's bleed from Sliding's fixed rate to our computed one."""
    sliding = get_sliding()
    if sliding is None:
        return

    pawn = obj.Pawn
    if pawn is None:
        return

    sliding_now = is_sliding(sliding)

    # Age the banked slide-jump speed. Note we may still be flagged as sliding for
    # a frame or two right after banking it, so don't treat that as consumed here -
    # `duck_pressed` clears it when a new slide actually picks it up.
    if Carry.frames > 0:
        Carry.frames -= 1
        if pawn.IsOnGroundOrShortFall():
            Carry.ground -= 1
            if Carry.ground <= 0:
                Carry.clear()
        else:
            # Still airborne - don't start counting the landing grace yet.
            Carry.ground = CARRY_GROUND_GRACE

    if not sliding_now:
        MomentumIn.clear()
        return

    # Sliding already removed delta * STOCK_DECAY this frame. Add back the
    # difference - negative when we want a faster bleed than stock.
    adjust = args.DeltaTime * (STOCK_DECAY - frame_decay_rate(pawn.CrouchedPct))
    if adjust == 0.0:
        return

    pawn.CrouchedPct = min(pawn.CrouchedPct + adjust, max(start_speed(), MomentumIn.ceiling))

    if MomentumIn.window > 0.0:
        MomentumIn.window -= args.DeltaTime
        match_momentum(pawn)


def _bank_slide_jump(obj: unreal.UObject) -> None:
    """Remember the speed this slide jump left with, for the next slide."""
    pc = obj.Outer
    if pc is None:
        return
    pawn = pc.Pawn
    if pawn is None:
        return
    Carry.bank(pawn.CrouchedPct)


@hook("WillowGame.WillowPlayerInput:Jump", Type.POST)
def jump(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Scale the horizontal velocity Sliding stashed for a slide jump."""
    sliding = get_sliding()
    if sliding is None:
        return

    try:
        if not sliding.State.do_slide_jump:
            return
        velocity = sliding.State.horizontal_velocity
    except AttributeError:
        return

    multiplier = amount(jump_momentum)
    if multiplier == 1.0:
        _bank_slide_jump(obj)
        return

    velocity.x *= multiplier
    velocity.y *= multiplier

    _bank_slide_jump(obj)


# (registered by Movement Overhaul)


# --- options: read from Movement Overhaul's menu --------------------------
from .. import options  # noqa: E402
from .bridge import Mapped  # noqa: E402

speed = Mapped(lambda: f"{options.pct(options.slide_speed):.2f}")
distance = Mapped(lambda: f"{options.pct(options.slide_distance):.2f}")
jump_momentum = Mapped(lambda: f"{options.pct(options.slide_jump):.2f}")
fast_finish = Mapped(lambda: bool(options.slide_fast_finish.value))
