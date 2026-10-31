"""One arrival runway of the shared airport world (CONSTRAINTS.md 2, CAP-9, WAKE-1..4, WAKE-7).

An airport has n = max(1, active_runways // 2) arrival runways "R1".."Rn". Each runway has its
own grid: points step 60 x n / R(t) minutes (accumulated as a float, rounded to whole minutes,
duplicates dropped), where R(t) is the airport arrival rate including the storm factor, so the
airport as a whole still lands R arrivals per hour. Runway k starts at (k - 1) x 60 / R, so the
runways are staggered. Each grid point is background traffic with probability 0.55 (CAP-7) and
carries a seeded wake class (WAKE-3). Seed keys end with the runway id.

Wake separation applies only between consecutive arrivals on the same runway (WAKE-4, as the
workbook states). `runway=None` everywhere means the merged stream of every runway: the old,
conservative model, kept only for callers that know no runway (a legacy "RWY" booking blocks
every runway).

Ledger views are read through `view_call`, which passes `runway=` only to views that accept it;
a view without it reports its bookings on every runway (conservative).
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable
from functools import lru_cache

from acacia import config, constraints
from acacia.models import AtcPrivate, LedgerView

TRAFFIC_PROBABILITY = constraints.BACKGROUND_LOAD  # CAP-7
WAKE_CLASSES = ("Super", "Heavy", "Large", "Small")

_RUNWAY_KW: dict[tuple[type, str], bool] = {}


@lru_cache(maxsize=1)
def separation_reach() -> int:
    """Whole minutes that cover the widest workbook wake minimum: movements further away than
    this can never violate separation, so only this window is scanned."""
    return math.ceil(max(constraints.wake_minutes(a, b) for a in WAKE_CLASSES for b in WAKE_CLASSES))


def _takes_runway(view: object, name: str) -> bool:
    key = (type(view), name)
    if key not in _RUNWAY_KW:
        try:
            params = inspect.signature(getattr(view, name)).parameters.values()
        except (TypeError, ValueError):
            params = []
        _RUNWAY_KW[key] = any(p.name == "runway" or p.kind is p.VAR_KEYWORD for p in params)
    return _RUNWAY_KW[key]


def view_call(view: LedgerView, name: str, airport: str, t: int, exclude_event: str | None,
              runway: str | None):
    """view.<name>(airport, t, ...) passing exclude_event and runway only when given and accepted."""
    fn = getattr(view, name, None)
    if not callable(fn):
        return None
    kw: dict = {} if exclude_event is None else {"exclude_event": exclude_event}
    if runway is not None and _takes_runway(view, name):
        kw["runway"] = runway
    return fn(airport, t, **kw)


def slot_taken(view: LedgerView, airport: str, t: int, exclude_event: str | None = None,
               runway: str | None = None) -> bool:
    return bool(view_call(view, "slot_taken", airport, t, exclude_event, runway))


def held_slot_class(view: LedgerView, airport: str, t: int, exclude_event: str | None = None,
                    runway: str | None = None) -> str | None:
    """Wake class of another event's held/committed slot at t on `runway` (any runway when None,
    WAKE-7); None when t is free. A view without `slot_class`, or a slot with no stored class,
    reads as Heavy (most restrictive)."""
    if not slot_taken(view, airport, t, exclude_event, runway):
        return None
    cls = view_call(view, "slot_class", airport, t, exclude_event, runway)
    return cls or constraints.UNKNOWN_SLOT_WAKE_CLASS


def separation_verdict(t: int, own_class: str, near: dict[int, str]) -> tuple[bool, str]:
    """Wake minimum to the nearest earlier (<= t) and later movement in `near` (time -> class)."""
    earlier = [p for p in near if p <= t]
    later = [p for p in near if p > t]
    notes: list[str] = []
    if earlier:
        lead = max(earlier)
        need = constraints.wake_minutes(near[lead], own_class)
        if t - lead < need:
            return False, f"{t - lead} min behind {near[lead]} at {lead}, need {need:.2f}"
        notes.append(f"{t - lead} min behind {near[lead]} at {lead} (need {need:.2f})")
    if later:
        follow = min(later)
        need = constraints.wake_minutes(own_class, near[follow])
        if follow - t < need:
            return False, f"{follow - t} min ahead of {near[follow]} at {follow}, need {need:.2f}"
        notes.append(f"{follow - t} min ahead of {near[follow]} at {follow} (need {need:.2f})")
    return True, "separated: " + ("; ".join(notes) or "no movement within reach")


def _class_for(u: float) -> str:
    acc = 0.0
    for label, share in constraints.TRAFFIC_CLASS_MIX:
        acc += share
        if u < acc:
            return label
    return constraints.TRAFFIC_CLASS_MIX[-1][0]


class Runway:
    """Grid, background traffic and wake classes of one arrival runway."""

    def __init__(self, airport: str, date: str, rid: str, index: int,
                 storm_at: Callable[[int], constraints.Storm]) -> None:
        self.id = rid
        self.grid: list[int] = self._build_grid(airport, index, storm_at)
        traffic_rng = config.seeded_rng(config.seed_key("world-traffic", airport, date, rid))
        self.traffic: dict[int, bool] = {p: traffic_rng.random() < TRAFFIC_PROBABILITY for p in self.grid}
        class_rng = config.seeded_rng(config.seed_key("world-class", airport, date, rid))
        self.classes: dict[int, str] = {p: _class_for(class_rng.random()) for p in self.grid}

    @staticmethod
    def _build_grid(airport: str, index: int, storm_at: Callable[[int], constraints.Storm]) -> list[int]:
        """Points step 60 x n / R(t); runway k (index k - 1) starts (k - 1) x 60 / R(00:00) in."""
        grid: list[int] = []
        point = index * 60.0 / constraints.arrival_rate(airport, storm_at(0))
        while point < config.MINUTES_PER_DAY:
            minute = int(point + 0.5)
            if minute < config.MINUTES_PER_DAY and (not grid or minute > grid[-1]):
                grid.append(minute)
            point += constraints.runway_step(airport, storm_at(minute))
        return grid

    def traffic_class(self, t: int) -> str | None:
        """Seeded wake class of the background movement at t; None when t carries none."""
        return self.classes.get(t) if self.traffic.get(t, False) else None

    def is_traffic(self, t: int) -> bool:
        return self.traffic.get(t, False)


# --- ATC private state per runway (AtcPrivate.free_by_runway; empty = legacy single stream) ------
def atc_slots(atc: AtcPrivate) -> list[tuple[int, str | None]]:
    """(slot, runway) pairs, earliest first, ties in runway order; legacy state: (slot, None)."""
    if atc.free_by_runway:
        return sorted((s, r) for r, slots in atc.free_by_runway.items() for s in slots)
    return [(s, None) for s in sorted(atc.free_slots)]


def atc_has_slot(atc: AtcPrivate, t: int, runway: str | None) -> bool:
    """t is a free slot on `runway`; runway None (or legacy state): on any runway."""
    if atc.free_by_runway and runway is not None:
        return t in atc.free_by_runway.get(runway, [])
    return t in atc.free_slots


def atc_demand(atc: AtcPrivate, t: int, runway: str | None) -> int:
    by_runway = atc.demand_by_runway.get(runway) if runway is not None else None
    return (by_runway if by_runway is not None else atc.demand).get(t, 1)


def atc_traffic(atc: AtcPrivate, runway: str | None) -> dict[int, str]:
    """Traffic (time -> wake class) on `runway`; None or legacy state: the merged stream."""
    if atc.traffic_by_runway and runway is not None:
        return dict(atc.traffic_by_runway.get(runway, {}))
    return {t: atc.traffic_classes.get(t, constraints.DEFAULT_OWN_CLASS) for t in atc.traffic}
