"""Shared airport world (FABLE_ADDENDUM 4, HANDOVER 18.2, PLAN 2.A.1 and 2.A.2, CONSTRAINTS.md 2).

The runway grid follows the workbook arrival capacity: points step 60 / R(t) minutes
(accumulated as a float, rounded to whole minutes, duplicates dropped), where R is 50% of the
airport's max runway movements per hour, x0.6 / x0.4 inside a moderate / severe storm window
(a RUNWAY_CLOSED injected event). Each traffic point carries a seeded wake class
(Heavy/Large/Small 15/80/5), drawn from its own rng so the gate draws do not depend on it.

Wake separation (CONSTRAINTS.md 2): a point is separated for an aircraft of `own_class` when it
is at least `constraints.wake_minutes` behind the nearest earlier movement and ahead of the
nearest later one. Movements are background traffic (their seeded class) and other events'
HELD/COMMITTED runway slots read through the ledger view (class Large, WAKE-7). The rule is
applied across the merged arrival stream (WAKE-4). `free_slots_after` returns only such points.

One `AirportWorld` per (airport, date, injected set): a runway grid with background traffic, a
pool of `constraints.WORLD_GATE_POOL` gates with background busy intervals (CAP-4), and
equipment-failure intervals. It is built
once from the full injected list and cached; nothing here calls an LLM or reads the ledger
except through the `LedgerView` the caller passes in.

Times are int minutes after local midnight; intervals are half-open [start, end).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from functools import lru_cache

from acacia import config, constraints
from acacia.models import Injected, LedgerView
from acacia.timeutil import to_min

Interval = tuple[int, int]

TRAFFIC_PROBABILITY = constraints.BACKGROUND_LOAD  # CAP-7
DEFAULT_STORM = "moderate"  # CAP-8: a RUNWAY_CLOSED window without a severity
OTHER_EVENT_CLASS = "Large"  # WAKE-7: the ledger stores no aircraft class
WAKE_CLASSES = ("Super", "Heavy", "Large", "Small")
GATE_COUNT = constraints.WORLD_GATE_POOL  # CAP-4
GATE_NUMBER_RANGE = (1, 30)
BUSY_COUNT_RANGE = (2, 4)
BUSY_START_RANGE = (5 * 60, 22 * 60)
BUSY_LENGTH_RANGE = (30, 70)
SKIP_RANGE = (0, 1)

_CACHE: dict[tuple[str, str, frozenset[tuple]], "AirportWorld"] = {}


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Union of half-open intervals, sorted, with touching or overlapping ones merged."""
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlaps(intervals: Sequence[Interval], start: int, end: int) -> list[Interval]:
    """Intervals that overlap [start, end)."""
    return [(s, e) for s, e in intervals if s < end and start < e]


def _contains(intervals: Sequence[Interval], t: int) -> bool:
    return any(s <= t < e for s, e in intervals)


@lru_cache(maxsize=1)
def separation_reach() -> int:
    """Whole minutes that cover the widest workbook wake minimum: movements further away than
    this can never violate separation, so only this window is scanned."""
    return math.ceil(max(constraints.wake_minutes(a, b) for a in WAKE_CLASSES for b in WAKE_CLASSES))


def _slot_taken(view: LedgerView, airport: str, t: int, exclude_event: str | None) -> bool:
    if exclude_event is None:
        return view.slot_taken(airport, t)
    return view.slot_taken(airport, t, exclude_event=exclude_event)


def _gate_letter(airport: str) -> str:
    return "A" if airport == "ATL" else airport[0].upper()


