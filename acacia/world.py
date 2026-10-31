"""Shared airport world (FABLE_ADDENDUM 4, HANDOVER 18.2, PLAN 2.A.1 and 2.A.2).

One `AirportWorld` per (airport, date, injected set): a runway grid with background traffic, a
pool of 8 gates with background busy intervals, and equipment-failure intervals. It is built
once from the full injected list and cached; nothing here calls an LLM or reads the ledger
except through the `LedgerView` the caller passes in.

Times are int minutes after local midnight; intervals are half-open [start, end).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from acacia import config
from acacia.models import Injected, LedgerView
from acacia.timeutil import to_min

Interval = tuple[int, int]

TRAFFIC_PROBABILITY = 0.55
GRID_STEP = 4
GRID_STEP_IN_CLOSURE = 8
GATE_COUNT = 8
GATE_NUMBER_RANGE = (1, 30)
BUSY_COUNT_RANGE = (2, 4)
BUSY_START_RANGE = (5 * 60, 22 * 60)
BUSY_LENGTH_RANGE = (30, 70)
SKIP_RANGE = (0, 1)

_CACHE: dict[tuple[str, str, frozenset[tuple[str, str, str, str]]], "AirportWorld"] = {}


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
        rng = config.seeded_rng(config.seed_key("world", self.airport, self.date))
        self._grid: list[int] = self._build_grid()
        self._traffic: dict[int, bool] = {p: rng.random() < TRAFFIC_PROBABILITY for p in self._grid}
        self._gates: list[str] = self._draw_gates(rng)
        self._busy: dict[str, list[Interval]] = {g: self._draw_busy(rng) for g in self._gates}
        self._failed: dict[str, list[Interval]] = {g: [] for g in self._gates}
        self._apply_failures()

    # --- construction ---------------------------------------------------------------------
    def _build_grid(self) -> list[int]:
        grid: list[int] = []
        point = 0
        while point < config.MINUTES_PER_DAY:
            grid.append(point)
            point += GRID_STEP_IN_CLOSURE if self.closure_at(point) else GRID_STEP
        return grid

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

    def is_traffic(self, t: int) -> bool:
        return self._traffic.get(t, False)

    def traffic_times(self, start: int, end: int) -> list[int]:
        """Grid points in [start, end) that carry background traffic."""
        return [p for p in self._grid if start <= p < end and self._traffic[p]]

    def free_slots_after(self, t: int, n: int, view: LedgerView) -> list[int]:
        """Up to n free grid points >= t, skipping rand(0..1) eligible points between slots."""
        if n <= 0:
            return []
        eligible = (
            p for p in self._grid
            if p >= t and not self._traffic[p] and not view.slot_taken(self.airport, p)
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
    return (airport.upper(), date, frozenset((i.type, i.ref, i.start, i.end) for i in injected))


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
