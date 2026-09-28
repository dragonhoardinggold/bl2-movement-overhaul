# Originally the standalone `autorun` mod, now part of Movement Overhaul.
# Only its registration, its links to the other movement mods and its option
# objects were rewired - see the bottom of this file.

"""Turns BL2's hold-to-sprint into a toggle, and keeps that toggle asserted."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, ClassVar, cast

from mods_base import BoolOption, build_mod, hook
from unrealsdk import logging
from unrealsdk.hooks import Block, prevent_hooking_direct_calls

from .bridge import peer  # noqa: F401

if TYPE_CHECKING:
    from unrealsdk import unreal


# Every window below is in seconds, and used to be in frames. That was a bug.
# These exist to be long enough for a crouch or a slide to get going, and a
# frame count is only a duration if you assume a framerate - at 144fps the
# suppression window was 139ms rather than the 333ms it was tuned to, so the
# slide-cancel this mod is written to prevent came back on fast machines.
#
# `PlayerInput` is an event taking `DeltaTime`, so the real figure was there the
# whole time; nothing was reading it.

# How long to wait before retrying a resume the game refused. Stops us calling
# into unreal every single frame in states that forbid sprinting.
RESUME_RETRY_SECONDS: float = 0.1

# Resume only after we've been continuously eligible this long. A slide or a
# crouch passes through transient states where the duck flag hasn't landed yet,
# and resuming inside that window force-stands you up and kills the slide.
RESUME_SETTLE_SECONDS: float = 0.167

# How long to refuse resuming for after any crouch press.
DUCK_SUPPRESS_SECONDS: float = 0.333

# How long to keep tracing after a crouch press while diagnostics are on.
TRACE_SECONDS: float = 0.667

# The longest single frame we'll believe. A hitch, a load screen or a breakpoint
# hands us a huge `DeltaTime`, and spending it in one go would run every window
# above to zero inside a single frame - precisely the early resume they exist to
# prevent. Clamping can only ever make a window outlast the hitch, which is the
# safe direction to be wrong in.
MAX_DELTA: float = 0.1

trace = BoolOption(
    "Diagnostics",
    False,
    "On",
    "Off",
    description="Log sprint/crouch state to unrealsdk.log. Turn off when not debugging.",
)


def snapshot(pc: unreal.UObject, tag: str) -> None:
    """Dump everything relevant to one line in the log."""
    try:
        pawn = pc.Pawn
        crouched = f"{pawn.CrouchedPct:.3f}" if pawn is not None else "?"
        pi = pc.PlayerInput
        intent = bool(pi.bTryToSprint) if pi is not None else "?"
        grounded = pawn.IsOnGroundOrShortFall() if pawn is not None else "?"
        accel = pawn.Acceleration if pawn is not None else None
        moving = "?" if accel is None else (accel.X != 0.0 or accel.Y != 0.0)
        logging.info(
            f"[autorun:{tag}] t={State.trace_left:.2f} "
            f"sprintState={bool(pc.bInSprintState)} bDuck={bool(pc.bDuck)} "
            f"tryToSprint={intent} crouchedPct={crouched} grounded={grounded} "
            f"moving={moving} sliding={is_sliding()} toggle={State.sprinting} "
            f"suppress={State.duck_suppress:.2f} settled={State.settled:.2f}",
        )
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[autorun:{tag}] snapshot failed: {ex!r}")


def is_sliding() -> bool:
    """True while the Sliding mod has us in a slide. False if it isn't installed."""
    sliding = peer("sliding")
    if sliding is None:
        return False
    try:
        return bool(sliding.OWN_SLIDE_STATE.is_sliding)
    except AttributeError:
        return False

start_sprinting = BoolOption(
    "Starting mode",
    True,
    "Sprint",
    "Walk",
    description="Whether you start out sprinting or walking when you spawn in.",
)

any_direction = BoolOption(
    "Sprint in any direction",
    True,
    "On",
    "Off",
    description=(
        "Sprint backwards and sideways at full speed, not just forwards. BL2"
        " ends a sprint the moment you stop moving forward - measured as"
        " GroundSpeed dropping 594 to 440 with the sprint flag going false - so"
        " this refuses that particular ending while you're still moving."
    ),
)

auto_resume = BoolOption(
    "Resume after slide",
    True,
    "On",
    "Off",
    description=(
        "Automatically start sprinting again once you can, so a slide or crouch"
        " doesn't drop you back to walking until you tap sprint."
    ),
)


