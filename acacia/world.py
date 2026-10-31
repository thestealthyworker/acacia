"""Shared airport world (FABLE_ADDENDUM 4, HANDOVER 18.2, PLAN 2.A.1 and 2.A.2, CONSTRAINTS.md 2).

Per-runway arrival model (CAP-9): the airport has n = max(1, active_runways // 2) arrival
runways "R1".."Rn" (`acacia.runway.Runway`). Each has its own grid with step 60 x n / R(t), where
R is 50% of the airport's max runway movements per hour, x0.6 / x0.4 inside a moderate / severe
storm window (a RUNWAY_CLOSED injected event), plus its own seeded background traffic (55%) and
wake classes (Heavy/Large/Small 15/80/5).

Wake separation (CONSTRAINTS.md 2, WAKE-4 as given): an arrival of `own_class` at t on runway r
is separated when it is at least `constraints.wake_minutes` behind the nearest earlier movement
on r and ahead of the nearest later one. Movements are r's background traffic and other events'
HELD/COMMITTED slots on r read through the ledger view (their stored class, WAKE-7).
`free_slots_after(..., runway=r)` returns only such points. Every runway method takes
`runway=None` for the merged stream of all runways (the old conservative model).

One `AirportWorld` per (airport, date, injected set): the runways, a pool of
`constraints.WORLD_GATE_POOL` gates with background busy intervals (CAP-4), and
equipment-failure intervals. It is built once from the full injected list and cached; nothing
here calls an LLM or reads the ledger except through the `LedgerView` the caller passes in.

Times are int minutes after local midnight; intervals are half-open [start, end).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from acacia import config, constraints
from acacia.models import Injected, LedgerView
from acacia.runway import (  # noqa: F401  (re-exported for simulate, checker and tests)
    TRAFFIC_PROBABILITY, WAKE_CLASSES, Runway, held_slot_class, separation_reach, separation_verdict,
    slot_taken,
)
from acacia.timeutil import to_min

Interval = tuple[int, int]

DEFAULT_STORM = "moderate"  # CAP-8: a RUNWAY_CLOSED window without a severity
OTHER_EVENT_CLASS = constraints.DEFAULT_OWN_CLASS  # default own and traffic class; held slots: held_slot_class
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
        self._runways: dict[str, Runway] = {
            rid: Runway(self.airport, self.date, rid, k, self.storm_at)
            for k, rid in enumerate(constraints.runway_ids(self.airport))
        }
        self._merged: list[int] = sorted({p for r in self._runways.values() for p in r.grid})
        rng = config.seeded_rng(config.seed_key("world", self.airport, self.date))
        self._gates: list[str] = self._draw_gates(rng)
        self._busy: dict[str, list[Interval]] = {g: self._draw_busy(rng) for g in self._gates}
        self._failed: dict[str, list[Interval]] = {g: [] for g in self._gates}
        self._apply_failures()

    # --- construction ---------------------------------------------------------------------
    def storm_at(self, t: int) -> constraints.Storm:
        """Storm level at t: the most severe RUNWAY_CLOSED window containing t, else "none"."""
        levels = {sev for s, e, sev in self._storms if s <= t < e}
        return "severe" if "severe" in levels else "moderate" if levels else "none"

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
    def runways(self) -> list[str]:
        """Arrival runway ids "R1".."Rn" (CAP-9)."""
        return list(self._runways)

    def _runway(self, runway: str) -> Runway:
        try:
            return self._runways[runway]
        except KeyError:
            raise KeyError(f"unknown runway {runway!r} at {self.airport} (have {self.runways()})") from None

    def _selected(self, runway: str | None) -> list[Runway]:
        return list(self._runways.values()) if runway is None else [self._runway(runway)]

    def grid(self, runway: str | None = None) -> list[int]:
        """Grid points of `runway`; None: the merged points of every runway."""
        return list(self._merged) if runway is None else list(self._runway(runway).grid)

    def closures(self) -> list[Interval]:
        return list(self._closures)

    def closure_at(self, t: int) -> bool:
        return _contains(self._closures, t)

    def traffic_class(self, t: int, runway: str | None = None) -> str | None:
        """Seeded wake class of the background movement at t on `runway` (WAKE-3); with None the
        first runway that has one; None when t carries none."""
        return next((c for r in self._selected(runway) if (c := r.traffic_class(t)) is not None), None)

    def is_traffic(self, t: int, runway: str | None = None) -> bool:
        return any(r.is_traffic(t) for r in self._selected(runway))

    def traffic_times(self, start: int, end: int, runway: str | None = None) -> list[int]:
        """Grid points in [start, end) that carry background traffic on `runway` (None: any)."""
        return [p for p in self.grid(runway) if start <= p < end and self.is_traffic(p, runway)]

    def _movements_near(self, t: int, view: LedgerView, exclude_event: str | None,
                        runway: str | None) -> dict[int, str]:
        """Traffic and other events' held slots on `runway` within the separation reach of t."""
        reach = separation_reach()
        out: dict[int, str] = {}
        for p in range(max(0, t - reach), min(config.MINUTES_PER_DAY, t + reach + 1)):
            cls = self.traffic_class(p, runway) or held_slot_class(view, self.airport, p, exclude_event, runway)
            if cls is not None:
                out[p] = cls
        return out

    def is_separated(
        self, t: int, own_class: str, view: LedgerView, exclude_event: str | None = None,
        runway: str | None = None,
    ) -> tuple[bool, str]:
        """Whether an arrival of `own_class` at t on `runway` keeps the workbook wake minimum to the
        nearest earlier and later movement on that runway (traffic or another event's held slot).
        Returns (ok, detail); `exclude_event` leaves that event's own slots out (re-checks of a held
        package). runway=None checks the merged stream of every runway (conservative)."""
        if not isinstance(own_class, str) or not own_class:
            raise ValueError(f"own_class must be a non-empty wake class, got {own_class!r}")
        if runway is not None:
            self._runway(runway)
        return separation_verdict(t, own_class, self._movements_near(t, view, exclude_event, runway))

    def _eligible(self, t: int, view: LedgerView, own_class: str, runway: str | None) -> Iterable[int]:
        return (
            p for p in self.grid(runway)
            if p >= t and not self.is_traffic(p, runway) and not slot_taken(view, self.airport, p, None, runway)
            and self.is_separated(p, own_class, view, runway=runway)[0]
        )

    def free_slots_after(
        self, t: int, n: int, view: LedgerView, own_class: str = OTHER_EVENT_CLASS,
        runway: str | None = None,
    ) -> list[int]:
        """Up to n free, wake-separated grid points >= t on `runway` for an arrival of
        `own_class`, skipping rand(0..1) eligible points between returned slots (the seeded skip,
        key suffixed with the runway). runway=None: the merged stream (legacy, conservative)."""
        if n <= 0:
            return []
        parts = ("slots", self.airport, self.date, t) + (() if runway is None else (runway,))
        rng = config.seeded_rng(config.seed_key(*parts))
        slots: list[int] = []
        skip = 0
        for point in self._eligible(t, view, own_class, runway):
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
