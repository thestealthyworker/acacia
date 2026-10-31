"""HANDOVER 14 test 5 (one pass and one fail per rule, with boundaries), the PLAN 2.A.1
turnaround-overlap case, test 21's checker part (a double booking fails RESOURCE_CONFLICT)
and a static check that checker.py imports nothing from the solver.

Separation uses the CONSTRAINTS.md 2 wake table at 140 kt (Large/Large 3 nm = 1.29 min,
Heavy->Large 5 nm = 2.14 min, Super->Large 7 nm = exactly 3.0 min), and GATE_CHANGE_NOTICE
is the workbook's hard 20 min notice (CONSTRAINTS.md 3)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from acacia import checker
from acacia.checker import check
from acacia.ledger import Ledger
from acacia.models import (
    AtcPrivate, EmptyLedgerView, GroundPrivate, PilotPrivate, PrivateState, ReasonCode,
    ResolutionPackage,
)
from acacia.simulate import build_private
from acacia.timeutil import to_hhmm
from acacia.triangulate import triangulate
from acacia.world import AirportWorld

EMPTY = EmptyLedgerView()
TI = 10  # DL1639 taxi-in


class FakeWorld:
    """Just the AirportWorld surface the checker reads, with hand-set intervals."""

    airport = "ATL"

    def __init__(self, busy=None, failed=None) -> None:
        self._busy = {"A7": [], "A10": [], "A12": [], **(busy or {})}
        self._failed = {g: [] for g in self._busy} | (failed or {})

    def gates(self) -> list[str]:
        return list(self._busy)

    def busy_intervals(self, gate: str) -> list[tuple[int, int]]:
        return list(self._busy[gate])

    def failed_intervals(self, gate: str) -> list[tuple[int, int]]:
        return list(self._failed[gate])


@pytest.fixture
def event(dl1639_row):
    return triangulate(dl1639_row)


def _private(free_slots=(740, 748), traffic=(736, 744 + 8), land_by=790, free_from=None,
             failed=(), min_turn=35, duty_limit=861, classes=None, own="Large") -> PrivateState:
    free_from = free_from or {"A7": 700, "A10": 701, "A12": 745}
    traffic_classes = {t: "Large" for t in traffic} | (classes or {})
    return PrivateState(
        atc=AtcPrivate(first=738, free_slots=list(free_slots), demand={s: 1 for s in free_slots},
                       traffic=list(traffic), closures=[], own_class=own,
                       traffic_classes=traffic_classes),
        pilot=PilotPrivate(land_by=land_by, pax=30, connect_by=760),
        ground=GroundPrivate(gates=list(free_from), assigned_gate=next(iter(free_from)),
                             free_from=free_from, failed=list(failed), min_turn=min_turn,
                             shift_end=duty_limit - 45, duty_limit=duty_limit, next_departure=820),
    )


def _pkg(event, slot: int, gate: str = "A7", pid: str = "P1") -> ResolutionPackage:
    return ResolutionPackage(event_id=event.event_id, package_id=pid, turn=2, slot=to_hhmm(slot),
                             gate=gate, score=0.5, score_breakdown={})


def _rule(result, code: ReasonCode):
    return next(r for r in result.rules if r.rule == code)


def _run(event, private, slot, gate="A7", world=None, ledger=EMPTY):
    return check(_pkg(event, slot, gate), event, private, world or FakeWorld(), ledger, "ATL")


# --- clean pass ---------------------------------------------------------------------------
def test_clean_package_passes_every_rule(event) -> None:
    result = _run(event, _private(), 740)
    assert result.passed and len(result.rules) == 6 and result.failed() == []
    assert {r.rule for r in result.rules} == {
        ReasonCode.SEPARATION, ReasonCode.FUEL_LIMIT, ReasonCode.GATE_CONFLICT,
        ReasonCode.CREW_DUTY, ReasonCode.RESOURCE_CONFLICT, ReasonCode.GATE_CHANGE_NOTICE}
    assert all(r.detail for r in result.rules)


# --- SEPARATION ---------------------------------------------------------------------------
def test_separation_exactly_at_wake_minimum_passes(event) -> None:
    """Super leader -> Large follower is 7 nm = exactly 3.0 min: a 3 min gap passes."""
    result = _run(event, _private(traffic=(737, 743), classes={737: "Super"}), 740)
    assert _rule(result, ReasonCode.SEPARATION).passed


def test_separation_below_wake_minimum_fails(event) -> None:
    result = _run(event, _private(traffic=(738,), classes={738: "Super"}), 740)
    assert not _rule(result, ReasonCode.SEPARATION).passed and not result.passed


def test_wake_table_replaces_flat_three_minutes(event) -> None:
    """2 min behind a Large passes (1.29 min); 2 min behind a Heavy fails (2.14 min)."""
    assert _rule(_run(event, _private(traffic=(738,)), 740), ReasonCode.SEPARATION).passed
    rule = _rule(_run(event, _private(traffic=(738,), classes={738: "Heavy"}), 740),
                 ReasonCode.SEPARATION)
    assert not rule.passed and "Heavy" in rule.detail


def test_heavy_own_class_needs_space_before_the_follower(event) -> None:
    p = _private(traffic=(742,), own="Heavy")  # Heavy -> Large follower needs 2.14 min
    assert not _rule(_run(event, p, 740), ReasonCode.SEPARATION).passed
    assert _rule(_run(event, _private(traffic=(743,), own="Heavy"), 740), ReasonCode.SEPARATION).passed


def test_traffic_at_the_same_minute_fails(event) -> None:
    assert not _rule(_run(event, _private(traffic=(740,)), 740), ReasonCode.SEPARATION).passed


def test_separation_slot_not_free_fails(event) -> None:
    result = _run(event, _private(), 741)  # off the free sequence (corrupt solver style)
    rule = _rule(result, ReasonCode.SEPARATION)
    assert not rule.passed and "not an ATC free slot" in rule.detail


# --- FUEL_LIMIT ---------------------------------------------------------------------------
def test_fuel_slot_exactly_at_land_by_passes(event) -> None:
    assert _rule(_run(event, _private(land_by=740), 740), ReasonCode.FUEL_LIMIT).passed


def test_fuel_slot_after_land_by_fails(event) -> None:
    result = _run(event, _private(land_by=739), 740)
    assert not _rule(result, ReasonCode.FUEL_LIMIT).passed and not result.passed


# --- GATE_CONFLICT --------------------------------------------------------------------------
def test_gate_free_exactly_at_arrival_passes(event) -> None:
    result = _run(event, _private(free_from={"A12": 750}), 740, gate="A12")
    assert _rule(result, ReasonCode.GATE_CONFLICT).passed


def test_gate_not_free_at_arrival_fails(event) -> None:
    result = _run(event, _private(free_from={"A12": 751}), 740, gate="A12")
    assert not _rule(result, ReasonCode.GATE_CONFLICT).passed


def test_failed_gate_fails(event) -> None:
    p = _private(free_from={"A7": 700, "A10": None}, failed=["A10"])
    assert not _rule(_run(event, p, 740, gate="A10"), ReasonCode.GATE_CONFLICT).passed


def test_world_failure_interval_fails(event) -> None:
    world = FakeWorld(failed={"A7": [(770, 800)]})
    rule = _rule(_run(event, _private(), 740, world=world), ReasonCode.GATE_CONFLICT)
    assert not rule.passed and "out of service" in rule.detail


def test_turnaround_overlap_with_later_busy_interval_fails(event) -> None:
    """PLAN 2.A.1: free at gate arrival, but a busy interval starts inside the turnaround."""
    world = FakeWorld(busy={"A7": [(600, 700), (770, 830)]})
    p = _private(free_from={"A7": 750})       # the old start-only rule would pass this
    rule = _rule(_run(event, p, 740, world=world), ReasonCode.GATE_CONFLICT)
    assert not rule.passed and "busy" in rule.detail


def test_turnaround_ending_exactly_at_busy_start_passes(event) -> None:
    world = FakeWorld(busy={"A7": [(785, 830)]})  # turnaround [750, 785) is half-open
    assert _rule(_run(event, _private(), 740, world=world), ReasonCode.GATE_CONFLICT).passed


def test_remote_stand_always_passes_gate_rule(event) -> None:
    world = FakeWorld(busy={"A7": [(0, 1440)]})
    assert _rule(_run(event, _private(), 740, gate="REMOTE-1", world=world),
                 ReasonCode.GATE_CONFLICT).passed


def test_unknown_gate_fails(event) -> None:
    assert not _rule(_run(event, _private(), 740, gate="Z99"), ReasonCode.GATE_CONFLICT).passed


# --- CREW_DUTY ------------------------------------------------------------------------------
def test_crew_done_exactly_at_duty_limit_passes(event) -> None:
    assert _rule(_run(event, _private(duty_limit=740 + TI + 35), 740), ReasonCode.CREW_DUTY).passed


def test_crew_past_duty_limit_fails(event) -> None:
    result = _run(event, _private(duty_limit=740 + TI + 34), 740)
    assert not _rule(result, ReasonCode.CREW_DUTY).passed and not result.passed


def _with_crew_end(private: PrivateState, end: int) -> PrivateState:
    return private.model_copy(update={"pilot": private.pilot.model_copy(update={"crew_duty_end": end})})


def test_flight_crew_duty_end_is_verified(event) -> None:
    ok = _rule(_run(event, _with_crew_end(_private(), 740 + TI), 740), ReasonCode.CREW_DUTY)
    assert ok.passed and "flight crew duty end" in ok.detail
    late = _run(event, _with_crew_end(_private(), 740 + TI - 1), 740)
    assert not _rule(late, ReasonCode.CREW_DUTY).passed and not late.passed
    after_today = _rule(_run(event, _with_crew_end(_private(), 1500), 740), ReasonCode.CREW_DUTY)
    assert after_today.passed and "after today" in after_today.detail


# --- RESOURCE_CONFLICT (live ledger) -----------------------------------------------------------
@pytest.fixture
def ledger(event, tmp_path) -> Ledger:
    return Ledger(event.flight.date, runs_dir=tmp_path)


def test_ledger_free_passes(event, ledger) -> None:
    ledger.hold("OTHER", "ATL", 748, "A10", 758, 793)
    assert _rule(_run(event, _private(), 740, ledger=ledger.live()), ReasonCode.RESOURCE_CONFLICT).passed


def test_slot_held_by_another_event_fails(event, ledger) -> None:
    ledger.hold("OTHER", "ATL", 740, "A10", 750, 785)
    rule = _rule(_run(event, _private(), 740, ledger=ledger.live()), ReasonCode.RESOURCE_CONFLICT)
    assert not rule.passed and "runway slot" in rule.detail


def test_gate_interval_overlapping_another_booking_fails(event, ledger) -> None:
    ledger.hold("OTHER", "ATL", 700, "A7", 770, 800)  # overlaps [750, 785)
    rule = _rule(_run(event, _private(), 740, ledger=ledger.live()), ReasonCode.RESOURCE_CONFLICT)
    assert not rule.passed and "A7" in rule.detail


def test_own_booking_is_not_a_conflict(event, ledger) -> None:
    ledger.hold(event.event_id, "ATL", 740, "A7", 750, 785)
    assert _run(event, _private(), 740, ledger=ledger.live()).passed


def test_checker_reads_live_ledger_not_a_snapshot(event, ledger) -> None:
    snapshot = ledger.view()
    ledger.hold("OTHER", "ATL", 740, "A10", 750, 785)  # booked after agents saw `snapshot`
    assert _run(event, _private(), 740, ledger=snapshot).passed
    assert not _run(event, _private(), 740, ledger=ledger.live()).passed


def test_held_slot_inside_wake_minimum_fails(event, ledger) -> None:
    ledger.hold("OTHER", "ATL", 741, "A10", 751, 786)  # 1 min < 1.29 min (Large/Large)
    rule = _rule(_run(event, _private(), 740, ledger=ledger.live()), ReasonCode.RESOURCE_CONFLICT)
    assert not rule.passed and "runway slot" in rule.detail


def test_held_slot_beyond_wake_minimum_passes(event, ledger) -> None:
    ledger.hold("OTHER", "ATL", 742, "A10", 752, 787)  # 2 min >= 1.29 min
    live = ledger.live()
    assert _rule(_run(event, _private(), 740, ledger=live), ReasonCode.RESOURCE_CONFLICT).passed
    heavy = _run(event, _private(own="Heavy"), 740, ledger=live)  # Heavy -> Large needs 2.14
    assert not _rule(heavy, ReasonCode.RESOURCE_CONFLICT).passed


def test_double_booking_package_fails_with_real_world(event, ledger) -> None:
    """Test 21 (checker part): a package taking another event's held slot and gate fails."""
    world = AirportWorld("ATL", event.flight.date, event.injected)
    private = build_private(event, None, world, ledger.view())
    slot, gate = private.atc.free_slots[0], private.ground.gates[0]
    clean = check(_pkg(event, slot, gate), event, private, world, ledger.live())
    assert _rule(clean, ReasonCode.RESOURCE_CONFLICT).passed
    start = slot + event.taxi_in
    ledger.hold("OTHER", "ATL", slot, gate, start, start + private.ground.min_turn)
    result = check(_pkg(event, slot, gate), event, private, world, ledger.live())
    assert not result.passed and not _rule(result, ReasonCode.RESOURCE_CONFLICT).passed


