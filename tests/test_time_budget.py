"""Negotiation time budget (TIME-1..7): the simulated clock moves past the decision time.

commit_time = decision_time + 3 (negotiation) + 2 (sign-off); landing slots must be at or after
commit_time + 5; the 20 min gate-change notice counts from commit_time. Every layer applies it:
simulate (ATC free slots), the agents' declarations, the solver (declarations only), the checker
(ground truth, no solver import), the fallback, the coordinator deadline guard and the
dispatcher's shared clock. DL1639: E 12:11, taxi-in 10, decision 12:01 (earliest injected start).
"""

from __future__ import annotations

import itertools

import pytest

from acacia import constraints
from acacia.agents.declared import gate_notice_value, slot_lead_value
from acacia.agents.factory import make_agents
from acacia.agents.llm import expected_declaration
from acacia.agents.rule import RuleATC, RuleGround
from acacia.checker import check
from acacia.coordinator import run_all
from acacia.fallback import plan_fallback
from acacia.models import (
    AgentRole, AtcPrivate, ConsensusRecord, ConstraintPacket, EmptyLedgerView, GroundPrivate, HardLimit,
    Option, PilotPrivate, PrivateState, ReasonCode, ResolutionPackage, SoftPref, Stress, Verdict,
)
from acacia.simulate import build_private
from acacia.solver import solve
from acacia.timeutil import to_hhmm, to_min
from acacia.triangulate import triangulate
from acacia.world import get_world

EMPTY = EmptyLedgerView()
TI = 10


@pytest.fixture
def event(dl1639_row):
    return triangulate(dl1639_row)


def _at(event, decision: str):
    return event.model_copy(update={"decision_time": decision})


# --- 1. budget arithmetic -------------------------------------------------------------------------
def test_budget_constants_and_registry_rows() -> None:
    assert (constraints.NEGOTIATION_BUDGET_MIN, constraints.SIGNOFF_BUDGET_MIN, constraints.SLOT_LEAD_MIN) == (3, 2, 5)
    assert constraints.time_budget_min() == 5
    rows = {c.id: c for c in constraints.REGISTRY}
    for i in range(1, 8):
        assert rows[f"TIME-{i}"].status == "ASSUMED"
    assert len({c.id for c in constraints.REGISTRY}) == len(constraints.REGISTRY)  # ids unique


def test_commit_time_is_decision_plus_negotiation_plus_signoff(event) -> None:
    assert constraints.decision_time_min(event) == 721  # 12:01
    assert constraints.commit_time_min(event) == 721 + 3 + 2
    assert constraints.earliest_slot_min(event) == 726 + 5
    late = _at(event, "12:20")
    assert (constraints.commit_time_min(late), constraints.earliest_slot_min(late)) == (745, 750)


def test_deadline_guard_boundary(event) -> None:
    assert constraints.has_time_to_negotiate(_at(event, "12:06"))  # E - 5: exactly the budget
    assert not constraints.has_time_to_negotiate(_at(event, "12:07"))  # E - 4


# --- 2. simulate: ATC free slots -----------------------------------------------------------------------
def test_atc_free_slots_start_at_commit_plus_lead(event) -> None:
    late = _at(event, "12:40")  # commit 12:45, earliest slot 12:50 (well after first 12:30)
    world = get_world(late.flight.dest, late.flight.date, late.injected)
    atc = build_private(late, None, world, EMPTY).atc
    assert atc.free_slots and min(atc.free_slots) >= to_min("12:50")
    assert atc.first >= to_min("12:50")


# --- 3. declarations (rule and LLM agents share acacia.agents.declared) -----------------------------
ATC = AtcPrivate(first=740, free_slots=[740, 748], demand={740: 1, 748: 1}, traffic=[736], closures=[])
GROUND = GroundPrivate(gates=["A7", "A12"], assigned_gate="A7", free_from={"A7": 700, "A12": 700}, failed=[],
                       min_turn=45, shift_end=900, duty_limit=945, next_departure=900)


