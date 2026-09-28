"""Movement Overhaul - sprint, slide, dash, air control and mantle for Borderlands 2.

The movement itself is the original set of mods, packaged unchanged in
`legacy/`: juso's Sliding, Slide Tuning, Stamina,
Autorun, Air Control, Mantle and Sprint Combat. This file registers all of
them as one mod, drives them from one options menu, and adds what's new around
them: original sound effects and the stamina bar styles.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from mods_base import HookType, build_mod, get_pc, hook, keybind
from networking import add_network_functions
from unrealsdk import logging
from unrealsdk.hooks import Type

from . import audio, hud, options, sounds
from .common import TAG, Warn
from .legacy import air_control, autorun, mantle, slide_tuning, sliding, sprint_combat, stamina
from .legacy.bridge import switched_on

if TYPE_CHECKING:
    from unrealsdk import unreal

# Registration order, which is also the order hooks on the same engine
# function are added in. Mirrors the order the separate mods loaded in.
MODULES = {
    "air_control": air_control,
    "autorun": autorun,
    "mantle": mantle,
    "slide_tuning": slide_tuning,
    "sliding": sliding,
    "sprint_combat": sprint_combat,
    "stamina": stamina,
}

# The old stamina bar; the new styles in hud.py draw instead.
EXCLUDED_HOOKS = {id(stamina.post_render)}


def hooks_of(module: Any) -> list[HookType]:
    return [
        value
        for value in vars(module).values()
        if isinstance(value, HookType) and id(value) not in EXCLUDED_HOOKS
    ]


FEATURE_HOOKS: dict[str, list[HookType]] = {name: hooks_of(module) for name, module in MODULES.items()}


# --- what's new: sounds and the stamina bar ---------------------------------------


class SlideReport:
    """With Diagnostics on, one log line per slide: what it started at, and how far and fast it went."""

    active: bool = False
    time: float = 0.0
    distance: float = 0.0
    peak: float = 0.0
    last: tuple[float, float] | None = None
    opening: float = 0.0
    ground_speed: float = 0.0


def active(feature_hook: HookType) -> bool:
    try:
        return feature_hook.get_active_count() > 0
    except Exception:  # noqa: BLE001
        return bool(getattr(feature_hook, "enabled", False))


def report_slide(pc: unreal.UObject, delta: float, now: bool) -> None:
    pawn = pc.Pawn
    if pawn is None:
        return
    here = (float(pawn.Location.X), float(pawn.Location.Y))
    r = SlideReport
    if now and not r.active:
        r.active, r.time, r.distance, r.peak, r.last = True, 0.0, 0.0, 0.0, here
        r.opening, r.ground_speed = float(pawn.CrouchedPct), float(pawn.GroundSpeed)
        return
    if not r.active:
        return
    if r.last is not None and delta > 0.0:
        step = ((here[0] - r.last[0]) ** 2 + (here[1] - r.last[1]) ** 2) ** 0.5
        r.distance += step
        r.peak = max(r.peak, step / delta)
    r.last = here
    r.time += delta
    if not now:
        r.active = False
        logging.info(
            f"{TAG} slide: {r.time:.2f}s, {r.distance:.0f} units, avg {r.distance / max(r.time, 1e-6):.0f} u/s,"
            f" peak {r.peak:.0f} u/s | opened at CrouchedPct {r.opening:.2f} x GroundSpeed {r.ground_speed:.0f}"
            f" | settings: speed {slide_tuning.speed.value}, distance {slide_tuning.distance.value},"
            f" fast finish {slide_tuning.fast_finish.value},"
            f" slide tuning hooks active {sum(1 for h in FEATURE_HOOKS['slide_tuning'] if active(h))}/3",
        )


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove", Type.POST)
def slide_sound(obj: unreal.UObject, args: unreal.WrappedStruct, _ret: Any, _func: unreal.BoundFunction) -> None:
    now = bool(sliding.OWN_SLIDE_STATE.is_sliding)
    try:
        audio.SlideSound.tick(now)
    except Exception as ex:  # noqa: BLE001
        Warn.once("slide-sound", f"slide sound failed: {ex!r}")
    if not options.diagnostics.value:
        SlideReport.active = False
        return
    try:
        report_slide(obj, float(args.DeltaTime), now)
    except Exception as ex:  # noqa: BLE001
        Warn.once("slide-report", f"slide report failed: {ex!r}")


@hook("WillowGame.WillowGameViewportClient:PostRender", Type.POST)
def post_render(_obj: unreal.UObject, args: unreal.WrappedStruct, _ret: Any, _func: unreal.BoundFunction) -> None:
    hud.draw(args.Canvas)


@keybind(
    "Dash",
    "LeftShift",
    description="Dash the way you're moving. Works on the ground and in the air, and costs a stamina charge.",
)
def dash() -> None:
    stamina.dash_pressed.callback()


# --- menu switches -------------------------------------------------------------------


def exit_slide_now() -> None:
    try:
        pc = get_pc()
        if pc is not None:
            sliding.exit_slide(pc)
    except Exception:  # noqa: BLE001, S110
        pass


def restore_air_control() -> None:
    try:
        pawn = air_control.State.pawn
        if pawn is not None:
            air_control.set_air_control(pawn, air_control.State.stock_air_control)
        air_control.State.was_falling = False
    except Exception:  # noqa: BLE001, S110
        pass


class Active:
    """Whether the mod is on. Tracked here rather than read off `mod`, because
    the mod manager enables a mod while `build_mod` is still constructing it -
    `on_enable` runs before the name `mod` exists."""

    enabled: bool = False


def apply_switches() -> None:
    """Enable or disable each feature's hooks to match the menu."""
    if not Active.enabled:
        return
    for name, feature_hooks in FEATURE_HOOKS.items():
        on = switched_on(name)
        for feature_hook in feature_hooks:
            if on:
                feature_hook.enable()
            else:
                feature_hook.disable()
    if not switched_on("sliding"):
        exit_slide_now()
    if not switched_on("air_control"):
        restore_air_control()
    if not switched_on("mantle"):
        mantle.on_disable()


