"""Every option the mod shows, in one place.

Numbers are integer sliders on purpose. BL2's mod menu hands slider changes
back as integers, so a fractional slider silently snaps to whole numbers and
writes the snapped value into your settings. Percentages and milliseconds keep
every value exact and still give you a real slider.

A lot of knobs the individual mods used to expose are now fixed at the values
that played best (see the constants in each feature module). Anything that
stayed here changes how the movement *feels*.
"""

from __future__ import annotations

from mods_base import BoolOption, DropdownOption, NestedOption, SliderOption

from . import sounds


def pct(option: SliderOption) -> float:
    """A percentage slider as a multiplier: 125 -> 1.25."""
    try:
        return float(option.value) / 100.0
    except (TypeError, ValueError):
        return float(option.default_value) / 100.0


def num(option: SliderOption) -> float:
    try:
        return float(option.value)
    except (TypeError, ValueError):
        return float(option.default_value)


# --- Sprint -------------------------------------------------------------------

toggle_sprint = BoolOption(
    "Toggle sprint",
    True,
    "On",
    "Off",
    description=(
        "Tap Sprint once to keep sprinting, tap again to walk. You start"
        " sprinting when you spawn, and sprint picks back up by itself after a"
        " slide or crouch. Off is the game's hold-to-sprint."
    ),
)

any_direction = BoolOption(
    "Sprint in any direction",
    True,
    "On",
    "Off",
    description="Sprint backwards and sideways at full speed, not only forwards.",
)

sprint_combat = BoolOption(
    "Sprint while shooting",
    True,
    "On",
    "Off",
    description=(
        "Keep sprinting while you fire or aim down sights, instead of being"
        " dropped to a walk."
    ),
)

no_aim_slowdown = BoolOption(
    "No aiming slowdown",
    True,
    "On",
    "Off",
    description="Move at full speed while aiming down sights. The game normally cuts you to about 40%.",
)

sprint_group = NestedOption(
    "Sprint",
    [toggle_sprint, any_direction, sprint_combat, no_aim_slowdown],
    description="Sprint toggle, sprinting in any direction, and sprinting while shooting.",
)


# --- Slide --------------------------------------------------------------------

slide_enabled = BoolOption(
    "Sliding",
    True,
    "On",
    "Off",
    description="Crouch while sprinting to slide. Jump out of a slide to keep its speed.",
)

slide_speed = SliderOption(
    "Slide speed",
    140,
    100,
    200,
    5,
    description=(
        "How fast a slide starts, in percent. Distance stays where Slide"
        " distance puts it, so a faster slide on its own is also a shorter one."
    ),
)

slide_distance = SliderOption(
    "Slide distance",
    300,
    100,
    400,
    10,
    description="How far a slide carries on flat ground, in percent. Slopes still speed you up or slow you down.",
)

slide_jump = SliderOption(
    "Slide jump boost",
    130,
    100,
    200,
    5,
    description="Horizontal speed kept when you jump out of a slide, in percent. 100 keeps the slide's speed exactly.",
)

slide_fast_finish = BoolOption(
    "Fast slide finish",
    True,
    "On",
    "Off",
    description=(
        "On: a slide stays faster than a sprint for its whole length, then"
        " drops out quickly. Off: it slows gradually all the way down to a"
        " crouch walk, like the original Sliding mod."
    ),
)

slide_lock = BoolOption(
    "Lock slide direction",
    True,
    "On",
    "Off",
    description=(
        "Off: a slide follows where you're looking, and ends if you let go of"
        " the movement keys. On: a slide holds the line it started on - turn"
        " the camera freely, keep sliding with no keys held, and steer it with"
        " the movement keys at the Slide steering rate."
    ),
)

slide_steering = SliderOption(
    "Slide steering",
    150,
    0,
    400,
    10,
    description=(
        "With Lock slide direction on: degrees per second the movement keys"
        " can bend a slide. 0 locks it perfectly straight."
    ),
)

slide_any_direction = BoolOption(
    "Slide in any direction",
    True,
    "On",
    "Off",
    description="Start a slide while sprinting backwards or sideways, not just forwards.",
)

slide_on_landing = BoolOption(
    "Slide on landing",
    True,
    "On",
    "Off",
    description="Land with crouch held and go straight into a slide.",
)

slide_group = NestedOption(
    "Slide",
    [
        slide_enabled,
        slide_speed,
        slide_distance,
        slide_jump,
        slide_fast_finish,
        slide_lock,
        slide_steering,
        slide_any_direction,
        slide_on_landing,
    ],
    description="Slide speed, distance, steering and slide jumps.",
)


# --- Dash and jumps -----------------------------------------------------------

charges = SliderOption(
    "Stamina charges",
    4,
    1,
    5,
    1,
    description="How many charges you hold. A dash, an air dash or an air jump spends one.",
)

recharge = SliderOption(
    "Seconds per charge",
    4,
    1,
    12,
    1,
    description="How long one spent charge takes to come back.",
)

dash_speed = SliderOption(
    "Dash speed",
    300,
    100,
    400,
    5,
    description=(
        "Dash speed as a percent of how fast you are moving on foot. Dashing"
        " out of something faster, like a slide, keeps that speed instead."
    ),
)

dash_duration = SliderOption(
    "Dash length (ms)",
    750,
    100,
    1200,
    50,
    description="How long a dash holds its speed, in milliseconds. Jump during a dash to carry it through the air.",
)

air_dashes = SliderOption(
    "Air dashes per jump",
    1,
    0,
    3,
    1,
    description="Dashes allowed between leaving the ground and landing, however many charges you hold.",
)

air_jumps = SliderOption(
    "Air jumps",
    1,
    0,
    3,
    1,
    description="Extra jumps in the air. 1 is a double jump, 0 turns it off.",
)

