"""HANDOVER 14 test 11: the fallback always returns a plan, diverts when the earliest slot is
past land_by, and never picks a failed gate."""

from __future__ import annotations

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from acacia.fallback import haversine_mi, nearest_alternate, plan_fallback
from acacia.models import AtcPrivate, FallbackPlan, GroundPrivate, PilotPrivate, PrivateState
from acacia.timeutil import to_min
from acacia.triangulate import triangulate


@pytest.fixture
def event(dl1639_row):
    return triangulate(dl1639_row)  # DCA -> ATL, Ti = 10


@pytest.fixture
def airports(sample_store) -> pd.DataFrame:
    return sample_store.airports


def _private(free_slots=(740, 748), land_by=790, free_from=None, failed=()) -> PrivateState:
    free_from = free_from or {"A7": 761, "A10": 701, "A12": 745}
    return PrivateState(
        atc=AtcPrivate(first=740, free_slots=list(free_slots), demand={s: 1 for s in free_slots},
                       traffic=[744], closures=[]),
        pilot=PilotPrivate(land_by=land_by, pax=30, connect_by=760),
        ground=GroundPrivate(gates=list(free_from), assigned_gate=next(iter(free_from)),
                             free_from=free_from, failed=list(failed), min_turn=35,
                             shift_end=816, duty_limit=861, next_departure=820),
    )


def test_fcfs_slot_and_earliest_free_gate(event, airports) -> None:
    plan = plan_fallback(event, _private(free_slots=(748, 740)), airports)
    assert isinstance(plan, FallbackPlan) and not plan.divert
    assert plan.slot == "12:20"          # earliest free slot
    assert plan.gate == "A10"            # earliest free time, free by gate arrival 12:30
    assert plan.check is None and plan.event_id == event.event_id


def test_never_picks_failed_gate(event, airports) -> None:
    p = _private(free_from={"A7": 761, "A10": None, "A12": 745}, failed=["A10"])
    assert plan_fallback(event, p, airports).gate == "A12"
    p = _private(free_from={"A7": 761, "A10": 701, "A12": 745}, failed=["A10"])
    assert plan_fallback(event, p, airports).gate == "A12"


def test_remote_stand_when_no_gate_free(event, airports) -> None:
    p = _private(free_from={"A7": 800, "A12": 900})
    plan = plan_fallback(event, p, airports)
    assert plan.gate == "REMOTE-1" and not plan.divert


def test_slot_at_land_by_does_not_divert(event, airports) -> None:
    assert not plan_fallback(event, _private(free_slots=(790,), land_by=790), airports).divert


def test_diverts_to_nearest_excluding_origin_and_dest(event, airports) -> None:
    plan = plan_fallback(event, _private(free_slots=(791,), land_by=790), airports)
    assert plan.divert and plan.slot is None and plan.gate is None and plan.check is None
    assert plan.alternate == "CHA"
    atl = (33.64044, -84.42694)
    assert plan.alternate_distance_mi == pytest.approx(haversine_mi(*atl, 35.03527, -85.20379), abs=0.1)
    assert 90 < plan.alternate_distance_mi < 120


def test_divert_skips_blank_coordinates_and_origin(airports) -> None:
    extra = pd.DataFrame([{"IATA_CODE": "ZZZ", "AIRPORT": "x", "CITY": "", "STATE": "", "COUNTRY": "USA",
                           "LATITUDE": "", "LONGITUDE": ""}])
    df = pd.concat([airports, extra], ignore_index=True)
    code, dist = nearest_alternate(df, dest="ATL", origin="CHA")
    assert code == "BHM" and dist > 100


def test_divert_with_no_slots_at_all(event, airports) -> None:
    plan = plan_fallback(event, _private(free_slots=()), airports)
    assert plan.divert and plan.alternate == "CHA"


def test_divert_without_destination_coordinates_still_returns_plan(event) -> None:
    df = pd.DataFrame([{"IATA_CODE": "BHM", "LATITUDE": "33.5", "LONGITUDE": "-86.7"}])
    plan = plan_fallback(event, _private(free_slots=(900,)), df)
    assert plan.divert and plan.alternate is None and plan.reason


@settings(max_examples=200, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    slots=st.lists(st.integers(600, 900), max_size=5, unique=True),
    land_by=st.integers(580, 960),
    free=st.dictionaries(st.sampled_from(["A1", "A2", "A3", "B4"]),
                         st.one_of(st.none(), st.integers(560, 950)), min_size=1, max_size=4),
    failed=st.lists(st.sampled_from(["A1", "A2", "A3", "B4"]), max_size=2, unique=True),
)
def test_fallback_property(event, airports, slots, land_by, free, failed) -> None:
    p = _private(free_slots=slots, land_by=land_by, free_from=free, failed=failed)
    plan = plan_fallback(event, p, airports)
    assert isinstance(plan, FallbackPlan)
    if not slots or min(slots) > land_by:
        assert plan.divert
        return
    assert not plan.divert and to_min(plan.slot) == min(slots)
    assert plan.gate not in failed
    if plan.gate != "REMOTE-1":
        assert free[plan.gate] is not None and free[plan.gate] <= min(slots) + event.taxi_in


class _BusyWorld:
    """Minimal world: gate_free_from honours fixed busy intervals (G2 fix, PLAN 2.A.1)."""

    def __init__(self, busy: dict[str, list[tuple[int, int]]]) -> None:
        self.busy = busy

    def gate_free_from(self, gate, t, view, duration=30):
        if gate not in self.busy:
            raise KeyError(gate)
        cand = t
        for s, e in sorted(self.busy[gate]):
            if s < cand + duration and cand < e:
                cand = e
        return cand


def test_gate_must_be_clear_for_full_turnaround_when_world_given(event, airports) -> None:
    p = _private()  # slot 12:20, gate arrival 12:30 (750), min_turn 35 -> [750, 785)
    world = _BusyWorld({"A7": [], "A10": [(770, 830)], "A12": []})
    assert plan_fallback(event, p, airports).gate == "A10"            # start-only rule
    assert plan_fallback(event, p, airports, world=world).gate == "A12"  # A10 busy at 12:50


def test_remote_stand_skips_one_already_booked(event, airports, tmp_path) -> None:
    from acacia.ledger import Ledger

    ledger = Ledger(event.flight.date, runs_dir=tmp_path)
    ledger.hold("EV-OTHER", "ATL", 600, "REMOTE-1", 745, 800)
    p = _private(free_from={"A7": 800, "A12": 900})
    plan = plan_fallback(event, p, airports, ledger_view=ledger.live())
    assert plan.gate == "REMOTE-2" and not plan.divert
    assert plan_fallback(event, p, airports).gate == "REMOTE-1"
