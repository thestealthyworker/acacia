"""HANDOVER 14 test 4 (private-state part): the same event and seed give identical private
state; plus the HANDOVER 6 / 18.2 formulas read from the shared world and the ledger."""

from __future__ import annotations

import pytest

from acacia import config
from acacia.ledger import Ledger
from acacia.models import EmptyLedgerView, Stress
from acacia.simulate import build_private, land_by_and_first_slot
from acacia.timeutil import to_min
from acacia.triangulate import triangulate
from acacia.world import AirportWorld

EMPTY = EmptyLedgerView()


@pytest.fixture
def event(dl1639_row):
    return triangulate(dl1639_row)  # DCA -> ATL, W = 30 (runway closure), Ti = 10


@pytest.fixture
def world(event) -> AirportWorld:
    return AirportWorld(event.flight.dest, event.flight.date, event.injected)


@pytest.fixture
def calm_event(make_row):
    return triangulate(make_row(WEATHER_DELAY="0", LATE_AIRCRAFT_DELAY="51"))


@pytest.fixture
def calm_world(calm_event) -> AirportWorld:
    return AirportWorld(calm_event.flight.dest, calm_event.flight.date, calm_event.injected)


def test_same_event_and_seed_give_identical_private_state(event) -> None:
    a = build_private(event, Stress(), AirportWorld("ATL", event.flight.date, event.injected), EMPTY)
    b = build_private(event, Stress(), AirportWorld("ATL", event.flight.date, event.injected), EMPTY)
    assert a.model_dump() == b.model_dump()


def test_salt_changes_private_state(event, world, monkeypatch) -> None:
    base = build_private(event, None, world, EMPTY)
    monkeypatch.setattr(config, "SEED_SALT", "other-salt")
    other_world = AirportWorld("ATL", event.flight.date, event.injected)
    assert build_private(event, None, other_world, EMPTY).model_dump() != base.model_dump()


def test_atc_weather_queue_and_closure_slots(event, world) -> None:
    atc = build_private(event, None, world, EMPTY).atc
    eta = to_min(event.eta_landing)
    assert atc.first == eta + round(30 * 0.6)
    assert len(atc.free_slots) == 3  # closure overlaps [E, first]
    assert atc.free_slots == world.free_slots_after(atc.first, 3, EMPTY)
    assert all(s >= atc.first and not world.is_traffic(s) for s in atc.free_slots)
    assert atc.min_separation == 3 and atc.closures


def test_atc_demand_traffic_and_no_closure(calm_event, calm_world) -> None:
    atc = build_private(calm_event, None, calm_world, EMPTY).atc
    eta = to_min(calm_event.eta_landing)
    assert 3 <= atc.first - eta <= 8
    assert len(atc.free_slots) == 4 and atc.closures == []
    assert set(atc.demand) == set(atc.free_slots)
    for i, slot in enumerate(atc.free_slots):
        assert max(1, min(4, 4 - i)) <= atc.demand[slot] <= max(1, min(4, 5 - i))
    assert atc.traffic == calm_world.traffic_times(eta - 24, eta + 151)


def test_ledger_held_slot_is_skipped_and_becomes_traffic(event, world, tmp_path) -> None:
    base = build_private(event, None, world, EMPTY).atc
    ledger = Ledger(event.flight.date, runs_dir=tmp_path)
    taken = base.free_slots[0]
    ledger.hold("OTHER", "ATL", taken, "A1", 1200, 1240)
    atc = build_private(event, None, world, ledger.view()).atc
    assert taken not in atc.free_slots and taken in atc.traffic


def test_pilot_values(event, world) -> None:
    p = build_private(event, None, world, EMPTY)
    eta, sched = to_min(event.eta_landing), to_min(event.sched_arrival)
    assert eta + 45 <= p.pilot.land_by <= eta + 74
    assert 24 <= p.pilot.pax <= 60  # ATL is a hub
    assert sched + 28 <= p.pilot.connect_by <= sched + 55
    assert p.pilot.low_fuel is False