air_jump_height = SliderOption(
    "Air jump height",
    180,
    50,
    250,
    5,
    description="Air jump strength as a percent of your normal jump. It cancels a fall however far into one you are.",
)

fast_fall = BoolOption(
    "Fast fall",
    True,
    "On",
    "Off",
    description="Double-tap crouch in the air to drop straight down. Free - costs no stamina.",
)

show_bar = BoolOption(
    "Stamina bar",
    True,
    "On",
    "Off",
    description="Show your stamina charges on screen.",
)

bar_style = DropdownOption(
    "Stamina bar style",
    "Diamonds",
    ["Chevrons", "Gauge", "Diamonds"],
    description=(
        "Chevrons: a slanted segment per charge. Gauge: one slanted bar split"
        " into charges. Diamonds: a diamond per charge that fills from the bottom."
    ),
)

bar_hide_full = BoolOption(
    "Hide bar when full",
    True,
    "On",
    "Off",
    description=(
        "Fade the stamina bar out over a second once every charge is back, and"
        " bring it back the moment you spend one."
    ),
)

bar_position = DropdownOption(
    "Stamina bar position",
    "Above XP bar",
    ["Below crosshair", "Above XP bar", "Below health and shields"],
    description="Where the stamina bar sits. Stamina bar height only applies to Below crosshair.",
)

bar_size = SliderOption(
    "Stamina bar size",
    100,
    50,
    200,
    25,
    description="Size of the stamina bar, in percent.",
)

bar_height = SliderOption(
    "Stamina bar height",
    57,
    30,
    90,
    1,
    description=(
        "For the Below crosshair position: how far down the screen the bar"
        " sits, in percent. 50 is the crosshair."
    ),
)

dash_group = NestedOption(
    "Dash and Jumps",
    [
        charges,
        recharge,
        dash_speed,
        dash_duration,
        air_dashes,
        air_jumps,
        air_jump_height,
        fast_fall,
        show_bar,
        bar_style,
        bar_hide_full,
        bar_position,
        bar_size,
        bar_height,
    ],
    description="Stamina charges, dash, air jumps, fast fall and the stamina bar. Bind Dash under Keybinds.",
)


# --- Air control --------------------------------------------------------------

air_control = BoolOption(
    "Air control",
    True,
    "On",
    "Off",
    description="Steer while airborne without losing the speed you jumped with.",
)

air_steering = SliderOption(
    "Air steering",
    100,
    5,
    200,
    5,
    description="How hard you can steer in the air, as a percent of your ground acceleration. The game's own is about 11.",
)

air_speed_cap = SliderOption(
    "Air speed cap",
    100,
    50,
    300,
    10,
    description=(
        "Fastest that steering alone can push you, as a percent of ground"
        " speed. Not a speed limit - speed you jumped in with is always kept."
    ),
)

air_group = NestedOption(
    "Air Control",
    [air_control, air_steering, air_speed_cap],
    description="Steering while airborne.",
)


# --- Mantle -------------------------------------------------------------------

mantle_mode = DropdownOption(
    "Mantle",
    "Hold jump",
    ["Hold jump", "Automatic", "Off"],
    description=(
        "Climb over ledges you run into. Hold jump: only while jump is held."
        " Automatic: whenever you walk into one."
    ),
)

mantle_height = SliderOption(
    "Max ledge height",
    220,
    60,
    400,
    10,
    description="Tallest ledge you'll climb, in game units. You stand about 160 tall.",
)

mantle_time = SliderOption(
    "Pull-up time (ms)",
    250,
    150,
    1000,
    50,
    description="How quickly you pull yourself up. Lower is snappier.",
)

mantle_group = NestedOption(
    "Mantle",
    [mantle_mode, mantle_height, mantle_time],
    description="Climbing over ledges.",
)


# --- Sounds -------------------------------------------------------------------

sound_enabled = BoolOption(
    "Movement sounds",
    True,
    "On",
    "Off",
    description=(
        "Sounds for slides, dashes, air jumps and mantles. They play through"
        " Windows, so the in-game volume sliders don't affect them - use the"
        " volumes here."
    ),
)

master_volume = SliderOption(
    "Master volume",
    60,
    0,
    100,
    5,
    description="Volume for every movement sound at once.",
)

clip_pickers: dict[str, DropdownOption] = {}
clip_volumes: dict[str, SliderOption] = {}

for _event in sounds.EVENTS:
    _choices = sounds.discover(_event.key)
    _default = _event.default_clip if _event.default_clip in _choices else _choices[0]
    clip_pickers[_event.key] = DropdownOption(
        f"{_event.label} sound",
        _default,
        _choices,
        description=(
            f"Which sound plays on {_event.label.lower()}. Add your own 16-bit"
            f" .wav files to sdk_mods/movement_overhaul_sounds/{_event.key} and"
            " restart the game to see them here."
        ),
    )
    clip_volumes[_event.key] = SliderOption(
        f"{_event.label} volume",
        _event.default_volume,
        0,
        100,
        5,
        description=f"Volume of the {_event.label.lower()} sound, before the master volume.",
    )

sound_group = NestedOption(
    "Sounds",
    [
        sound_enabled,
        master_volume,
        *(opt for e in sounds.EVENTS for opt in (clip_pickers[e.key], clip_volumes[e.key])),
    ],
    description="Movement sounds and their volumes.",
)


# --- Diagnostics --------------------------------------------------------------

diagnostics = BoolOption(
    "Diagnostics",
    False,
    "On",
    "Off",
    description="Write movement details to the SDK log (unrealsdk.log) for bug reports. Leave off otherwise.",
)


ALL = [
    sprint_group,
    slide_group,
    dash_group,
    air_group,
    mantle_group,
    sound_group,
    diagnostics,
]
