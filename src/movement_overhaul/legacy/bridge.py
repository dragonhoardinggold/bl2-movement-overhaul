"""How the original movement mods talk to Movement Overhaul.

The mods in this folder were separate once, and found each other with
`sys.modules.get("<name>")`. `peer()` answers those lookups with the copies in
this package - and answers None for a feature switched off in the menu, which
is exactly what each of them already does when a partner mod isn't installed.

Their options are `Mapped` proxies: `.value` reads the matching Movement
Overhaul option, converted into the units the old code expects.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from .. import options

# Which menu switch turns each module off. A module not listed is always on.
SWITCHES: dict[str, Callable[[], bool]] = {
    "sliding": lambda: bool(options.slide_enabled.value),
    "slide_tuning": lambda: bool(options.slide_enabled.value),
    "autorun": lambda: bool(options.toggle_sprint.value),
    "air_control": lambda: bool(options.air_control.value),
    "mantle": lambda: str(options.mantle_mode.value) != "Off",
}


def switched_on(name: str) -> bool:
    switch = SWITCHES.get(name)
    return True if switch is None else switch()


def peer(name: str) -> Any:
    """The packaged copy of a sibling module, or None if it's switched off."""
    if not switched_on(name):
        return None
    return sys.modules.get(f"{__package__}.{name}")


class Mapped:
    """Stands in for one of the old modules' option objects."""

    def __init__(self, read: Callable[[], Any]) -> None:
        self._read = read

    @property
    def value(self) -> Any:
        return self._read()
