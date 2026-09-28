# Originally the standalone `sprint_combat` mod, now part of Movement Overhaul.
# Only its registration, its links to the other movement mods and its option
# objects were rewired - see the bottom of this file.

"""Keep sprinting when you shoot, and when you aim down sights.

Borderlands 2 drops you out of a sprint the moment you pull the trigger or
bring up the sights. In a pack built around staying in motion that is the last
restriction that still makes you stop and start, and unlike most of this pack's
problems the game hands us the lever directly.

## The mechanism

Sprinting is gated by two script functions on the player pawn:

    WillowPlayerPawn.CanSprint()            - may a sprint begin?
    WillowPlayerPawn.CanContinueSprinting() - may this one carry on?

Both are `Defined`, which means they have script bytecode and so can be hooked,
and that matters more here than it sounds. Most of the interesting movement
functions in this game are `Final|Native`, which through the SDK cannot be
called at all - they return zeroed defaults without executing and never raise.
These two are ordinary script, so a PRE hook can take the call, ask the stock
implementation what it would have said, and answer differently.

That last part is the important bit of restraint. This does not force the gates
open; it runs them first and only overrides a refusal, and only while you are
actually firing or aiming. Every other reason the game has for refusing a
sprint - downed, dead, in a vehicle, mid-animation - is still respected,
because the stock answer is still what decides those.

## Knowing what you're doing

Firing comes from the weapon's own state machine: `WeaponFiring.BeginState` and
`EndState` are `Defined|Simulated|Event`, so the transitions can be hooked
directly rather than inferred. They fire for every weapon in the level,
including enemies', so each one is checked against the local player's pawn
before it counts.

Aiming is read rather than asked, because `IsZoomed` and `IsZoomedIn` are both
native and therefore unusable. `WillowWeapon.ZoomState` is a plain byte
property holding `EZoomState`, and a property read works fine:

    0 ZST_NotZoomed   1 ZST_ZoomingIn   2 ZST_Zoomed   3 ZST_ZoomingOut

Anything but `NotZoomed` counts, so the sprint survives the transition in as
well as the held sights.

## The aiming slowdown

Aiming costs about 60% of your movement speed, and it is applied to
`GroundSpeed` - measured, not assumed:

    not aiming, walking     440        aiming, walking     178   (0.40x)
    not aiming, sprinting   594        aiming, sprinting   229   (0.39x)

Since it lands on a property, it can simply be written back. What gets
written is the speed you had *the instant before you raised the sights*,
snapshotted then and held for as long as you keep them up.

That is deliberately not a walk-versus-sprint classification, which is what
this did first and got badly wrong. `autorun` implements sprinting in any
direction by forcing `GroundSpeed` up to sprint speed **while
`bInSprintState` is false** - that is the entire trick. Keying a "walking
baseline" off that flag therefore learned 594 as the walking speed, restored it
on every aim, and with a `stamina` dash in the mix could capture 742 and hold
*that*. Speed went through the roof.

A snapshot cannot do this. It is taken from a frame before we touched anything,
it is never re-learned from our own writes, and whatever the other movement
mods had legitimately arranged for your speed at that moment is simply what you
keep. It also yields outright while `stamina` is driving a dash, because a dash
is a more specific claim on the property than this is.

## The firing pose that used to be here

There was a feature that tried to drop the weapon out of its run stance while
you shot, by clearing the controller's `bInSprintState` for a moment. It is
gone. The reasoning is kept so nobody rebuilds it.

**It never moved the model.** That was the entire point of it, and it was
measured not to happen: the weapon stays in its run pose with the flag cleared.

**It moved everything else.** `DoSprint` runs every frame and re-enters a
sprint whenever the pawn is willing, which it usually is - so clearing the flag
invited `BeginSprint` back sixty times a second. Each one re-applied the sprint
FOV, which is the view lurching on every burst, and compounded the sprint
speed-up until the player was moving at 25,696 units per second. A thrashing
FOV also rescales mouse input through `ProcessDeviceLookAxes`, which threw the
camera around. One bad lever, three visible faults, none of them the thing it
was for.

**The pose is not an animation.** The first-person arms tree - `pawn.Arms`,
which is `AddCameraBone` over `WeaponRecoil` and `AimState` - has no sprint
node anywhere in it, and `Idle+Fire` is already playing underneath while you
sprint. The run stance is native arms code offset by `SprintingPct`, and no
script handle for it has been found.

If it is ever attempted again: `bIsSprinting` is attribute-backed
(`bIsSprintingBaseValue`, `bIsSprintingModifierStack`) and writing the computed
field desyncs the attribute system badly enough to stop the pawn moving at all.
`SprintingPct` has no such backing and is the only candidate worth testing.

## What this deliberately does not do

It does not touch `EndSprint`. That is the other obvious place to intervene and
it is a trap: `autorun` *calls* `EndSprint` directly to turn its own toggle
off, so blocking the function would break that mod's toggle rather than the
game's behaviour. If the gates turn out not to be the whole story, the
diagnostics below log every `EndSprint` without interfering, which is enough to
tell us where else to look.
"""

