# Originally the standalone `stamina` mod, now part of Movement Overhaul.
# Its registration, links to the other movement mods and option objects were
# rewired (see the bottom of this file), and v1.2 added dash momentum carrying
# into slides.

"""the reference game's stamina charges, and the three moves that spend them.

the reference game meters mobility with a small pool of discrete charges rather than a
continuous bar: you hold three, a dash or an air jump costs one, and each comes
back on its own timer. That's what makes it read as a resource you *spend*
instead of a meter you *drain* - three is a number you can keep in your head
mid-fight, and the refill timer is what paces an engagement.

The defaults below are measured off a live the reference game sandbox rather than guessed,
using `cl_showpos` and the stamina arc under the crosshair:

    base stamina        3 charges (a 4th comes from the Extra Stamina item)
    regen               ~3.7-4.0s per charge, one at a time
    run speed           ~377 rising to ~420 u/s
    dash speed          546.80 u/s on the ground, dead flat, no ramp in or out
    dash duration       ~0.75s grounded, then back to running pace
    dash direction      required - the key does nothing with no key held
    air dashes          exactly ONE per airborne period, whatever the pool holds
    double jump         exists, costs a charge; the ground jump is free
    jump strength       284 vertical standing, 511 for the air jump - the second
                        jump is 1.8x the first, not a weaker top-up
    air dash            holds Z flat for its whole length (gravity suspended),
                        and is not bound by the grounded 546.80 clamp
    regen delay         none - a spent charge is visibly refilling 200ms later
    regen airborne      yes, at the same rate as on the ground
    slide               cancels a dash outright, mid-window
    whole chain         dash-slide-jump-slide costs ONE charge, the dash

    dash jump           546.80 at the window, ~625 horizontal at the apex,
                        lands at 480 and is back at running pace ~140ms later

That last line is the important one, and it took a capture of a human doing it
properly to find - driven attempts kept jumping too early and landing before the
interesting part, which made the dash look like it simply expired in mid-air. It
does not. 546.80 is a *grounded* clamp, and jumping out of a dash escapes it:
horizontal climbs past the clamp and holds ~625 for the whole flight. Escaping
that clamp is the entire reason the tech exists, so a hard cut at the end of the
window would delete it.

The payoff is airborne only. Landing dumps you back to running pace almost at
once. Two captures of the same jump agree on the apex to within half a unit
(625.94 driven, 625.41 human), which is what makes that reading trustworthy.

Three things here overturned what this file first assumed. The dash is slower
than guessed (1.30x run speed, not 2.20x) but lasts two and a half times longer.
The air-dash cap is a mechanic separate from stamina, without which a deep pool
alone bounds an air chain. And the end of a dash is a decaying ceiling, not a
step down.

One measurement trap worth recording: `cl_showpos`'s `vel` is 3D speed, so every
airborne reading has the jump's own vertical folded into it. A dash jump reads
813.64 leaving the ground, which decomposes as sqrt(625^2 + 520^2) = 813.0 once
the air jump's 511-ish vertical is known - horizontal snaps to 625 at the jump
and holds there to the apex. Read naively it looks like a 267-unit speed boost.
Only a reading where vertical is known - the apex, or a standing jump - says
anything honest about horizontal speed.

The pool here is a float reservoir refilling at `1 / regen_time` charges per
second, clamped to `max_charges`; spending needs a whole charge. That's
indistinguishable from the reference game's independent per-charge timers for anything you
can actually feel, and it's one number instead of five.

Three moves draw on it, and two more ride along free.

`Dash` is a flat held speed along the direction you're holding - and it needs a
direction held, exactly as the reference game does. It is applied as a *held* velocity
rather than a one-frame shove, because UE3's `CalcVelocity` trims a walking pawn
back to `Pawn.GroundSpeed` every frame, so a shove would be gone before you saw
it. For the length of the dash we raise `GroundSpeed` to match, so the engine
stops trimming. Sliding solves the same problem from the other end, through
`CrouchedPct`, which is BL2's own crouched speed multiplier.

`Air dash` is that same impulse while falling, and with `air_dash_flatten` on it
suspends gravity outright for the length of the dash rather than merely zeroing
your fall once. the reference game's moves Z by two and a half units across 493ms; a
one-off zeroing would give back about 125 units of drop over the same window,
which reads as a dive rather than a dash.

`Air jump` sets vertical velocity outright, which also wipes whatever downward
speed you had built up - so a double jump rescues you at any point in a fall,
the way it does in the reference game. It also releases the air dash's gravity hold, or a
jump taken during a dash would be zeroed on the frame it was applied.

`Fast fall` - double-tap crouch airborne - slams you down, and costs nothing.
the reference game has no equivalent; it is here because it is useful.

The `stamina bar` is the charge arc under the crosshair, drawn on Canvas. Two
BL2 specifics made that harder than it looks, both recorded at `tile()` and
`in_gameplay()`: this build's `DrawTile` takes its colour as an argument and
ignores `Canvas.DrawColor`, and `PostRender` keeps firing over menus, so the
bar has to gate itself on actually being in play.

When the window closes, the flat clamp becomes a *ceiling* rather than a step
down. It holds while airborne and bleeds off once you land - `momentum_bleed`.
That ceiling is not optional. air_control's Drift clamps against
`max(cap, speed at the start of the frame)`, so any speed carried into the air
becomes a floor it can never scrub, and an air dash with nothing to bound it
would glide at dash speed forever. Putting the bleed on landing rather than on a
timer is what lets a dash jump carry for its whole flight without carrying
indefinitely, and it works whichever order the two mods' `PlayerMove` hooks run
in.

An air jump also has to stand clear of a slide jump. Sliding sets
`State.do_slide_jump` from a PRE hook on the same input, so by the time our POST
hook runs it's already there to check.

`charges()` and `spend()` are module level on purpose: making a slide cost
stamina later is a call from slide_tuning, not a change in here.
"""

from __future__ import annotations

import ctypes
import math
import sys
from typing import TYPE_CHECKING, Any, ClassVar

from mods_base import (
    ENGINE,
    BoolOption,
    SpinnerOption,
    build_mod,
    get_pc,
    hook,
    keybind,
)
from networking import add_network_functions
from networking.decorators import host
from unrealsdk import find_enum, find_object, logging, make_struct
from unrealsdk.hooks import Type, prevent_hooking_direct_calls

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

# Same fallbacks air_control uses, for a pawn that turns up without them.
GROUND_SPEED_FALLBACK: float = 440.0
ACCEL_RATE_FALLBACK: float = 2048.0
JUMP_Z_FALLBACK: float = 630.0

# Frames between diagnostic lines, so the log stays readable.
TRACE_INTERVAL: int = 15

# A slide jump stands the pawn up, and standing up fires DuckReleased even
# though the key is still held - proven in the log: the release arrives before
# the landing on every run that failed to re-slide. Releases inside this window
# after a jump are the engine's, not the player's, so they're ignored.
#
# Armed only when jumping out of a slide, since that is the only jump that
# stands the pawn up and so the only one that can synthesise a release. Being
# targeted lets it be generous enough to actually cover the stand-up: at 0.08
# it sometimes missed it, breaking the third slide in a chain. A stale flag is
# no longer dangerous either - `release_forced_duck` and the grounded backstop
# both guarantee we cannot leave the player crouched.
DUCK_RELEASE_GRACE: float = 0.20


def steps(low: float, high: float, step: float) -> list[str]:
    """Exact multiples of `step` as display strings.

    Same trick as slide_tuning and air_control: the mod menu's slider hands the
    callback an int, so anything fractional has to go through a SpinnerOption.
    """
    count = round((high - low) / step)
    decimals = 0 if step >= 1 else 2
    return [f"{low + i * step:.{decimals}f}" for i in range(count + 1)]


max_charges = SpinnerOption(
    "Stamina charges",
    "4",
    steps(1, 5, 1),
    description=(
        "How many charges you hold. A dash, an air dash or an air jump spends"
        " one. the reference game runs 3; 4 leaves room for a full air chain."
    ),
)

regen_time = SpinnerOption(
    "Seconds per charge",
    "4.00",
    steps(1.0, 12.0, 0.5),
    description="How long one spent charge takes to come back.",
)