class AirportWorld:
    """Runway grid and gate pool for one airport and date, built from all injected events."""

    def __init__(self, airport: str, date: str, injected: Sequence[Injected]) -> None:
        if len(airport) != 3 or not airport.isalpha():
            raise ValueError(f"airport must be a 3-letter code, got {airport!r}")
        self.airport = airport.upper()
        self.date = date
        self._injected = sorted(injected, key=lambda i: (i.type, i.start, i.end, i.ref))
        self._closures = merge_intervals(
            (to_min(i.start), to_min(i.end)) for i in self._injected if i.type == "RUNWAY_CLOSED"
        )
        self._storms = [(to_min(i.start), to_min(i.end), i.severity or DEFAULT_STORM)
                        for i in self._injected if i.type == "RUNWAY_CLOSED"]
        rng = config.seeded_rng(config.seed_key("world", self.airport, self.date))
        self._grid: list[int] = self._build_grid()
        self._traffic: dict[int, bool] = {p: rng.random() < TRAFFIC_PROBABILITY for p in self._grid}
        self._classes: dict[int, str] = self._draw_classes()
        self._gates: list[str] = self._draw_gates(rng)
        self._busy: dict[str, list[Interval]] = {g: self._draw_busy(rng) for g in self._gates}
        self._failed: dict[str, list[Interval]] = {g: [] for g in self._gates}
        self._apply_failures()

    # --- construction ---------------------------------------------------------------------
    def storm_at(self, t: int) -> constraints.Storm:
        """Storm level at t: the most severe RUNWAY_CLOSED window containing t, else "none"."""
        levels = {sev for s, e, sev in self._storms if s <= t < e}
        return "severe" if "severe" in levels else "moderate" if levels else "none"

    def _build_grid(self) -> list[int]:
        grid: list[int] = []
        point = 0.0
        while point < config.MINUTES_PER_DAY:
            minute = int(point + 0.5)
            if minute < config.MINUTES_PER_DAY and (not grid or minute > grid[-1]):
                grid.append(minute)
            point += 60.0 / constraints.arrival_rate(self.airport, self.storm_at(minute))
        return grid

    def _draw_classes(self) -> dict[int, str]:
        rng = config.seeded_rng(config.seed_key("world-class", self.airport, self.date))
        out: dict[int, str] = {}
        for p in self._grid:
            u, acc = rng.random(), 0.0
            out[p] = constraints.TRAFFIC_CLASS_MIX[-1][0]
            for label, share in constraints.TRAFFIC_CLASS_MIX:
                acc += share
                if u < acc:
                    out[p] = label
                    break
        return out

    def _draw_gates(self, rng) -> list[str]:
        low, high = GATE_NUMBER_RANGE
        numbers = sorted(rng.sample(range(low, high + 1), GATE_COUNT))
        letter = _gate_letter(self.airport)
        return [f"{letter}{n}" for n in numbers]

    @staticmethod
    def _draw_busy(rng) -> list[Interval]:
        count = rng.randint(*BUSY_COUNT_RANGE)
        intervals = []
        for _ in range(count):
            start = rng.randint(*BUSY_START_RANGE)
            length = rng.randint(*BUSY_LENGTH_RANGE)
            intervals.append((start, min(start + length, config.MINUTES_PER_DAY)))
        return merge_intervals(intervals)

    def _apply_failures(self) -> None:
        for inj in self._injected:
            if inj.type != "EQUIPMENT_FAILURE":
                continue
            start, end = to_min(inj.start), to_min(inj.end)
            if start >= end:
                continue
            rng = config.seeded_rng(config.seed_key("fail", self.airport, self.date, inj.start, inj.end))
            gate = rng.choice(self._gates)
            self._failed[gate] = merge_intervals([*self._failed[gate], (start, end)])

    # --- runway ------------------------------------------------------------------------------
    def grid(self) -> list[int]:
        return list(self._grid)

    def closures(self) -> list[Interval]:
        return list(self._closures)

    def closure_at(self, t: int) -> bool:
        return _contains(self._closures, t)

    def traffic_class(self, t: int) -> str | None:
        """Seeded wake class of the background movement at t (WAKE-3); None when t carries none."""
        return self._classes.get(t) if self._traffic.get(t, False) else None

    def is_traffic(self, t: int) -> bool:
        return self._traffic.get(t, False)

    def traffic_times(self, start: int, end: int) -> list[int]:
        """Grid points in [start, end) that carry background traffic."""
        return [p for p in self._grid if start <= p < end and self._traffic[p]]

    def _movements_near(self, t: int, view: LedgerView, exclude_event: str | None) -> dict[int, str]:
        """Traffic and other events' held slots within the separation reach of t, by class."""
        reach = separation_reach()
        out: dict[int, str] = {}
        for p in range(max(0, t - reach), min(config.MINUTES_PER_DAY, t + reach + 1)):
            cls = self.traffic_class(p)
            if cls is not None:
                out[p] = cls
            elif _slot_taken(view, self.airport, p, exclude_event):
                out[p] = OTHER_EVENT_CLASS
        return out

    def is_separated(
        self, t: int, own_class: str, view: LedgerView, exclude_event: str | None = None
    ) -> tuple[bool, str]:
        """Whether an arrival of `own_class` at t keeps the workbook wake minimum to the nearest
        earlier and later movement (traffic or another event's held slot). Returns (ok, detail);
        `exclude_event` leaves that event's own slots out (re-checks of a held package)."""
        if not isinstance(own_class, str) or not own_class:
            raise ValueError(f"own_class must be a non-empty wake class, got {own_class!r}")
        near = self._movements_near(t, view, exclude_event)
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

    def free_slots_after(
        self, t: int, n: int, view: LedgerView, own_class: str = OTHER_EVENT_CLASS
    ) -> list[int]:
        """Up to n free, wake-separated grid points >= t for an arrival of `own_class`, skipping
        rand(0..1) eligible points between returned slots (the seeded skip)."""
        if n <= 0:
            return []
        eligible = (
            p for p in self._grid
            if p >= t and not self._traffic[p] and not view.slot_taken(self.airport, p)
            and self.is_separated(p, own_class, view)[0]
        )
        rng = config.seeded_rng(config.seed_key("slots", self.airport, self.date, t))
        slots: list[int] = []
        skip = 0
        for point in eligible:
            if skip > 0:
                skip -= 1
                continue
            slots.append(point)
            if len(slots) == n:
                break
            skip = rng.randint(*SKIP_RANGE)
        return slots

    # --- gates ---------------------------------------------------------------------------------
    def gates(self) -> list[str]:
        return list(self._gates)

    def _check_gate(self, gate: str) -> None:
        if gate not in self._busy:
            raise KeyError(f"unknown gate {gate!r} at {self.airport} {self.date}")

    def busy_intervals(self, gate: str) -> list[Interval]:
        self._check_gate(gate)
        return list(self._busy[gate])

    def failed_intervals(self, gate: str) -> list[Interval]:
        self._check_gate(gate)
        return list(self._failed[gate])

    def is_failed(self, gate: str, t: int) -> bool:
        self._check_gate(gate)
        return _contains(self._failed[gate], t)

    def gate_free_from(
        self, gate: str, t: int, view: LedgerView, duration: int = config.GATE_MIN_FREE
    ) -> int | None:
        """Earliest time >= t at which `gate` is free for `duration` minutes, else None.

        Free means clear of background busy intervals, failure intervals and ledger bookings
        (read through `view`). None when the gate is out of service at t or no window fits
        before the end of the day.
        """
        self._check_gate(gate)
        if duration <= 0:
            raise ValueError(f"duration must be positive, got {duration}")
        if self.is_failed(gate, t):
            return None
        static = merge_intervals([*self._busy[gate], *self._failed[gate]])
        candidate = max(t, 0)
        while candidate + duration <= config.MINUTES_PER_DAY:
            blocking = _overlaps(static, candidate, candidate + duration)
            if blocking:
                candidate = max(e for _, e in blocking)
            elif view.gate_busy(self.airport, gate, candidate, candidate + duration):
                candidate += 1
            else:
                return candidate
        return None


def _cache_key(airport: str, date: str, injected: Iterable[Injected]):
    return (airport.upper(), date,
            frozenset((i.type, i.ref, i.start, i.end, i.severity) for i in injected))


def get_world(airport: str, date: str, injected: Sequence[Injected]) -> AirportWorld:
    """The world for (airport, date, injected set), built once and cached."""
    key = _cache_key(airport, date, injected)
    world = _CACHE.get(key)
    if world is None:
        world = AirportWorld(airport, date, list(injected))
        _CACHE[key] = world
    return world


def clear_world_cache() -> None:
    _CACHE.clear()