# --- GATE_CHANGE_NOTICE (CONSTRAINTS.md 3, hard 20 min) -------------------------------------
def test_assigned_gate_needs_no_notice(event) -> None:
    late = event.model_copy(update={"decision_time": "12:29"})  # 1 min before gate arrival
    assert _rule(_run(late, _private(), 740, gate="A7"), ReasonCode.GATE_CHANGE_NOTICE).passed


def test_gate_change_with_exactly_twenty_minutes_notice_passes(event) -> None:
    timed = event.model_copy(update={"decision_time": "12:10"})  # gate arrival 12:30
    assert _rule(_run(timed, _private(), 740, gate="A10"), ReasonCode.GATE_CHANGE_NOTICE).passed


def test_gate_change_with_nineteen_minutes_notice_fails(event) -> None:
    timed = event.model_copy(update={"decision_time": "12:11"})
    result = _run(timed, _private(), 740, gate="A10")
    rule = _rule(result, ReasonCode.GATE_CHANGE_NOTICE)
    assert not rule.passed and not result.passed and "19 min" in rule.detail


def test_remote_stand_counts_as_a_gate_change(event) -> None:
    timed = event.model_copy(update={"decision_time": "12:11"})
    rule = _rule(_run(timed, _private(), 740, gate="REMOTE-1"), ReasonCode.GATE_CHANGE_NOTICE)
    assert not rule.passed