from __future__ import annotations

import math
import sys
from typing import TYPE_CHECKING, Any, ClassVar

from mods_base import BoolOption, SpinnerOption, build_mod, get_pc, hook
from unrealsdk import find_enum, logging
from unrealsdk.hooks import Block, Type, prevent_hooking_direct_calls

from .bridge import peer  # noqa: F401

if TYPE_CHECKING:
    from unrealsdk import unreal


def _not_zoomed_value() -> int:
    """`EZoomState.ZST_NotZoomed`, or its literal if the enum isn't found."""
    try:
        return int(find_enum("EZoomState").ZST_NotZoomed)
    except Exception:  # noqa: BLE001 - read out of the package as 0
        return 0


ZST_NOT_ZOOMED: int = _not_zoomed_value()


while_shooting = BoolOption(
    "Shoot while sprinting",
    True,
    "On",
    "Off",
    description="Keep sprinting when you fire, instead of being dropped to a walk.",
)

while_aiming = BoolOption(
    "Aim while sprinting",
    True,
    "On",
    "Off",
    description="Keep sprinting while aiming down sights.",
)

full_speed_aiming = BoolOption(
    "Full speed while aiming",
    True,
    "On",
    "Off",
    description=(
        "Remove the movement penalty for aiming down sights. BL2 cuts you to"
        " about 40% speed while zoomed; this puts it back."
    ),
)



trace = BoolOption(
    "Diagnostics",
    False,
    "On",
    "Off",
    description=(
        "Log every sprint refusal, whether it was overridden, and any EndSprint"
        " the game makes. Turn off when not debugging."
    ),
)


# One line per this many ordinary refusals, which are constant and normal.
REFUSAL_LOG_INTERVAL: int = 500


def number(option: SpinnerOption, fallback: float) -> float:
    """A spinner's value as a float, defensively."""
    try:
        return float(str(option.value))
    except (AttributeError, ValueError):
        return fallback


class State:
    firing: ClassVar[bool] = False
    overrides: ClassVar[int] = 0
    refusals: ClassVar[int] = 0
    end_sprints: ClassVar[int] = 0

    # The speed seen on the last frame we were not aiming, and the value
    # snapshotted from it when the sights went up. Never written from a frame
    # this mod touched, which is what stops it ratcheting.
    last_unaimed: ClassVar[float] = 0.0
    hold: ClassVar[float] = 0.0
    was_aiming: ClassVar[bool] = False
    logged_speeds: ClassVar[bool] = False

    # Speed watchdog. `GroundSpeed` is contended - stamina, autorun, mantle and
    # this mod all write it - so when it drops for no reason the useful thing
    # is a snapshot of who was doing what at that instant.
    # GroundSpeed as last seen on a frame nobody was inflating: no dash, no
    # pose hold, no aim hold. The only value either feature is allowed to pin.
    clean_speed: ClassVar[float] = 0.0

    prev_speed: ClassVar[float] = 0.0
    prev_xy: ClassVar[tuple[float, float] | None] = None
    prev_measured: ClassVar[float] = 0.0
    drops: ClassVar[int] = 0


def local_pawn() -> Any:
    try:
        pc = get_pc()
        return None if pc is None else pc.Pawn
    except Exception:  # noqa: BLE001
        return None


def belongs_to_player(weapon: unreal.UObject) -> bool:
    """Whether a weapon is the one the local player is holding.

    The firing-state events fire for every weapon in the level, so without this
    an enemy opening up across the map would read as us pulling the trigger.
    """
    pawn = local_pawn()
    if pawn is None:
        return False
    for attr in ("Instigator", "Owner"):
        try:
            if getattr(weapon, attr) == pawn:
                return True
        except Exception:  # noqa: BLE001, S112
            continue
    return False


def is_aiming(pawn: unreal.UObject) -> bool:
    """Whether the held weapon is anywhere in its zoom cycle."""
    try:
        weapon = pawn.Weapon
        if weapon is None:
            return False
        return int(weapon.ZoomState) != ZST_NOT_ZOOMED
    except Exception:  # noqa: BLE001
        return False


def should_override(pawn: unreal.UObject) -> str | None:
    """Why we're overriding a refusal, or None to let it stand."""
    if State.firing and while_shooting.value:
        return "firing"
    if while_aiming.value and is_aiming(pawn):
        return "aiming"
    return None