def test_atc_declares_slot_lead_with_commit_time(event) -> None:
    timed = _at(event, "12:10")
    a_h3 = next(h for h in RuleATC().declare(timed, ATC).hard if h.id == "A_H3")
    assert a_h3.rule == "SLOT_LEAD"
    assert a_h3.value == {"commit_time": "12:15", "slot_lead_min": 5, "earliest_slot": "12:20"}
    assert a_h3.value == slot_lead_value(timed)
    assert ("hard", "A_H3", "SLOT_LEAD", a_h3.value) in expected_declaration(AgentRole.ATC, ATC, timed)


def test_ground_declares_commit_time_in_gate_change_notice(event) -> None:
    timed = _at(event, "12:10")
    g_h3 = next(h for h in RuleGround().declare(timed, GROUND).hard if h.id == "G_H3")
    assert g_h3.value == {"min_notice_min": 20, "decision_time": "12:10", "commit_time": "12:15",
                          "assigned_gate": "A7"}
    assert g_h3.value == gate_notice_value(timed, GROUND)


def test_ground_measures_notice_from_commit_time(event) -> None:
    timed = _at(event, "12:05")  # commit 12:10 (730)
    packet = RuleGround().declare(timed, GROUND)

    def vote(slot: int, gate: str):
        pkg = ResolutionPackage(event_id=event.event_id, package_id="P1", turn=2, slot=to_hhmm(slot),
                                gate=gate, score=0.5, score_breakdown={})
        return RuleGround().decide(timed, pkg, GROUND, packet)

    assert vote(740, "A12").verdict == Verdict.ACCEPT  # arrive 750 - commit 730 = exactly 20
    d = vote(739, "A12")  # 19 min after commit (it would be 24 after the decision time)
    assert d.verdict == Verdict.REJECT and d.reason_code == ReasonCode.GATE_CHANGE_NOTICE and d.cites == "G_H3"


# --- 4. solver: declarations only ------------------------------------------------------------------------
def _packets(slots: list[int], lead: dict | None, notice: dict | None) -> dict[AgentRole, ConstraintPacket]:
    a_hard = [HardLimit(id="A_H1", rule="SLOT_IN_SEQUENCE", value=[to_hhmm(s) for s in slots])]
    if lead is not None:
        a_hard.append(HardLimit(id="A_H3", rule="SLOT_LEAD", value=lead))
    g_hard = [HardLimit(id="G_H1", rule="GATE_FREE_FROM", value={"A7": "11:00", "A12": "11:00"}),
              HardLimit(id="G_H2", rule="CREW_DUTY_LIMIT", value="16:00")]
    if notice is not None:
        g_hard.append(HardLimit(id="G_H3", rule="GATE_CHANGE_NOTICE", value=notice))
    soft = [SoftPref(id="A_S1", preference="low demand", weight=0.7, metric="slot_demand")]
    return {
        AgentRole.ATC: ConstraintPacket(event_id="E", agent=AgentRole.ATC, turn=1, hard=a_hard, soft=soft,
                                        options=[Option(type="SLOT", value=to_hhmm(s), demand=1) for s in slots]),
        AgentRole.PILOT: ConstraintPacket(event_id="E", agent=AgentRole.PILOT, turn=1, soft=[], options=[],
                                          hard=[HardLimit(id="P_H1", rule="LAND_BY", value="14:00")]),
        AgentRole.GROUND: ConstraintPacket(event_id="E", agent=AgentRole.GROUND, turn=1, hard=g_hard, soft=[],
                                           options=[Option(type="GATE", value=g) for g in ("A7", "A12")],
                                           facts_owned={"min_turn_min": 45}),
    }


def _cand(result, slot: int, gate: str):
    return next(c for c in result.all if c.package.slot == to_hhmm(slot) and c.package.gate == gate)


