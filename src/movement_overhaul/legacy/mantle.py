# Originally the standalone `mantle` mod, now part of Movement Overhaul.
# Only its registration, its links to the other movement mods and its option
# objects were rewired - see the bottom of this file.

"""Pull yourself over a ledge instead of bouncing off it.

Borderlands 2 has no mantle. UE3's walking physics steps the pawn up anything
under `MaxStepHeight`, which is 35 units on `Engine.Pawn`, and treats everything
above that as a wall. So a waist-high crate stops you dead, and the only way up
is to find the angle where a jump happens to clear it. With a dash, an air dash
and a double jump in the kit, that is the last thing that still breaks a run.

## Everything this mod cannot use

The obvious implementations are all closed off, and each one had to be measured
to find that out.

**pyunrealsdk cannot call pure-native UFunctions in this build.** They return
zeroed defaults without executing and never raise, so the failure looks exactly
like your own logic being wrong:

- `Trace` returned `(None, {0,0,0}, {0,0,0}, <empty HitInfo>)` fired straight
  down from the pawn to 400 units below its feet, which cannot miss the floor.
- `FastTrace` returned `False` - its "blocked" answer - for *everything*,
  including a zero-length segment from a point to itself.
- `FindSpot` and `SetLocation` are the same shape and equally unusable.

The dividing line is the `Defined` flag, meaning the function has script
bytecode. `IsOnGroundOrShortFall`, `EndSprint` and `DoJump` are `Defined` and
call fine; `Final|Native` alone is a function you cannot call. Properties are
unaffected and behave normally.

**The game's own mantle is out of reach.** `WillowAIPawn.MantleFinished` and
`DoCoverMantleLerp` belong to the AI cover system, hanging off `CoverComponent`
and `MyCoverState`, which a player pawn does not have.

**`HitWall` never fires for this.** It is `Defined|Event|Public` and hookable,
`WillowPlayerPawn` does not override it, and there is no `bNotifyHitWall` gate
in this build - and it still produced not one event across a session of running
into walls. UE3's walking physics slides a pawn along a wall rather than
reporting a collision, so for the case that matters there is nothing to hear.

## So this asks the engine nothing and reads only motion

Two property reads per frame, both of which the other movement mods already
depend on:

- `Location`, differenced against last frame, is how fast we are *actually*
  moving.
- `Acceleration` is what we are *asking* to do - the same wish-direction signal
  `air_control` steers by.

Wanting to move and not moving means something is in the way. That is the whole
detection: no trace, no collision event, nothing the engine has to volunteer.

## And it lets physics decide what a ledge is

There is no way left to measure how high an obstacle is, so this does not try.
While you are blocked with jump held it simply drives you upward and forward,
and watches what happens:

- Movement resumes and you have risen past `MaxStepHeight` - you cleared it.
  That was a ledge, and you are standing on it.
- You reach `Max ledge height` still blocked - that is a wall, not a ledge.
  Let go and fall.
- `Pull-up time` expires still blocked - same answer, let go.

Which is more robust than measuring would have been. It does not care what
shape the geometry is, it cannot mantle into a ceiling because you simply stay
blocked, and it handles every ledge height under the cap without being told any
of them.

Movement is by writing `Velocity`, re-asserted each frame, since `SetLocation`
is native and does nothing. That is the technique `stamina` uses to hold a pawn
up through an air dash, and collision stays intact throughout.
"""

from __future__ import annotations

import ctypes
import math
import sys
from typing import TYPE_CHECKING, Any, ClassVar

from mods_base import BoolOption, DropdownOption, SpinnerOption, build_mod, hook
from unrealsdk import find_enum, logging
from unrealsdk.hooks import Type

from .bridge import peer  # noqa: F401

if TYPE_CHECKING:
    from unrealsdk import unreal

from .. import audio as movement_audio

# `Engine.Pawn.MaxStepHeight`. Rising less than this proves nothing - the engine
# steps you up that much on its own, over kerbs and stairs.
MAX_STEP_HEIGHT: float = 35.0

# Treat as "not actually moving": this many units per second, or a quarter of
# what we are trying to move at, whichever is larger. A blocked pawn reads
# near zero; the floor is here so a slow walk against a wall still counts.
STUCK_SPEED: float = 60.0
STUCK_FRACTION: float = 0.25

