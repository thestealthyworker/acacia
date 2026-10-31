"""Solver (HANDOVER 7, PLAN 3.2 and 3.6). Test 6: never breaks a declared hard limit.
Test 7: reads declarations only (no private-state parameter; garbage changes nothing)."""

from __future__ import annotations

import inspect

from hypothesis import given, settings
from hypothesis import strategies as st

from acacia.config import POLICY
from acacia.constraints import time_budget_min
from acacia.models import (
    AgentRole, ConstraintPacket, Decision, HardLimit, Option, ReasonCode, SoftPref, Stress, Verdict,
)
from acacia.solver import Cut, SolveResult, cut_from_reject, package_key, solve
from acacia.timeutil import to_hhmm, to_min

EID = "EVT-1"
TI = 10


def atc(slots: dict[int, int], traffic: list[int] | None = None) -> ConstraintPacket:
    return ConstraintPacket(
        event_id=EID, agent=AgentRole.ATC, turn=1,
        hard=[HardLimit(id="A_H1", rule="SLOT_IN_SEQUENCE", value=[to_hhmm(s) for s in slots]),
              HardLimit(id="A_H2", rule="MIN_SEPARATION_MIN", value=3)],
        soft=[SoftPref(id="A_S1", preference="low demand", weight=0.7, metric="slot_demand")],
        options=[Option(type="SLOT", value=to_hhmm(s), demand=d) for s, d in slots.items()],
        facts_owned={"traffic": [to_hhmm(t) for t in (traffic or [])]},
    )


def pilot(land_by: int, connect_by: int, pax: int = 38, crew_end: int | None = None,
          eta: int | None = None) -> ConstraintPacket:
    hard = [HardLimit(id="P_H1", rule="LAND_BY", value=to_hhmm(land_by))]
    if crew_end is not None:
        hard.append(HardLimit(id="P_H2", rule="FLIGHT_CREW_DUTY_END", value=to_hhmm(crew_end)))
    return ConstraintPacket(
        event_id=EID, agent=AgentRole.PILOT, turn=1, hard=hard,
        soft=[SoftPref(id="P_S1", preference="connections", weight=0.7, metric="gate_arrival_by",
                       target=to_hhmm(connect_by), stake=pax),
              SoftPref(id="P_S2", preference="fuel", weight=0.3, metric="fuel_margin",
                       target=None if eta is None else to_hhmm(eta))],
        options=[],
    )


def ground(free: dict[str, int | None], duty: int, shift_end: int, min_turn: int = 35,
           assigned: str | None = None, decided: int | None = None) -> ConstraintPacket:
    gates = list(free)
    hard = [HardLimit(id="G_H1", rule="GATE_FREE_FROM",
                      value={g: ("unavailable" if t is None else to_hhmm(t)) for g, t in free.items()}),
            HardLimit(id="G_H2", rule="CREW_DUTY_LIMIT", value=to_hhmm(duty))]
    if decided is not None:
        hard.append(HardLimit(id="G_H3", rule="GATE_CHANGE_NOTICE", value={
            "min_notice_min": 20, "decision_time": to_hhmm(decided), "assigned_gate": assigned or gates[0]}))
    return ConstraintPacket(
        event_id=EID, agent=AgentRole.GROUND, turn=1, hard=hard,
        soft=[SoftPref(id="G_S1", preference="no overtime", weight=0.6, metric="turnaround_done_by",
                       target=to_hhmm(shift_end)),
              SoftPref(id="G_S2", preference="keep gate", weight=0.4, metric="keep_gate",
                       target=assigned or gates[0])],
        options=[Option(type="GATE", value=g) for g, t in free.items() if t is not None],
        facts_owned={"min_turn_min": min_turn},
    )


def packets(**kw) -> dict[AgentRole, ConstraintPacket]:
    return {
        AgentRole.ATC: kw.get("atc", atc({740: 4, 748: 3, 760: 2, 772: 1}, [744, 752, 756, 764])),
        AgentRole.PILOT: kw.get("pilot", pilot(790, 760)),
        AgentRole.GROUND: kw.get("ground", ground({"A7": 761, "A10": None, "A12": 750}, 861, 816)),
    }


def _feasible(r: SolveResult):
    return [c for c in r.all if c.status == "FEASIBLE"]


# --- steps 1 to 4 -------------------------------------------------------------------------
def test_enumerates_slot_by_gate_and_numbers_packages() -> None:
    r = solve(packets(), TI, [], set())
    assert [c.package.package_id for c in sorted(r.all, key=lambda c: int(c.package.package_id[1:]))] == [
        f"P{i}" for i in range(1, 9)]  # 4 slots x 2 offered gates
    assert {(c.package.slot, c.package.gate) for c in r.all} >= {("12:20", "A7"), ("12:52", "A12")}


