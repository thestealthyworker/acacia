"""Coordinator: tests 4 (log-chain determinism), 8 (safety invariant), 9 (termination) and
10 (reject without own cite), plus the CONSTRAINTS.md duties (fallback reason, stamp, holding fuel)."""

from __future__ import annotations

import itertools

import pytest

from acacia import constraints
from acacia.agents.base import AgentTimeout
from acacia.agents.factory import make_agents
from acacia.coordinator import run_all
from acacia.log import verify_chain
from acacia.models import (
    AgentRole, AtcPrivate, ConsensusRecord, Decision, GroundPrivate, LogEntry, PilotPrivate,
    ReasonCode, Stress, Verdict,
)
from acacia.triangulate import triangulate
from acacia.world import get_world

STRESSES = [Stress(low_fuel=a, tight_crew=b, corrupt_solver=c)
            for a, b, c in itertools.product((False, True), repeat=3)]


# --- fixtures and fake agents ----------------------------------------------------------------
@pytest.fixture(scope="module")
def events(sample_store):
    return [triangulate(r.to_dict()) for _, r in sample_store.qualifying_rows().iterrows()]


@pytest.fixture(scope="module")
def dl1639(sample_store):
    return triangulate(sample_store.find_flight("DL", "1639", "2015-02-26"))


def _rule(role: AgentRole):
    return make_agents("rule")[role]


class AlwaysReject:
    def __init__(self, role: AgentRole) -> None:
        self.role, self._inner = role, _rule(role)

    def declare(self, event, private):
        return self._inner.declare(event, private)

    def decide(self, event, package, private, own_packet):
        hard = own_packet.hard[0]
        return Decision(event_id=event.event_id, package_id=package.package_id, agent=self.role,
                        turn=package.turn, verdict=Verdict.REJECT, reason_code=ReasonCode.SEPARATION,
                        cites=hard.id, rationale="always reject")


class Malformed:
    def __init__(self, role: AgentRole) -> None:
        self.role = role

    def declare(self, event, private):
        return {"garbage": True}

    def decide(self, event, package, private, own_packet):
        return "not a decision"


class TimesOut:
    def __init__(self, role: AgentRole, on: str = "both") -> None:
        self.role, self.on, self._inner = role, on, _rule(role)

    def declare(self, event, private):
        if self.on in ("both", "declare"):
            raise AgentTimeout(self.role, "declare", "20 s")
        return self._inner.declare(event, private)

    def decide(self, event, package, private, own_packet):
        if self.on in ("both", "decide"):
            raise AgentTimeout(self.role, "decide", "20 s")
        return self._inner.decide(event, package, private, own_packet)


class Crashes(TimesOut):
    def declare(self, event, private):
        raise RuntimeError("boom")

    def decide(self, event, package, private, own_packet):
        raise RuntimeError("boom")


class ForeignCite:
    """Pilot that rejects every package citing Ground's limit."""

    role = AgentRole.PILOT

    def __init__(self) -> None:
        self._inner = _rule(AgentRole.PILOT)

    def declare(self, event, private):
        return self._inner.declare(event, private)

    def decide(self, event, package, private, own_packet):
        return Decision(event_id=event.event_id, package_id=package.package_id, agent=self.role,
                        turn=package.turn, verdict=Verdict.REJECT, reason_code=ReasonCode.GATE_CONFLICT,
                        cites="G_H1", rationale="not my constraint")


class Spy:
    def __init__(self, role: AgentRole, audit: list | None = None) -> None:
        self.role, self._inner, self.seen = role, _rule(role), []
        self.last_audit = audit or []

    def declare(self, event, private):
        self.seen.append(type(private))
        return self._inner.declare(event, private)

    def decide(self, event, package, private, own_packet):
        self.seen.append(type(private))
        return self._inner.decide(event, package, private, own_packet)


class AllTaken:
    def slot_taken(self, airport, t, exclude_event=None):
        return True

    def gate_busy(self, airport, gate, start, end, exclude_event=None):
        return True