def test_solver_discards_slots_before_declared_earliest_slot() -> None:
    lead = {"commit_time": "12:15", "slot_lead_min": 5, "earliest_slot": "12:20"}
    r = solve(_packets([739, 740], lead, None), TI, [], set())
    assert ReasonCode.SLOT_LEAD in _cand(r, 739, "A7").discard
    assert _cand(r, 740, "A7").discard == []  # exactly commit + 5 passes
    assert r.top is not None and r.top.slot == "12:20"


def test_solver_derives_earliest_slot_from_commit_time_and_lead() -> None:
    lead = {"commit_time": "12:15", "slot_lead_min": 5}  # no earliest_slot: commit + lead
    r = solve(_packets([739, 740], lead, None), TI, [], set())
    assert ReasonCode.SLOT_LEAD in _cand(r, 739, "A7").discard and _cand(r, 740, "A7").discard == []


def test_solver_measures_gate_notice_from_declared_commit_time() -> None:
    notice = {"min_notice_min": 20, "decision_time": "12:05", "commit_time": "12:10", "assigned_gate": "A7"}
    r = solve(_packets([739, 740], None, notice), TI, [], set())
    assert _cand(r, 740, "A12").discard == []  # arrive 12:30 - commit 12:10 = exactly 20
    assert ReasonCode.GATE_CHANGE_NOTICE in _cand(r, 739, "A12").discard
    assert _cand(r, 739, "A7").discard == []  # assigned gate: no notice needed


def test_solver_without_declared_commit_time_adds_the_public_budget() -> None:
    notice = {"min_notice_min": 20, "decision_time": "12:05", "assigned_gate": "A7"}  # older packet shape
    r = solve(_packets([739, 740], None, notice), TI, [], set())
    assert _cand(r, 740, "A12").discard == [] and ReasonCode.GATE_CHANGE_NOTICE in _cand(r, 739, "A12").discard


# --- 5. checker: ground truth -----------------------------------------------------------------------------
class FakeWorld:
    airport = "ATL"

    def gates(self) -> list[str]:
        return ["A7", "A12"]

    def busy_intervals(self, gate: str) -> list:
        return []

    def failed_intervals(self, gate: str) -> list:
        return []


def _private(free_slots=(738, 739, 740)) -> PrivateState:
    return PrivateState(
        atc=AtcPrivate(first=738, free_slots=list(free_slots), demand={}, traffic=[], closures=[]),
        pilot=PilotPrivate(land_by=800, pax=10, connect_by=800),
        ground=GroundPrivate(gates=["A7", "A12"], assigned_gate="A7", free_from={"A7": 700, "A12": 700},
                             failed=[], min_turn=45, shift_end=900, duty_limit=945, next_departure=900),
    )


def _rule(ev, slot: int, gate: str, code: ReasonCode):
    pkg = ResolutionPackage(event_id=ev.event_id, package_id="P1", turn=2, slot=to_hhmm(slot), gate=gate,
                            score=0.5, score_breakdown={})
    result = check(pkg, ev, _private(), FakeWorld(), EMPTY, "ATL")
    return next(r for r in result.rules if r.rule == code)


def test_checker_slot_lead_boundary(event) -> None:
    timed = _at(event, "12:10")  # commit 12:15, earliest slot 12:20 (740)
    assert _rule(timed, 740, "A7", ReasonCode.SLOT_LEAD).passed  # exactly commit + 5
    fail = _rule(timed, 739, "A7", ReasonCode.SLOT_LEAD)
    assert not fail.passed and "12:15" in fail.detail


def test_checker_measures_notice_from_commit_time(event) -> None:
    assert _rule(_at(event, "12:05"), 740, "A12", ReasonCode.GATE_CHANGE_NOTICE).passed  # exactly 20
    fail = _rule(_at(event, "12:06"), 740, "A12", ReasonCode.GATE_CHANGE_NOTICE)  # 19 after commit
    assert not fail.passed and "commit" in fail.detail