# Telling "over the lip" from "still grinding up it" has been the hard part of
# this mod, and two attempts got it wrong in the same way. Both were satisfiable
# by what the climb itself was doing: the climb drives horizontal velocity into
# the wall, so it kept reading its own pushing as evidence it had arrived, let
# go, dropped, re-detected the block and started over. Pull, drop, pull.
#
# First attempt was instantaneous speed over 120 u/s, which any creep against an
# irregular face satisfies. Second was 55 units of forward progress, which is
# fine on a clean ledge and wrong on a deep sloped lip, where you can grind that
# far forward without ever being over.
#
# Third attempt was moving at most of the commanded speed, and that is fakeable
# too - by the lift. On a sloped lip, rising raises how far forward the pawn is
# allowed to be, and the lift climbs faster than the pawn pushes, so it travels
# completely unclamped while nowhere near over.
#
# The way out is to stop looking for a signal that proves we are over, because
# every one of them can be produced by the climb. What is true of a lip and not
# of a ledge is that the forward motion is CAUSED by the rising: stop rising and
# a lip re-clamps you within a frame, while a real ledge lets you keep going. So
# coasting is a test rather than a commitment - hold altitude, see whether we
# are still free, and drop back to lifting if we are not. Altitude never falls
# during that, so however many times it toggles it stays one upward motion.
FREE_MOVE_FRACTION: float = 0.75
CLEAR_PROGRESS: float = 70.0

# `EPhysics.PHYS_Walking` - the engine reporting the pawn as supported. Landing
# on the ledge is what ends a climb, and being supported is the only unfakeable
# way to know it happened.
PHYS_WALKING_FALLBACK: int = 1

# How long the coast phase may run, as a multiple of the pull-up time, before
# we accept that we are never going to land and let go.
TIMEOUT_FACTOR: float = 2.5

# The lift speed is set against a typical ledge, NOT against the height cap.
# They were the same number once - climb speed was `ceiling / pull_time` - and
# raising the cap to reach taller ledges made the lift violently faster, which
# overshot every low ledge and confused the coast test. The cap says how far we
# are willing to go; the pull-up time says how fast. They are separate things.
LIFT_REFERENCE: float = 150.0

# Moving freely at no height means nothing was ever really in the way - the
# block reading was a graze off a doorframe or a corner. Give it this long to
# resolve, then abandon the climb rather than hold the pawn in the air for the
# whole timeout doing nothing. In one session 16 of 50 failures were these,
# each one floating the player in place for almost half a second.
FALSE_START_SECONDS: float = 0.12

# Be blocked this long before it counts. One frame of contact is a graze, and
# starting a climb off that would fire constantly against every doorframe.
STUCK_SECONDS: float = 0.1

# After a climb, refuse another for this long. Chaining up stacked crates is
# the point, but without a gap the same obstacle re-triggers as you land.
COOLDOWN_SECONDS: float = 0.3

# Holding jump is the trigger, and `WillowPlayerInput.Jump` only fires on the
# press - so if you are already holding it as you come at a ledge, the event
# fired while there was nothing to grab and never fires again. Windows will
# simply tell us instead. Same trick `stamina` uses for its landing slide.
VK_SPACE: int = 0x20

# Tested only on a genuine jump press, when by definition the bound key is down.
VK_CANDIDATES: tuple[int, ...] = (
    0x20,  # space
    0xA0,  # left shift
    0x0D,  # enter
    0x45,  # E
    0x46,  # F
    0x51,  # Q
    0x52,  # R
    0x01,  # left mouse
    0x02,  # right mouse
    0x04,  # middle mouse
)

try:
    _user32: Any = ctypes.windll.user32
except Exception:  # noqa: BLE001 - pragma: no cover
    _user32 = None


def _walking_value() -> int:
    """`EPhysics.PHYS_Walking`, or UE3's literal for it if the enum isn't found."""
    try:
        return int(find_enum("EPhysics").PHYS_Walking)
    except Exception:  # noqa: BLE001 - the literal has been 1 since UE3 shipped
        return PHYS_WALKING_FALLBACK


PHYS_WALKING: int = _walking_value()


trigger = DropdownOption(
    "Trigger",
    "Hold jump",
    ["Hold jump", "Automatic"],
    description=(
        "How a mantle starts. Hold jump takes a ledge for as long as the jump"
        " key is down, the way movement shooters do - so running at a wall"
        " with jump held pulls you up. Automatic needs no key."
    ),
)

max_height = SpinnerOption(
    "Max ledge height",
    "220",
    [str(v) for v in range(60, 401, 10)],
    description=(
        "How far you will climb before deciding it is a wall rather than a"
        " ledge, in unreal units. The pawn's half-height is about 81. Measured"
        " against real geometry, ledges worth taking run to about 150, so this"
        " leaves headroom above that. Raising it does not slow down rejecting a"
        " wall - the lift speed scales with it, so either way that takes one"
        " pull-up time."
    ),
)