regen_delay = SpinnerOption(
    "Regen delay",
    "0.00",
    steps(0.00, 2.00, 0.25),
    description=(
        "A pause after spending before the pool starts refilling at all. the reference game"
        " has none - a spent charge is already visibly refilling 200ms later - so"
        " this defaults off. Raise it if you want spending to commit harder."
    ),
)

regen_airborne = BoolOption(
    "Regen in the air",
    True,
    "On",
    "Off",
    description=(
        "Whether charges come back while you're off the ground. Off makes long"
        " airborne chains commit you - you land empty and have to walk it off."
    ),
)

dash_speed = SpinnerOption(
    "Dash speed",
    "1.25",
    steps(1.0, 4.0, 0.05),
    description=(
        "Dash speed, as a multiple of your ground speed, held flat for the whole"
        " dash, and measured against whatever you are CURRENTLY doing - BL2's"
        " sprint raises GroundSpeed to 594, so this multiplies 594 while"
        " sprinting and 440 while walking. the reference game measures 1.25 against its"
        " sustained run. Dashing while already faster than this -"
        " out of a slide - redirects what you have rather than slowing you down."
    ),
)

dash_duration = SpinnerOption(
    "Dash duration",
    "0.75",
    steps(0.05, 1.20, 0.05),
    description=(
        "Seconds the dash holds for. Distance is speed times duration, so this is"
        " the knob for how far it carries. the reference game measures ~0.75s."
    ),
)

momentum_bleed = SpinnerOption(
    "Landing momentum bleed",
    "0.65",
    steps(0.05, 2.00, 0.05),
    description=(
        "How fast speed above running pace bleeds off ONCE YOU LAND, in ground"
        " speeds per second. Airborne speed is never bled - a dash jump is meant"
        " to carry for the whole flight, and this is the cost of touching down."
        " Low values let you skim off a landing and keep going."
    ),
)

dash_buffer = SpinnerOption(
    "Dash input buffer",
    "0.25",
    steps(0.00, 0.60, 0.05),
    description=(
        "How long a dash press keeps trying before it gives up. Without this a"
        " press lands on exactly one frame, so asking for a dash a moment before"
        " you are allowed one throws the input away instead of honouring it."
    ),
)

slide_momentum = BoolOption(
    "Slide follows momentum",
    True,
    "On",
    "Off",
    description=(
        "A slide carries on in the direction you were already travelling, even"
        " with no movement key held - so you can turn around mid-slide and keep"
        " going the way you were. Holding a direction still steers as normal."
    ),
)

slide_steer = SpinnerOption(
    "Slide steering",
    "150",
    steps(0, 400, 10),
    description=(
        "Degrees per second you can turn a slide with the movement keys. The"
        " slide keeps its own momentum, and this is how much you may bend it -"
        " 0 locks it to the direction you entered at, high values let you point"
        " it wherever you like."
    ),
)

slide_any_direction = BoolOption(
    "Slide in any direction",
    True,
    "On",
    "Off",
    description=(
        "Start a slide while moving backwards or sideways, not just forwards."
        " Sliding only starts one when BL2's sprint flag is set, and that flag"
        " is never set off-forward - the same refusal autorun works around for"
        " speed. This uses autorun's override as the signal instead."
    ),
)

slide_on_landing = BoolOption(
    "Slide on landing",
    True,
    "On",
    "Off",
    description=(
        "Land with crouch already held and drop straight into a slide. Sliding"
        " only ever starts a slide from a crouch PRESS, so holding it through a"
        " jump otherwise lands you in a crouch and nothing else."
    ),
)

fast_fall = BoolOption(
    "Fast fall",
    True,
    "On",
    "Off",
    description="Double-tap crouch in the air to slam straight down.",
)

fast_fall_speed = SpinnerOption(
    "Fast fall speed",
    "2.50",
    steps(1.00, 5.00, 0.25),
    description=(
        "Downward speed of a fast fall, as a multiple of your jump strength. It"
        " is set outright, so it converts a rise into a drop instantly rather"
        " than fighting whatever you were already doing."
    ),
)

fast_fall_window = SpinnerOption(
    "Double-tap window",
    "0.30",
    steps(0.10, 0.60, 0.05),
    description="How close together the two crouch presses must be to count as a double-tap.",
)

air_dash_flatten = BoolOption(
    "Air dash flattens fall",
    True,
    "On",
    "Off",
    description=(
        "Zero your vertical speed when you air dash, so it reads as a clean"
        " sideways jolt instead of a dive. Off keeps the fall you were in."
    ),
)

max_air_dashes = SpinnerOption(
    "Air dashes per jump",
    "1",
    steps(0, 3, 1),
    description=(
        "How many dashes you get between leaving the ground and landing again."
        " This is a HARD cap, checked before stamina - the reference game allows exactly"
        " one no matter how many charges you are holding, which is what stops an"
        " air chain running as long as your pool does."
    ),
)

max_air_jumps = SpinnerOption(
    "Air jumps",
    "1",
    steps(0, 3, 1),
    description=(
        "How many jumps you get after leaving the ground, per airborne period."
        " 1 is a double jump. 0 turns air jumping off and leaves the dashes."
    ),
)

air_jump_height = SpinnerOption(
    "Air jump height",
    "1.80",
    steps(0.50, 2.50, 0.05),
    description=(
        "Air jump strength as a multiple of your normal jump. It sets vertical"
        " speed outright, so it cancels a fall however far into one you are."
        " the reference game's second jump is markedly STRONGER than its first - 511 of"
        " vertical against 284 - which is why the double jump reads as a real"
        " recovery rather than a small hop."
    ),
)

show_bar = BoolOption(
    "Show stamina bar",
    True,
    "On",
    "Off",
    description="Draw the charge pips on screen. A charge system you can't see is a guess.",
)

bar_scale = SpinnerOption(
    "Bar size",
    "1.00",
    steps(0.50, 2.00, 0.25),
    description="Size of the stamina pips, as a multiple of the default.",
)

bar_height = SpinnerOption(
    "Bar height",
    "0.57",
    steps(0.30, 0.90, 0.01),
    description=(
        "Where the pips sit vertically, as a fraction of screen height. 0.50 is"
        " dead centre; the default sits just below the crosshair."
    ),
)

trace = BoolOption(
    "Diagnostics",
    False,
    "On",
    "Off",
    description="Log charges, dashes and air jumps to unrealsdk.log. Off unless debugging.",
)


def amount(option: SpinnerOption, fallback: float = 0.0) -> float:
    """The number behind one of the spinners above."""
    try:
        return float(option.value)
    except (TypeError, ValueError):
        return fallback


