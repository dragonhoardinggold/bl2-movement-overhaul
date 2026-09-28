"""Where movement sounds come from.

Two sources, merged by name:

- **Bundled** clips inside the mod, under `sounds/<event>/`. These ship with
  the release and are all original - synthesised by `tools/make_sounds.py`.
- **Your own** clips, in `sdk_mods/movement_overhaul_sounds/<event>/`. That
  folder lives outside the mod on purpose: updating the mod never touches it,
  and nothing in it is ever packaged. A clip there with the same name as a
  bundled one replaces it.

Bundled clips are read through `importlib.resources`, so this works the same
whether the mod is installed as a folder or as a zipped `.sdkmod`.
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from pathlib import Path

from mods_base import MODS_DIR

NO_SOUND: str = "(none)"

USER_DIR: Path = MODS_DIR / "movement_overhaul_sounds"
CACHE_DIR: Path = USER_DIR / ".cache"


@dataclass(frozen=True)
class Event:
    """One thing that can make a noise, and how it behaves against the others."""

    key: str
    label: str
    priority: int
    default_clip: str
    default_volume: int


# Dash, air jump and mantle outrank the slide: they are sharp, deliberate
# one-shots, and the slide is a bed you are already hearing.
EVENTS: tuple[Event, ...] = (
    Event("slide", "Slide", priority=10, default_clip="Scrape Soft", default_volume=35),
    Event("dash", "Dash", priority=20, default_clip="Thruster", default_volume=45),
    Event("air_jump", "Air jump", priority=20, default_clip="Jump Pad", default_volume=45),
    Event("mantle", "Mantle", priority=20, default_clip="Grab", default_volume=45),
)
BY_KEY: dict[str, Event] = {e.key: e for e in EVENTS}


def _bundled_dir(key: str) -> importlib.resources.abc.Traversable | None:
    try:
        folder = importlib.resources.files(__package__).joinpath("sounds", key)
    except (ModuleNotFoundError, TypeError):
        return None
    return folder if folder.is_dir() else None


def _bundled(key: str) -> dict[str, importlib.resources.abc.Traversable]:
    folder = _bundled_dir(key)
    if folder is None:
        return {}
    return {
        entry.name[:-4]: entry
        for entry in folder.iterdir()
        if entry.is_file() and entry.name.lower().endswith(".wav")
    }


def _user(key: str) -> dict[str, Path]:
    folder = USER_DIR / key
    if not folder.is_dir():
        return {}
    return {p.stem: p for p in folder.glob("*.wav") if p.is_file()}


def ensure_user_folders() -> None:
    """Create the empty per-event folders, so players can see where clips go."""
    try:
        for event in EVENTS:
            (USER_DIR / event.key).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def discover(key: str) -> list[str]:
    """Every clip name available for one event, plus "(none)"."""
    names = set(_bundled(key)) | set(_user(key))
    return [*sorted(names, key=str.lower), NO_SOUND]


def read(key: str, name: str) -> bytes | None:
    """The raw bytes of a clip, or None if it's missing.

    Your own folder wins over the bundled clip of the same name.
    """
    source = _user(key).get(name) or _bundled(key).get(name)
    if source is None:
        return None
    try:
        return source.read_bytes()
    except OSError:
        return None