pull_time = SpinnerOption(
    "Pull-up time",
    "0.25",
    [f"{v / 100:.2f}" for v in range(15, 101, 5)],
    description=(
        "Longest a climb may last. Reaching this while still blocked means it"
        " was a wall, and you let go."
    ),
)

exit_boost = SpinnerOption(
    "Exit boost",
    "0.60",
    [f"{v / 100:.2f}" for v in range(0, 151, 10)],
    description=(
        "How much of your walking speed carries you forward onto the ledge as"
        " the climb ends. This is what puts you on top rather than on the edge."
    ),
)

from_ground = BoolOption(
    "Mantle from standing",
    True,
    "On",
    "Off",
    description=(
        "Allow a mantle with both feet on the ground, not just in mid-air. Off"
        " means you have to leave the ground to take a ledge."
    ),
)

trace = BoolOption(
    "Diagnostics",
    False,
    "On",
    "Off",
    description="Log blockage and every climb to unrealsdk.log. Turn off when not debugging.",
)


def number(option: SpinnerOption, fallback: float) -> float:
    """A spinner's value as a float, defensively."""
    try:
        return float(str(option.value))
    except (AttributeError, ValueError):
        return fallback


# ---------------------------------------------------------------------------
# Plain numbers, so the decisions can be checked without the game
# ---------------------------------------------------------------------------


def is_blocked(actual_speed: float, pressing: bool, ground_speed: float) -> bool:
    """Wanting to move and not moving means something is in the way."""
    if not pressing:
        return False
    return actual_speed < max(STUCK_SPEED, ground_speed * STUCK_FRACTION)


def is_over_lip(actual_speed: float, commanded: float, progress: float) -> bool:
    """Whether we have stopped grinding and started actually moving over.

    Moving at most of the speed we are asking for means nothing is in the way
    any more. The distance test is only a backstop, for a lip smooth enough
    that the pawn was never really stopped by it.
    """
    if commanded > 0.0 and actual_speed >= commanded * FREE_MOVE_FRACTION:
        return True
    return progress >= CLEAR_PROGRESS


def has_landed(supported: bool, rise: float) -> bool:
    """On something, higher than the engine would have stepped us anyway.

    Being supported is the one signal a climb cannot manufacture for itself,
    which is exactly why the climb ends on it.
    """
    return supported and rise > MAX_STEP_HEIGHT


def normalise(x: float, y: float) -> tuple[float, float]:
    length = math.hypot(x, y)
    if length <= 0.0:
        return 0.0, 0.0
    return x / length, y / length