# --- 6. fallback --------------------------------------------------------------------------------------------
def test_fallback_never_picks_a_slot_before_commit_plus_lead(event, sample_store) -> None:
    timed = _at(event, "12:20")  # earliest slot 12:30
    plan = plan_fallback(timed, _private(free_slots=(745, 749, 750, 760)), sample_store.airports)
    assert plan.slot == "12:30"


def test_fallback_measures_notice_from_commit_time(event, sample_store) -> None:
    private = _private(free_slots=(740, 760))
    busy = private.model_copy(update={"ground": private.ground.model_copy(
        update={"free_from": {"A7": 790, "A12": 700}})})  # assigned A7 not free: a gate change is needed
    plan = plan_fallback(_at(event, "12:06"), busy, sample_store.airports)  # commit 12:11: 12:20 gives 19
    assert (plan.slot, plan.gate) == ("12:40", "A12")


# --- 7. coordinator deadline guard ----------------------------------------------------------------------
def _record(entries) -> ConsensusRecord:
    return ConsensusRecord.model_validate(entries[-1].payload)


def test_deadline_guard_goes_straight_to_checked_fallback(event) -> None:
    late = _at(event, "12:07")  # E - 4 < 5 min budget
    entries = run_all(late, None, make_agents("rule"))
    assert not [e for e in entries if e.type in ("ConstraintPacket", "Decision", "ResolutionPackage")]
    guard = next(e for e in entries if e.state == "FALLBACK")
    assert guard.payload["code"] == "DEADLINE" and "no time to negotiate" in guard.payload["reason"]
    plan = next(e for e in entries if e.type == "FallbackPlan")
    assert plan.payload["used"] is True
    rec = _record(entries)
    assert rec.outcome == "FALLBACK" and rec.turns_used == 0
    assert rec.package["fallback_reason"].startswith("no time to negotiate")
    if not rec.package.get("divert"):
        assert plan.payload["check"] is not None and rec.checker_pass is not None


def test_deadline_guard_lets_an_exact_budget_negotiate(event) -> None:
    entries = run_all(_at(event, "12:06"), None, make_agents("rule"))  # E - 5
    assert any(e.type == "ConstraintPacket" for e in entries)


# --- 8. hash stability and wall-clock meta -------------------------------------------------------------
def test_two_runs_give_identical_hashes_and_wall_time_is_not_hashed(event) -> None:
    a = run_all(event, None, make_agents("rule"))
    b = run_all(event, None, make_agents("rule"))
    assert [e.hash for e in a] == [e.hash for e in b]
    meta = a[-1].meta
    assert meta is not None and meta["wall_clock_s"] >= 0 and meta["wall_budget_s"] == 180
    assert "wall_clock_s" not in a[-1].payload


class _SlowLLM:
    """Wraps a rule agent but behaves like a live LLM agent for the wall-clock budget."""

    enforces_wall_budget = True

    def __init__(self, inner) -> None:
        self.inner = inner

    def declare(self, event, private):
        return self.inner.declare(event, private)

    def decide(self, event, package, private, own):
        return self.inner.decide(event, package, private, own)


def _ticking(step_s: float):
    counter = itertools.count()
    return lambda: next(counter) * step_s


def test_llm_run_over_wall_budget_falls_back_with_reason(event) -> None:
    agents = {r: _SlowLLM(a) for r, a in make_agents("rule").items()}
    entries = run_all(event, None, agents, clock=_ticking(100.0))
    guard = next(e for e in entries if e.state == "FALLBACK")
    assert guard.payload["code"] == "TIME_BUDGET" and "wall-clock" in guard.payload["reason"]
    rec = _record(entries)
    assert rec.outcome == "FALLBACK" and entries[-1].meta["wall_clock_s"] > 180


def test_rule_agents_ignore_the_wall_clock(event) -> None:
    entries = run_all(event, None, make_agents("rule"), clock=_ticking(100.0))
    assert _record(entries).outcome == "CONSENSUS"