def _with(**override):
    agents = make_agents("rule")
    agents.update({AgentRole(k.upper()): v for k, v in override.items()})
    return agents


def _record(entries: list[LogEntry]) -> ConsensusRecord:
    assert entries[-1].type == "ConsensusRecord"
    return ConsensusRecord.model_validate(entries[-1].payload)


def _assert_terminates(entries: list[LogEntry]) -> ConsensusRecord:
    rec = _record(entries)
    assert entries[-1].state == "AWAITING_SIGNOFF" and rec.status == "AWAITING_SIGNOFF"
    assert rec.turns_used <= 3
    assert sum(e.type == "SEND" for e in entries) <= 2
    assert all((e.turn or 0) <= 3 for e in entries)
    assert verify_chain(entries) == (True, None)
    return rec


# --- test 4: determinism --------------------------------------------------------------------------
def test_same_event_gives_identical_hash_chain(dl1639) -> None:
    for stress in (Stress(), Stress(corrupt_solver=True)):
        a = run_all(dl1639, stress, make_agents("rule"))
        b = run_all(dl1639, stress, make_agents("rule"))
        assert [e.hash for e in a] == [e.hash for e in b]
        assert [e.payload for e in a] == [e.payload for e in b]


def test_explicit_world_matches_default_world(dl1639) -> None:
    world = get_world(dl1639.flight.dest, dl1639.flight.date, dl1639.injected)
    a = run_all(dl1639, Stress(), make_agents("rule"), world=world)
    b = run_all(dl1639, Stress(), make_agents("rule"))
    assert [e.hash for e in a] == [e.hash for e in b]


# --- test 8: safety invariant ---------------------------------------------------------------------
def test_nothing_committed_unless_checker_passes(events) -> None:
    for event, stress in itertools.product(events, STRESSES):
        entries = run_all(event, stress, make_agents("rule"))
        rec = _assert_terminates(entries)
        commits = [e for e in entries if e.state == "COMMITTED"]
        if rec.outcome == "CONSENSUS":
            assert rec.checker_pass is True and len(commits) == 1
            pid = rec.package["package_id"]
            checks = [e for e in entries if e.type == "CheckResult" and e.payload["package_id"] == pid]
            assert checks and checks[-1].payload["passed"] is True
            assert checks[-1].seq < commits[0].seq
        else:
            assert not commits
        corrupted = {e.payload["package"]["package_id"] for e in entries
                     if e.type == "ResolutionPackage" and e.payload.get("corrupted") and e.payload["package"]}
        assert rec.outcome != "CONSENSUS" or rec.package["package_id"] not in corrupted


def test_checker_reads_the_live_ledger(dl1639) -> None:
    entries = run_all(dl1639, Stress(), make_agents("rule"), ledger_live=AllTaken())
    rec = _assert_terminates(entries)
    assert rec.outcome == "FALLBACK"
    failed = [r["rule"] for e in entries if e.type == "CheckResult" for r in e.payload["rules"] if not r["passed"]]
    assert "RESOURCE_CONFLICT" in failed


# --- test 9: termination ---------------------------------------------------------------------------
@pytest.mark.parametrize("factory", [AlwaysReject, Malformed, TimesOut, Crashes])
@pytest.mark.parametrize("role", list(AgentRole))
def test_every_run_terminates_with_bad_agents(dl1639, factory, role) -> None:
    for stress in (Stress(), Stress(corrupt_solver=True), Stress(low_fuel=True)):
        rec = _assert_terminates(run_all(dl1639, stress, _with(**{role.value: factory(role)})))
        # an agent that always rejects or never answers validly can never be outvoted into a commit
        assert rec.outcome == "FALLBACK"


def test_all_agents_broken_still_terminates(events) -> None:
    for event in events:
        agents = {r: Malformed(r) for r in AgentRole}
        rec = _assert_terminates(run_all(event, Stress(), agents))
        assert rec.outcome == "FALLBACK"