def test_hard_filter_reasons() -> None:
    p = packets(pilot=pilot(755, 760),
                ground=ground({"A7": 761, "A12": 750}, duty=800, shift_end=790))
    r = solve(p, TI, [], set())
    by = {(c.package.slot, c.package.gate): c for c in r.all}
    assert ReasonCode.GATE_CONFLICT in by[("12:20", "A7")].discard  # arrive 750 < 761
    assert ReasonCode.FUEL_LIMIT in by[("12:40", "A12")].discard    # 760 > 755
    assert ReasonCode.CREW_DUTY in by[("12:52", "A12")].discard     # 772+10+35 > 800
    assert by[("12:20", "A12")].status == "FEASIBLE"
    assert by[("12:20", "A12")].package.satisfies_hard is True
    assert by[("12:20", "A7")].package.satisfies_hard is False


def test_scoring_matches_formula() -> None:
    r = solve(packets(), TI, [], set())
    c = next(c for c in r.all if (c.package.slot, c.package.gate) == ("12:40", "A7"))
    # demand 2 -> 1-(1/3); arrive 770 vs 760 -> 1-10/30; fuel (790-760)/45; overtime 0, assigned gate
    exp = {"throughput": 2 / 3, "connections": 2 / 3, "fuel": 30 / 45, "turnaround": 1.0}
    for k, u in exp.items():
        assert abs(c.package.score_breakdown[k] - POLICY[k] * u) < 1e-3
    assert abs(c.package.score - sum(POLICY[k] * u for k, u in exp.items())) < 1e-3
    assert abs(c.utilities["P_S1"] - 2 / 3) < 1e-9 and c.utilities["G_S2"] == 1.0


def test_top_is_highest_score_and_result_ranked() -> None:
    r = solve(packets(), TI, [], set())
    feas = _feasible(r)
    assert r.top is not None and r.top == feas[0].package
    scores = [c.package.score for c in feas]
    assert scores == sorted(scores, reverse=True)
    assert r.top.turn == 2 and r.top.event_id == EID


def test_overtime_lowers_turnaround() -> None:
    p = packets(ground=ground({"A12": 700}, duty=900, shift_end=780))
    r = solve(p, TI, [], set())
    c = next(c for c in r.all if c.package.slot == "12:52")  # done 817, overtime 37
    assert abs(c.utilities["G_S1"] - (1 - 1.5 * 37 / 60)) < 1e-9  # overtime costs 1.5x (CREW-2)


# --- steps 5 and 6: cuts and tried -----------------------------------------------------------
def test_tried_packages_excluded() -> None:
    first = solve(packets(), TI, [], set()).top
    r = solve(packets(), TI, [], {package_key(first.slot, first.gate)}, turn=3)
    assert r.top is not None and package_key(r.top.slot, r.top.gate) != package_key(first.slot, first.gate)
    assert any(c.status == "TRIED" for c in r.all)


def _reject(agent: AgentRole, code: ReasonCode, cites: str, pid: str = "P1") -> Decision:
    return Decision(event_id=EID, package_id=pid, agent=agent, turn=2, verdict=Verdict.REJECT,
                    reason_code=code, cites=cites, rationale="no")


def test_soft_cut_requires_strictly_higher_utility() -> None:
    p = packets()
    top = solve(p, TI, [], set()).top
    cut = cut_from_reject(_reject(AgentRole.PILOT, ReasonCode.CONNECTION_RISK, "P_S1"), top, p, TI)
    assert isinstance(cut, Cut) and cut.kind == "SOFT" and cut.pref == "P_S1"
    r = solve(p, TI, [cut], {package_key(top.slot, top.gate)}, turn=3)
    for c in _feasible(r):
        assert c.utilities["P_S1"] > cut.min_utility
    assert all(c.utilities["P_S1"] <= cut.min_utility for c in r.all if c.status == "CUT")


def test_hard_cuts_per_plan() -> None:
    p = packets()
    top = solve(p, TI, [], set()).top
    cases = {
        (AgentRole.ATC, "A_H1"): lambda c: c.package.slot != top.slot,
        (AgentRole.GROUND, "G_H1"): lambda c: c.package.gate != top.gate,
        (AgentRole.PILOT, "P_H1"): lambda c: to_min(c.package.slot) < to_min(top.slot),
        (AgentRole.GROUND, "G_H2"): lambda c: to_min(c.package.slot) < to_min(top.slot),
    }
    for (agent, cites), ok in cases.items():
        cut = cut_from_reject(_reject(agent, ReasonCode.SEPARATION, cites), top, p, TI)
        assert cut is not None
        r = solve(p, TI, [cut], set(), turn=3)
        assert _feasible(r) and all(ok(c) for c in _feasible(r)), cites