class State:
    """The pool, the dash in flight, and what we borrowed from the pawn."""

    pawn: ClassVar[Any] = None
    stock_ground_speed: ClassVar[float] = GROUND_SPEED_FALLBACK
    warned: ClassVar[bool] = False
    frames: ClassVar[int] = 0

    charges: ClassVar[float] = 0.0
    block: ClassVar[float] = 0.0

    pending_left: ClassVar[float] = 0.0
    dashing: ClassVar[bool] = False
    dash_left: ClassVar[float] = 0.0
    dash_span: ClassVar[float] = 0.0
    dash_dir: ClassVar[tuple[float, float]] = (1.0, 0.0)
    dash_peak: ClassVar[float] = 0.0
    dash_release: ClassVar[float] = 0.0

    air_jumps_used: ClassVar[int] = 0
    air_dashes_used: ClassVar[int] = 0
    dash_float: ClassVar[bool] = False
    # Seconds left in which a second crouch press counts as a double-tap.
    duck_window: ClassVar[float] = 0.0
    # Buffered presses retry every frame; this keeps the log to one line.
    denial_logged: ClassVar[bool] = False
    # Complain once if Canvas drawing fails, not sixty times a second.
    draw_warned: ClassVar[bool] = False
    render_logged: ClassVar[bool] = False
    sound_warned: ClassVar[bool] = False
    # Resolved once, on the first frame we actually draw.
    white: ClassVar[Any] = None
    white_ul: ClassVar[float] = 1.0
    white_vl: ClassVar[float] = 1.0
    blend: ClassVar[Any] = None
    # Last gate verdict, so the log records transitions not every frame.
    last_gate: ClassVar[str] = ""
    was_airborne: ClassVar[bool] = False
    # The crouch KEY, tracked separately from the pawn's crouch state: a
    # slide jump forces the pawn to stand, so `pc.bDuck` goes false while
    # the key is still physically held.
    duck_held: ClassVar[bool] = False
    # Seconds during which a DuckReleased is treated as the stand-up, not
    # the player.
    jump_grace: ClassVar[float] = 0.0
    # True while WE hold the pawn crouched, rather than the player. Must
    # always be undone, or they stand stuck in a crouch they never asked
    # for and cannot clear.
    forced_duck: ClassVar[bool] = False
    crouch_vk: ClassVar[int] = 0xA2
    # Whether the last movement frame was in a slide - to spot a slide starting.
    in_slide: ClassVar[bool] = False

    # Travel direction of the slide in progress, held so the slide keeps
    # going that way when the player stops steering.
    slide_dir: ClassVar[tuple[float, float] | None] = None
    momentum_frames: ClassVar[int] = 0
    slide_seen: ClassVar[bool] = False

    # Speed ceiling left over after a dash, bleeding back down to running pace.
    # Zero when there's nothing to carry.
    carry: ClassVar[float] = 0.0

    # The GroundSpeed the GAME wants, sampled when a move begins. BL2's
    # sprint raises GroundSpeed itself - 440 walking, 594 sprinting - so a
    # value captured once at spawn is the walking one and restoring it mid
    # sprint would write the player down to a walk.
    base_speed: ClassVar[float] = GROUND_SPEED_FALLBACK

    @classmethod
    def adopt(cls, pawn: unreal.UObject) -> None:
        """Note a new pawn and remember the ground speed it came with.

        We only ever write `GroundSpeed` during a dash, and a brand new pawn
        hasn't been written to yet, so whatever it holds right now is stock.
        """
        if cls.pawn is not None and cls.pawn == pawn:
            return
        cls.pawn = pawn
        cls.stock_ground_speed = getattr(pawn, "GroundSpeed", 0.0) or GROUND_SPEED_FALLBACK
        cls.dashing = False
        cls.dash_left = 0.0
        cls.pending_left = 0.0
        cls.air_jumps_used = 0
        cls.air_dashes_used = 0
        cls.carry = 0.0
        cls.dash_float = False
        cls.charges = amount(max_charges, 3.0)
        cls.block = 0.0
        logging.info(
            f"[stamina] pawn adopted: GroundSpeed={cls.stock_ground_speed:.1f} "
            f"JumpZ={getattr(pawn, 'JumpZ', 0.0):.1f} charges={cls.charges:.1f}",
        )


def charges() -> float:
    """How much stamina is in the pool right now, in charges."""
    return State.charges


def spend(cost: float = 1.0) -> bool:
    """Take `cost` charges if they're there. False, and nothing taken, if not."""
    if State.charges < cost:
        return False
    State.charges -= cost
    State.block = amount(regen_delay, 0.5)
    return True


def regenerate(delta: float, grounded: bool) -> None:
    """Refill the pool for one frame."""
    if State.block > 0.0:
        State.block -= delta
        return
    if not grounded and not regen_airborne.value:
        return
    ceiling = amount(max_charges, 3.0)
    if State.charges >= ceiling:
        return
    per_second = 1.0 / max(amount(regen_time, 4.0), 0.01)
    State.charges = min(ceiling, State.charges + delta * per_second)


def set_ground_speed(pawn: unreal.UObject, value: float) -> None:
    """Write `Pawn.GroundSpeed`, complaining at most once if the property is gone."""
    try:
        pawn.GroundSpeed = value
    except Exception as ex:  # noqa: BLE001 - never break the movement tick
        if not State.warned:
            State.warned = True
            logging.error(f"[stamina] cannot set Pawn.GroundSpeed: {ex!r}")


def wish_direction(pawn: unreal.UObject) -> tuple[float, float] | None:
    """The horizontal unit vector the player is asking to move along.

    Read off `Pawn.Acceleration` in a POST hook, after the walking state has
    already built it from input and rotated it by the camera's yaw - so sticks
    and any control remapping come along for free. Same as air_control does it;
    eight lines is cheaper than a dependency between two movement mods.
    """
    accel = pawn.Acceleration
    x, y = accel.X, accel.Y
    length = math.hypot(x, y)
    if length < 1e-4:
        return None
    return x / length, y / length


def is_sliding() -> bool:
    """True while the Sliding mod has us in a slide. False if it isn't loaded."""
    sliding = peer("sliding")
    if sliding is None:
        return False
    try:
        return bool(sliding.OWN_SLIDE_STATE.is_sliding)
    except AttributeError:
        return False


def is_client() -> bool:
    """True when we're a guest in someone else's game."""
    try:
        e_net_mode = find_enum("ENetMode")
        return ENGINE.GetCurrentWorldInfo().NetMode == e_net_mode.NM_Client
    except Exception:  # noqa: BLE001
        return False


@host.json_message
def server_set_velocity(vel_x: float, vel_y: float, vel_z: float) -> None:
    """Put our new velocity on the host's copy of this pawn.

    Sliding needs this because the host runs the slide; we need it only so the
    host's simulation doesn't decide we've moved further than we should have and
    correct us back. Sent once when a move starts, never per frame.
    """
    pc = server_set_velocity.sender.Owner
    if pc is None or pc.Pawn is None:
        return
    velocity = pc.Pawn.Velocity
    velocity.X = vel_x
    velocity.Y = vel_y
    velocity.Z = vel_z


def tell_host(pawn: unreal.UObject) -> None:
    """Forward this pawn's velocity to the host, if we aren't it."""
    if not is_client():
        return
    try:
        velocity = pawn.Velocity
        server_set_velocity(velocity.X, velocity.Y, velocity.Z)
    except Exception as ex:  # noqa: BLE001 - a network hiccup must not eat the dash
        logging.error(f"[stamina] could not reach host: {ex!r}")


def start_dash(pc: unreal.UObject, pawn: unreal.UObject, grounded: bool) -> bool:
    """Begin a dash, if there's stamina for one. True if one actually started.

    Pressing dash during a dash restarts the window rather than being ignored.
    `entry` is read live, so a re-dash inherits the speed of the one it cancels
    and swings it onto the new direction.
    """
    # the reference game refuses a dash with nothing held, and measurement confirms it:
    # the key from a standstill costs no charge and moves you nowhere. There is
    # no dash-where-you-look fallback.
    direction = wish_direction(pawn)
    if direction is None:
        return False

    # The hard cap, checked BEFORE stamina, because that is the order the reference game
    # enforces it in: four consecutive airborne attempts holding three charges
    # spent exactly one. Without this the pool alone bounds an air chain, which
    # is what made ours float.
    if not grounded and State.air_dashes_used >= int(amount(max_air_dashes, 1.0)):
        if trace.value and not State.denial_logged:
            State.denial_logged = True
            logging.info(
                f"[stamina] air dash refused: {State.air_dashes_used} already used"
                f" this jump, charges={State.charges:.2f}",
            )
        return False

    velocity = pawn.Velocity
    entry = math.hypot(velocity.X, velocity.Y)

    ground = State.base_speed

    if not spend():
        if trace.value and not State.denial_logged:
            State.denial_logged = True
            logging.info(f"[stamina] dash denied, charges={State.charges:.2f}")
        return False

    if not grounded:
        State.air_dashes_used += 1

    # Peak never slows you down: dashing out of a slide redirects what you have.
    # Release is what air control inherits when the window closes, so it has to
    # be an ordinary speed - see the note at the top about Drift's floor.
    State.dash_peak = max(ground * amount(dash_speed, 1.25), entry)
    State.dash_release = max(entry, ground)
    State.dash_dir = direction
    State.dash_span = max(amount(dash_duration, 0.2), 0.01)
    State.dash_left = State.dash_span
    State.dashing = True

    # Not a one-off zeroing: the reference game suspends gravity for the whole air dash.
    # Measured Z over 493ms of one: -517.47, -516.28, -515.88, -515.09. Two and
    # a half units. Zeroing once and letting go would drop us ~125 units over
    # the same window, which is a dive, not a dash.
    State.dash_float = bool(not grounded and air_dash_flatten.value)
    if State.dash_float:
        velocity.Z = 0.0

    apply_dash(pawn, 0.0)
    tell_host(pawn)
    play("dash")

    if trace.value:
        logging.info(
            f"[stamina] dash {'ground' if grounded else 'air'}: "
            f"entry={entry:.1f} peak={State.dash_peak:.1f} "
            f"release={State.dash_release:.1f} charges={State.charges:.2f}",
        )

    return True


