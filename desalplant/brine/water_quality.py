"""Feedwater quality fixes the emergency-exit mode.

Sensitive water (seawater) may only leave the emergency position one confirmed
step at a time; forgiving water (brackish) is released in a single move. The
mapping is fixed by water type, not chosen by the caller of an exit attempt.
"""

from enum import Enum


class WaterType(str, Enum):
    """Feedwater categories the plant is designed for."""

    SEAWATER = "seawater"
    BRACKISH = "brackish"


class ExitMode(str, Enum):
    """How an emergency position may be released."""

    STEPPED = "stepped"
    ONE_SHOT = "one_shot"


_STEPPED_WATER = frozenset({WaterType.SEAWATER})


def exit_mode(water: WaterType) -> ExitMode:
    """Return the exit mode fixed for a feedwater type."""
    if water in _STEPPED_WATER:
        return ExitMode.STEPPED
    return ExitMode.ONE_SHOT
