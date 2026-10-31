"""HANDOVER 14 test 4 (private-state part): the same event and seed give identical private
state; plus the HANDOVER 6 / 18.2 formulas read from the shared world and the ledger, with the
CONSTRAINTS.md workbook numbers (wake table, per-airport turnaround and spare gates, land_by)."""

from __future__ import annotations

import pytest

from acacia import config, constraints
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


def _wake_ok(slot: int, own: str, atc) -> bool:
    """CONSTRAINTS.md 2: nearest previous and next traffic, wake table at 140 kt."""
    prev = [t for t in atc.traffic if t <= slot]
    nxt = [t for t in atc.traffic if t > slot]
    ok_prev = not prev or slot - max(prev) >= constraints.wake_minutes(atc.traffic_classes[max(prev)], own)
    ok_next = not nxt or min(nxt) - slot >= constraints.wake_minutes(own, atc.traffic_classes[min(nxt)])
    return ok_prev and ok_next


def test_atc_weather_queue_and_closure_slots(event, world) -> None:
    atc = build_private(event, None, world, EMPTY).atc
    eta = to_min(event.eta_landing)
    assert atc.first == eta + round(30 * 0.6)
    assert len(atc.free_slots) == 3  # closure overlaps [E, first]
    assert atc.free_slots == world.free_slots_after(atc.first, 3, EMPTY, own_class="Large")
    assert all(world.is_separated(s, "Large", EMPTY)[0] for s in atc.free_slots)
    assert all(s >= atc.first and not world.is_traffic(s) for s in atc.free_slots)
    assert atc.closures


def test_atc_wake_classes(event, world, make_row) -> None:
    atc = build_private(event, None, world, EMPTY).atc
    assert atc.own_class == "Large"  # distance 547 <= 2000 (WAKE-3)
    assert set(atc.traffic_classes) == set(atc.traffic)
    assert set(atc.traffic_classes.values()) <= {"Super", "Heavy", "Large", "Small"}
    assert all(_wake_ok(s, atc.own_class, atc) for s in atc.free_slots)
    heavy = triangulate(make_row(DISTANCE="2100"))
    assert build_private(heavy, None, world, EMPTY).atc.own_class == "Heavy"


def test_atc_asks_the_world_for_slots_separated_for_its_own_class(event, world, make_row,
                                                                  monkeypatch) -> None:
    """ATC declares exactly the world's wake-separated free slots for its own class (3 near a storm)."""
    calls: list[tuple[int, int, str]] = []
    real = world.free_slots_after

    def spy(t, n, view, own_class="Large"):
        calls.append((t, n, own_class))
        return real(t, n, view, own_class=own_class)

    monkeypatch.setattr(world, "free_slots_after", spy)
    heavy = triangulate(make_row(DISTANCE="2100"))
    atc = build_private(heavy, None, world, EMPTY).atc
    assert calls == [(atc.first, 3, "Heavy")]
    assert all(world.is_separated(s, "Heavy", EMPTY)[0] for s in atc.free_slots)


def test_traffic_classes_come_from_the_world(event, world) -> None:
    atc = build_private(event, None, world, EMPTY).atc
    assert atc.traffic_classes == {t: world.traffic_class(t) for t in atc.traffic}


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
    assert p.pilot.land_by == eta + 45 + round(0.05 * 125)  # FUEL-2/3: AIR_TIME 125 from the row
    # CREW-1: report = scheduled arrival - SCHEDULED_TIME (120) - 60, plus 13 h
    assert p.pilot.crew_duty_end == sched - 120 - 60 + 780
    assert 24 <= p.pilot.pax <= 60  # ATL is a hub
    assert sched + 28 <= p.pilot.connect_by <= sched + 55
    assert p.pilot.low_fuel is False


def test_land_by_falls_back_to_scheduled_time_and_none_without_times(event, world) -> None:
    sched_only = event.model_copy(update={"air_time": None, "scheduled_time": 120})
    p = build_private(sched_only, None, world, EMPTY).pilot
    assert p.land_by == to_min(event.eta_landing) + 45 + round(0.05 * 100)  # SCHEDULED_TIME - 20
    bare = build_private(event.model_copy(update={"air_time": None, "scheduled_time": None}),
                         None, world, EMPTY).pilot
    assert bare.land_by == to_min(event.eta_landing) + 45 and bare.crew_duty_end is None


