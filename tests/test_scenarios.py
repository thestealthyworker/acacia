"""Test 14, scenarios A-D (HANDOVER 12) on DL1639 and the other qualifying sample rows.

Exact times depend on the seed; these tests assert behaviours. Scenarios E and F (multi-trigger,
pre-emption) belong to the dispatcher workstream.
"""

from __future__ import annotations

import pytest

from acacia.agents.factory import make_agents
from acacia.coordinator import run_all
from acacia.models import ConsensusRecord, LogEntry, Stress, Verdict
from acacia.timeutil import to_min
from acacia.triangulate import triangulate


@pytest.fixture(scope="module")
def dl1639(sample_store):
    return triangulate(sample_store.find_flight("DL", "1639", "2015-02-26"))


@pytest.fixture(scope="module")
def events(sample_store):
    return [triangulate(r.to_dict()) for _, r in sample_store.qualifying_rows().iterrows()]


def _run(event, stress: Stress) -> tuple[list[LogEntry], ConsensusRecord]:
    entries = run_all(event, stress, make_agents("rule"))
    assert entries[-1].type == "ConsensusRecord" and entries[-1].state == "AWAITING_SIGNOFF"
    return entries, ConsensusRecord.model_validate(entries[-1].payload)


def _rejects(rec: ConsensusRecord, turn: int | None = None) -> list:
    return [d for d in rec.decisions if d.verdict == Verdict.REJECT and (turn is None or d.turn == turn)]


# --- A. normal --------------------------------------------------------------------------------------
def test_a_normal_dl1639_reaches_checked_consensus(dl1639) -> None:
    entries, rec = _run(dl1639, Stress())
    assert rec.outcome == "CONSENSUS" and rec.turns_used in (2, 3)
    assert rec.checker_pass is True
    if dl1639.causes.weather > 15:
        assert to_min(rec.package["gate_arrival"]) <= to_min(dl1639.hist_arrival)
        assert rec.delta_vs_history_min is not None and rec.delta_vs_history_min >= 0


def test_a_every_sample_row_ends_with_a_plan(events) -> None:
    for event in events:
        _, rec = _run(event, Stress())
        if rec.outcome == "CONSENSUS":
            assert rec.checker_pass is True and rec.turns_used in (2, 3)
        else:
            assert rec.package.get("fallback_reason")
        assert rec.package.get("flight_plan")


# --- B. safety test --------------------------------------------------------------------------------------
def _corrupted_ids(entries: list[LogEntry]) -> set[str]:
    return {e.payload["package"]["package_id"] for e in entries
            if e.type == "ResolutionPackage" and e.payload.get("corrupted") and e.payload.get("package")}


def test_b_corrupt_solver_is_caught_on_turn_two(events) -> None:
    for event in events:
        entries, rec = _run(event, Stress(corrupt_solver=True))
        bad = _corrupted_ids(entries)
        if not bad:
            continue  # nothing to corrupt (no package at all)
        turn2_check = next(e for e in entries if e.type == "CheckResult" and e.turn == 2)
        assert _rejects(rec, 2) or turn2_check.payload["passed"] is False
        assert not (rec.outcome == "CONSENSUS" and rec.package["package_id"] in bad)


def test_b_dl1639_recovers_with_a_later_package_or_fallback(dl1639) -> None:
    entries, rec = _run(dl1639, Stress(corrupt_solver=True))
    assert _corrupted_ids(entries)
    assert rec.outcome == "FALLBACK" or (rec.turns_used == 3 and rec.checker_pass is True)


# --- C. low fuel ---------------------------------------------------------------------------------------
def test_c_low_fuel_diverts_to_nearest_alternate(events, dl1639) -> None:
    for event in [dl1639, *events]:
        entries, rec = _run(event, Stress(low_fuel=True))
        first_solve = next(e for e in entries if e.type == "ResolutionPackage")
        assert first_solve.payload["package"] is None  # feasible set empty after declare
        assert rec.outcome == "FALLBACK" and rec.package["divert"] is True
        assert rec.package["alternate"] not in (None, event.flight.dest, event.flight.origin)
        assert rec.package["alternate_distance_mi"] > 0
        fb = next(e for e in entries if e.type == "FallbackPlan")
        assert fb.payload["divert"] is True and fb.payload["check"] is None


# --- D. tight crew ---------------------------------------------------------------------------------------
def _overtime(rec: ConsensusRecord, event, entries: list[LogEntry]) -> int:
    ground = next(e.payload for e in entries if e.type == "ConstraintPacket" and e.who == "ground")
    shift_end = to_min(next(s["target"] for s in ground["soft"] if s["id"] == "G_S1"))
    done = to_min(rec.package["gate_arrival"]) + ground["facts_owned"]["min_turn_min"]
    return max(0, done - shift_end)


def test_d_tight_crew_rejects_on_g_s1_end_at_turn_three_or_fallback(events, dl1639) -> None:
    fired = 0
    for event in [dl1639, *events]:
        entries, rec = _run(event, Stress(tight_crew=True))
        g_s1 = [d for d in _rejects(rec) if d.cites == "G_S1"]
        if g_s1:
            fired += 1
            assert rec.outcome == "FALLBACK" or rec.turns_used == 3
        if rec.outcome == "CONSENSUS":
            assert _overtime(rec, event, entries) <= 20  # Ground's red line holds in any agreed plan
    assert fired >= 1, "no sample row produced a Ground CREW_DUTY reject citing G_S1"


def test_d_tight_crew_brings_the_shift_end_forward(dl1639) -> None:
    def shift_end(stress: Stress) -> int:
        entries = run_all(dl1639, stress, make_agents("rule"))
        ground = next(e.payload for e in entries if e.type == "ConstraintPacket" and e.who == "ground")
        return to_min(next(s["target"] for s in ground["soft"] if s["id"] == "G_S1"))

    assert shift_end(Stress(tight_crew=True)) < shift_end(Stress())