def apply_dash(pawn: unreal.UObject, delta: float) -> None:
    """Hold the dash speed FLAT, and raise the clamp far enough to allow it.

    the reference game's dash does not ramp. Sampled at 188, 312, 427, 538 and 683ms it
    read exactly 546.80 every time, then dropped to ~434 by 824ms. So it is a
    constant held for the window and a step down at the end, not a decay - which
    is what the earlier linear version got wrong, and why ours felt like a short
    shove instead of a committed move.

    `GroundSpeed` is raised to match so `CalcVelocity` stops trimming us.
    """
    State.dash_left -= delta
    if State.dash_left <= 0.0:
        end_dash(pawn)
        return

    speed = State.dash_peak
    set_ground_speed(pawn, max(State.base_speed, speed))

    velocity = pawn.Velocity
    velocity.X = State.dash_dir[0] * speed
    velocity.Y = State.dash_dir[1] * speed

    # Hold the float. Landing ends it - otherwise we'd pin the pawn to whatever
    # height it touched down at for the rest of the window.
    if State.dash_float:
        if pawn.IsOnGroundOrShortFall():
            State.dash_float = False
        else:
            velocity.Z = 0.0


def end_dash(pawn: unreal.UObject | None, *, keep_momentum: bool = True) -> None:
    """Close the window and hand whatever speed is left to the bleed.

    An earlier version stepped hard down to running pace here, on the strength of
    a reading that turned out to be a landed frame rather than an airborne one.
    The real profile: a dash jump leaves the window at 546.80, reaches 625 at the
    apex, lands at 567 and only then bleeds down over more than a second - always
    above both the dash clamp and running pace. Cutting it off at the window is
    what would kill the tech, because escaping that clamp is the entire point of
    jumping out of a dash.

    So the window's end just converts the clamp into a ceiling that decays. The
    ceiling still has to exist: air_control's Drift treats frame-start speed as a
    floor it may never scrub, so with nothing bleeding it, an air dash would
    glide at dash speed forever.
    """
    State.dashing = False
    State.dash_left = 0.0
    State.dash_float = False
    if pawn is None:
        return

    if not keep_momentum:
        State.carry = 0.0
        set_ground_speed(pawn, State.base_speed)
        return

    velocity = pawn.Velocity
    speed = math.hypot(velocity.X, velocity.Y)
    State.carry = max(speed, State.dash_release)


def apply_momentum(pawn: unreal.UObject, delta: float, grounded: bool) -> None:
    """Carry the leftover speed ceiling, and bleed it off only once we land.

    The bleed is a LANDING cost, not a timer. A clean single dash jump lands at
    480 and is pinned at running pace 140ms later, while the airborne stretch
    holds ~625 the whole way from apex to touchdown. So the ceiling holds while
    falling and only decays with feet on the ground.

    A first pass read this as a slow decay everywhere, off a human capture that
    turned out to be a chain of hops - the apparent gradual bleed was airborne
    segments feeding vertical speed back into a 3D reading.

    Held as a ceiling rather than an assignment, so slowing down, turning or
    stopping stay free; this only ever removes speed no longer earnt.
    """
    stock = State.base_speed
    if State.carry <= stock:
        if State.carry:
            State.carry = 0.0
            set_ground_speed(pawn, stock)
        return

    if grounded:
        State.carry = max(stock, State.carry - stock * amount(momentum_bleed, 0.65) * delta)

    # Keep the engine's own clamp out of the way while we run our own.
    set_ground_speed(pawn, State.carry)

    velocity = pawn.Velocity
    speed = math.hypot(velocity.X, velocity.Y)
    if speed > State.carry and speed > 0.0:
        scale = State.carry / speed
        velocity.X *= scale
        velocity.Y *= scale


# --- sound -----------------------------------------------------------------
#
# All of it now lives in the `movement_audio` mod, which owns the clips, the
# volumes and the one `winsound` voice they all have to share. This module used
# to carry its own copy, and so did `slide_sound` - which was the bug: winsound
# plays one sound per process, so a dash landing during a slide did not layer
# over it, it cut it off. See that module's docstring for the arbitration.
#
# Imported softly. A missing audio mod must cost you the noise and nothing else.

from .. import audio as movement_audio

def play(event: str) -> None:
    """Fire a movement cue, if there is anything around to play it."""
    if movement_audio is None:
        return
    try:
        movement_audio.play(event)
    except Exception as ex:  # noqa: BLE001 - audio must never break a dash
        if not State.sound_warned:
            State.sound_warned = True
            logging.error(f"[stamina] sound failed: {ex!r}")


def pc_of(pawn: unreal.UObject) -> Any:
    """The controller driving a pawn, for read-only diagnostics."""
    try:
        return pawn.Controller
    except Exception:  # noqa: BLE001
        return None


def snapshot(pawn: unreal.UObject, grounded: bool) -> None:
    """One line of state, for tuning by log.

    Every frame while a dash or its leftover momentum is live, one in fifteen
    otherwise. At 60fps the sparse rate is a sample every 250ms, which would
    describe a 0.75s dash in three points - useless for reading the shape of it.
    Idling at that rate keeps the log readable between runs.
    """
    State.frames += 1
    busy = State.dashing or State.carry > 0.0
    if not busy and State.frames % TRACE_INTERVAL:
        return
    try:
        velocity = pawn.Velocity
        logging.info(
            f"[stamina] charges={State.charges:5.2f} "
            f"hspeed={math.hypot(velocity.X, velocity.Y):7.1f} vz={velocity.Z:+8.1f} "
            f"ground={pawn.GroundSpeed:6.1f} base={State.base_speed:6.1f} "
            f"carry={State.carry:6.1f} "
            f"grounded={int(grounded)} dash={State.dash_left:.2f} "
            f"float={int(State.dash_float)} airJumps={State.air_jumps_used} "
            f"airDashes={State.air_dashes_used} "
            f"sprint={int(bool(getattr(pc_of(pawn), 'bInSprintState', False)))} "
            f"duck={int(bool(getattr(pc_of(pawn), 'bDuck', False)))}",
        )
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[stamina] snapshot failed: {ex!r}")


# One segment per charge, on a shallow arc under the crosshair - the same
# reading the reference game puts there.
#
# UE3's Canvas has no rotated primitive; DrawRect is axis-aligned and that is
# all we get. So each segment is drawn as a run of thin vertical slices whose
# tops follow a parabola. At this size a parabola and a circular arc are
# indistinguishable, and the slices read as one smooth curved bar.
PIP_W: float = 30.0
PIP_H: float = 9.0
PIP_GAP: float = 7.0
ARC_SLICES: int = 10
ARC_DEPTH: float = 15.0

# Styled to sit with BL2's own reticle: a light face with a darker grey edge.
# The outline is drawn as a slightly larger tile behind each slice rather than
# as a stroke, since Canvas has no stroke - inflate, then draw the face inside.
OUTLINE: float = 2.0

# Opaque on purpose. The slices overlap by a pixel to hide seams, and with any
# translucency that overlap double-blends into a visible stripe down every
# slice boundary - the fill has to be solid for the overlap trick to work.
COLOUR_OUTLINE: tuple[int, int, int, int] = (44, 48, 54, 255)
COLOUR_FULL: tuple[int, int, int, int] = (246, 249, 252, 255)
COLOUR_REGEN: tuple[int, int, int, int] = (236, 196, 44, 255)
COLOUR_EMPTY: tuple[int, int, int, int] = (26, 29, 34, 205)


def blend_translucent() -> int:
    """`EBlendMode.BLEND_Translucent`, so the alpha in our colours means something."""
    if State.blend is None:
        try:
            State.blend = int(find_enum("EBlendMode").BLEND_Translucent)
        except Exception:  # noqa: BLE001 - UE3 has had this at 2 forever
            State.blend = 2
    return State.blend


