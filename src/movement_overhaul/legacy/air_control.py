# Originally the standalone `air_control` mod, now part of Movement Overhaul.
# Only its registration, its links to the other movement mods and its option
# objects were rewired - see the bottom of this file.

"""Gives a falling pawn the ability to steer, without bleeding off its momentum.

UE3's `physFalling` scales the pawn's acceleration by `Pawn.AirControl` before
integrating it, and BL2 ships that at 0.11 - hence jumping committing you to
whatever direction you were already going.

Raising `AirControl` is the obvious fix, and it feels right, because acceleration
*vector-adds* to the velocity you already have: mouse-turn and strafe keys blend
continuously instead of fighting. That's also how the reference game works - it's Source
underneath, and Source air movement is acceleration-based.

Its one flaw is the speed clamp. `CalcVelocity` trims you back to the pawn's max
speed while acceleration points along velocity, so steering hard enough also
scrubs off whatever you jumped in with. That's what eats a slide jump.

`Drift` is the fix, and the default: set `AirControl` to 0 so the engine stops
both steering and clamping, then apply the same acceleration ourselves under a
rule the engine doesn't have - air control may steer you freely and may slow you
down, but it can only ever *raise* your speed up to the cap. Whatever you entered
the air with is a floor that air control can never scrub off.

Two others are kept for comparison. `Engine` is the stock behaviour with the
value turned up. `Momentum` rotates the velocity vector instead of accelerating,
which preserves scalar speed but swings your heading bodily onto whatever key you
press - interesting with mouse-only steering, bad with WASD.

Wish direction is read off `Pawn.Acceleration` in a POST hook, after the walking
state has already built it from input and camera yaw - so analog sticks and any
control remapping come along for free.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, ClassVar

from mods_base import BoolOption, DropdownOption, SpinnerOption, build_mod, hook
from unrealsdk import find_enum, logging
from unrealsdk.hooks import Type

from .bridge import peer  # noqa: F401

if TYPE_CHECKING:
    from unrealsdk import unreal


def _falling_value() -> int:
    """`EPhysics.PHYS_Falling`, or UE3's literal for it if the enum isn't found."""
    try:
        return int(find_enum("EPhysics").PHYS_Falling)
    except Exception:  # noqa: BLE001 - the literal has been 2 since UE3 shipped
        return 2


PHYS_FALLING: int = _falling_value()

# Fallbacks for the pawn properties we scale against, in case a pawn turns up
# without them. Unreal units per second, and per second squared.
GROUND_SPEED_FALLBACK: float = 440.0
ACCEL_RATE_FALLBACK: float = 2048.0

# Below this horizontal speed there's no meaningful direction to rotate, so
# Momentum's steering falls back to plain acceleration.
STEER_FLOOR: float = 20.0

# Frames between diagnostic lines, so the log stays readable.
TRACE_INTERVAL: int = 15


def steps(low: float, high: float, step: float) -> list[str]:
    """Exact multiples of `step` as display strings.

    Same trick as slide_tuning: the mod menu's slider hands the callback an int,
    so anything fractional has to go through a SpinnerOption over strings.
    """
    count = round((high - low) / step)
    decimals = 0 if step >= 1 else 2
    return [f"{low + i * step:.{decimals}f}" for i in range(count + 1)]


style = DropdownOption(
    "Style",
    "Drift",
    ["Drift", "Engine", "Momentum", "Air-strafe"],
    description=(
        "Drift: accelerate toward where you're aiming, and never lose the speed"
        " you jumped in with. Engine: the game's own air control turned up, which"
        " steers but trims your speed back down. Momentum: rotate your heading"
        " onto the key you press. Air-strafe: Quake rules, needs real technique."
    ),
)

authority = SpinnerOption(
    "Air authority",
    "0.60",
    steps(0.05, 2.00, 0.05),
    description=(
        "Drift style. How hard you accelerate toward where you're aiming while"
        " airborne, as a multiple of your ground acceleration. BL2's stock air"
        " control is 0.11, so anything above that is already an improvement;"
        " 1.00 is as responsive in the air as you are on the ground."
    ),
)

speed_cap = SpinnerOption(
    "Air speed cap",
    "1.00",
    steps(0.50, 3.00, 0.10),
    description=(
        "Drift style, as a multiple of your ground speed. The fastest air control"
        " alone can push you. It is NOT a speed limit - entering the air faster"
        " than this, off a slide jump, keeps every bit of it."
    ),
)

turn_rate = SpinnerOption(
    "Turn rate",
    "270",
    steps(30, 720, 30),
    description=(
        "Momentum style. Degrees per second your heading can swing onto the"
        " direction you're holding. Try it with mouse steering and no A/D."
    ),
)

air_accel = SpinnerOption(
    "Air acceleration",
    "10",
    steps(1, 30, 1),
    description=(
        "Air-strafe style, and Momentum's ramp-up from a standing jump. Source's"
        " default is 10."
    ),
)

air_cap = SpinnerOption(
    "Air-strafe speed cap",
    "0.10",
    steps(0.05, 1.00, 0.05),
    description=(
        "Air-strafe style, as a fraction of ground speed. Caps how much speed you"
        " can add ALONG the direction you're holding, which is why it does nothing"
        " until you hold a direction well off your heading. That is the technique."
    ),
)

engine_strength = SpinnerOption(
    "Engine air control",
    "0.45",
    steps(0.00, 1.00, 0.05),
    description="Engine style. What to set Pawn.AirControl to while falling. Stock is 0.11.",
)

trace = BoolOption(
    "Diagnostics",
    False,
    "On",
    "Off",
    description="Log airborne speed and direction to unrealsdk.log. Off unless debugging.",
)


def amount(option: SpinnerOption, fallback: float = 0.0) -> float:
    """The number behind one of the spinners above."""
    try:
        return float(option.value)
    except (TypeError, ValueError):
        return fallback


class State:
    """What we know about the pawn we're currently steering."""

    pawn: ClassVar[Any] = None
    stock_air_control: ClassVar[float] = 0.0
    was_falling: ClassVar[bool] = False
    frames: ClassVar[int] = 0
    warned: ClassVar[bool] = False
    airborne_logged: ClassVar[bool] = False

    @classmethod
    def adopt(cls, pawn: unreal.UObject) -> None:
        """Note a new pawn and remember the air control it came with.

        We only ever write `AirControl` while falling, and a brand new pawn hasn't
        been written to yet, so whatever it holds right now is the stock value.
        """
        if cls.pawn is not None and cls.pawn == pawn:
            return
        cls.pawn = pawn
        cls.stock_air_control = getattr(pawn, "AirControl", 0.0)
        cls.was_falling = False
        logging.info(
            f"[air_control] pawn adopted: AirControl={cls.stock_air_control:.3f} "
            f"GroundSpeed={getattr(pawn, 'GroundSpeed', 0.0):.1f} "
            f"AccelRate={getattr(pawn, 'AccelRate', 0.0):.1f}",
        )


