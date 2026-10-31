"""Seeded simulated private state (HANDOVER 6, 18.2; PLAN 3.5; CONSTRAINTS.md 2-5).

Queue, pilot pax and connections, crew shift and red-line inputs are drawn per event from one
rng seeded `"{SALT}|{event_id}|private"`. The runway grid, traffic and gate pool come from the
shared `AirportWorld`, filtered through a `LedgerView`, so events at the same airport compete
for the same slots and gates. Workbook limits (wake table, turnaround, spare gates, fuel) come
only from `acacia.constraints`.

Draw order (fixed, so runs repeat): queue rand(3..8); demand rand(0..1) per free slot; pax;
connect_by rand(28..55); assigned gate choice; shift rand(25..85); next_departure rand(20..60).
Every draw is always made, even when a weather queue or a stress flag overrides it, so a
stress flag changes only the value it targets. land_by is no longer drawn: it is
E + 45 + contingency (CONSTRAINTS.md 4).

Everything here is simulated and must be labelled so in the UI.
"""

from __future__ import annotations

import random

from acacia import config, constraints
from acacia.models import (
    AtcPrivate, DisruptionEvent, GroundPrivate, LedgerView, PilotPrivate, PrivateState, Stress,
)
from acacia.timeutil import to_min
from acacia.world import OTHER_EVENT_CLASS, AirportWorld, separation_reach

QUEUE_WEATHER_FACTOR = 0.6
QUEUE_RANGE = (3, 8)
SLOTS_WITH_CLOSURE = 3
SLOTS_WITHOUT_CLOSURE = 4
DEMAND_MAX = 4
DEMAND_MIN = 1
TRAFFIC_BEFORE_E = 24
TRAFFIC_AFTER_E = 150
LOW_FUEL_MARGIN = 3
HUB_PAX_RANGE = (24, 60)
OTHER_PAX_RANGE = (6, 30)
CONNECT_BY_RANGE = (28, 55)
TIGHT_CREW_SLACK = 6
SHIFT_RANGE = (25, 85)
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
    """A runway closure (storm window) overlaps [E, first], the window the queue waits through."""
    return any(start <= first and eta < end for start, end in world.closures())


# --- runway: traffic and wake separation (CONSTRAINTS.md 2) -----------------------------------
def _neighbours(world: AirportWorld, view: LedgerView, start: int, end: int) -> dict[int, str]:
    """Traffic (seeded wake class) and other events' held slots (Large, WAKE-7) in [start, end)."""
    start, end = max(0, start), min(config.MINUTES_PER_DAY, end)
    out = {t: world.traffic_class(t) or OTHER_EVENT_CLASS for t in world.traffic_times(start, end)}
    for p in world.grid():
        if start <= p < end and p not in out and view.slot_taken(world.airport, p):
            out[p] = OTHER_EVENT_CLASS
    return out


def _atc(event: DisruptionEvent, world: AirportWorld, view: LedgerView, rng: random.Random) -> AtcPrivate:
    eta = to_min(event.eta_landing)
    first = eta + _queue(event, rng)
    closure = _closure_near(world, eta, first)
    own = constraints.wake_class(event.distance)
    count = SLOTS_WITH_CLOSURE if closure else SLOTS_WITHOUT_CLOSURE
    slots = world.free_slots_after(first, count, view, own_class=own)  # wake-separated only
    demand = {
        slot: max(DEMAND_MIN, min(DEMAND_MAX, DEMAND_MAX - i + rng.randint(0, 1)))
        for i, slot in enumerate(slots)
    }
    reach = separation_reach()
    window_end = max([eta + TRAFFIC_AFTER_E, *(s + reach for s in slots)])
    traffic = _neighbours(world, view, eta - TRAFFIC_BEFORE_E, window_end + 1)
    closures = [(s, e) for s, e in world.closures() if s <= window_end and eta - TRAFFIC_BEFORE_E < e]
    return AtcPrivate(first=first, free_slots=slots, demand=demand, traffic=sorted(traffic),
                      closures=closures, min_separation=config.MIN_SEPARATION, own_class=own,
                      traffic_classes=dict(sorted(traffic.items())))