def gate(pawn: unreal.UObject, func: unreal.BoundFunction, name: str) -> tuple[Any, bool]:
    """Run the stock check, then answer it - overriding only a refusal.

    The stock implementation is called exactly once and its result returned, so
    every reason the game has for refusing a sprint still applies. We only
    disagree with it about shooting and aiming.
    """
    try:
        with prevent_hooking_direct_calls():
            stock = bool(func())
    except Exception as ex:  # noqa: BLE001 - never break sprinting over this
        if trace.value:
            logging.error(f"[sprint_combat] {name} failed: {ex!r}")
        return None, False  # type: ignore[return-value]

    if stock:
        return Block, True

    State.refusals += 1
    reason = should_override(pawn)
    if reason is None:
        # Rate limited. A refusal is ordinary - standing still is one - and the
        # gates are consulted every frame, so logging each put 40,979 lines in
        # one session's log. The count is what's informative, not the lines.
        if trace.value and State.refusals % REFUSAL_LOG_INTERVAL == 1:
            logging.info(
                f"[sprint_combat] {name} refused, not ours to override"
                f" ({State.refusals} so far, {State.overrides} overridden)",
            )
        return Block, False

    State.overrides += 1
    if trace.value:
        logging.info(f"[sprint_combat] {name} refused while {reason} - overridden")
    return Block, True


@hook("WillowGame.WillowPlayerPawn:CanContinueSprinting", Type.PRE)
def can_continue_sprinting(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    func: unreal.BoundFunction,
) -> tuple[Any, bool]:
    """The gate that ends a sprint in progress."""
    return gate(obj, func, "CanContinueSprinting")


@hook("WillowGame.WillowPlayerPawn:CanSprint", Type.PRE)
def can_sprint(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    func: unreal.BoundFunction,
) -> tuple[Any, bool]:
    """The gate that decides whether a sprint may start at all."""
    return gate(obj, func, "CanSprint")