class Pending:
    """A switch changed; apply it once the option has its new value.

    Option callbacks run *before* the value changes, so applying there would
    read the old value. The next input tick applies it instead.
    """

    dirty: bool = False


def on_switch_change(_option: Any, _new_value: Any) -> None:
    Pending.dirty = True


@hook("WillowGame.WillowPlayerInput:PlayerInput", Type.POST)
def settle_switches(_obj: unreal.UObject, _args: unreal.WrappedStruct, _ret: Any, _func: unreal.BoundFunction) -> None:
    if Pending.dirty:
        Pending.dirty = False
        apply_switches()


for _switch in (options.slide_enabled, options.toggle_sprint, options.air_control, options.mantle_mode):
    _switch.on_change_while_enabled = on_switch_change


# --- enable / disable -----------------------------------------------------------------

# The standalone versions of what's packaged here. Running both doubles every effect.
STANDALONE: dict[str, str] = {
    "sliding": "Sliding",
    "autorun": "Autorun",
    "slide_tuning": "Slide Tuning",
    "stamina": "Stamina",
    "air_control": "Air Control",
    "mantle": "Mantle",
    "sprint_combat": "Sprint Combat",
    "movement_audio": "Movement Audio",
    "slide_sound": "Slide Sound",
}


def standalone_running() -> list[str]:
    found = []
    for module_name, label in STANDALONE.items():
        module = sys.modules.get(module_name)
        other = getattr(module, "mod", None) if module is not None else None
        if other is not None and getattr(other, "is_enabled", False):
            found.append(label)
    return found


def on_enable() -> None:
    Active.enabled = True
    sounds.ensure_user_folders()
    audio.prune_cache()
    apply_switches()
    running = standalone_running()
    if running:
        logging.warning(
            f"{TAG} These mods do the same job and should be turned off: {', '.join(running)}."
            " Movement Overhaul includes all of them.",
        )


def on_disable() -> None:
    Active.enabled = False
    exit_slide_now()
    for name, fn in (
        ("stamina", stamina.on_disable),
        ("mantle", mantle.on_disable),
        ("sprint", sprint_combat.on_disable),
        ("air control", restore_air_control),
        ("sound", audio.stop),
        ("sound", audio.SlideSound.reset),
    ):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001
            Warn.once(f"disable:{name}", f"{name} did not shut down cleanly: {ex!r}")


mod = build_mod(
    options=options.ALL,
    hooks=[
        *(h for feature_hooks in FEATURE_HOOKS.values() for h in feature_hooks),
        slide_sound,
        post_render,
        settle_switches,
    ],
    keybinds=[dash],
    on_enable=on_enable,
    on_disable=on_disable,
)

add_network_functions(
    mod,
    [
        sliding.server_set_slide_jump_velocity,
        sliding.server_exit_slide,
        sliding.client_exit_slide,
        sliding.server_enter_slide,
        stamina.server_set_velocity,
    ],
)

logging.info(f"{TAG} loaded v{mod.version}")