def test_missing_agents_dict_terminates(dl1639) -> None:
    rec = _assert_terminates(run_all(dl1639, Stress(), {}))
    assert rec.outcome == "FALLBACK"


def test_timeout_on_decide_never_counts_as_accept(dl1639) -> None:
    entries = run_all(dl1639, Stress(), _with(ground=TimesOut(AgentRole.GROUND, on="decide")))
    rec = _assert_terminates(entries)
    assert rec.outcome == "FALLBACK"
    ground = [d for d in rec.decisions if d.agent == AgentRole.GROUND]
    assert ground and all(d.verdict == Verdict.REJECT for d in ground)
    assert any(e.type == "DEFAULT" for e in entries)


def test_atc_timeout_goes_straight_to_fallback_with_reason(dl1639) -> None:
    entries = run_all(dl1639, Stress(), _with(atc=TimesOut(AgentRole.ATC)))
    rec = _assert_terminates(entries)
    assert rec.outcome == "FALLBACK" and rec.turns_used == 1
    assert not any(e.type == "ResolutionPackage" for e in entries)
    fb = next(e for e in entries if e.type == "FallbackPlan")
    assert "ATC timeout" in fb.payload["reason"]
    assert "ATC timeout" in rec.package["fallback_reason"]


def test_pilot_and_ground_timeout_defaults_are_logged(dl1639) -> None:
    entries = run_all(dl1639, Stress(), _with(pilot=TimesOut(AgentRole.PILOT, on="declare"),
                                             ground=TimesOut(AgentRole.GROUND, on="declare")))
    _assert_terminates(entries)
    defaults = {e.payload["role"]: e.payload for e in entries if e.type == "DEFAULT"}
    pilot, ground = defaults["PILOT"]["packet"], defaults["GROUND"]["packet"]
    atc = next(e.payload for e in entries if e.type == "ConstraintPacket" and e.who == "atc")
    assert pilot["hard"][0]["value"] == min(o["value"] for o in atc["options"])
    assert [o["value"] for o in ground["options"]] == ["REMOTE-1"]
    assert ground["facts_owned"]["min_turn_min"] == constraints.min_turnaround(dl1639.flight.dest)
    assert any(h["rule"] == "GATE_CHANGE_NOTICE" for h in ground["hard"])


# --- test 10: reject without own cite ------------------------------------------------------------
def test_reject_citing_foreign_constraint_becomes_accept_with_objection(dl1639) -> None:
    entries = run_all(dl1639, Stress(), _with(pilot=ForeignCite()))
    rec = _assert_terminates(entries)
    objections = [e for e in entries if e.type == "OBJECTION"]
    assert objections and objections[0].payload["original"]["cites"] == "G_H1"
    pilot = [d for d in rec.decisions if d.agent == AgentRole.PILOT]
    assert pilot and all(d.verdict == Verdict.ACCEPT for d in pilot)
    assert all("objection" in d.rationale for d in pilot)
    assert rec.outcome == "CONSENSUS"


# --- isolation, audit, pre-emption, dispatch link -------------------------------------------------
def test_each_agent_sees_only_its_own_private_state(dl1639) -> None:
    spies = {r: Spy(r) for r in AgentRole}
    run_all(dl1639, Stress(), spies)
    assert set(spies[AgentRole.ATC].seen) == {AtcPrivate}
    assert set(spies[AgentRole.PILOT].seen) == {PilotPrivate}
    assert set(spies[AgentRole.GROUND].seen) == {GroundPrivate}


def test_declaration_audit_notes_are_logged(dl1639) -> None:
    note = {"agent": "PILOT", "id": "P_H1", "text": "P_H1 LAND_BY declared 13:30; private state says 13:02"}
    entries = run_all(dl1639, Stress(), _with(pilot=Spy(AgentRole.PILOT, audit=[note])))
    audits = [e for e in entries if e.type == "AUDIT"]
    assert len(audits) == 1 and "13:30" in audits[0].text


