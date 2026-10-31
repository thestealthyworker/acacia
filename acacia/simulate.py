"""Seeded simulated private state (HANDOVER 6, 18.2; PLAN 3.5).

Queue, pilot state, crew shift and red-line inputs are drawn per event from one rng seeded
`"{SALT}|{event_id}|private"`. The runway grid and gate pool come from the shared
`AirportWorld`, filtered through a `LedgerView`, so events at the same airport compete for
the same slots and gates.

Draw order (fixed, so runs repeat): queue rand(3..8); demand rand(0..1) per free slot;
land_by rand(45..74); pax; connect_by rand(28..55); assigned gate choice; shift rand(25..85);
next_departure rand(20..60). Every draw is always made, even when a weather queue or a
stress flag overrides it, so a stress flag changes only the value it targets.

Everything here is simulated and must be labelled so in the UI.
"""

from __future__ import annotations

import random

from acacia import config
from acacia.models import (
    AtcPrivate, DisruptionEvent, GroundPrivate, LedgerView, PilotPrivate, PrivateState, Stress,
)
from acacia.timeutil import to_min
from acacia.world import AirportWorld

QUEUE_WEATHER_FACTOR = 0.6
QUEUE_RANGE = (3, 8)
SLOTS_WITH_CLOSURE = 3
SLOTS_WITHOUT_CLOSURE = 4
DEMAND_MAX = 4
DEMAND_MIN = 1
TRAFFIC_BEFORE_E = 24
TRAFFIC_AFTER_E = 150
LOW_FUEL_MARGIN = 3
LAND_BY_RANGE = (45, 74)
HUB_PAX_RANGE = (24, 60)
OTHER_PAX_RANGE = (6, 30)
CONNECT_BY_RANGE = (28, 55)
GATES_PER_EVENT = 3
TIGHT_CREW_SLACK = 6
SHIFT_RANGE = (25, 85)
DUTY_EXTENSION = 45
NEXT_DEPARTURE_RANGE = (20, 60)


def private_rng(event: DisruptionEvent) -> random.Random:
    """The per-event private-state rng: seed key "{SALT}|{event_id}|private"."""
    return config.seeded_rng(config.seed_key(event.event_id, "private"))


def _check_world(event: DisruptionEvent, world: AirportWorld) -> None:
    if world.airport != event.flight.dest.upper() or world.date != event.flight.date:
        raise ValueError(
            f"world {world.airport} {world.date} does not match event {event.event_id} "
            f"({event.flight.dest} {event.flight.date})"
        )


def _queue(event: DisruptionEvent, rng: random.Random) -> int:
    drawn = rng.randint(*QUEUE_RANGE)
    weather = event.causes.weather
    return round(weather * QUEUE_WEATHER_FACTOR) if weather > config.WEATHER_THRESHOLD else drawn


def _closure_near(world: AirportWorld, eta: int, first: int) -> bool:
    """A runway closure overlaps [E, first] (the window the queue waits through)."""
    return any(start <= first and eta < end for start, end in world.closures())


def _traffic(world: AirportWorld, eta: int, view: LedgerView) -> list[int]:
    """Background traffic plus slots other events hold, from E - 24 to E + 150."""
    start = max(0, eta - TRAFFIC_BEFORE_E)
    end = min(config.MINUTES_PER_DAY, eta + TRAFFIC_AFTER_E + 1)
    held = [p for p in world.grid() if start <= p < end and view.slot_taken(world.airport, p)]
    return sorted(set(world.traffic_times(start, end)) | set(held))