def set_air_control(pawn: unreal.UObject, value: float) -> None:
    """Write `Pawn.AirControl`, complaining at most once if the property is gone."""
    try:
        pawn.AirControl = value
    except Exception as ex:  # noqa: BLE001 - never break the movement tick
        if not State.warned:
            State.warned = True
            logging.error(f"[air_control] cannot set Pawn.AirControl: {ex!r}")


def wish_direction(pawn: unreal.UObject) -> tuple[float, float] | None:
    """The horizontal unit vector the player is asking to move along.

    `PlayerWalking.PlayerMove` has already turned stick/key input into
    `Pawn.Acceleration`, rotated into world space by the camera's yaw, so all
    that's left is to drop Z and normalise. Returns None when nothing is held.
    """
    accel = pawn.Acceleration
    x, y = accel.X, accel.Y
    length = math.hypot(x, y)
    if length < 1e-4:
        return None
    return x / length, y / length


def drift(
    vx: float,
    vy: float,
    wx: float,
    wy: float,
    accel: float,
    cap: float,
    delta: float,
) -> tuple[float, float]:
    """Accelerate toward the wish direction, under a cap that can only hold you back.

    The clamp compares against `max(cap, speed you already had)`, so it only ever
    trims acceleration that would push you past the cap - it can never scrub speed
    you brought into the air. Steering and slowing down stay completely free,
    because both of those come out below the speed you started the frame with.
    """
    before = math.hypot(vx, vy)

    gain = accel * delta
    vx += wx * gain
    vy += wy * gain

    after = math.hypot(vx, vy)
    limit = max(cap, before)
    if after > limit and after > 0.0:
        scale = limit / after
        vx *= scale
        vy *= scale

    return vx, vy


def accelerate(
    vx: float,
    vy: float,
    wx: float,
    wy: float,
    wish_speed: float,
    rate: float,
    delta: float,
) -> tuple[float, float]:
    """Quake's air-accelerate.

    Only the component of velocity *along* the wish direction is capped, so
    holding a direction offset from where you're travelling keeps adding speed
    instead of redirecting what you already have. That asymmetry is the whole
    reason strafe-jumping works - and the reason this does nothing at all when
    you hold the direction you're already moving in.
    """
    along = vx * wx + vy * wy
    add = wish_speed - along
    if add <= 0.0:
        return vx, vy
    gain = min(rate * wish_speed * delta, add)
    return vx + wx * gain, vy + wy * gain