class State:
    # Learned by watching, not hardcoded: whatever GroundSpeed reads while the
    # game says we're sprinting, versus while we're merely walking.
    sprint_speed: ClassVar[float] = 0.0
    walk_speed: ClassVar[float] = 0.0
    forcing: ClassVar[bool] = False
    sprinting: ClassVar[bool] = True
    cooldown: ClassVar[float] = 0.0
    settled: ClassVar[float] = 0.0
    duck_suppress: ClassVar[float] = 0.0
    trace_left: ClassVar[float] = 0.0

    @classmethod
    def block_resume(cls) -> None:
        cls.settled = 0.0
        cls.cooldown = 0.0

    @classmethod
    def tick(cls, delta: float) -> None:
        """Run every countdown down by one frame's worth of real time.

        Centralised so the timers keep decaying regardless of which branch the
        frame takes. They used to tick inside `try_resume`, which is only
        reached while the toggle is on and auto-resume is enabled - so with
        either off, a suppression window simply never expired.
        """
        cls.duck_suppress = max(0.0, cls.duck_suppress - delta)
        cls.cooldown = max(0.0, cls.cooldown - delta)
        cls.trace_left = max(0.0, cls.trace_left - delta)


def try_resume(player_input: unreal.UObject, delta: float) -> None:
    """Restart a sprint the game dropped while our toggle still says sprint.

    Ducking - which is what sliding does - clears the controller's sprint state.
    `bTryToSprint` alone won't bring it back, since the game only ever starts a
    sprint off the input event, so re-run that event ourselves.

    The guards matter more than the resume does: starting a sprint force-stands
    the pawn, so resuming even one frame inside a crouch cancels it outright.
    """
    if State.duck_suppress > 0.0:
        State.block_resume()
        return

    if is_sliding():
        State.block_resume()
        return

    pc = player_input.Outer
    if pc is None:
        return

    if pc.bInSprintState:
        State.block_resume()
        return

    pawn = pc.Pawn
    if pawn is None or pc.bDuck or not pawn.IsOnGroundOrShortFall():
        State.block_resume()
        return

    # Standing still is not a sprint, let the player stand still.
    accel = pawn.Acceleration
    if accel.X == 0.0 and accel.Y == 0.0:
        State.block_resume()
        return

    # Only resume once conditions have held steady, never on a transient frame.
    if State.settled < RESUME_SETTLE_SECONDS:
        State.settled += delta
        return

    if State.cooldown > 0.0:
        return
    State.cooldown = RESUME_RETRY_SECONDS

    # Direct call, with our own hook suppressed so it doesn't flip the toggle.
    if trace.value:
        snapshot(pc, "RESUME-FIRE")
    with prevent_hooking_direct_calls():
        player_input.SprintPressed()