def test_decision_time_defaults(event) -> None:
    assert checker.decision_time_min(event) == 12 * 60 + 1  # earliest injected start (12:01)
    bare = event.model_copy(update={"injected": [], "decision_time": None})
    assert checker.decision_time_min(bare) == 12 * 60 + 11 - 30  # E - 30
    assert checker.decision_time_min(event.model_copy(update={"decision_time": "11:00"})) == 660


# --- input validation and isolation ---------------------------------------------------------
def test_package_for_another_event_is_an_error(event) -> None:
    pkg = _pkg(event, 740).model_copy(update={"event_id": "EVT-OTHER"})
    with pytest.raises(ValueError):
        check(pkg, event, _private(), FakeWorld(), EMPTY)


def test_checker_does_not_import_solver() -> None:
    tree = ast.parse(Path(checker.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported |= {f"{node.module}.{a.name}" for a in node.names}
    assert not any("solver" in name for name in imported), imported
    assert "solver" in (checker.__doc__ or "")



# --- SEPARATION against the real world and the live ledger (CONSTRAINTS.md 2) ------------------
def test_separation_uses_world_wake_check_with_live_ledger(event, ledger) -> None:
    world = AirportWorld("ATL", event.flight.date, event.injected)
    private = build_private(event, None, world, ledger.view())
    slot, gate = private.atc.free_slots[0], private.ground.assigned_gate
    ok = _rule(check(_pkg(event, slot, gate), event, private, world, ledger.live()), ReasonCode.SEPARATION)
    assert ok.passed and "separated" in ok.detail
    ledger.hold("OTHER", "ATL", slot + 1, "REMOTE-9", slot + 11, slot + 56)  # 1 min behind: < 1.29
    bad = _rule(check(_pkg(event, slot, gate), event, private, world, ledger.live()), ReasonCode.SEPARATION)
    assert not bad.passed and "wake" in bad.detail


def test_separation_ignores_the_events_own_held_slot(event, ledger) -> None:
    world = AirportWorld("ATL", event.flight.date, event.injected)
    private = build_private(event, None, world, ledger.view())
    slot, gate = private.atc.free_slots[0], private.ground.assigned_gate
    ledger.hold(event.event_id, "ATL", slot, gate, slot + TI, slot + TI + private.ground.min_turn)
    result = check(_pkg(event, slot, gate), event, private, world, ledger.live())
    assert _rule(result, ReasonCode.SEPARATION).passed


def test_world_separation_exactly_at_the_wake_minimum_passes(event) -> None:
    """A movement exactly the wake minimum ahead passes; the world decides with float minutes."""

    class SeparatedAt(FakeWorld):
        def __init__(self, ok: bool) -> None:
            super().__init__()
            self.ok, self.calls = ok, []

        def is_separated(self, t, own_class, view, exclude_event=None):
            self.calls.append((t, own_class, exclude_event))
            return self.ok, "3 min behind Super at 737 (need 3.00)" if self.ok else "need 3.00"

    w = SeparatedAt(True)
    assert _rule(_run(event, _private(), 740, world=w), ReasonCode.SEPARATION).passed
    assert w.calls == [(740, "Large", event.event_id)]
    assert not _rule(_run(event, _private(), 740, world=SeparatedAt(False)), ReasonCode.SEPARATION).passed


def test_gate_turnaround_interval_uses_private_min_turn(event) -> None:
    busy = FakeWorld(busy={"A7": [(784, 800)]})  # turnaround [750, 750 + min_turn)
    assert _rule(_run(event, _private(min_turn=34), 740, world=busy), ReasonCode.GATE_CONFLICT).passed
    assert not _rule(_run(event, _private(min_turn=35), 740, world=busy), ReasonCode.GATE_CONFLICT).passed