def white_texture() -> Any:
    """A 1x1 white texture to tint, resolved once.

    `Canvas.DrawRect` renders a textured tile, so with nothing bound it draws
    zero pixels and reports no error - which is why the first version of this
    hooked correctly, ran cleanly, and showed nothing at all.
    """
    if State.white is None:
        try:
            texture = find_object("Texture2D", "EngineResources.WhiteSquareTexture")
        except Exception as ex:  # noqa: BLE001
            if not State.draw_warned:
                State.draw_warned = True
                logging.error(f"[stamina] no white texture: {ex!r}")
            return None
        State.white = texture
        # Sample the WHOLE texture, not one texel. DrawTile's UL/VL are in
        # texture pixels, so passing 1,1 reads the single texel at the origin -
        # and whatever colour that happens to be multiplies everything we draw,
        # which turned both the bed and the fill the same flat dark.
        State.white_ul = float(getattr(texture, "SizeX", 0) or 1)
        State.white_vl = float(getattr(texture, "SizeY", 0) or 1)
        logging.info(
            f"[stamina] white texture {texture._path_name()} "
            f"{State.white_ul:.0f}x{State.white_vl:.0f}",
        )
    return State.white


def tile(canvas: Any, x: float, y: float, w: float, h: float,
         colour: tuple[int, int, int, int]) -> None:
    """One solid rectangle, tinted by the draw colour."""
    texture = white_texture()
    if texture is None or w <= 0.0 or h <= 0.0:
        return

    # BL2's DrawTile takes the colour as an ARGUMENT - its signature is
    # (Tex, XL, YL, U, V, UL, VL, LColor, ClipTile, Blend) - and ignores
    # Canvas.DrawColor completely. Setting DrawColor read back as pure white
    # and still drew black, because the unfilled LColor struct is zeroed, and
    # a zeroed colour is black whatever texture you hand it.
    red, green, blue, alpha = colour
    lcolor = make_struct(
        "LinearColor",
        R=red / 255.0,
        G=green / 255.0,
        B=blue / 255.0,
        A=alpha / 255.0,
    )

    canvas.SetPos(x, y)
    canvas.DrawTile(
        texture,
        w,
        h,
        0.0,
        0.0,
        State.white_ul,
        State.white_vl,
        lcolor,
        False,
        blend_translucent(),
    )


def draw_bar(canvas: unreal.UObject) -> None:
    """Draw the charge arc for this frame."""
    total = int(amount(max_charges, 4.0))
    if total <= 0:
        return

    width = canvas.ClipX
    height = canvas.ClipY
    if not width or not height:
        return

    scale = amount(bar_scale, 1.0)
    pip_w, pip_h, gap = PIP_W * scale, PIP_H * scale, PIP_GAP * scale
    depth = ARC_DEPTH * scale

    span = total * pip_w + (total - 1) * gap
    x0 = (width - span) / 2.0
    base_y = height * amount(bar_height, 0.57)

    def arc_y(centre_x: float) -> float:
        """Height of the arc at a pixel, dipping lowest in the middle."""
        u = ((centre_x - x0) / span) * 2.0 - 1.0
        return base_y + depth * (1.0 - u * u)

    slice_w = pip_w / ARC_SLICES
    charges_now = State.charges

    # Three passes over the whole arc, not outline-bed-fill per slice. Drawing
    # them interleaved meant each slice's inflated outline painted over the
    # previous slice's finished fill, leaving a comb of vertical stripes.
    def slices() -> "list[tuple[float, float, int, float]]":
        out = []
        for index in range(total):
            left = x0 + index * (pip_w + gap)
            filled = max(0.0, min(1.0, charges_now - index))
            for step in range(ARC_SLICES):
                sx = left + step * slice_w
                out.append((sx, arc_y(sx + slice_w / 2.0), step, filled))
        return out

    cells = slices()

    for sx, sy, _step, _filled in cells:
        tile(
            canvas,
            sx - OUTLINE,
            sy - OUTLINE,
            slice_w + 1.0 + OUTLINE * 2.0,
            pip_h + OUTLINE * 2.0,
            COLOUR_OUTLINE,
        )

    for sx, sy, _step, _filled in cells:
        tile(canvas, sx, sy, slice_w + 1.0, pip_h, COLOUR_EMPTY)

    for sx, sy, step, filled in cells:
        if filled <= 0.0 or filled <= step / ARC_SLICES:
            continue
        covered = (step + 1) / ARC_SLICES
        colour = COLOUR_FULL if filled >= 1.0 else COLOUR_REGEN
        # The slice the fill ends inside is drawn short, so a regenerating
        # charge grows smoothly instead of ticking a slice at a time.
        part = slice_w if filled >= covered else slice_w * (
            (filled - step / ARC_SLICES) * ARC_SLICES
        )
        tile(canvas, sx, sy, part + (1.0 if filled >= covered else 0.0), pip_h, colour)




# --- is the crouch key actually down? --------------------------------------
#
# Everything above this was inferring key state from DuckPressed/DuckReleased
# events, and that inference kept going stale: a slide jump stands the pawn up
# and fires a release the player never made, so the flag had to be second
# guessed with grace windows and backstops. Each fix traded one failure for
# another - a stuck slide, or a chain that died on the third link.
#
# Windows will simply tell us. GetAsyncKeyState reports the live physical state
# of a key, independent of any event we may have missed or wrongly ignored, so
# the landing slide can be gated on fact instead of bookkeeping.

VK_LCONTROL: int = 0xA2

# Keys worth testing when learning which one crouch is bound to. Checked only
# on a genuine crouch press, when by definition the right key is down.
VK_CANDIDATES: tuple[int, ...] = (
    0xA2,  # left ctrl
    0xA3,  # right ctrl
    0x11,  # either ctrl
    0x43,  # C
    0xA0,  # left shift
    0x56,  # V
    0x58,  # X
    0x5A,  # Z
)

try:
    _user32: Any = ctypes.windll.user32
except Exception:  # noqa: BLE001 - pragma: no cover
    _user32 = None


