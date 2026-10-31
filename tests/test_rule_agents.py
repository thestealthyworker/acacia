"""Rule agents (HANDOVER 8.2, PLAN 3.3 and 3.4): declarations and decisions."""

from __future__ import annotations

import pytest

from acacia.agents import AgentModeUnavailable, make_agents
from acacia.agents.base import Agent, AgentTimeout, private_for
from acacia.agents.rule import RuleATC, RuleGround, RulePilot
from acacia.models import (
    AgentRole, AtcPrivate, ConstraintPacket, DisruptionEvent, GroundPrivate, PilotPrivate,
    PrivateState, ReasonCode, ResolutionPackage, Verdict,
)
from acacia.timeutil import to_hhmm
from acacia.triangulate import triangulate

# DL1639: E = 12:11 (731), Ti = 10, S = 12:00 (720)
ATC = AtcPrivate(first=740, free_slots=[740, 748, 760, 772], demand={740: 4, 748: 3, 760: 2, 772: 1},
                 traffic=[732, 736, 744, 752, 756, 764, 768, 776], closures=[(700, 760)])
PILOT = PilotPrivate(land_by=790, pax=38, connect_by=760)
GROUND = GroundPrivate(gates=["A7", "A10", "A12"], assigned_gate="A7",
                       free_from={"A7": 761, "A10": None, "A12": 750}, failed=["A10"],
                       min_turn=35, shift_end=816, duty_limit=861, next_departure=820)


@pytest.fixture
def event(dl1639_row) -> DisruptionEvent:
    return triangulate(dl1639_row)


def _pkg(event: DisruptionEvent, slot: int, gate: str) -> ResolutionPackage:
    return ResolutionPackage(event_id=event.event_id, package_id="P1", turn=2, slot=to_hhmm(slot),
                             gate=gate, score=0.5, score_breakdown={})


# --- declarations -------------------------------------------------------------------------
def test_atc_declares_slots_separation_and_demand(event) -> None:
    k = RuleATC().declare(event, ATC)
    assert isinstance(k, ConstraintPacket) and k.agent == AgentRole.ATC and k.turn == 1
    hard = {h.id: h for h in k.hard}
    assert hard["A_H1"].rule == "SLOT_IN_SEQUENCE"
    assert hard["A_H1"].value == ["12:20", "12:28", "12:40", "12:52"]
    assert hard["A_H2"].rule == "MIN_SEPARATION_MIN" and hard["A_H2"].value == 3
    assert [(s.id, s.metric, s.weight) for s in k.soft] == [("A_S1", "slot_demand", 0.7)]
    assert [(o.type, o.value, o.demand) for o in k.options][:2] == [("SLOT", "12:20", 4), ("SLOT", "12:28", 3)]
    assert "12:24" in k.facts_owned["traffic"] or "12:16" in k.facts_owned["traffic"]


def test_pilot_declares_land_by_and_prefs(event) -> None:
    k = RulePilot().declare(event, PILOT)
    assert [(h.id, h.rule, h.value) for h in k.hard] == [("P_H1", "LAND_BY", "13:10")]
    soft = {s.id: s for s in k.soft}
    assert soft["P_S1"].metric == "gate_arrival_by" and soft["P_S1"].target == "12:40"
    assert soft["P_S1"].stake == 38 and soft["P_S1"].weight == 0.7
    assert soft["P_S2"].metric == "fuel_margin" and soft["P_S2"].weight == 0.3
    assert k.options == []


def test_ground_declares_gates_duty_min_turn(event) -> None:
    k = RuleGround().declare(event, GROUND)
    hard = {h.id: h for h in k.hard}
    assert hard["G_H1"].rule == "GATE_FREE_FROM"
    assert hard["G_H1"].value == {"A7": "12:41", "A10": "unavailable", "A12": "12:30"}
    assert hard["G_H2"].rule == "CREW_DUTY_LIMIT" and hard["G_H2"].value == "14:21"
    soft = {s.id: s for s in k.soft}
    assert soft["G_S1"].metric == "turnaround_done_by" and soft["G_S1"].target == "13:36"
    assert soft["G_S2"].metric == "keep_gate" and soft["G_S2"].target == "A7"
    assert [o.value for o in k.options] == ["A7", "A12"]
    assert k.facts_owned["min_turn_min"] == 35


def test_declare_accepts_dict_private(event) -> None:
    k = RulePilot().declare(event, PILOT.model_dump())
    assert k.hard[0].value == "13:10"


# --- decisions -----------------------------------------------------------------------------
def _decide(agent, event, private, slot, gate):
    packet = agent.declare(event, private)
    return agent.decide(event, _pkg(event, slot, gate), private, packet), packet