def _atc(event: DisruptionEvent, world: AirportWorld, view: LedgerView, rng: random.Random) -> AtcPrivate:
    eta = to_min(event.eta_landing)
    first = eta + _queue(event, rng)
    closure = _closure_near(world, eta, first)
    count = SLOTS_WITH_CLOSURE if closure else SLOTS_WITHOUT_CLOSURE
    slots = world.free_slots_after(first, count, view)
    demand = {
        slot: max(DEMAND_MIN, min(DEMAND_MAX, DEMAND_MAX - i + rng.randint(0, 1)))
        for i, slot in enumerate(slots)
    }
    window_end = eta + TRAFFIC_AFTER_E
    closures = [(s, e) for s, e in world.closures() if s <= window_end and eta - TRAFFIC_BEFORE_E < e]
    return AtcPrivate(first=first, free_slots=slots, demand=demand,
                      traffic=_traffic(world, eta, view), closures=closures,
                      min_separation=config.MIN_SEPARATION)


def _pilot(event: DisruptionEvent, stress: Stress, first: int, rng: random.Random) -> PilotPrivate:
    eta = to_min(event.eta_landing)
    land_by_drawn = eta + rng.randint(*LAND_BY_RANGE)
    land_by = first - LOW_FUEL_MARGIN if stress.low_fuel else land_by_drawn
    pax_range = HUB_PAX_RANGE if event.flight.dest.upper() in config.HUBS else OTHER_PAX_RANGE
    pax = rng.randint(*pax_range)
    connect_by = to_min(event.sched_arrival) + rng.randint(*CONNECT_BY_RANGE)
    return PilotPrivate(land_by=land_by, pax=pax, connect_by=connect_by, low_fuel=stress.low_fuel)


def _pick_gates(world: AirportWorld, assigned: str, free_from: dict[str, int | None]) -> list[str]:
    """The assigned gate, then the two others with the earliest free time (pool order on ties)."""
    pool = world.gates()
    others = sorted(
        (g for g in pool if g != assigned),
        key=lambda g: (free_from[g] is None, free_from[g] or 0, pool.index(g)),
    )
    return [assigned, *others[: GATES_PER_EVENT - 1]]


def _ground(event: DisruptionEvent, stress: Stress, first: int, world: AirportWorld,
            view: LedgerView, rng: random.Random) -> GroundPrivate:
    eta, ti = to_min(event.eta_landing), event.taxi_in
    gate_arrival = eta + ti
    turn = config.min_turn(event.distance)
    assigned = rng.choice(world.gates())
    free_all = {g: world.gate_free_from(g, gate_arrival, view, duration=turn) for g in world.gates()}
    gates = _pick_gates(world, assigned, free_all)
    shift_drawn = gate_arrival + turn + rng.randint(*SHIFT_RANGE)
    shift_end = first + ti + turn - TIGHT_CREW_SLACK if stress.tight_crew else shift_drawn
    next_departure = to_min(event.sched_arrival) + turn + rng.randint(*NEXT_DEPARTURE_RANGE)
    return GroundPrivate(
        gates=gates, assigned_gate=assigned, free_from={g: free_all[g] for g in gates},
        failed=[g for g in gates if world.is_failed(g, gate_arrival)], min_turn=turn,
        shift_end=shift_end, duty_limit=shift_end + DUTY_EXTENSION, next_departure=next_departure,
    )


def build_private(event: DisruptionEvent, stress: Stress | None, world: AirportWorld,
                  ledger_view: LedgerView) -> PrivateState:
    """Ground-truth private state for one event, read from the world through the ledger view."""
    _check_world(event, world)
    stress = stress or Stress()
    rng = private_rng(event)
    atc = _atc(event, world, ledger_view, rng)
    pilot = _pilot(event, stress, atc.first, rng)
    ground = _ground(event, stress, atc.first, world, ledger_view, rng)
    return PrivateState(atc=atc, pilot=pilot, ground=ground)


def land_by_and_first_slot(event: DisruptionEvent, stress: Stress | None, world: AirportWorld,
                           view: LedgerView) -> tuple[int, int]:
    """(land_by, first free slot) for triage's fuel-margin tier rule.

    With no free slot left in the day, the first slot is reported as 1440 (end of day), so
    the fuel margin is negative and the trigger is tier 0.
    """
    private = build_private(event, stress, world, view)
    slots = private.atc.free_slots
    first_slot = min(slots) if slots else config.MINUTES_PER_DAY
    return private.pilot.land_by, first_slot