def key_down(vk: int) -> bool:
    """Live physical state of one virtual key."""
    if _user32 is None:
        return False
    try:
        return bool(_user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:  # noqa: BLE001
        return False


def learn_crouch_key() -> None:
    """Called on a genuine crouch press, when the bound key must be down.

    Defaults to left ctrl and only goes looking if that isn't the one held, so
    a rebound crouch key is picked up without the player configuring anything.
    """
    if key_down(State.crouch_vk):
        return
    for vk in VK_CANDIDATES:
        if key_down(vk):
            State.crouch_vk = vk
            if trace.value:
                logging.info(f"[stamina] crouch key learned: vk=0x{vk:02X}")
            return


def crouch_held() -> bool:
    """Whether crouch is physically held right now. No inference involved."""
    return key_down(State.crouch_vk)



def input_direction(pc: unreal.UObject) -> tuple[float, float] | None:
    """World-space direction the player is asking for, from the raw axes.

    `PlayerInput.aForward` and `aStrafe` come straight from the device, before
    anything turns them into `Pawn.Acceleration`. That matters during a slide,
    where we drive Acceleration ourselves and so cannot read intent from it.
    """
    player_input = getattr(pc, "PlayerInput", None)
    if player_input is None:
        return None
    try:
        forward = float(player_input.aForward)
        strafe = float(player_input.aStrafe)
    except (AttributeError, TypeError, ValueError):
        return None
    if abs(forward) < 0.01 and abs(strafe) < 0.01:
        return None

    try:
        yaw = pc.Rotation.Yaw
    except Exception:  # noqa: BLE001
        return None
    # Unreal rotator units: 65536 to the full turn.
    radians = yaw * math.pi / 32768.0
    cos_y, sin_y = math.cos(radians), math.sin(radians)

    # Unreal's basis for a yaw of t: forward is (cos t, sin t) and right is
    # (-sin t, cos t). Using (sin t, -cos t) for right - its exact negative -
    # is what made strafe steer the wrong way.
    x = cos_y * forward - sin_y * strafe
    y = sin_y * forward + cos_y * strafe
    length = math.hypot(x, y)
    if length < 1e-4:
        return None
    return x / length, y / length


def rotate_towards(
    current: tuple[float, float],
    target: tuple[float, float],
    limit: float,
) -> tuple[float, float]:
    """Turn `current` towards `target` by at most `limit` radians."""
    difference = math.atan2(target[1], target[0]) - math.atan2(current[1], current[0])
    # Wrap into (-pi, pi] so we always turn the short way round.
    difference = (difference + math.pi) % (2.0 * math.pi) - math.pi
    turn = max(-limit, min(limit, difference))
    cos_t, sin_t = math.cos(turn), math.sin(turn)
    return (
        current[0] * cos_t - current[1] * sin_t,
        current[0] * sin_t + current[1] * cos_t,
    )


def maintain_slide_momentum(pc: unreal.UObject, pawn: unreal.UObject, delta: float) -> None:
    """Hold a slide on the heading it began with.

    The heading is captured once, on the frame the slide starts, and held for
    its whole length. An earlier version tried to detect whether the player was
    steering and only take over when they weren't - that never worked, for two
    reasons. `Pawn.Acceleration` is never actually zero during a slide, so the
    test read "steering" every frame; and it was self-defeating anyway, since
    writing Acceleration to keep the slide alive made our own write look like
    player input on the following frame.

    Holding the entry heading is also just what was asked for: the slide goes
    where you were going, so you can spin the camera mid-slide and keep
    travelling the same way. Speed is left alone entirely - only direction is
    pinned - so it decays exactly as a slide normally would.
    """
    velocity = pawn.Velocity
    speed = math.hypot(velocity.X, velocity.Y)

    if State.slide_dir is None:
        if speed < 1.0:
            return
        State.slide_dir = (velocity.X / speed, velocity.Y / speed)
        if trace.value:
            logging.info(
                f"[stamina] slide heading locked: ({State.slide_dir[0]:+.2f},"
                f"{State.slide_dir[1]:+.2f}) at {speed:.0f}",
            )

    # Steer, using the RAW INPUT AXES rather than Acceleration. Acceleration is
    # useless as an input signal here because we write it ourselves below, so it
    # always reads as though the player is steering - that mistake is what made
    # the first attempt do nothing at all.
    wish = input_direction(pc)
    if wish is not None:
        limit = math.radians(amount(slide_steer, 150.0)) * max(delta, 0.0)
        State.slide_dir = rotate_towards(State.slide_dir, wish, limit)

    dir_x, dir_y = State.slide_dir

    # Sliding's `can_slide()` exits the moment Acceleration reads zero, so this
    # both keeps the slide alive with no keys held and points it the right way.
    rate = getattr(pawn, "AccelRate", 0.0) or ACCEL_RATE_FALLBACK
    accel = pawn.Acceleration
    accel.X = dir_x * rate
    accel.Y = dir_y * rate

    # Re-point velocity without touching its magnitude, so the slide decays
    # naturally instead of being driven.
    if speed > 1.0:
        velocity.X = dir_x * speed
        velocity.Y = dir_y * speed

    if trace.value:
        State.momentum_frames += 1
        if State.momentum_frames % 15 == 1:
            after = pawn.Acceleration
            logging.info(
                f"[stamina] momentum: dir=({dir_x:+.2f},{dir_y:+.2f}) speed={speed:6.1f} "
                f"accel_readback=({after.X:+.0f},{after.Y:+.0f})",
            )



def release_forced_duck(pc: unreal.UObject) -> None:
    """Undo a crouch we forced, once the slide it was for has ended.

    A synthesised DuckPressed has no release coming - the player never touched
    the key - so without this they are left crouched permanently. If they are
    genuinely holding crouch they can simply press again; being wrongly stood
    up is recoverable, being wrongly stuck down is not.
    """
    State.forced_duck = False
    # Deliberately does NOT touch `duck_held`. That flag mirrors the player's
    # physical key, and releasing a crouch *we* forced says nothing about
    # whether their finger is still down - clearing it here broke the third
    # slide in a chain, because the next landing had nothing left to act on.
    # If it really is stale, the grounded backstop below clears it instead.

    player_input = getattr(pc, "PlayerInput", None)
    if player_input is None:
        return
    try:
        with prevent_hooking_direct_calls():
            player_input.DuckReleased()
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[stamina] DuckReleased failed: {ex!r}")

    if trace.value:
        logging.info("[stamina] forced crouch released")


def try_slide_on_landing(pc: unreal.UObject, pawn: unreal.UObject) -> None:
    """Drop straight into a slide if we land with crouch already held.

    Sliding starts a slide from `DuckPressed`, and holding the key through a
    jump produces no press on landing - so it just lands you crouched. This
    supplies the missing start.

    Opening speed comes from slide_tuning when it's loaded, so a slide entered
    this way matches one entered by pressing crouch instead of quietly using
    Sliding's stock value.
    """
    sliding = peer("sliding")
    if sliding is None:
        return

    velocity0 = pawn.Velocity
    speed0 = math.hypot(velocity0.X, velocity0.Y)
    if trace.value:
        logging.info(
            f"[stamina] LANDED: duck_held={State.duck_held} bDuck={bool(pc.bDuck)} "
            f"speed={speed0:.1f} need>{State.base_speed * 0.5:.1f} "
            f"sliding={getattr(sliding.OWN_SLIDE_STATE, 'is_sliding', '?')}",
        )

    try:
        if sliding.OWN_SLIDE_STATE.is_sliding:
            return
    except AttributeError:
        return

    # Ask Windows, do not infer. `bDuck` is false here because the slide jump
    # stood the pawn up, and the event-tracked flag has proven unreliable in
    # both directions. The physical key is the only thing that answers the
    # actual question: is the player asking to slide?
    #
    # `bDuck` stays in the test for toggle-crouch, where the key is genuinely
    # up but the player is genuinely crouched.
    if not (crouch_held() or pc.bDuck):
        return

    # A drop onto the spot shouldn't slide - you need to be actually going
    # somewhere, same as a slide off a sprint does.
    velocity = pawn.Velocity
    if math.hypot(velocity.X, velocity.Y) < State.base_speed * 0.5:
        return

    # Starting the slide isn't enough on its own. Sliding's `can_slide()` tests
    # `pc.bDuck` every tick and exits the moment it's false, and after the
    # stand-up the game's input state says the key is up - so the slide was
    # being cancelled on the frame after it started. Writing `bDuck` directly
    # doesn't hold either, since the game rewrites it from input.
    #
    # So make the pawn genuinely duck by invoking the input function, the same
    # way autorun re-fires SprintPressed to restart a sprint the game dropped.
    player_input = getattr(pc, "PlayerInput", None)
    if player_input is not None:
        try:
            # Hooks suppressed: without this our own duck_pressed handler fires
            # on our synthetic press and sets `duck_held`, which then re-arms
            # this whole path on the next landing - a crouch that feeds itself.
            # With it suppressed, every press our hooks see is a real one.
            with prevent_hooking_direct_calls():
                player_input.DuckPressed()
            State.forced_duck = True
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[stamina] DuckPressed on landing failed: {ex!r}")

    # Sliding hooks DuckPressed itself, so it may already have started one.
    try:
        if not sliding.OWN_SLIDE_STATE.is_sliding:
            sliding.enter_slide(pc)
    except Exception as ex:  # noqa: BLE001 - never break the movement tick
        logging.error(f"[stamina] slide on landing failed: {ex!r}")
        return

    tuning = peer("slide_tuning")
    if tuning is not None:
        try:
            pawn.CrouchedPct = tuning.start_speed()
        except Exception:  # noqa: BLE001 - stock opening speed is fine
            pass

    if trace.value:
        logging.info(
            f"[stamina] slide on landing: speed={math.hypot(velocity.X, velocity.Y):.1f} "
            f"crouchedPct={pawn.CrouchedPct:.2f} bDuck={bool(pc.bDuck)} "
            f"sliding={getattr(sliding.OWN_SLIDE_STATE, 'is_sliding', '?')}",
        )


def carry_momentum_into_slide(pawn: unreal.UObject) -> None:
    """Hand the dash's horizontal speed to slide_tuning as the slide's opening speed."""
    tuning = peer("slide_tuning")
    if tuning is None:
        return
    velocity = pawn.Velocity
    speed = math.hypot(velocity.X, velocity.Y)
    try:
        tuning.carry_in(pawn, speed)
    except Exception as ex:  # noqa: BLE001 - never break the movement tick
        logging.error(f"[stamina] carrying momentum into slide failed: {ex!r}")
        return
    if trace.value:
        logging.info(
            f"[stamina] slide took dash momentum: speed={speed:.1f} "
            f"ground={pawn.GroundSpeed:.1f} crouchedPct={pawn.CrouchedPct:.2f}",
        )


def in_gameplay() -> tuple[bool, str]:
    """Whether the player is actually playing, rather than sitting in a menu.

    `PostRender` keeps firing over the main menu, the pause screen and the
    inventory, so without this the bar is painted onto every screen in the game
    like a sticker on the monitor. The reticle vanishes in those states and the
    bar should go with it.

    Returns the reason alongside the verdict, so a state we guessed wrong about
    names itself in the log instead of needing another probe.
    """
    try:
        pc = get_pc()
    except Exception:  # noqa: BLE001 - no controller at all, e.g. main menu
        return False, "no player controller"

    if pc is None:
        return False, "no player controller"
    if pc.Pawn is None:
        return False, "no pawn"

    # The HUD movie is the closest thing to "is the reticle up" that we can ask
    # for; ui_utils treats a missing one as "not showing HUD" too.
    try:
        if pc.GetHUDMovie() is None:
            return False, "no hud movie"
    except Exception as ex:  # noqa: BLE001
        return False, f"hud movie check failed: {ex!r}"

    for flag in ("bStatusMenuOpen", "bInMenu", "bIsPaused"):
        value = getattr(pc, flag, None)
        if value:
            return False, flag

    return True, "playing"


@hook("WillowGame.WillowGameViewportClient:PostRender", Type.POST)
def post_render(
    _obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Draw the bar. Never let a rendering slip take the frame down with it."""
    canvas = args.Canvas
    if not State.render_logged:
        State.render_logged = True
        logging.info(
            f"[stamina] post_render fired: canvas={canvas is not None} "
            f"show_bar={show_bar.value} charges={State.charges:.2f}",
        )
    if not show_bar.value or canvas is None:
        return

    playing, reason = in_gameplay()
    if reason != State.last_gate:
        State.last_gate = reason
        if trace.value:
            logging.info(f"[stamina] bar gate: {'draw' if playing else 'hide'} ({reason})")
    if not playing:
        return
    try:
        draw_bar(canvas)
    except Exception as ex:  # noqa: BLE001
        if not State.draw_warned:
            State.draw_warned = True
            logging.error(f"[stamina] stamina bar draw failed: {ex!r}")


@keybind(
    "Dash",
    "LeftShift",
    description=(
        "Dash along the direction you're holding, or along your aim if you're"
        " holding nothing. Works on the ground and in the air. Costs a charge."
        " Shift is BL2's stock Sprint key, so rebind Sprint off it in the game's"
        " own controls or both will fire on the same press."
    ),
)
def dash_pressed() -> None:
    """Arm a dash for the next movement tick.

    Deferred rather than done here, for the same reason Sliding defers its slide
    jump: `Acceleration` is only trustworthy once the walking state has built it,
    and the movement tick is the one place we know the pawn is mid-simulation.
    """
    State.pending_left = amount(dash_buffer, 0.25)
    State.denial_logged = False


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove", Type.POST)
def player_move(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Run the pool, start a dash that's waiting, and carry one that's in flight."""
    pawn = obj.Pawn
    if pawn is None:
        return

    State.adopt(pawn)

    delta = args.DeltaTime
    if delta <= 0.0:
        return

    # Airborne is decided by physics, NOT by `IsOnGroundOrShortFall()`. That
    # helper stays true through a "short fall", so for the first stretch after
    # a jump it reports you grounded while you are visibly in the air. Using it
    # here classified an early air dash as a GROUND dash - no float, no air-dash
    # allowance spent - so the move only behaved right if you pressed it late
    # enough. That is the "only works at a specific time" bug.
    try:
        airborne = int(pawn.Physics) == PHYS_FALLING
    except (TypeError, ValueError):  # an enum wrapper that won't coerce
        airborne = not pawn.IsOnGroundOrShortFall()
    grounded = not airborne

    # Landing this frame? Catch the transition before the counters reset.
    landed = grounded and State.was_airborne
    State.was_airborne = airborne

    if landed and slide_on_landing.value:
        try_slide_on_landing(obj, pawn)

    if grounded:
        State.air_jumps_used = 0
        State.air_dashes_used = 0

        # Backstop against a stuck `duck_held`. If we're on the ground, not
        # sliding, and the pawn is demonstrably not crouched, then the key is
        # not down whatever we think - so stop believing otherwise. Without
        # this, one swallowed release slides the player on every landing from
        # then on.
        # A crouch we forced only lasts as long as the slide that needed it.
        if State.slide_dir is not None and not is_sliding():
            State.slide_dir = None
            State.momentum_frames = 0
            State.slide_seen = False

        if State.forced_duck and not is_sliding():
            release_forced_duck(obj)

        if State.duck_held and not is_sliding() and not obj.bDuck and State.jump_grace <= 0.0:
            State.duck_held = False
            if trace.value:
                logging.info("[stamina] duck_held cleared: grounded, not crouched")

        # Sample the locomotion baseline only with feet down, and only when we
        # aren't driving GroundSpeed ourselves. BL2 drops GroundSpeed back to
        # the walk value at some point in a flight, so sampling it at the moment
        # of an AIR dash gave 440 or 594 depending on timing - the same input
        # producing 550 or 742 at random. Holding the grounded value means an
        # air dash is always measured against the speed you took off at.
        if not State.dashing and State.carry <= 0.0:
            State.base_speed = getattr(pawn, "GroundSpeed", 0.0) or State.stock_ground_speed

    if State.duck_window > 0.0:
        State.duck_window -= delta
    if State.jump_grace > 0.0:
        State.jump_grace -= delta

    regenerate(delta, grounded)

    # A dash that starts this frame has already had its opening frame applied, so
    # don't age it twice. A press we couldn't afford falls through to carrying
    # whatever dash was already running, rather than dropping a frame of it.
    # A slide cancels a dash. Measured: crouching mid-dash dropped the reference game from
    # 546.80 to 407 with ~300ms still on the dash timer. It also settles who owns
    # velocity - Sliding drives speed through CrouchedPct while this drives it
    # through Velocity, and without a rule the two overwrite each other every
    # frame for as long as the window lasts.
    # A slide cancels a dash outright, momentum and all - measured at 546.80
    # dropping to 407 the moment crouch went down, with ~300ms still on the
    # timer. It also settles who owns velocity: Sliding drives speed through
    # CrouchedPct while this drives it through Velocity, and with no rule the
    # two overwrite each other every frame for as long as the window lasts.
    if is_sliding():
        # A slide starting with dash momentum still live opens at that speed
        # instead of dropping to the slide's usual opening speed.
        took_momentum = False
        if not State.in_slide:
            State.in_slide = True
            took_momentum = State.dashing or State.carry > 0.0
        if trace.value and not State.slide_seen:
            State.slide_seen = True
            logging.info(
                f"[stamina] SLIDE active: momentum_opt={slide_momentum.value} "
                f"dashing={State.dashing}",
            )
        if slide_momentum.value and not State.dashing:
            maintain_slide_momentum(obj, pawn, delta)
        if State.dashing:
            end_dash(pawn, keep_momentum=False)
        elif State.carry:
            State.carry = 0.0
            set_ground_speed(pawn, State.base_speed)
        # After the dash has handed GroundSpeed back - which leaves velocity
        # alone - so the speed converts against the ground speed the slide runs at.
        if took_momentum:
            carry_momentum_into_slide(pawn)
    else:
        State.in_slide = False

    # A queued dash survives for `dash_buffer` seconds rather than being spent
    # on the single frame it was pressed. A press a fraction before landing,
    # or during any frame the move is momentarily illegal, then still lands
    # instead of silently vanishing.
    started = False
    if State.pending_left > 0.0:
        State.pending_left -= delta
        started = start_dash(obj, pawn, grounded)
        if started:
            State.pending_left = 0.0

    if State.dashing and not started:
        apply_dash(pawn, delta)
    elif not State.dashing:
        apply_momentum(pawn, delta, grounded)

    if trace.value:
        snapshot(pawn, grounded)


@hook("WillowGame.WillowPlayerInput:Jump", Type.POST)
def jump(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Air jump, unless the ground or a slide jump has already claimed this press."""
    # Armed first, before any early return: this matters for the GROUNDED slide
    # jump, which never reaches the air-jump logic below but is precisely the
    # case that generates a spurious DuckReleased.
    #
    # Only a jump out of a SLIDE stands the pawn up, so only that jump can
    # produce a synthetic release. Arming it on every jump meant an ordinary
    # jump could swallow a real release too.
    if is_sliding():
        State.jump_grace = DUCK_RELEASE_GRACE

    if amount(max_air_jumps, 1.0) <= 0.0:
        return

    # Sliding flags a slide jump from a PRE hook on this same function, so it's
    # already set by the time we get here. That press belongs to the slide.
    sliding = peer("sliding")
    if sliding is not None:
        try:
            if sliding.State.do_slide_jump:
                return
        except AttributeError:
            pass

    pc = obj.Outer
    if pc is None:
        return
    pawn = pc.Pawn
    if pawn is None:
        return

    # Physics alone decides this. An earlier version also bailed on
    # `IsOnGroundOrShortFall()`, reasoning that refusing a charge was the
    # harmless direction - it is not. That helper stays true through a short
    # fall, so it refused every air jump taken promptly after leaving the
    # ground, which is exactly when you want one.
    #
    # A press while genuinely grounded is the normal jump: the input fires
    # before the pawn leaves the floor, so Physics is still PHYS_Walking here
    # and we correctly decline to spend anything.
    try:
        if int(pawn.Physics) != PHYS_FALLING:
            return
    except (TypeError, ValueError):  # an enum wrapper that won't coerce
        if pawn.IsOnGroundOrShortFall():
            return

    if State.air_jumps_used >= int(amount(max_air_jumps, 1.0)):
        return

    if not spend():
        if trace.value:
            logging.info(f"[stamina] air jump denied, charges={State.charges:.2f}")
        return

    State.air_jumps_used += 1

    # An air dash holds Z at zero every frame for its whole window. Without
    # releasing that here, a double jump taken during a dash would be zeroed on
    # the same frame it was applied - the jump simply vanished, which is why
    # dash-then-double-jump did not chain.
    State.dash_float = False

    # Set outright rather than add, so it cancels a fall however deep into one
    # we are - that is the whole point of the move.
    velocity = pawn.Velocity
    velocity.Z = (getattr(pawn, "JumpZ", 0.0) or JUMP_Z_FALLBACK) * amount(air_jump_height, 1.0)

    tell_host(pawn)
    play("air_jump")

    if trace.value:
        logging.info(
            f"[stamina] air jump {State.air_jumps_used}: "
            f"vz={velocity.Z:.1f} charges={State.charges:.2f}",
        )


def sprinting_any_direction(pc: unreal.UObject) -> bool:
    """Whether we're at sprint pace, including directions BL2 won't sprint in.

    `pc.bInSprintState` only covers forwards. autorun's `forcing` flag is true
    exactly when it is holding GroundSpeed at the sprint value for a direction
    the game refuses, which is the missing half of the same question.
    """
    if pc.bInSprintState:
        return True
    autorun = peer("autorun")
    if autorun is None:
        return False
    try:
        return bool(autorun.State.forcing)
    except AttributeError:
        return False


def try_slide_any_direction(pc: unreal.UObject, pawn: unreal.UObject) -> None:
    """Start a slide from a crouch press when Sliding's own gate won't.

    Sliding hooks this same input and starts a slide if `bInSprintState`, so
    forwards is already handled and this only ever picks up what that misses.
    `enter_slide` is idempotent, and Sliding's PRE hook runs before this POST
    one, so there's no risk of starting two.
    """
    sliding = peer("sliding")
    if sliding is None:
        return

    try:
        if sliding.OWN_SLIDE_STATE.is_sliding:
            return
    except AttributeError:
        return

    if not sprinting_any_direction(pc):
        return

    velocity = pawn.Velocity
    speed = math.hypot(velocity.X, velocity.Y)
    if speed < State.base_speed * 0.5:
        return

    try:
        sliding.enter_slide(pc)
    except Exception as ex:  # noqa: BLE001 - never break the input path
        logging.error(f"[stamina] backwards slide failed: {ex!r}")
        return

    tuning = peer("slide_tuning")
    if tuning is not None:
        try:
            pawn.CrouchedPct = tuning.start_speed()
        except Exception:  # noqa: BLE001 - stock opening speed is fine
            pass

    if trace.value:
        logging.info(f"[stamina] slide (any direction): speed={speed:.1f}")


@hook("WillowGame.WillowPlayerInput:DuckPressed", Type.POST)
def duck_pressed(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Double-tap crouch in the air to slam down.

    POST, and deliberately passive: Sliding and autorun both hook this same
    input, Sliding to start a slide and autorun to stand off the sprint resume.
    Neither cares about an airborne press - a slide needs the ground - so taking
    the second tap here steps on nothing.
    """
    # Only genuine presses reach here now - ours are sent with hooks suppressed.
    State.duck_held = True
    State.forced_duck = False
    learn_crouch_key()
    if trace.value:
        logging.info("[stamina] >>> DuckPressed")

    pc = obj.Outer
    if pc is None:
        return
    pawn = pc.Pawn
    if pawn is None:
        return

    try:
        airborne = int(pawn.Physics) == PHYS_FALLING
    except (TypeError, ValueError):
        airborne = not pawn.IsOnGroundOrShortFall()

    if not airborne:
        # On the ground this is an ordinary crouch. Arm the window anyway, so a
        # tap taken just before leaving a ledge can still pair with one after.
        State.duck_window = amount(fast_fall_window, 0.3)
        if slide_any_direction.value:
            try_slide_any_direction(pc, pawn)
        return

    # Everything below is the fast fall, which is the only airborne use.
    if not fast_fall.value:
        return

    if State.duck_window <= 0.0:
        State.duck_window = amount(fast_fall_window, 0.3)
        return

    # Second tap inside the window, and we're airborne: slam.
    State.duck_window = 0.0

    # An air dash suspends gravity; a fast fall has to win that argument or the
    # float would hold us up against the dive we just asked for.
    State.dash_float = False

    jump_z = getattr(pawn, "JumpZ", 0.0) or JUMP_Z_FALLBACK
    velocity = pawn.Velocity
    velocity.Z = -(jump_z * amount(fast_fall_speed, 2.5))

    tell_host(pawn)

    if trace.value:
        logging.info(f"[stamina] fast fall: vz={velocity.Z:.1f}")


@hook("WillowGame.WillowPlayerInput:DuckReleased", Type.POST)
def duck_released(
    _obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Let go of crouch. Only bookkeeping - nothing else keys off the release."""
    if State.jump_grace > 0.0:
        if trace.value:
            logging.info("[stamina] <<< DuckReleased ignored (stand-up from jump)")
        return

    State.duck_held = False
    if trace.value:
        logging.info("[stamina] <<< DuckReleased")


def on_disable() -> None:
    """Give the pawn its ground speed back, even if we're switched off mid-dash."""
    end_dash(State.pawn, keep_momentum=False)
    State.pending_left = 0.0


# (registered by Movement Overhaul)

# (network functions registered by Movement Overhaul)



# --- options: read from Movement Overhaul's menu --------------------------
from .. import options  # noqa: E402
from .bridge import Mapped  # noqa: E402

max_charges = Mapped(lambda: str(int(options.num(options.charges))))
regen_time = Mapped(lambda: f"{options.num(options.recharge):.2f}")
regen_delay = Mapped(lambda: "0.00")
regen_airborne = Mapped(lambda: True)
dash_speed = Mapped(lambda: f"{options.pct(options.dash_speed):.2f}")
dash_duration = Mapped(lambda: f"{options.num(options.dash_duration) / 1000.0:.2f}")
momentum_bleed = Mapped(lambda: "0.65")
dash_buffer = Mapped(lambda: "0.25")
slide_momentum = Mapped(lambda: options.slide_lock.value)
slide_steer = Mapped(lambda: str(int(options.num(options.slide_steering))))
slide_any_direction = Mapped(lambda: options.slide_any_direction.value)
slide_on_landing = Mapped(lambda: options.slide_on_landing.value)
fast_fall = Mapped(lambda: options.fast_fall.value)
fast_fall_speed = Mapped(lambda: "3.00")
fast_fall_window = Mapped(lambda: "0.30")
air_dash_flatten = Mapped(lambda: True)
max_air_dashes = Mapped(lambda: str(int(options.num(options.air_dashes))))
max_air_jumps = Mapped(lambda: str(int(options.num(options.air_jumps))))
air_jump_height = Mapped(lambda: f"{options.pct(options.air_jump_height):.2f}")
show_bar = Mapped(lambda: False)  # the bar is drawn by Movement Overhaul's hud
trace = Mapped(lambda: options.diagnostics.value)