def test_atc_rejects_slot_outside_sequence(event) -> None:
    d, packet = _decide(RuleATC(), event, ATC, 744, "A12")
    assert d.verdict == Verdict.REJECT and d.reason_code == ReasonCode.SEPARATION and d.cites == "A_H1"
    assert d.cites in packet.ids()
    assert _decide(RuleATC(), event, ATC, 748, "A12")[0].verdict == Verdict.ACCEPT


def test_pilot_fuel_limit_boundary(event) -> None:
    pilot = PILOT.model_copy(update={"connect_by": 800})
    assert _decide(RulePilot(), event, pilot, 790, "A12")[0].verdict == Verdict.ACCEPT
    d, _ = _decide(RulePilot(), event, pilot, 791, "A12")
    assert d.reason_code == ReasonCode.FUEL_LIMIT and d.cites == "P_H1"


def test_pilot_connection_red_line(event) -> None:
    pilot = PilotPrivate(land_by=900, pax=25, connect_by=760)
    # gate arrival 789 = connect_by + 29: utility > 0, accept
    assert _decide(RulePilot(), event, pilot, 779, "A12")[0].verdict == Verdict.ACCEPT
    # gate arrival 790 = connect_by + 30: connections utility 0 and pax >= 25 -> reject
    d, _ = _decide(RulePilot(), event, pilot, 780, "A12")
    assert d.reason_code == ReasonCode.CONNECTION_RISK and d.cites == "P_S1"
    few = pilot.model_copy(update={"pax": 24})
    assert _decide(RulePilot(), event, few, 780, "A12")[0].verdict == Verdict.ACCEPT


def test_ground_gate_conflicts(event) -> None:
    d, _ = _decide(RuleGround(), event, GROUND, 760, "A10")  # failed gate
    assert d.reason_code == ReasonCode.GATE_CONFLICT and d.cites == "G_H1"
    d, _ = _decide(RuleGround(), event, GROUND, 750, "A7")  # arrive 760 < free 761
    assert d.reason_code == ReasonCode.GATE_CONFLICT
    assert _decide(RuleGround(), event, GROUND, 751, "A7")[0].verdict == Verdict.ACCEPT
    assert _decide(RuleGround(), event, GROUND, 740, "REMOTE-1")[0].verdict == Verdict.ACCEPT
    d, _ = _decide(RuleGround(), event, GROUND, 740, "B99")  # unknown gate
    assert d.reason_code == ReasonCode.GATE_CONFLICT


def test_ground_duty_breach_and_overtime_red_line(event) -> None:
    g = GROUND.model_copy(update={"shift_end": 790, "duty_limit": 835})
    # arrive 760, done 795: overtime 5 -> accept
    assert _decide(RuleGround(), event, g, 750, "A12")[0].verdict == Verdict.ACCEPT
    # overtime exactly 20 -> accept; 21 -> reject citing G_S1
    assert _decide(RuleGround(), event, g, 765, "A12")[0].verdict == Verdict.ACCEPT
    d, _ = _decide(RuleGround(), event, g, 766, "A12")
    assert d.reason_code == ReasonCode.CREW_DUTY and d.cites == "G_S1"
    # done 836 > duty 835 -> reject citing G_H2
    d, _ = _decide(RuleGround(), event, g, 791, "A12")
    assert d.reason_code == ReasonCode.CREW_DUTY and d.cites == "G_H2"


def test_every_reject_cites_own_packet(event) -> None:
    private = PrivateState(atc=ATC, pilot=PILOT, ground=GROUND)
    for agent in make_agents("rule").values():
        own = private_for(agent.role, private)
        packet = agent.declare(event, own)
        for slot in range(700, 900, 3):
            for gate in ("A7", "A10", "A12", "REMOTE-1"):
                d = agent.decide(event, _pkg(event, slot, gate), own, packet)
                assert d.agent == agent.role and d.turn == 2
                if d.verdict == Verdict.REJECT:
                    assert d.cites in packet.ids()


# --- interface and factory -----------------------------------------------------------------
def test_factory_rule_mode_returns_three_protocol_agents() -> None:
    agents = make_agents("rule")
    assert set(agents) == set(AgentRole)
    for role, agent in agents.items():
        assert agent.role == role and isinstance(agent, Agent)


def test_factory_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        make_agents("magic")  # type: ignore[arg-type]


def test_factory_missing_llm_module_is_clear(monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "acacia.agents.llm", None)
    monkeypatch.setitem(sys.modules, "acacia.agents.replay", None)
    for mode in ("llm", "replay"):
        with pytest.raises(AgentModeUnavailable, match=mode):
            make_agents(mode)


def test_agent_timeout_is_exception() -> None:
    assert issubclass(AgentTimeout, Exception)
