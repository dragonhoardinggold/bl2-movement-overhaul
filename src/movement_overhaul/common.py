"""Small helpers every feature module shares."""

from __future__ import annotations

import ctypes
import math
from typing import TYPE_CHECKING, Any

from mods_base import ENGINE
from unrealsdk import find_enum, logging

if TYPE_CHECKING:
    from unrealsdk import unreal

TAG = "[Movement Overhaul]"


def _enum_value(enum: str, member: str, fallback: int) -> int:
    try:
        return int(getattr(find_enum(enum), member))
    except Exception:  # noqa: BLE001 - these literals have been stable since UE3 shipped
        return fallback


PHYS_WALKING: int = _enum_value("EPhysics", "PHYS_Walking", 1)
PHYS_FALLING: int = _enum_value("EPhysics", "PHYS_Falling", 2)
ZST_NOT_ZOOMED: int = _enum_value("EZoomState", "ZST_NotZoomed", 0)

# Fallbacks for pawn properties, for a pawn that turns up without them.
GROUND_SPEED_FALLBACK: float = 440.0
# Stock BL2 sprint is 594 against a 440 walk.
SPRINT_RATIO: float = 594.0 / 440.0
ACCEL_RATE_FALLBACK: float = 2048.0
JUMP_Z_FALLBACK: float = 630.0

# The longest single frame we'll believe. A hitch or a load screen hands us a
# huge DeltaTime, and spending it in one go would run every timer to zero.
MAX_DELTA: float = 0.1


def clamp_delta(delta: float) -> float:
    return min(float(delta), MAX_DELTA)


class Speed:
    """The game's own walk and sprint speeds, and every write we make to them.

    Several features raise `GroundSpeed` (dash, sprinting sideways, aiming at
    full speed). They must never *learn* a speed from a frame one of them
    wrote, or a dash's leftover speed gets remembered as your walking pace. So
    all writes go through `write()`, and `learn()` - run first thing each
    frame - only trusts a frame after which nobody wrote.

    When a feature lets go it hands back `intended()`: the speed for what you
    are doing *now*, never a value remembered from before a jump.
    """

    walk: float = 0.0
    sprint: float = 0.0
    written: bool = False
    last_value: float | None = None

    @classmethod
    def write(cls, pawn: unreal.UObject, value: float) -> None:
        try:
            pawn.GroundSpeed = value
            cls.written = True
            cls.last_value = float(value)
        except Exception as ex:  # noqa: BLE001
            Warn.once("groundspeed", f"cannot set GroundSpeed: {ex!r}")

    @classmethod
    def learn(cls, pc: unreal.UObject) -> None:
        wrote_last_frame, cls.written = cls.written, False
        pawn = pc.Pawn
        if wrote_last_frame or pawn is None or is_aiming(pawn) or bool(pc.bDuck):
            return
        value = float(getattr(pawn, "GroundSpeed", 0.0) or 0.0)
        if value <= 0.0:
            return
        # Still holding a value we put there: not the game's, don't learn it.
        if cls.last_value is not None and abs(value - cls.last_value) < 0.5:
            return
        cls.last_value = None
        if pc.bInSprintState:
            cls.sprint = value
        else:
            cls.walk = value

    @classmethod
    def intended(cls, pawn: unreal.UObject, sprinting: bool) -> float:
        """Sprint speed if you're sprinting (or meant to be), else walk speed."""
        del pawn
        walk = cls.walk or (cls.sprint / SPRINT_RATIO if cls.sprint else GROUND_SPEED_FALLBACK)
        if not sprinting:
            return walk
        # Never fall back to the current GroundSpeed: during a dash that is the
        # dash's own speed, and handing it back would ratchet upward.
        return cls.sprint if cls.sprint > walk else walk * SPRINT_RATIO

    @classmethod
    def reset(cls) -> None:
        cls.walk = cls.sprint = 0.0
        cls.written = False
        cls.last_value = None