@hook("WillowGame.WillowWeapon:WeaponFiring.BeginState", Type.POST)
def firing_began(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    if belongs_to_player(obj):
        State.firing = True


@hook("WillowGame.WillowWeapon:WeaponFiring.EndState", Type.POST)
def firing_ended(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    if belongs_to_player(obj):
        State.firing = False












@hook("WillowGame.WillowPlayerController:EndSprint", Type.POST)
def end_sprint(
    _obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Put back the speed that aiming takes away, and watch for sags."""
    State.end_sprints += 1
    if trace.value:
        logging.info(
            f"[sprint_combat] EndSprint called (firing={State.firing},"
            f" overrides={State.overrides}, refusals={State.refusals},"
            f" endsprints={State.end_sprints})",
        )


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


# A fall bigger than this in a single frame is not the engine easing you down.
SPEED_DROP_ALARM: float = 60.0

# A single frame cannot legitimately raise GroundSpeed by more than this
# multiple. Anything steeper is another mod, or the game, compounding something
# - and it must never be adopted as a baseline. This is a backstop, not the
# fix: it exists because a runaway once reached 25,696 and was then learned as
# the sprint speed by two different mods, which outlived the runaway itself.
SANITY_JUMP: float = 1.5

# One report per this many drops, so a persistent cause can't flood the log.
DROP_LOG_INTERVAL: int = 20


def who_owns_speed() -> str:
    """A one-line snapshot of every mod that might be writing GroundSpeed."""
    bits = []
    stamina = peer("stamina")
    if stamina is not None:
        try:
            bits.append(
                f"stamina(dash={bool(stamina.State.dashing)},"
                f"carry={stamina.State.carry:.0f})",
            )
        except Exception:  # noqa: BLE001
            bits.append("stamina(?)")
    autorun = peer("autorun")
    if autorun is not None:
        try:
            bits.append(
                f"autorun(forcing={bool(autorun.State.forcing)},"
                f"walk={autorun.State.walk_speed:.0f},"
                f"sprint={autorun.State.sprint_speed:.0f})",
            )
        except Exception:  # noqa: BLE001
            bits.append("autorun(?)")
    mantle = peer("mantle")
    if mantle is not None:
        try:
            bits.append(
                f"mantle(climbing={bool(mantle.State.active)},"
                f"blocked={mantle.State.blocked_for:.2f},"
                f"cooldown={mantle.State.cooldown:.2f})",
            )
        except Exception:  # noqa: BLE001
            bits.append("mantle(?)")
    bits.append(
        f"sprint_combat(aimhold={State.hold:.0f},"
        f"clean={State.clean_speed:.0f})",
    )
    return " ".join(bits)


def watch_speed(
    pc: unreal.UObject,
    pawn: unreal.UObject,
    current: float,
    aiming: bool,
    delta: float,
) -> None:
    """Report a sudden drop in speed, with who was active at the time.

    Purely diagnostic - it never writes anything. Speed sagging and recovering
    is the kind of fault no single mod can be blamed for from the outside, so
    this records the whole picture at the moment it happens rather than asking
    for it to be reproduced under a probe.

    Both speeds are watched, and that matters: `GroundSpeed` is the property
    `stamina` and `autorun` contend over, but `mantle` never touches it - it
    writes `Velocity` directly, and a false-started climb drives that along the
    wish heading for up to 0.12s. Watching only the property would have missed
    the likeliest culprit entirely.
    """
    previous, State.prev_speed = State.prev_speed, current

    here = None
    measured = 0.0
    try:
        loc = pawn.Location
        here = (float(loc.X), float(loc.Y))
    except Exception:  # noqa: BLE001, S110
        pass
    if here is not None and State.prev_xy is not None and delta > 0.0:
        measured = math.dist(State.prev_xy, here) / delta
    State.prev_xy = here
    was_measured, State.prev_measured = State.prev_measured, measured

    if not trace.value:
        return

    property_dropped = previous > 0.0 and current < previous - SPEED_DROP_ALARM
    actual_dropped = (
        was_measured > SPEED_DROP_ALARM
        and measured < was_measured - SPEED_DROP_ALARM
    )
    if not (property_dropped or actual_dropped):
        return

    State.drops += 1
    if State.drops % DROP_LOG_INTERVAL != 1:
        return
    try:
        sprinting = bool(pc.bInSprintState)
    except Exception:  # noqa: BLE001
        sprinting = False
    what = "GroundSpeed" if property_dropped else "actual"
    logging.info(
        f"[sprint_combat] SPEED DROP ({what}) prop {previous:.0f}->{current:.0f}"
        f" actual {was_measured:.0f}->{measured:.0f} (drop {State.drops})"
        f" sprint={int(sprinting)} aiming={int(aiming)} | {who_owns_speed()}",
    )








@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove", Type.POST)
def per_frame(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Drive both speed-related features, each behind its own guard.

    They were behind a shared early return, which meant switching off the
    aiming option also silently switched off the firing pose. Separate
    features get separate guards.
    """
    pawn = obj.Pawn
    if pawn is None:
        State.hold = 0.0
        State.was_aiming = False
        return

    try:
        delta = float(args.DeltaTime)
        current = float(pawn.GroundSpeed)
    except Exception:  # noqa: BLE001
        return
    if current <= 0.0:
        return

    dashing = stamina_owns_speed()
    aiming = is_aiming(pawn)
    watch_speed(obj, pawn, current, aiming, delta)

    # A frame nobody is inflating: no dash, and neither of our own holds in
    # force. This is the only value either feature is allowed to pin, which is
    # what stops a dash - or one of our own writes - ratcheting upward.
    # Never while aiming, full stop. The guard here was once
    # `not (aiming and State.hold > 0.0)`, which looks equivalent and is not:
    # `hold` is assigned further down the same frame, so on the FIRST aiming
    # frame it is still zero and the penalised speed got adopted as the clean
    # baseline. Everything downstream then held 178 instead of 440.
    if not dashing and not not aiming:
        if State.clean_speed <= 0.0 or current <= State.clean_speed * SANITY_JUMP:
            State.clean_speed = current
        elif trace.value:
            logging.info(
                f"[sprint_combat] refusing absurd speed {current:.0f} as a"
                f" baseline (was {State.clean_speed:.0f})",
            )

    # --- aiming speed ------------------------------------------------------
    if not full_speed_aiming.value:
        State.hold = 0.0
        State.was_aiming = False
        return
    if dashing:
        return

    if not aiming:
        State.last_unaimed = current
        State.hold = 0.0
        State.was_aiming = False
        return

    if not State.was_aiming:
        State.was_aiming = True
        State.hold = State.last_unaimed
        if trace.value:
            logging.info(f"[sprint_combat] sights up at {State.hold:.0f}, holding it")

    if State.hold <= 0.0 or current >= State.hold:
        return

    pawn.GroundSpeed = State.hold
    if trace.value and not State.logged_speeds:
        State.logged_speeds = True
        logging.info(
            f"[sprint_combat] aiming speed restored: {current:.0f} -> {State.hold:.0f}",
        )


def on_disable() -> None:
    State.firing = False
    State.hold = 0.0
    State.was_aiming = False



# (registered by Movement Overhaul)


# --- options: read from Movement Overhaul's menu --------------------------
from .. import options  # noqa: E402
from .bridge import Mapped  # noqa: E402

while_shooting = Mapped(lambda: options.sprint_combat.value)
while_aiming = Mapped(lambda: options.sprint_combat.value)
full_speed_aiming = Mapped(lambda: options.no_aim_slowdown.value)
trace = Mapped(lambda: options.diagnostics.value)