# --- 9. dispatcher shared clock ---------------------------------------------------------------------------
def test_dispatcher_clock_is_monotonic_and_cumulative(tmp_path) -> None:
    from pathlib import Path

    from acacia import config
    from acacia.data import DataStore
    from acacia.dispatcher import Dispatcher
    from acacia.ledger import Ledger

    root = Path(__file__).resolve().parent.parent
    store = DataStore(root / "data", flights_path=root / "tests" / "fixtures" / "multi_trigger_day.csv",
                      airports_path=root / "data" / "sample_airports.csv")
    d = Dispatcher(store, make_agents("rule"), Ledger("2015-03-12", runs_dir=tmp_path), config.IMPACT_POLICY)
    d.collect(scan=("ATL", "2015-03-12", 180))
    entries = list(d.run())
    dispatches = [e for e in entries if e.type == "DISPATCH"]
    assert len(dispatches) >= 2
    previous = None
    for e in dispatches:
        clock = e.payload["clock"]
        decided, commit = to_min(clock["decision_time"]), to_min(clock["commit_time"])
        assert commit == decided + constraints.time_budget_min()
        assert decided >= to_min(clock["own_decision_time"])
        assert clock["previous_commit_time"] == (None if previous is None else to_hhmm(previous))
        if previous is not None:
            assert decided >= previous  # later triggers see later decision times
        previous = commit
        assert "clock" in e.text.lower()
        first = next(x for x in d.logs[e.payload["event_id"]] if x.type == "DisruptionEvent")
        assert first.payload["decision_time"] == clock["decision_time"]


# --- 10. TIME-8: clusters dispatch in deadline order, tier 0 still first ---------------------------------
def _fixture_dispatcher(tmp_path, stress=None):
    from pathlib import Path

    from acacia import config
    from acacia.data import DataStore
    from acacia.dispatcher import Dispatcher
    from acacia.ledger import Ledger

    root = Path(__file__).resolve().parent.parent
    store = DataStore(root / "data", flights_path=root / "tests" / "fixtures" / "multi_trigger_day.csv",
                      airports_path=root / "data" / "sample_airports.csv")
    return Dispatcher(store, make_agents("rule"), Ledger("2015-03-12", runs_dir=tmp_path), config.IMPACT_POLICY,
                      stress)


def _dispatched_codes(d, entries) -> list[str]:
    return [d.events[e.event_id].flight.code for e in entries if e.type == "DISPATCH"]


def test_clusters_dispatch_by_earliest_deadline_and_queue_matches(tmp_path) -> None:
    d = _fixture_dispatcher(tmp_path)
    d.collect(scan=("ATL", "2015-03-12", 180))
    entries = list(d.run())
    codes = _dispatched_codes(d, entries)
    # the 12:xx cluster (earliest landing 12:30) runs before the 14:xx cluster, though DL204 has the top impact
    assert codes[:3] == ["DL201", "DL203", "DL202"] and codes[3:] == ["DL204", "DL205"]
    queue = next(e for e in entries if e.type == "TriggerQueue")
    assert [it["event_id"] for it in queue.payload["items"]] == [e.event_id for e in entries if e.type == "DISPATCH"]
    assert "earliest decision deadline" in queue.text
    rec = {d.events[k].flight.code: v for k, v in d.records.items()}
    assert not any(r.package.get("divert") for r in rec.values())  # the clock causes no unstressed divert


def test_tier_zero_still_dispatches_before_earlier_deadline_clusters(tmp_path) -> None:
    probe = _fixture_dispatcher(tmp_path / "probe")
    probe.collect(scan=("ATL", "2015-03-12", 180))
    dl205 = next(e for e, ev in probe.events.items() if ev.flight.code == "DL205")
    d = _fixture_dispatcher(tmp_path / "run", stress={dl205: Stress(low_fuel=True)})
    d.collect(scan=("ATL", "2015-03-12", 180))
    entries = list(d.run())
    assert d.assessments[dl205].tier == 0
    assert _dispatched_codes(d, entries)[:4] == ["DL205", "DL201", "DL203", "DL202"]