@hook("WillowGame.WillowPlayerInput:DuckPressed")
def duck_pressed(
    _obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Never fight a crouch. Hands off long enough for a slide to get going."""
    State.duck_suppress = DUCK_SUPPRESS_SECONDS
    State.block_resume()
    if trace.value:
        State.trace_left = TRACE_SECONDS
        pc = _obj.Outer
        if pc is not None:
            snapshot(pc, "DUCK-PRESS")


@hook("WillowGame.WillowPlayerInput:SprintPressed")
def sprint_pressed(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> type[Block] | None:
    """Tapping sprint flips the toggle rather than starting a held sprint."""
    State.sprinting = not State.sprinting
    State.block_resume()
    if trace.value:
        pc_dbg = obj.Outer
        if pc_dbg is not None:
            snapshot(pc_dbg, "SPRINT-KEY")
    obj.bTryToSprint = State.sprinting

    if State.sprinting:
        # Let the stock function run, it starts the sprint for us.
        return None

    # Toggling off: the stock function would only ever start a sprint, so end it
    # ourselves and swallow the call.
    pc = obj.Outer
    if pc is not None:
        pc.EndSprint()
    return Block


@hook("WillowGame.WillowPlayerInput:SprintReleased")
def sprint_released(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> type[Block]:
    """Always swallowed - letting go of the key must not end the sprint."""
    obj.bTryToSprint = State.sprinting
    return Block




def is_aiming(pawn: unreal.UObject) -> bool:
    """Whether the held weapon is anywhere in its zoom cycle.

    Speeds must never be learned from an aimed frame. Aiming cuts `GroundSpeed`
    to about 40% - 440 becomes 178 - and learning that as the walking speed
    would leave the player crawling the moment this mod handed the property
    back. `ZoomState` is a byte property, which is the only reason we can ask:
    `IsZoomed` and `IsZoomedIn` are both native and unusable through the SDK.
    """
    try:
        weapon = pawn.Weapon
        if weapon is None:
            return False
        return int(weapon.ZoomState) != 0  # EZoomState.ZST_NotZoomed
    except Exception:  # noqa: BLE001
        return False


def stamina_owns_speed() -> bool:
    """True while the stamina mod is driving GroundSpeed for a dash.

    Two mods writing the same property every frame would fight, and a dash is
    the more specific claim, so this one yields for the duration.
    """
    stamina = peer("stamina")
    if stamina is None:
        return False
    try:
        return bool(stamina.State.dashing) or stamina.State.carry > 0.0
    except AttributeError:
        return False


def maintain_any_direction_sprint(pc: unreal.UObject) -> None:
    """Keep sprint speed while moving in a direction BL2 refuses to sprint in.

    BL2 gates sprinting on forward input, and it does not do it through
    `EndSprint` - that was measured, and the function is never called when you
    turn around. Native code clears `bInSprintState` directly, so there is no
    call to intercept and the only honest lever left is the speed itself.

    Both speeds are learned by observation rather than hardcoded, so this keeps
    working if a mod or a class mod changes how fast the character runs.
    """
    pawn = pc.Pawn
    if pawn is None:
        return

    if stamina_owns_speed():
        State.forcing = False
        return

    sprinting_now = bool(pc.bInSprintState)
    speed_now = getattr(pawn, "GroundSpeed", 0.0)

    if not State.forcing and speed_now and not is_aiming(pawn):
        if sprinting_now:
            State.sprint_speed = speed_now
        else:
            State.walk_speed = speed_now

    accel = pawn.Acceleration
    moving = accel.X != 0.0 or accel.Y != 0.0
    wanted = (
        any_direction.value
        and State.sprinting
        and moving
        and not sprinting_now
        and not pc.bDuck
        and not is_sliding()
        and State.sprint_speed > State.walk_speed > 0.0
    )

    if wanted:
        pawn.GroundSpeed = State.sprint_speed
        State.forcing = True
    elif State.forcing:
        # Hand it back, rather than leaving the pawn permanently quick.
        if not sprinting_now and State.walk_speed > 0.0:
            pawn.GroundSpeed = State.walk_speed
        State.forcing = False


@hook("WillowGame.WillowPlayerInput:PlayerInput")
def player_input(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """The game clears the intent flag as you move, so reassert it each frame."""
    delta = min(args.DeltaTime, MAX_DELTA)
    if delta <= 0.0:
        # Paused, or a frame the engine didn't advance. Nothing to age.
        return
    State.tick(delta)

    # Hands off while crouching or sliding. The game clears `bTryToSprint` to let
    # a duck happen at all; forcing it back on makes the game drop the duck
    # instead, which killed the slide one frame after it started.
    pc = obj.Outer
    crouching = is_sliding() or (pc is not None and bool(pc.bDuck))

    if not crouching and obj.bTryToSprint != State.sprinting:
        obj.bTryToSprint = State.sprinting

    if trace.value and State.trace_left > 0.0 and pc is not None:
        snapshot(pc, "frame")

    if State.sprinting and auto_resume.value:
        try_resume(obj, delta)

    if pc is not None:
        maintain_any_direction_sprint(pc)


@hook("WillowGame.WillowPlayerController:SpawningProcessComplete")
def spawning_process_complete(
    _obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Reset to the configured starting mode whenever we spawn in."""
    State.sprinting = cast("bool", start_sprinting.value)
    State.block_resume()
    State.duck_suppress = 0.0


# (registered by Movement Overhaul)


# --- options: read from Movement Overhaul's menu --------------------------
from .. import options  # noqa: E402
from .bridge import Mapped  # noqa: E402

start_sprinting = Mapped(lambda: True)
any_direction = Mapped(lambda: options.any_direction.value)
auto_resume = Mapped(lambda: True)
trace = Mapped(lambda: options.diagnostics.value)