def test_accept_and_unknown_cite_make_no_cut() -> None:
    p = packets()
    top = solve(p, TI, [], set()).top
    acc = Decision(event_id=EID, package_id="P1", agent=AgentRole.ATC, turn=2,
                   verdict=Verdict.ACCEPT, rationale="ok")
    assert cut_from_reject(acc, top, p, TI) is None
    assert cut_from_reject(_reject(AgentRole.ATC, ReasonCode.SEPARATION, "P_S1"), top, p, TI) is None


def test_empty_feasible_set() -> None:
    r = solve(packets(pilot=pilot(700, 760)), TI, [], set())
    assert r.top is None and r.all and not _feasible(r)
    assert solve({AgentRole.PILOT: pilot(790, 760)}, TI, [], set()).top is None


# --- corrupt_solver stress -------------------------------------------------------------------
def test_corrupt_returns_hard_breaking_package_on_first_solve() -> None:
    p = packets(pilot=pilot(755, 760))
    r = solve(p, TI, [], set(), stress=Stress(corrupt_solver=True), turn=2)
    assert r.corrupted and r.top is not None and r.top.satisfies_hard is True
    assert r.corrupt_breaks
    c = next(c for c in r.all if c.package.package_id == r.top.package_id)
    assert c.discard  # it really breaks a declared hard limit
    later = solve(p, TI, [], set(), stress=Stress(corrupt_solver=True), turn=3)
    assert not later.corrupted


def test_corrupt_moves_slot_onto_declared_traffic_when_all_feasible() -> None:
    p = packets(ground=ground({"A12": 600}, duty=1200, shift_end=1100),
                atc=atc({740: 1, 760: 2}, [744, 752, 764]))
    clean = solve(p, TI, [], set())
    assert all(c.status == "FEASIBLE" for c in clean.all)
    r = solve(p, TI, [], set(), stress=Stress(corrupt_solver=True))
    assert r.corrupted and r.top.satisfies_hard is True
    assert r.top.slot not in {"12:20", "12:40"}
    assert to_min(r.top.slot) in {744, 752, 764}
    assert "SEPARATION" in r.corrupt_breaks


# --- test 6: property over random declared packets --------------------------------------------
@st.composite
def declared(draw):
    slots = draw(st.dictionaries(st.integers(600, 900), st.integers(1, 4), min_size=0, max_size=6))
    gates = draw(st.dictionaries(st.sampled_from(["A1", "A2", "B3", "C4", "REMOTE-1"]),
                                 st.one_of(st.none(), st.integers(560, 950)), min_size=0, max_size=5))
    land_by = draw(st.integers(580, 960))
    duty = draw(st.integers(620, 1100))
    min_turn = draw(st.sampled_from([35, 45, 60]))
    taxi = draw(st.integers(3, 25))
    crew_end = draw(st.one_of(st.none(), st.integers(600, 1000)))
    decided = draw(st.one_of(st.none(), st.integers(560, 900)))
    p = {AgentRole.ATC: atc(slots),
         AgentRole.PILOT: pilot(land_by, draw(st.integers(600, 900)), crew_end=crew_end)}
    if gates:
        p[AgentRole.GROUND] = ground(gates, duty, draw(st.integers(600, 1000)), min_turn,
                                     decided=decided)
    limits = {"crew_end": crew_end, "decided": decided if gates else None,
              "assigned": next(iter(gates), None)}
    return p, taxi, land_by, duty, min_turn, gates, limits


@settings(max_examples=300, deadline=None, derandomize=True)
@given(declared())
def test_solver_never_breaks_declared_hard_limit(data) -> None:
    p, taxi, land_by, duty, min_turn, gates, limits = data
    r = solve(p, taxi, [], set())
    for c in _feasible(r) + ([] if r.top is None else [next(x for x in r.all if x.package == r.top)]):
        slot = to_min(c.package.slot)
        assert slot <= land_by
        free = gates.get(c.package.gate)
        assert free is not None or c.package.gate.startswith("REMOTE")
        if free is not None:
            assert slot + taxi >= free
        assert slot + taxi + min_turn <= duty
        if limits["crew_end"] is not None:
            assert slot + taxi <= limits["crew_end"]
        if limits["decided"] is not None and c.package.gate != limits["assigned"]:
            assert slot + taxi - (limits["decided"] + time_budget_min()) >= 20  # from commit, TIME-4
        assert c.package.satisfies_hard