# --- pilot (CONSTRAINTS.md 4) -------------------------------------------------------------------
def _pilot(event: DisruptionEvent, stress: Stress, first: int, rng: random.Random) -> PilotPrivate:
    eta = to_min(event.eta_landing)
    fuel_limit = constraints.land_by(eta, event.air_time, event.scheduled_time)
    land_by = first - LOW_FUEL_MARGIN if stress.low_fuel else fuel_limit
    pax_range = HUB_PAX_RANGE if event.flight.dest.upper() in config.HUBS else OTHER_PAX_RANGE
    pax = rng.randint(*pax_range)
    connect_by = to_min(event.sched_arrival) + rng.randint(*CONNECT_BY_RANGE)
    crew_end = constraints.crew_duty_end(to_min(event.sched_arrival), event.scheduled_time)
    wake = constraints.wake_class(event.distance)
    return PilotPrivate(land_by=land_by, pax=pax, connect_by=connect_by, low_fuel=stress.low_fuel,
                        crew_duty_end=crew_end, wake_class=wake,
                        burn_kg_per_h=constraints.burn_kg_per_h(wake))


# --- ground (CONSTRAINTS.md 3, 5) -------------------------------------------------------------
def _pick_gates(world: AirportWorld, assigned: str, free_from: dict[str, int | None],
                spare: int) -> list[str]:
    """The assigned gate, then `spare` others with the earliest free time (pool order on ties)."""
    pool = world.gates()
    others = sorted(
        (g for g in pool if g != assigned),
        key=lambda g: (free_from[g] is None, free_from[g] or 0, pool.index(g)),
    )
    return [assigned, *others[:max(0, spare)]]


def _ground(event: DisruptionEvent, stress: Stress, first: int, world: AirportWorld,
            view: LedgerView, rng: random.Random, last_slot: int | None = None) -> GroundPrivate:
    eta, ti = to_min(event.eta_landing), event.taxi_in
    gate_arrival = eta + ti
    limits = constraints.airport_limits(world.airport)
    turn = limits.min_turnaround_min
    assigned = rng.choice(world.gates())
    # G_H1 is one free-from time per gate, so it must hold for the turnaround from every offered
    # slot at or after it: ask for a window that also spans up to the last offered gate arrival.
    span = turn + max(0, (last_slot + ti - gate_arrival) if last_slot is not None else 0)
    free_all = {g: world.gate_free_from(g, gate_arrival, view, duration=span) for g in world.gates()}
    gates = _pick_gates(world, assigned, free_all, limits.spare_gates)
    shift_drawn = gate_arrival + turn + rng.randint(*SHIFT_RANGE)
    shift_end = first + ti + turn - TIGHT_CREW_SLACK if stress.tight_crew else shift_drawn
    next_departure = to_min(event.sched_arrival) + turn + rng.randint(*NEXT_DEPARTURE_RANGE)
    return GroundPrivate(
        gates=gates, assigned_gate=assigned, free_from={g: free_all[g] for g in gates},
        failed=[g for g in gates if world.is_failed(g, gate_arrival)], min_turn=turn,
        shift_end=shift_end, duty_limit=shift_end + constraints.GROUND_MAX_OVERTIME_MIN,
        next_departure=next_departure, shift_start=shift_end - constraints.GROUND_SHIFT_MIN,
        spare_gates=limits.spare_gates,
    )


def build_private(event: DisruptionEvent, stress: Stress | None, world: AirportWorld,
                  ledger_view: LedgerView) -> PrivateState:
    """Ground-truth private state for one event, read from the world through the ledger view."""
    _check_world(event, world)
    stress = stress or Stress()
    rng = private_rng(event)
    atc = _atc(event, world, ledger_view, rng)
    pilot = _pilot(event, stress, atc.first, rng)
    ground = _ground(event, stress, atc.first, world, ledger_view, rng,
                     last_slot=max(atc.free_slots, default=None))
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