def key_down(vk: int) -> bool:
    """Live physical state of one virtual key."""
    if _user32 is None:
        return False
    try:
        return bool(_user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class State:
    # motion tracking
    last_xy: ClassVar[tuple[float, float] | None] = None
    blocked_for: ClassVar[float] = 0.0

    # the climb
    active: ClassVar[bool] = False
    coasting: ClassVar[bool] = False
    elapsed: ClassVar[float] = 0.0
    start_z: ClassVar[float] = 0.0
    start_xy: ClassVar[tuple[float, float]] = (0.0, 0.0)
    heading: ClassVar[tuple[float, float]] = (0.0, 0.0)

    cooldown: ClassVar[float] = 0.0
    needs_ground: ClassVar[bool] = False
    jump_vk: ClassVar[int] = VK_SPACE
    dims_logged: ClassVar[bool] = False

    @classmethod
    def clear(cls) -> None:
        cls.active = False
        cls.coasting = False
        cls.elapsed = 0.0
        cls.blocked_for = 0.0


def learn_jump_key() -> None:
    """Called on a genuine jump press, when the bound key must be held."""
    if key_down(State.jump_vk):
        return
    for vk in VK_CANDIDATES:
        if key_down(vk):
            State.jump_vk = vk
            if trace.value:
                logging.info(f"[mantle] jump key learned: vk=0x{vk:02X}")
            return


def jump_held() -> bool:
    """Whether jump is physically down right now. No inference involved."""
    return key_down(State.jump_vk)


# ---------------------------------------------------------------------------
# The game-facing half
# ---------------------------------------------------------------------------


def grounded(pawn: unreal.UObject) -> bool:
    try:
        return bool(pawn.IsOnGroundOrShortFall())
    except Exception:  # noqa: BLE001
        return False


def cancel_dash() -> None:
    """End a stamina dash in flight, so it doesn't fight the climb."""
    stamina = peer("stamina")
    if stamina is None:
        return
    try:
        if stamina.State.dashing:
            stamina.State.dashing = False
            stamina.State.dash_left = 0.0
    except AttributeError:
        pass


@hook("WillowGame.WillowPlayerInput:Jump")
def jump_pressed(
    _obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Only here to learn which key jump is bound to.

    The mantle itself is decided per frame off the physical key state - a press
    event cannot express "still holding it", which is what we need.
    """
    learn_jump_key()


def begin(
    pawn: unreal.UObject,
    heading: tuple[float, float],
    z: float,
    xy: tuple[float, float],
) -> None:
    """Start climbing whatever is in the way."""
    State.active = True
    State.coasting = False
    State.elapsed = 0.0
    State.start_z = z
    State.start_xy = xy
    State.heading = heading
    State.blocked_for = 0.0
    cancel_dash()

    if movement_audio is not None:
        try:
            movement_audio.play("mantle")
        except Exception:  # noqa: BLE001, S110 - audio must never break a mantle
            pass

    if trace.value:
        logging.info(
            f"[mantle] climbing: heading {heading[0]:+.2f},{heading[1]:+.2f} from z={z:.0f}",
        )


def finish(
    pawn: unreal.UObject,
    *,
    cleared: bool,
    why: str,
    penalise: bool = True,
) -> None:
    """Hand the pawn back, with a push onto the ledge if we made it."""
    try:
        velocity = pawn.Velocity
        if cleared:
            speed = float(getattr(pawn, "GroundSpeed", 0.0) or 0.0) * number(exit_boost, 0.6)
            velocity.X = State.heading[0] * speed
            velocity.Y = State.heading[1] * speed
            velocity.Z = 0.0
        else:
            # Let go cleanly rather than leaving the climb's upward push on.
            velocity.Z = 0.0
    except Exception as ex:  # noqa: BLE001
        if trace.value:
            logging.error(f"[mantle] could not settle: {ex!r}")

    if trace.value:
        logging.info(
            f"[mantle] {'made it' if cleared else 'let go'} after "
            f"{State.elapsed:.2f}s - {why}",
        )
    State.clear()
    State.cooldown = COOLDOWN_SECONDS
    # Refused once, so don't try again until the pawn is back on the ground.
    # Otherwise each attempt lifts by the height allowance and the next one
    # starts from there, walking straight up a wall a climb at a time. A false
    # start is exempt: nothing was climbed, so there is nothing to ratchet, and
    # making the player land first would eat a legitimate ledge right after.
    State.needs_ground = penalise and not cleared


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove", Type.POST)
def player_move(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Watch how fast we're really moving, and climb whatever stops us."""
    pawn = obj.Pawn
    if pawn is None:
        State.clear()
        State.last_xy = None
        return

    delta = float(args.DeltaTime)
    if delta <= 0.0:
        return

    try:
        location = pawn.Location
        here = (float(location.X), float(location.Y))
        z = float(location.Z)
        accel = pawn.Acceleration
        wish = normalise(float(accel.X), float(accel.Y))
        ground_speed = float(getattr(pawn, "GroundSpeed", 0.0) or 0.0)
    except Exception as ex:  # noqa: BLE001
        if trace.value:
            logging.error(f"[mantle] could not read the pawn: {ex!r}")
        return

    # How fast we ACTUALLY moved, which is the only thing that knows about walls.
    previous = State.last_xy
    State.last_xy = here
    if previous is None:
        return
    actual_speed = math.dist(previous, here) / delta

    if State.active:
        run_climb(pawn, here, z, actual_speed, ground_speed, delta)
        return

    if State.cooldown > 0.0:
        State.cooldown = max(0.0, State.cooldown - delta)
        return

    if State.needs_ground:
        if not grounded(pawn):
            State.blocked_for = 0.0
            return
        State.needs_ground = False

    pressing = wish != (0.0, 0.0)
    if not is_blocked(actual_speed, pressing, ground_speed):
        State.blocked_for = 0.0
        return

    State.blocked_for += delta
    if State.blocked_for < STUCK_SECONDS:
        return

    if str(trigger.value) != "Automatic" and not jump_held():
        return
    if not bool(from_ground.value) and grounded(pawn):
        return

    begin(pawn, wish, z, here)


def run_climb(
    pawn: unreal.UObject,
    here: tuple[float, float],
    z: float,
    actual_speed: float,
    commanded: float,
    delta: float,
) -> None:
    """Drive one frame of a climb.

    Two phases that swap back and forth freely. **Lift** drives the pawn upward
    against whatever stopped it. **Coast** holds altitude and travels forward,
    and exists to answer the one question that matters: is there floor under us
    yet? If the answer is no - we re-clamp against the lip - it goes straight
    back to lifting, having lost nothing, because coasting holds height rather
    than giving it up.

    Every frame ends by writing a velocity, including the ones where the phase
    changes. An earlier version returned early on a phase change, and gravity
    took those frames; on geometry that toggled often, enough of them were lost
    that the climb bled height and ran out of clock partway up.

    Landing is what ends a climb. It is the only signal the climb cannot
    manufacture for itself, which three earlier attempts all learned the hard
    way - see the notes on `FREE_MOVE_FRACTION`.
    """
    State.elapsed += delta
    rise = z - State.start_z
    ceiling = number(max_height, 150.0)
    span = max(number(pull_time, 0.25), 0.05)
    budget = span * TIMEOUT_FACTOR

    # Distance made good along the heading since the climb started. Projected,
    # so sliding sideways along a wall doesn't count as getting over it.
    progress = (
        (here[0] - State.start_xy[0]) * State.heading[0]
        + (here[1] - State.start_xy[1]) * State.heading[1]
    )

    try:
        supported = int(pawn.Physics) == PHYS_WALKING
    except Exception:  # noqa: BLE001
        supported = False

    # --- which phase are we in this frame? --------------------------------
    moving_freely = is_over_lip(actual_speed, commanded, progress)
    if State.coasting != moving_freely:
        State.coasting = moving_freely
        if trace.value:
            logging.info(
                f"[mantle] {'coasting' if moving_freely else 're-clamped, lifting'}"
                f" at {rise:.0f} up ({actual_speed:.0f}/{commanded:.0f} u/s)",
            )

    # --- was there ever anything there? ------------------------------------
    # Free movement at no height means we were never blocked. Let go at once.
    if (
        moving_freely
        and rise <= MAX_STEP_HEIGHT
        and State.elapsed >= FALSE_START_SECONDS
    ):
        finish(
            pawn,
            cleared=False,
            why=f"nothing was blocking us ({rise:.0f} up)",
            penalise=False,
        )
        return

    # --- is it over? -------------------------------------------------------
    if State.coasting and has_landed(supported, rise):
        finish(pawn, cleared=True, why=f"landed {rise:.0f} up, {progress:.0f} forward")
        return
    # Only a height we are still STUCK at proves a wall. A ledge sitting exactly
    # at the cap would otherwise be rejected on the very frame we got over it,
    # which failed every ledge at the limit. Coasting pins Z, so rise cannot run
    # away here - the time budget below is what bounds it.
    if rise >= ceiling and not State.coasting:
        finish(pawn, cleared=False, why=f"reached {rise:.0f} still stuck, that's a wall")
        return
    if State.elapsed >= budget:
        finish(pawn, cleared=False, why=f"out of time at {rise:.0f} up")
        return

    # --- drive ------------------------------------------------------------
    # A typical ledge in one pull-up time. Deliberately not scaled to the cap -
    # see LIFT_REFERENCE.
    climb_speed = LIFT_REFERENCE / span
    try:
        velocity = pawn.Velocity
        # Coasting pins Z to zero - level flight across the lip. Neither of the
        # alternatives worked: carrying the lift speed in flung the pawn well
        # past the ledge, and letting gravity have it pulled it back below the
        # lip before it had crossed the width of its own body.
        velocity.Z = 0.0 if State.coasting else climb_speed
        velocity.X = State.heading[0] * commanded
        velocity.Y = State.heading[1] * commanded
    except Exception as ex:  # noqa: BLE001
        if trace.value:
            logging.error(f"[mantle] could not steer: {ex!r}")
        State.clear()
        State.cooldown = COOLDOWN_SECONDS


def on_disable() -> None:
    State.clear()
    State.cooldown = 0.0
    State.needs_ground = False
    State.last_xy = None


logging.info("[mantle] loaded")

# (registered by Movement Overhaul)


# --- options: read from Movement Overhaul's menu --------------------------
from .. import options  # noqa: E402
from .bridge import Mapped  # noqa: E402

trigger = Mapped(lambda: "Automatic" if str(options.mantle_mode.value) == "Automatic" else "Hold jump")
max_height = Mapped(lambda: str(int(options.num(options.mantle_height))))
pull_time = Mapped(lambda: f"{options.num(options.mantle_time) / 1000.0:.2f}")
exit_boost = Mapped(lambda: "0.60")
from_ground = Mapped(lambda: True)
trace = Mapped(lambda: options.diagnostics.value)