def is_airborne(pawn: unreal.UObject) -> bool:
    """Decided by physics, not `IsOnGroundOrShortFall()`.

    That helper stays true through a "short fall", so for the first stretch
    after a jump it reports you grounded while you are visibly in the air.
    """
    try:
        return int(pawn.Physics) == PHYS_FALLING
    except (TypeError, ValueError):
        return not pawn.IsOnGroundOrShortFall()


def on_ground_or_short_fall(pawn: unreal.UObject) -> bool:
    try:
        return bool(pawn.IsOnGroundOrShortFall())
    except Exception:  # noqa: BLE001
        return False


def wish_direction(pawn: unreal.UObject) -> tuple[float, float] | None:
    """The horizontal unit vector the player is asking to move along.

    Read off `Pawn.Acceleration` after the walking state has built it from
    input and rotated it by the camera's yaw, so sticks and remapped controls
    come along for free. None when nothing is held.
    """
    accel = pawn.Acceleration
    x, y = accel.X, accel.Y
    length = math.hypot(x, y)
    if length < 1e-4:
        return None
    return x / length, y / length


def horizontal_speed(pawn: unreal.UObject) -> float:
    velocity = pawn.Velocity
    return math.hypot(velocity.X, velocity.Y)


def is_aiming(pawn: unreal.UObject) -> bool:
    """Whether the held weapon is anywhere in its zoom cycle.

    `IsZoomed` is native and cannot be called through the SDK, but
    `WillowWeapon.ZoomState` is a plain byte property.
    """
    try:
        weapon = pawn.Weapon
        if weapon is None:
            return False
        return int(weapon.ZoomState) != ZST_NOT_ZOOMED
    except Exception:  # noqa: BLE001
        return False


def is_client() -> bool:
    """True when we're a guest in someone else's game."""
    try:
        return ENGINE.GetCurrentWorldInfo().NetMode == find_enum("ENetMode").NM_Client
    except Exception:  # noqa: BLE001
        return False


def rotate_towards(
    current: tuple[float, float],
    target: tuple[float, float],
    limit: float,
) -> tuple[float, float]:
    """Turn `current` towards `target` by at most `limit` radians."""
    difference = math.atan2(target[1], target[0]) - math.atan2(current[1], current[0])
    difference = (difference + math.pi) % (2.0 * math.pi) - math.pi
    turn = max(-limit, min(limit, difference))
    cos_t, sin_t = math.cos(turn), math.sin(turn)
    return (
        current[0] * cos_t - current[1] * sin_t,
        current[0] * sin_t + current[1] * cos_t,
    )


# --- physical key state -------------------------------------------------------
#
# Some decisions need "is the key held right now", which input events can't
# express: a slide jump stands the pawn up and fires a crouch release the
# player never made, and a jump press fires once while we need to know it is
# still held. Windows answers directly.

try:
    _user32: Any = ctypes.windll.user32
except Exception:  # noqa: BLE001 - not on Windows
    _user32 = None


def key_down(vk: int) -> bool:
    if _user32 is None:
        return False
    try:
        return bool(_user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:  # noqa: BLE001
        return False


class LearnedKey:
    """A game action's physical key, learned by watching which key is down
    when the game reports that action - so rebinds need no configuration."""

    def __init__(self, default_vk: int, candidates: tuple[int, ...]) -> None:
        self.vk = default_vk
        self.candidates = candidates

    def learn(self) -> None:
        if key_down(self.vk):
            return
        for vk in self.candidates:
            if key_down(vk):
                self.vk = vk
                return

    def held(self) -> bool:
        return key_down(self.vk)


class Warn:
    """Log a failure once per session instead of sixty times a second."""

    seen: set[str] = set()

    @classmethod
    def once(cls, key: str, message: str) -> None:
        if key in cls.seen:
            return
        cls.seen.add(key)
        logging.error(f"{TAG} {message}")