def test_land_by_adds_contingency_from_air_time(event, world) -> None:
    timed = event.model_copy(update={"air_time": 125})
    p = build_private(timed, None, world, EMPTY)
    assert p.pilot.land_by == to_min(event.eta_landing) + 45 + round(0.05 * 125)


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
    private = build_private(event, None, world, EMPTY)
    g = private.ground
    t = to_min(event.eta_landing) + event.taxi_in
    span = g.min_turn + max(0, max(private.atc.free_slots) + event.taxi_in - t)  # every offered slot
    assert g.min_turn == 45  # CAP-6: ATL workbook turnaround, not the distance rule (35)
    assert len(g.gates) == 1 + 3  # CAP-5: assigned + ATL's 3 spare gates
    assert len(set(g.gates)) == len(g.gates) and g.gates[0] == g.assigned_gate
    assert set(g.gates) <= set(world.gates())
    for gate in g.gates:
        assert g.free_from[gate] == world.gate_free_from(gate, t, EMPTY, duration=span)
    others = [x for x in world.gates() if x != g.assigned_gate]
    best = sorted(world.gate_free_from(x, t, EMPTY, duration=span) or 10**6 for x in others)[:3]
    assert sorted(g.free_from[x] or 10**6 for x in g.gates[1:]) == best


def test_non_atl_turnaround_and_spare_gates(make_row) -> None:
    ev = triangulate(make_row(DESTINATION_AIRPORT="BWI"))
    g = build_private(ev, None, AirportWorld("BWI", ev.flight.date, ev.injected), EMPTY).ground
    lim = constraints.airport_limits("BWI")
    assert g.min_turn == lim.min_turnaround_min
    assert len(g.gates) == 1 + lim.spare_gates


def test_ground_shift_duty_and_next_departure(event, world) -> None:
    g = build_private(event, None, world, EMPTY).ground
    t = to_min(event.eta_landing) + event.taxi_in
    sched = to_min(event.sched_arrival)
    assert t + 45 + 25 <= g.shift_end <= t + 45 + 85
    assert g.duty_limit == g.shift_end + 45  # CREW-3: 45 min max overtime
    assert g.shift_start == g.shift_end - 480  # CREW-2: 8 h shift
    assert g.spare_gates == 3  # CAP-5: ATL
    assert sched + 45 + 20 <= g.next_departure <= sched + 45 + 60


def test_tight_crew_changes_only_shift(event, world) -> None:
    base = build_private(event, None, world, EMPTY)
    tight = build_private(event, Stress(tight_crew=True), world, EMPTY)
    assert tight.ground.shift_end == tight.atc.first + event.taxi_in + 45 - 6
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


def test_pilot_wake_class_burn_and_crew_duty_end(event, world, make_row) -> None:
    # CONSTRAINTS.md 4: duty end = (scheduled departure - 60) + 780, scheduled departure = S - SCHEDULED_TIME
    p = build_private(event, None, world, EMPTY).pilot
    assert p.wake_class == "Large" and p.burn_kg_per_h == 2400
    assert p.crew_duty_end == (to_min(event.sched_arrival) - event.scheduled_time - 60) + 780
    heavy = build_private(triangulate(make_row(DISTANCE="2100")), None, world, EMPTY).pilot
    assert heavy.wake_class == "Heavy" and heavy.burn_kg_per_h == 6500


def test_declared_gate_free_from_holds_for_every_offered_slot(sample_store) -> None:
    """G_H1 is a single free-from time per gate, so it must be true for the whole turnaround
    from every offered slot at or after it (validator G3: AA8 HNL-DFW had a checker GATE_CONFLICT
    on a busy interval that started after the declared free time)."""
    for _, row in sample_store.qualifying_rows().iterrows():
        ev = triangulate(row.to_dict())
        w = AirportWorld(ev.flight.dest, ev.flight.date, ev.injected)
        p = build_private(ev, None, w, EMPTY)
        for gate, free in p.ground.free_from.items():
            if free is None:
                continue
            for slot in p.atc.free_slots:
                arr = slot + ev.taxi_in
                if arr >= free:
                    assert w.gate_free_from(gate, arr, EMPTY, duration=p.ground.min_turn) == arr, (
                        ev.flight.code, gate, slot)