def steer(
    vx: float,
    vy: float,
    wx: float,
    wy: float,
    max_degrees: float,
    delta: float,
) -> tuple[float, float]:
    """Rotate a horizontal velocity toward the wish direction, keeping its length."""
    speed = math.hypot(vx, vy)
    if speed < STEER_FLOOR:
        return vx, vy

    difference = math.atan2(wy, wx) - math.atan2(vy, vx)
    # Wrap into (-pi, pi] so we always turn the short way round.
    difference = (difference + math.pi) % (2.0 * math.pi) - math.pi

    limit = math.radians(max_degrees) * delta
    turn = max(-limit, min(limit, difference))

    cos_t, sin_t = math.cos(turn), math.sin(turn)
    return vx * cos_t - vy * sin_t, vx * sin_t + vy * cos_t


def snapshot(pawn: unreal.UObject, wish: tuple[float, float] | None) -> None:
    """One line of airborne state, for tuning by log."""
    State.frames += 1
    if State.frames % TRACE_INTERVAL:
        return
    try:
        velocity = pawn.Velocity
        speed = math.hypot(velocity.X, velocity.Y)
        held = "none" if wish is None else f"{math.degrees(math.atan2(wish[1], wish[0])):+7.1f}deg"
        logging.info(
            f"[air_control] style={style.value} hspeed={speed:7.1f} "
            f"vz={velocity.Z:+8.1f} ground={pawn.GroundSpeed:6.1f} "
            f"held={held} airControl={pawn.AirControl:.3f}",
        )
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[air_control] snapshot failed: {ex!r}")


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove", Type.POST)
def player_move(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Steer the pawn while it's falling.

    POST, so the walking state has already built this frame's `Acceleration` from
    input - and so anything Sliding stashed on the way out of a slide jump is
    already in `Velocity` for us to carry.
    """
    pawn = obj.Pawn
    if pawn is None:
        return

    State.adopt(pawn)

    try:
        physics = int(pawn.Physics)
    except (TypeError, ValueError):  # an enum wrapper that won't coerce
        physics = PHYS_FALLING if not pawn.IsOnGroundOrShortFall() else -1

    if physics != PHYS_FALLING:
        if State.was_falling:
            # Hand the pawn back exactly as we found it.
            set_air_control(pawn, State.stock_air_control)
            State.was_falling = False
        return

    if not State.was_falling:
        State.was_falling = True
        if not State.airborne_logged:
            # One line, once per session, regardless of the diagnostics toggle: if
            # this is missing from the log the mod is switched off, not broken.
            State.airborne_logged = True
            logging.info(f"[air_control] first airborne frame, style={style.value}")

    chosen = style.value

    if chosen == "Engine":
        # Let the engine do the work - it reads AirControl during its own tick.
        set_air_control(pawn, amount(engine_strength))
        if trace.value:
            snapshot(pawn, wish_direction(pawn))
        return

    # We're driving velocity ourselves, so take the engine out of it entirely.
    # Zeroing AirControl also sidesteps CalcVelocity's max-speed clamp, which only
    # triggers while acceleration points along velocity - that clamp is exactly
    # what trims a slide jump back down to a walk under Engine style.
    set_air_control(pawn, 0.0)

    wish = wish_direction(pawn)
    if trace.value:
        snapshot(pawn, wish)
    if wish is None:
        return

    delta = args.DeltaTime
    if delta <= 0.0:
        return

    velocity = pawn.Velocity
    vx, vy = velocity.X, velocity.Y
    wx, wy = wish
    ground = getattr(pawn, "GroundSpeed", 0.0) or GROUND_SPEED_FALLBACK

    if chosen == "Drift":
        rate = (getattr(pawn, "AccelRate", 0.0) or ACCEL_RATE_FALLBACK) * amount(authority, 0.6)
        vx, vy = drift(vx, vy, wx, wy, rate, ground * amount(speed_cap, 1.0), delta)
    elif chosen == "Air-strafe":
        vx, vy = accelerate(
            vx,
            vy,
            wx,
            wy,
            ground * amount(air_cap, 0.1),
            amount(air_accel, 10.0),
            delta,
        )
    else:
        # Momentum. Jumping from a standstill leaves nothing to steer, so ramp up
        # to walking pace first; past that, turning is all rotation.
        if math.hypot(vx, vy) < ground:
            vx, vy = accelerate(vx, vy, wx, wy, ground, amount(air_accel, 10.0), delta)
        vx, vy = steer(vx, vy, wx, wy, amount(turn_rate, 270.0), delta)

    velocity.X = vx
    velocity.Y = vy


# (registered by Movement Overhaul)



# --- options: read from Movement Overhaul's menu --------------------------
from .. import options  # noqa: E402
from .bridge import Mapped  # noqa: E402

style = Mapped(lambda: "Drift")
authority = Mapped(lambda: f"{options.pct(options.air_steering):.2f}")
speed_cap = Mapped(lambda: f"{options.pct(options.air_speed_cap):.2f}")
trace = Mapped(lambda: options.diagnostics.value)