def test_should_yield_finishes_the_turn_then_preempts(dl1639) -> None:
    calls = iter([False, True])
    entries = run_all(dl1639, Stress(), _with(atc=AlwaysReject(AgentRole.ATC)),
                      should_yield=lambda: next(calls, True))
    last = entries[-1]
    assert last.state == "PREEMPTED" and last.type == "STATE"
    assert not any(e.type == "ConsensusRecord" for e in entries)
    assert [e.turn for e in entries if e.type == "CheckResult"] == [2]
    assert verify_chain(entries) == (True, None)


def test_preempt_before_any_vote(dl1639) -> None:
    entries = run_all(dl1639, Stress(), make_agents("rule"), should_yield=lambda: True)
    assert entries[-1].state == "PREEMPTED" and not any(e.type == "SEND" for e in entries)


def test_dispatched_by_is_on_the_first_entry(dl1639) -> None:
    link = {"seq": 7, "hash": "ab" * 32}
    entries = run_all(dl1639, Stress(), make_agents("rule"), dispatched_by=link)
    assert entries[0].dispatched_by == link and entries[0].payload["dispatched_by"] == link
    assert all(e.dispatched_by is None for e in entries[1:])


# --- constraints duties ------------------------------------------------------------------------------
def test_stamp_carries_versions_and_constraints_version(dl1639) -> None:
    rec = _record(run_all(dl1639, Stress(), make_agents("rule")))
    for key in ("policy", "solver", "checker", "model", "prompt_hashes"):
        assert key in rec.stamp
    assert rec.stamp["constraints"] == constraints.constraints_version()


def test_consensus_record_shows_revised_arrival_gate_and_holding_fuel(dl1639) -> None:
    entries = run_all(dl1639, Stress(), make_agents("rule"))
    rec = _record(entries)
    pkg = rec.package
    for key in ("slot", "gate", "gate_arrival", "holding_min", "holding_fuel_kg", "flight_plan"):
        assert pkg.get(key) is not None, key
    eta = int(dl1639.eta_landing[:2]) * 60 + int(dl1639.eta_landing[3:])
    slot = int(pkg["slot"][:2]) * 60 + int(pkg["slot"][3:])
    wake = constraints.wake_class(dl1639.distance)
    assert pkg["holding_fuel_kg"] == constraints.holding_fuel_kg(slot, eta, wake)
    assert pkg["slot"] in entries[-1].text and pkg["gate"] in entries[-1].text
    assert "kg" in entries[-1].text


def test_divert_record_shows_the_flight_plan(dl1639) -> None:
    entries = run_all(dl1639, Stress(low_fuel=True), make_agents("rule"))
    rec = _record(entries)
    assert rec.outcome == "FALLBACK" and rec.package["divert"] is True
    assert rec.package["alternate"] and rec.package["alternate"] in rec.package["flight_plan"]
    assert "LAND_BY" in rec.package["fallback_reason"]
    assert rec.package["alternate"] in entries[-1].text


def test_fallback_after_turn_three_names_the_rejects(dl1639) -> None:
    entries = run_all(dl1639, Stress(), _with(ground=AlwaysReject(AgentRole.GROUND)))
    rec = _record(entries)
    assert rec.outcome == "FALLBACK" and rec.turns_used == 3
    reason = rec.package["fallback_reason"]
    assert reason.startswith("turn 3:") and "Ground REJECT" in reason
    state = next(e for e in entries if e.state == "FALLBACK")
    assert state.payload["reason"] == reason
    fb = next(e for e in entries if e.type == "FallbackPlan")
    assert fb.payload["used"] is True and fb.payload["reason"].startswith(reason)


def test_reference_fallback_is_marked_not_used(dl1639) -> None:
    entries = run_all(dl1639, Stress(), make_agents("rule"))
    fb = next(e for e in entries if e.type == "FallbackPlan")
    assert fb.payload["used"] is False and fb.payload["reason"]
    if not fb.payload["divert"]:
        assert fb.payload["check"] is not None