def test_low_fuel_changes_only_land_by(event, world) -> None:
    base = build_private(event, None, world, EMPTY)
    fuel = build_private(event, Stress(low_fuel=True), world, EMPTY)
    assert fuel.pilot.land_by == fuel.atc.first - 3 and fuel.pilot.low_fuel
    assert fuel.pilot.pax == base.pilot.pax and fuel.pilot.connect_by == base.pilot.connect_by
    assert fuel.ground == base.ground and fuel.atc == base.atc


def test_non_hub_pax_range(make_row, sample_store) -> None:
    ev = triangulate(make_row(DESTINATION_AIRPORT="BWI"))
    w = AirportWorld("BWI", ev.flight.date, ev.injected)
    assert 6 <= build_private(ev, None, w, EMPTY).pilot.pax <= 30


def test_ground_gates_and_free_from_come_from_world(event, world) -> None:
    g = build_private(event, None, world, EMPTY).ground
    t = to_min(event.eta_landing) + event.taxi_in
    assert g.min_turn == 35  # distance 547
    assert len(g.gates) == 3 and len(set(g.gates)) == 3 and g.gates[0] == g.assigned_gate
    assert set(g.gates) <= set(world.gates())
    for gate in g.gates:
        assert g.free_from[gate] == world.gate_free_from(gate, t, EMPTY, duration=g.min_turn)
    others = [x for x in world.gates() if x != g.assigned_gate]
    best = sorted(world.gate_free_from(x, t, EMPTY, duration=35) or 10**6 for x in others)[:2]
    assert sorted(g.free_from[x] or 10**6 for x in g.gates[1:]) == best


def test_ground_shift_duty_and_next_departure(event, world) -> None:
    g = build_private(event, None, world, EMPTY).ground
    t = to_min(event.eta_landing) + event.taxi_in
    sched = to_min(event.sched_arrival)
    assert t + 35 + 25 <= g.shift_end <= t + 35 + 85
    assert g.duty_limit == g.shift_end + 45
    assert sched + 35 + 20 <= g.next_departure <= sched + 35 + 60


def test_tight_crew_changes_only_shift(event, world) -> None:
    base = build_private(event, None, world, EMPTY)
    tight = build_private(event, Stress(tight_crew=True), world, EMPTY)
    assert tight.ground.shift_end == tight.atc.first + event.taxi_in + 35 - 6
    assert tight.ground.duty_limit == tight.ground.shift_end + 45
    assert tight.ground.next_departure == base.ground.next_departure
    assert tight.pilot == base.pilot and tight.atc == base.atc


def test_failed_gate_has_no_free_from(event, monkeypatch, world) -> None:
    g = build_private(event, None, world, EMPTY).ground
    gate = g.assigned_gate
    t = to_min(event.eta_landing) + event.taxi_in
    monkeypatch.setitem(world._failed, gate, [(t - 20, t + 180)])
    failed = build_private(event, None, world, EMPTY).ground
    assert failed.free_from[gate] is None and gate in failed.failed


def test_ledger_gate_booking_shifts_free_from(event, world, tmp_path) -> None:
    g = build_private(event, None, world, EMPTY).ground
    gate = g.gates[1]
    start = g.free_from[gate]
    assert start is not None
    ledger = Ledger(event.flight.date, runs_dir=tmp_path)
    ledger.hold("OTHER", "ATL", 300, gate, start, start + 40)
    shifted = build_private(event, None, world, ledger.view()).ground
    assert shifted.free_from.get(gate) is None or shifted.free_from[gate] >= start + 40


def test_world_must_match_event(event) -> None:
    with pytest.raises(ValueError):
        build_private(event, None, AirportWorld("DFW", event.flight.date, []), EMPTY)


def test_land_by_and_first_slot_matches_private_state(event, world) -> None:
    p = build_private(event, Stress(low_fuel=True), world, EMPTY)
    assert land_by_and_first_slot(event, Stress(low_fuel=True), world, EMPTY) == (
        p.pilot.land_by, p.atc.free_slots[0])