# --- test 7: isolation from private state ------------------------------------------------------
def test_solve_signature_has_no_private_state() -> None:
    params = set(inspect.signature(solve).parameters)
    assert params == {"packets", "taxi_in", "cuts", "tried", "stress", "turn"}
    assert not any("private" in n or "state" in n for n in params)


def test_results_identical_when_garbage_changes() -> None:
    from acacia.models import AtcPrivate, GroundPrivate, PilotPrivate, PrivateState

    private = PrivateState(
        atc=AtcPrivate(first=740, free_slots=[740], demand={740: 1}, traffic=[744], closures=[]),
        pilot=PilotPrivate(land_by=790, pax=38, connect_by=760),
        ground=GroundPrivate(gates=["A7"], assigned_gate="A7", free_from={"A7": 700}, failed=[],
                             min_turn=35, shift_end=816, duty_limit=861, next_departure=820))
    p = packets()
    before = solve(p, TI, [], set()).model_dump()
    garbage = private.model_copy(update={
        "atc": private.atc.model_copy(update={"free_slots": [1], "traffic": list(range(0, 1440))}),
        "pilot": private.pilot.model_copy(update={"land_by": -999, "pax": 10**6}),
        "ground": private.ground.model_copy(update={"free_from": {"A7": None}, "duty_limit": 0}),
    })
    private = garbage  # replace the private state passed alongside with garbage
    after = solve(p, TI, [], set()).model_dump()
    assert before == after
    try:
        solve(p, TI, [], set(), private=private)  # type: ignore[call-arg]
    except TypeError:
        pass
    else:
        raise AssertionError("solve must not accept private state")


def test_rule_agent_packets_feed_the_solver(dl1639_row) -> None:
    """Rule agents' declarations drive the solver; feasible packages pass every agent's hard rules."""
    from acacia.agents import make_agents
    from acacia.models import AtcPrivate, GroundPrivate, PilotPrivate, PrivateState
    from acacia.agents.base import private_for
    from acacia.triangulate import triangulate

    event = triangulate(dl1639_row)
    state = PrivateState(
        atc=AtcPrivate(first=740, free_slots=[740, 748, 760], demand={740: 4, 748: 3, 760: 2},
                       traffic=[736, 744, 752, 756, 764], closures=[(700, 760)]),
        pilot=PilotPrivate(land_by=790, pax=38, connect_by=760),
        ground=GroundPrivate(gates=["A7", "A10", "A12"], assigned_gate="A7",
                             free_from={"A7": 761, "A10": None, "A12": 745}, failed=["A10"],
                             min_turn=35, shift_end=816, duty_limit=861, next_departure=820))
    agents = make_agents("rule")
    p = {role: a.declare(event, private_for(role, state)) for role, a in agents.items()}
    r = solve(p, event.taxi_in, [], set())
    assert r.top is not None and r.top.event_id == event.event_id
    for c in _feasible(r):
        for role, a in agents.items():
            d = a.decide(event, c.package, private_for(role, state), p[role])
            assert d.verdict == Verdict.ACCEPT or d.cites not in {"A_H1", "P_H1", "G_H1", "G_H2"}
    bad = solve(p, event.taxi_in, [], set(), stress=Stress(corrupt_solver=True))
    assert bad.corrupted and bad.top.satisfies_hard


# --- CONSTRAINTS.md hard limits declared by the agents ----------------------------------------
def _cand(r: SolveResult, slot: int, gate: str):
    return next(c for c in r.all if c.package.slot == to_hhmm(slot) and c.package.gate == gate)


def test_gate_change_notice_discards_late_gate_changes() -> None:
    # decided 12:25 + 5 min budget = commit 12:30 (750, TIME-4): A12 at slot 760 -> arrive 770,
    # 20 min notice passes; 748 -> 758 fails
    g = ground({"A7": 700, "A12": 700}, 900, 900, assigned="A7", decided=745)
    r = solve(packets(ground=g), TI, [], set())
    assert ReasonCode.GATE_CHANGE_NOTICE in _cand(r, 748, "A12").discard
    assert _cand(r, 760, "A12").discard == []
    assert _cand(r, 748, "A7").discard == []  # the assigned gate needs no notice


def test_flight_crew_duty_end_discards_late_gate_arrival() -> None:
    r = solve(packets(pilot=pilot(790, 760, crew_end=758)), TI, [], set())
    assert _cand(r, 748, "A12").discard == []  # arrive 758 == crew end
    assert ReasonCode.CREW_DUTY in _cand(r, 760, "A12").discard


