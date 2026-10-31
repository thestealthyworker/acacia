"""LLM and replay agents (HANDOVER 8.3), offline with a fake Anthropic client."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from acacia.agents import make_agents
from acacia.agents.base import Agent, AgentTimeout
from acacia.agents.llm import (
    DECIDE_TOOL, DECLARE_TOOL, LLMAgent, audit_declaration, decision_time_min, expected_declaration,
    make_llm_agents, recording_path, tool_choice_for, wake_value,
)
from acacia.agents.replay import RecordingMissingError, ReplayAgent, ReplayMismatchWarning, make_replay_agents
from acacia.agents.rule import RuleATC, RuleGround, RulePilot
from acacia.models import (
    AgentRole, AtcPrivate, ConstraintPacket, Decision, DisruptionEvent, GroundPrivate, HardLimit,
    PilotPrivate, ReasonCode, ResolutionPackage, Verdict,
)
from acacia.triangulate import triangulate

ATC = AtcPrivate(first=740, free_slots=[740, 748, 760, 772], demand={740: 4, 748: 3, 760: 2, 772: 1},
                 traffic=[732, 736, 744, 752, 756, 764, 768, 776], closures=[(700, 760)],
                 own_class="Large", traffic_classes={732: "Heavy", 736: "Large", 744: "Small"})
PILOT = PilotPrivate(land_by=790, pax=38, connect_by=760)
GROUND = GroundPrivate(gates=["A7", "A10", "A12"], assigned_gate="A7",
                       free_from={"A7": 761, "A10": None, "A12": 750}, failed=["A10"],
                       min_turn=35, shift_end=816, duty_limit=861, next_departure=820)
PRIVATE = {AgentRole.ATC: ATC, AgentRole.PILOT: PILOT, AgentRole.GROUND: GROUND}
RULE = {AgentRole.ATC: RuleATC(), AgentRole.PILOT: RulePilot(), AgentRole.GROUND: RuleGround()}


# --- fake client ------------------------------------------------------------------------------
class FakeMessages:
    """Serves queued tool inputs (dict), raw responses (SimpleNamespace) or exceptions in order."""

    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("fake client ran out of responses")
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, SimpleNamespace):
            return out
        block = SimpleNamespace(type="tool_use", id="toolu_1", name=kwargs["tools"][0]["name"], input=out)
        return SimpleNamespace(content=[block], stop_reason="tool_use", model=kwargs["model"])


class FakeClient:
    def __init__(self, outputs: list[Any]) -> None:
        self.messages = FakeMessages(outputs)


def _dump(model: ConstraintPacket | Decision) -> dict:
    return model.model_dump(mode="json")


def _complete(event: DisruptionEvent, role: AgentRole) -> ConstraintPacket:
    """A rule packet with every hard limit CONSTRAINTS.md asks for (wake, gate-change notice).

    Independent of whether rule.py has moved to the challenge limits yet: limits are replaced by id.
    """
    packet = RULE[role].declare(event, PRIVATE[role])
    hard = {h.id: h for h in packet.hard}
    for kind, limit_id, rule, value in expected_declaration(role, PRIVATE[role], event):
        if kind == "hard":
            hard[limit_id] = HardLimit(id=limit_id, rule=rule, value=value)
    return packet.model_copy(update={"hard": list(hard.values())})


@pytest.fixture
def event(dl1639_row) -> DisruptionEvent:
    return triangulate(dl1639_row)


def _pkg(event: DisruptionEvent, slot: str = "12:40", gate: str = "A12", pid: str = "P1") -> ResolutionPackage:
    return ResolutionPackage(event_id=event.event_id, package_id=pid, turn=2, slot=slot, gate=gate,
                             score=0.5, score_breakdown={})


# --- declare / decide ----------------------------------------------------------------------------
def test_valid_declare_one_forced_tool_call(event) -> None:
    expected = _complete(event, AgentRole.ATC)
    client = FakeClient([_dump(expected)])
    agent = LLMAgent(AgentRole.ATC, client=client, model="claude-haiku-5-5")
    assert isinstance(agent, Agent)
    packet = agent.declare(event, ATC)
    assert packet == expected
    assert agent.last_audit == []
    [call] = client.messages.calls
    assert "Air Traffic Control" in call["system"]
    assert call["tools"][0]["name"] == DECLARE_TOOL
    assert call["tools"][0]["input_schema"] == ConstraintPacket.model_json_schema()
    assert call["tool_choice"] == {"type": "tool", "name": DECLARE_TOOL}
    assert call["timeout"] > 0 and "temperature" not in call
    user = call["messages"][0]["content"]
    assert event.event_id in user and "12:20" in user  # shared event + own private state (HH:MM)
    assert "land_by" not in user and "duty_limit" not in user  # never another agent's state
    assert '"own_wake_class": "Large"' in user and '"Heavy>Large": 2.1' in user  # workbook wake table


def test_each_role_gets_its_own_system_prompt(event) -> None:
    systems = {}
    for role in AgentRole:
        client = FakeClient([_dump(RULE[role].declare(event, PRIVATE[role]))])
        LLMAgent(role, client=client).declare(event, PRIVATE[role])
        systems[role] = client.messages.calls[0]["system"]
    assert len(set(systems.values())) == 3


def test_valid_decide(event) -> None:
    packet = RulePilot().declare(event, PILOT)
    expected = RulePilot().decide(event, _pkg(event), PILOT, packet)
    client = FakeClient([_dump(expected)])
    decision = LLMAgent(AgentRole.PILOT, client=client).decide(event, _pkg(event), PILOT, packet)
    assert decision == expected and decision.verdict == Verdict.ACCEPT
    [call] = client.messages.calls
    assert call["tools"][0]["name"] == DECIDE_TOOL
    assert call["tools"][0]["input_schema"] == Decision.model_json_schema()
    user = call["messages"][0]["content"]
    assert "P_H1" in user and "P1" in user and "12:50" in user  # own packet, package, gate arrival


def test_invalid_then_valid_retries_with_error(event) -> None:
    packet = RuleGround().declare(event, GROUND)
    good = RuleGround().decide(event, _pkg(event), GROUND, packet)
    bad = {**_dump(good), "verdict": "REJECT", "reason_code": None, "cites": None}
    client = FakeClient([bad, _dump(good)])
    decision = LLMAgent(AgentRole.GROUND, client=client).decide(event, _pkg(event), GROUND, packet)
    assert decision == good
    assert len(client.messages.calls) == 2
    retry_text = client.messages.calls[1]["messages"][-1]["content"]
    assert "reason_code" in retry_text and "invalid" in retry_text.lower()


def test_missing_tool_call_counts_as_invalid(event) -> None:
    text_only = SimpleNamespace(content=[SimpleNamespace(type="text", text="hello")], stop_reason="end_turn")
    good = RuleATC().declare(event, ATC)
    client = FakeClient([text_only, _dump(good)])
    assert LLMAgent(AgentRole.ATC, client=client).declare(event, ATC) == good


def test_two_invalid_outputs_raise_agent_timeout(event) -> None:
    client = FakeClient([{"nonsense": 1}, {"still": "bad"}])
    with pytest.raises(AgentTimeout) as info:
        LLMAgent(AgentRole.PILOT, client=client).declare(event, PILOT)
    assert info.value.role == AgentRole.PILOT and info.value.step == "declare"
    assert len(client.messages.calls) == 2


def test_wrong_identity_fields_are_invalid(event) -> None:
    good = RuleATC().declare(event, ATC)
    wrong = {**_dump(good), "agent": "PILOT"}
    client = FakeClient([wrong, wrong])
    with pytest.raises(AgentTimeout):
        LLMAgent(AgentRole.ATC, client=client).declare(event, ATC)


def test_api_timeout_raises_agent_timeout(event) -> None:
    err = anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    client = FakeClient([err])
    with pytest.raises(AgentTimeout) as info:
        LLMAgent(AgentRole.GROUND, client=client).declare(event, GROUND)
    assert info.value.step == "declare"


def test_reject_citing_foreign_id_still_parses(event) -> None:
    packet = RuleATC().declare(event, ATC)
    reject = {"event_id": event.event_id, "package_id": "P1", "agent": "ATC", "turn": 2,
              "verdict": "REJECT", "reason_code": "SEPARATION", "cites": "P_H1", "rationale": "No."}
    client = FakeClient([reject])
    decision = LLMAgent(AgentRole.ATC, client=client).decide(event, _pkg(event), ATC, packet)
    assert decision.verdict == Verdict.REJECT and decision.cites == "P_H1"
    assert decision.cites not in packet.ids()  # the coordinator converts it later


# --- declaration audit ----------------------------------------------------------------------------
def test_audit_flags_mismatched_hard_limit_without_correcting(event) -> None:
    lying = _dump(RulePilot().declare(event, PILOT))
    lying["hard"][0]["value"] = "13:30"  # truth is 13:10
    agent = LLMAgent(AgentRole.PILOT, client=FakeClient([lying]))
    packet = agent.declare(event, PILOT)
    assert packet.hard[0].value == "13:30"
    [note] = agent.last_audit
    assert note["id"] == "P_H1" and note["declared"] == "13:30" and note["expected"] == "13:10"
    assert "P_H1" in note["text"]


def test_audit_clean_for_constraint_complete_declarations(event) -> None:
    for role in AgentRole:
        assert audit_declaration(role, _complete(event, role), PRIVATE[role], event) == []


def test_audit_flags_missing_ground_limits(event) -> None:
    packet = _complete(event, AgentRole.GROUND).model_copy(update={"hard": [], "facts_owned": {}})
    ids = {n["id"] for n in audit_declaration(AgentRole.GROUND, packet, GROUND, event)}
    assert {"G_H1", "G_H2", "G_H3", "min_turn_min"} <= ids


def test_audit_flags_legacy_flat_separation(event) -> None:
    legacy = _complete(event, AgentRole.ATC)
    legacy = legacy.model_copy(update={"hard": [legacy.hard[0], HardLimit(id="A_H2", rule="MIN_SEPARATION_MIN",
                                                                          value=3)]})
    [note] = audit_declaration(AgentRole.ATC, legacy, ATC, event)
    assert note["id"] == "A_H2" and note["rule"] == "WAKE_SEPARATION" and "MIN_SEPARATION_MIN" in note["text"]


def test_wake_value_uses_workbook_minutes_at_140_kt() -> None:
    value = wake_value("Large")
    assert value["own_class"] == "Large" and value["table"] == "workbook"
    assert value["min_minutes"] == {"Heavy>Large": 2.1, "Large>Heavy": 1.3, "Large>Large": 1.3,
                                    "Large>Small": 1.7, "Small>Large": 1.3}
    assert wake_value("Heavy")["min_minutes"]["Heavy>Small"] == 2.6


def test_decision_time_payload_then_injected_start_then_eta_minus_30(event) -> None:
    assert decision_time_min(event) == 12 * 60 + 1  # earliest injected start 12:01
    assert decision_time_min(event.model_copy(update={"decision_time": "11:30"})) == 11 * 60 + 30
    bare = event.model_copy(update={"injected": [], "decision_time": None})
    assert decision_time_min(bare) == 12 * 60 + 11 - 30


def test_ground_gate_change_notice_declared_and_computed(event) -> None:
    short = event.model_copy(update={"decision_time": "12:40"})  # gate arrival 12:50 -> 10 min notice
    packet = _complete(short, AgentRole.GROUND)
    g_h3 = next(h for h in packet.hard if h.id == "G_H3")
    assert g_h3.rule == "GATE_CHANGE_NOTICE"
    assert g_h3.value == {"min_notice_min": 20, "decision_time": "12:40", "assigned_gate": "A7"}
    reject = {"event_id": event.event_id, "package_id": "P1", "agent": "GROUND", "turn": 2,
              "verdict": "REJECT", "reason_code": "GATE_CHANGE_NOTICE", "cites": "G_H3",
              "rationale": "Only 10 min notice for a move off A7."}
    client = FakeClient([reject])
    decision = LLMAgent(AgentRole.GROUND, client=client).decide(short, _pkg(short), GROUND, packet)
    assert decision.reason_code == ReasonCode.GATE_CHANGE_NOTICE and decision.cites in packet.ids()
    user = client.messages.calls[0]["messages"][0]["content"]
    assert '"gate_change": true' in user and '"gate_change_notice_min": 10' in user
    assert '"overtime_min": 0' in user and '"turnaround_end": "13:25"' in user


def test_pilot_decide_gets_fuel_margin_computed_by_code(event) -> None:
    packet = RulePilot().declare(event, PILOT)
    client = FakeClient([_dump(RulePilot().decide(event, _pkg(event), PILOT, packet))])
    LLMAgent(AgentRole.PILOT, client=client).decide(event, _pkg(event), PILOT, packet)
    assert '"fuel_margin_min": 30' in client.messages.calls[0]["messages"][0]["content"]  # 13:10 - 12:40


def test_tool_choice_auto_for_models_without_forced_tools() -> None:
    assert tool_choice_for("claude-haiku-5-5", DECLARE_TOOL) == {"type": "tool", "name": DECLARE_TOOL}
    assert tool_choice_for("claude-sonnet-5-5", DECLARE_TOOL)["type"] == "auto"


# --- record and replay ----------------------------------------------------------------------------
def _record_all(event: DisruptionEvent, tmp_path) -> tuple[dict, dict]:
    packets, decisions = {}, {}
    for role in AgentRole:
        rule = RULE[role]
        pk = _complete(event, role)
        dec = rule.decide(event, _pkg(event), PRIVATE[role], pk)
        agent = LLMAgent(role, client=FakeClient([_dump(pk), _dump(dec)]), record=True,
                         recordings_dir=tmp_path)
        packets[role] = agent.declare(event, PRIVATE[role])
        decisions[role] = agent.decide(event, _pkg(event), PRIVATE[role], packets[role])
    return packets, decisions


def test_record_then_replay_round_trip(event, tmp_path) -> None:
    packets, decisions = _record_all(event, tmp_path)
    path = recording_path(tmp_path, event.event_id, AgentRole.GROUND, "decide", 2)
    assert path.exists() and path.name == "ground-decide-2.json"
    saved = json.loads(path.read_text())
    assert saved["prompt_hash"] and saved["exchanges"][0]["request"]["system"]
    replay = make_replay_agents(recordings_dir=tmp_path)
    assert set(replay) == set(AgentRole)
    for role in AgentRole:
        assert replay[role].declare(event, PRIVATE[role]) == packets[role]
        assert replay[role].decide(event, _pkg(event), PRIVATE[role], packets[role]) == decisions[role]
        assert replay[role].last_audit == []


def test_replay_warns_on_prompt_hash_mismatch(event, tmp_path) -> None:
    packets, _ = _record_all(event, tmp_path)
    changed = PilotPrivate(land_by=795, pax=38, connect_by=760)
    with pytest.warns(ReplayMismatchWarning):
        packet = ReplayAgent(AgentRole.PILOT, recordings_dir=tmp_path).declare(event, changed)
    assert packet == packets[AgentRole.PILOT]


def test_replay_missing_recording_is_a_clear_error(event, tmp_path) -> None:
    with pytest.raises(RecordingMissingError, match="--record"):
        ReplayAgent(AgentRole.ATC, recordings_dir=tmp_path).declare(event, ATC)


def test_replay_of_recorded_failure_raises_agent_timeout(event, tmp_path) -> None:
    agent = LLMAgent(AgentRole.ATC, client=FakeClient([{}, {}]), record=True, recordings_dir=tmp_path)
    with pytest.raises(AgentTimeout):
        agent.declare(event, ATC)
    with pytest.raises(AgentTimeout):
        ReplayAgent(AgentRole.ATC, recordings_dir=tmp_path).declare(event, ATC)


def test_factories_return_one_agent_per_role() -> None:
    agents = make_llm_agents(client=FakeClient([]))
    assert set(agents) == set(AgentRole) and all(a.role == r for r, a in agents.items())
    assert set(make_agents("replay")) == set(AgentRole)


def test_real_sdk_client_over_mock_transport(event) -> None:
    """The request body passes the installed SDK and its typed response parses (no network)."""
    packet = RulePilot().declare(event, PILOT)
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": DECLARE_TOOL, "input": _dump(packet)}],
            "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    client = anthropic.Anthropic(api_key="test", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    assert LLMAgent(AgentRole.PILOT, client=client, model="claude-haiku-5-5").declare(event, PILOT) == packet
    assert "temperature" not in seen["body"] and seen["body"]["tool_choice"]["name"] == DECLARE_TOOL



def test_audit_flags_missing_or_wrong_pilot_eta_fact(event) -> None:
    packet = _complete(event, AgentRole.PILOT)
    assert packet.facts_owned["eta"] == event.eta_landing
    no_eta = packet.model_copy(update={"facts_owned": {"connecting_pax": 38}})
    [note] = audit_declaration(AgentRole.PILOT, no_eta, PILOT, event)
    assert note["id"] == "eta" and note["expected"] == event.eta_landing
    wrong = packet.model_copy(update={"facts_owned": {**packet.facts_owned, "eta": "12:30"}})
    assert [n["id"] for n in audit_declaration(AgentRole.PILOT, wrong, PILOT, event)] == ["eta"]
    assert audit_declaration(AgentRole.PILOT, no_eta, PILOT) == []  # no event: eta not audited


def test_private_views_carry_the_workbook_facts_the_prompts_ask_for(event) -> None:
    """Validator G3: pilot.md says declare burn_kg_per_h only if the private state gives it, and
    Ground declares spare_gates and shift_start; the LLM views must therefore carry them."""
    from acacia.agents.llm import private_view

    pilot = PILOT.model_copy(update={"wake_class": "Heavy", "burn_kg_per_h": 6500})
    pv = private_view(AgentRole.PILOT, pilot, event)
    assert pv["wake_class"] == "Heavy" and pv["burn_kg_per_h"] == 6500
    ground = GROUND.model_copy(update={"spare_gates": 3, "shift_start": 336})
    gv = private_view(AgentRole.GROUND, ground, event)
    assert gv["spare_gates"] == 3 and gv["shift_start"] == "05:36"
    assert private_view(AgentRole.GROUND, GROUND, event)["shift_start"] is None
