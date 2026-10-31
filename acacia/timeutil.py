"""Time helpers. Wire format is "HH:MM"; inside the code times are int minutes after local midnight."""

from __future__ import annotations

import math
import re

MINUTES_PER_DAY = 1440
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def to_min(hhmm: str) -> int:
    """Convert "HH:MM" (00:00..23:59) to minutes after midnight. Raises ValueError on bad input."""
    if not isinstance(hhmm, str):
        raise ValueError(f"expected 'HH:MM' string, got {type(hhmm).__name__}")
    match = _HHMM_RE.match(hhmm.strip())
    if match is None:
        raise ValueError(f"invalid time {hhmm!r}; expected 'HH:MM' 00:00..23:59")
    return int(match.group(1)) * 60 + int(match.group(2))


def to_hhmm(minutes: int) -> str:
    """Convert minutes after midnight to "HH:MM". Values outside 0..1439 wrap around the day."""
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise ValueError(f"expected int minutes, got {minutes!r}")
    wrapped = minutes % MINUTES_PER_DAY
    return f"{wrapped // 60:02d}:{wrapped % 60:02d}"


def is_hhmm(value: object) -> bool:
    """True when value is a valid "HH:MM" string."""
    return isinstance(value, str) and _HHMM_RE.match(value) is not None


def parse_hhmm(raw: object) -> int | None:
    """Parse a CSV hhmm value ("0648", "648", "2400") to minutes. Blank or missing -> None.

    Rule (HANDOVER 4.2): h = int(s)//100 % 24, m = int(s) % 100, so "2400" -> 0.
    """
    if raw is None:
        return None
    if isinstance(raw, float) and math.isnan(raw):
        return None
    text = str(raw).strip()
    if text == "" or text.lower() == "nan":
        return None
    try:
        value = int(float(text))
    except ValueError as exc:
        raise ValueError(f"invalid hhmm value {raw!r}") from exc
    if value < 0:
        raise ValueError(f"invalid hhmm value {raw!r}")
    return (value // 100 % 24) * 60 + value % 100