def test_crew_duty_cite_cuts_slot_and_later() -> None:
    p = packets(pilot=pilot(790, 760, crew_end=900))
    top = solve(p, TI, [], set()).top
    cut = cut_from_reject(_reject(AgentRole.PILOT, ReasonCode.CREW_DUTY, "P_H2", top.package_id), top, p, TI)
    assert cut is not None and cut.kind == "SLOT_FROM" and cut.slot == top.slot


def test_fuel_utility_spans_land_by_minus_declared_eta() -> None:
    # land_by 790, E 760: span 30, so slot 760 -> 1.0, 772 -> 18/30 = 0.6 (45 min span would give 0.4)
    r = solve(packets(pilot=pilot(790, 760, eta=760)), TI, [], set())
    assert _cand(r, 772, "A12").utilities["P_S2"] == 0.6
    plain = solve(packets(), TI, [], set())
    assert plain.all and _cand(plain, 772, "A12").utilities["P_S2"] == 0.4


# --- CONSTRAINTS.md 4-5: fuel span, overtime cost, informational keys, G_H3 cut ------------------
def _with_facts(packet: ConstraintPacket, **facts) -> ConstraintPacket:
    return packet.model_copy(update={"facts_owned": {**packet.facts_owned, **facts}})


def test_crew_utility_is_one_minus_1_5_overtime_over_60_and_floors_at_zero() -> None:
    p = packets(ground=ground({"A12": 700}, duty=900, shift_end=742))  # 740+10+35 = 785: 43 min over
    r = solve(p, TI, [], set())
    assert _cand(r, 740, "A12").utilities["G_S1"] == 0.0  # 1 - 1.5*43/60 < 0
    p2 = packets(ground=ground({"A12": 700}, duty=900, shift_end=785))
    assert solve(p2, TI, [], set()).all and _cand(solve(p2, TI, [], set()), 740, "A12").utilities["G_S1"] == 1.0


def test_fuel_span_uses_pilot_declared_eta_fact() -> None:
    pil = _with_facts(pilot(790, 760), eta=to_hhmm(760))  # no P_S2 target: the fact gives E
    r = solve(packets(pilot=pil), TI, [], set())
    assert _cand(r, 772, "A12").utilities["P_S2"] == 0.6  # (790-772)/(790-760)


def test_informational_keys_are_reported_but_not_scored() -> None:
    pil = _with_facts(pilot(790, 760), eta=to_hhmm(730), burn_kg_per_h=2400)
    p = packets(pilot=pil, ground=ground({"A12": 700}, duty=900, shift_end=780))
    c = _cand(solve(p, TI, [], set()), 772, "A12")
    bd = c.package.score_breakdown
    assert bd["holding_fuel_kg"] == 1680.0      # (772 - 730) min x 2400 kg/h / 60
    assert bd["overtime_cost_min"] == 55.5      # 1.5 x (772+10+35 - 780 = 37)
    weighted = sum(bd[k] for k in POLICY)
    assert abs(c.package.score - weighted) < 1e-9
    assert set(bd) == set(POLICY) | {"holding_fuel_kg", "overtime_cost_min"}


def test_holding_fuel_omitted_without_declared_burn() -> None:
    c = _cand(solve(packets(), TI, [], set()), 760, "A12")  # done 805 <= shift end 816
    assert "holding_fuel_kg" not in c.package.score_breakdown
    assert c.package.score_breakdown["overtime_cost_min"] == 0.0


def test_gate_change_notice_cite_cuts_that_gate() -> None:
    p = packets()
    top = solve(p, TI, [], set()).top
    rej = _reject(AgentRole.GROUND, ReasonCode.GATE_CHANGE_NOTICE, "G_H3", top.package_id)
    g = p[AgentRole.GROUND]
    p[AgentRole.GROUND] = g.model_copy(update={"hard": [*g.hard, HardLimit(
        id="G_H3", rule="GATE_CHANGE_NOTICE",
        value={"min_notice_min": 20, "decision_time": "12:00", "assigned_gate": "A7"})]})
    cut = cut_from_reject(rej, top, p, TI)
    assert cut is not None and cut.kind == "GATE" and cut.gate == top.gate
    again = solve(p, TI, [cut], set())
    assert all(c.status != "FEASIBLE" for c in again.all if c.package.gate == top.gate)


def test_slot_exactly_at_land_by_is_feasible() -> None:
    r = solve(packets(pilot=pilot(760, 760)), TI, [], set())
    assert ReasonCode.FUEL_LIMIT not in _cand(r, 760, "A12").discard
    assert ReasonCode.FUEL_LIMIT in _cand(r, 772, "A12").discard
