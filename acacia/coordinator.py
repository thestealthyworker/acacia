"""Coordinator: the single-event negotiation state machine (HANDOVER 11, PLAN 3.1).

IDLE -> TRIANGULATED -> DECLARING -> SOLVING -> CHECKING -> COMMITTED | FALLBACK -> AWAITING_SIGNOFF
(TRIANGULATED -> FALLBACK directly when E - decision time is under the time budget, TIME-5)

Turn 1 is declare; entering SOLVING sets turn 2; a reject at turn 2 sets turn 3; a reject at
turn 3 goes to FALLBACK, so at most two packages are voted on. Agents only declare and vote.
The solver reads declarations only, the checker reads ground truth plus the live ledger, and
a package is committed only when all three ACCEPT and the checker passes. The coordinator
never writes the ledger; the dispatcher books on COMMITTED or a non-divert FALLBACK.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import lru_cache

import pandas as pd

from acacia import config, constraints
from acacia import coordinator_text as txt
from acacia.agents.base import Agent, AgentTimeout, private_for
from acacia.coordinator_clock import Clock, WallClock, budget_text, deadline_reason, enforces_wall_budget
from acacia.coordinator_outcome import (
    build_record, compute_fallback, fallback_text, make_stamp, record_text, safe_check,
)
from acacia.log import LogChain
from acacia.models import (
    AgentRole, CheckResult, ConstraintPacket, Decision, DisruptionEvent, EmptyLedgerView, HardLimit,
    LedgerView, LogEntry, Option, PrivateState, ReasonCode, ResolutionPackage, Stress, Verdict,
)
from acacia.simulate import build_private
from acacia.solver import Cut, SolveResult, cut_from_reject, package_key, solve
from acacia.timeutil import to_hhmm, to_min
from acacia.world import AirportWorld, get_world

FIRST_VOTE_TURN = 2
ROLES = (AgentRole.ATC, AgentRole.PILOT, AgentRole.GROUND)
DEFAULT_REMOTE = "REMOTE-1"
TIMEOUT_MARK = "no valid answer"
UNKNOWN_ASSIGNED_GATE = ""  # Ground missing: every gate counts as a gate change (most restrictive)
# Agent boundary: any failure (timeout, bad schema, crash) counts as a timeout, never as ACCEPT.
AGENT_FAILURES = (Exception,)
_TIMEOUT_REJECT = {AgentRole.ATC: (ReasonCode.SEPARATION, "A_H1"),
                   AgentRole.PILOT: (ReasonCode.FUEL_LIMIT, "P_H1"),
                   AgentRole.GROUND: (ReasonCode.GATE_CONFLICT, "G_H1")}


@lru_cache(maxsize=1)
def _default_airports() -> pd.DataFrame:
    for name in ("airports.csv", "sample_airports.csv"):
        path = config.DATA_DIR / name
        if path.exists():
            return pd.read_csv(path, dtype=str, keep_default_na=False)
    raise FileNotFoundError(f"no airports.csv or sample_airports.csv in {config.DATA_DIR}")


class _Run:
    """One negotiation. Methods named _stage_* are generators yielding log entries."""

    def __init__(self, event: DisruptionEvent, stress: Stress, agents: dict[AgentRole, Agent],
                 view: LedgerView, live: LedgerView, world: AirportWorld, airports: pd.DataFrame,
                 should_yield: Callable[[], bool] | None, clock: Clock | None = None) -> None:
        self.event, self.stress, self.agents = event, stress, agents
        self.view, self.live, self.world, self.airports = view, live, world, airports
        self.should_yield = should_yield
        self.log = LogChain(event_id=event.event_id)
        self.private: PrivateState | None = None
        self.packets: dict[AgentRole, ConstraintPacket] = {}
        self.missing: set[AgentRole] = set()
        self.decisions: list[Decision] = []
        self.timeout_rejects: list[Decision] = []
        self.cuts: list[Cut] = []
        self.tried: set[str] = set()
        self.turn = 1
        self.outcome: str | None = None
        self.final: ResolutionPackage | None = None
        self.preempted = False
        self.fallback_reason: str | None = None
        self.atc_error: str | None = None
        self.wall = WallClock(enforces_wall_budget(agents), clock)

    def emit(self, who: str, type_: str, text: str, **kw) -> LogEntry:
        return self.log.append(who, type_, text, **kw)

    def coord(self, type_: str, text: str, state: str | None, stage: int, **kw) -> LogEntry:
        return self.emit("coordinator", type_, text, state=state, stage=stage, turn=self.turn, **kw)

    # --- stages 0..2 ---------------------------------------------------------------------------
    def stage_start(self, dispatched_by: dict | None) -> Iterator[LogEntry]:
        ev = self.event
        payload = {"from": "IDLE", "to": "TRIANGULATED", "guard": txt.guard_text(ev),
                   "stress": self.stress.model_dump()}
        if dispatched_by is not None:
            payload["dispatched_by"] = dispatched_by
        yield self.emit("coordinator", "STATE", (
            f"Row selected: `{ev.flight.code}` {ev.flight.origin} to {ev.flight.dest}, {ev.flight.date}. "
            f"Guard passes ({txt.guard_text(ev)}). Moving `IDLE` to `TRIANGULATED`."),
            state="TRIANGULATED", stage=0, turn=0, payload=payload, dispatched_by=dispatched_by)
        inj = "; ".join(f"{i.type.replace('_', ' ').lower()} ({i.ref}) {i.start}-{i.end}" for i in ev.injected)
        yield self.emit("builder", "DisruptionEvent", (
            f"Delay of {ev.hist_delay_min} min broken down: {ev.causes.weather} weather, "
            f"{ev.causes.late_aircraft} late aircraft, {ev.causes.air_system} air system. Classified "
            f"{ev.scenario_type.value}. Injected (simulated): {inj or 'none'}. Earliest landing "
            f"{ev.eta_landing}."), stage=0, turn=0, payload=ev.model_dump(mode="json"))

    def stage_no_time(self, reason: str) -> Iterator[LogEntry]:
        """Deadline guard (TIME-5): E is too close to negotiate, so the checked fallback decides."""
        self.turn = 0
        self.private = build_private(self.event, self.stress, self.world, self.view)
        yield self._to_fallback("DEADLINE", reason, "Deadline guard: the negotiation and sign-off budget "
                                "does not fit before the earliest landing, so no agent is asked. Guard: "
                                "`TRIANGULATED` goes straight to `FALLBACK`; the checker still runs on the plan.")

    def stage_declare(self) -> Iterator[LogEntry]:
        self.turn = 1
        yield self.coord("BROADCAST", (
            "Turn 1 of 3. Shared facts broadcast to ATC, Pilot and Ground. Each agent loads only its "
            "own private state. Waiting for three `ConstraintPacket`s. Timeout rule: a missing agent "
            "counts as most restrictive."), "DECLARING", 1, payload={"turn": 1, "roles": [r.value for r in ROLES]})
        self.private = build_private(self.event, self.stress, self.world, self.view)
        shared = self.event.model_copy(update={"turn": 1})
        for role in ROLES:
            yield from self._declare_one(role, shared)

    def _declare_one(self, role: AgentRole, shared: DisruptionEvent) -> Iterator[LogEntry]:
        agent = self.agents.get(role)
        try:
            if agent is None:
                raise AgentTimeout(role, "declare", "no agent configured")
            packet = self._valid_packet(role, agent.declare(shared, private_for(role, self.private)))
        except AGENT_FAILURES as exc:
            yield from self._apply_default(role, exc)
            return
        self.packets[role] = packet
        yield self.emit(role.value.lower(), "ConstraintPacket", txt.packet_text(packet), stage=2, turn=1,
                        payload=packet.model_dump(mode="json"))
        for note in list(getattr(agent, "last_audit", None) or []):
            yield self.coord("AUDIT", f"Declaration audit ({role.value}): {note.get('text', note)} "
                             "Not corrected; the checker uses ground truth.", None, 2,
                             payload=note if isinstance(note, dict) else {"text": str(note)})

    def _valid_packet(self, role: AgentRole, raw: object) -> ConstraintPacket:
        packet = raw if isinstance(raw, ConstraintPacket) else ConstraintPacket.model_validate(raw)
        if packet.event_id != self.event.event_id or packet.agent != role:
            raise ValueError(f"packet identity mismatch: {packet.event_id}/{packet.agent.value}")
        return packet

    def _apply_default(self, role: AgentRole, exc: Exception) -> Iterator[LogEntry]:
        self.missing.add(role)
        why = f"{type(exc).__name__}: {exc}"
        if role == AgentRole.ATC:
            self.atc_error = why
            yield self.coord("DEFAULT", f"ATC did not answer ({why}). Most restrictive default: no "
                             "slots can be sequenced, so the run goes straight to `FALLBACK`.", None, 2,
                             payload={"role": role.value, "default": "FALLBACK", "error": why})
            return
        packet, desc = self._default_packet(role)
        self.packets[role] = packet
        yield self.coord("DEFAULT", f"{role.value} did not answer ({why}). Most restrictive default "
                         f"applied: {desc}. A timeout never counts as ACCEPT.", None, 2,
                         payload={"role": role.value, "packet": packet.model_dump(mode="json"), "error": why})

    def _default_packet(self, role: AgentRole) -> tuple[ConstraintPacket, str]:
        ev, base = self.event, {"event_id": self.event.event_id, "agent": role, "turn": 1}
        if role == AgentRole.PILOT:
            atc = self.packets.get(AgentRole.ATC)
            offered = sorted(to_min(o.value) for o in (atc.options if atc else []) if o.type == "SLOT")
            land_by = to_hhmm(offered[0]) if offered else ev.eta_landing
            return (ConstraintPacket(**base, hard=[HardLimit(id="P_H1", rule="LAND_BY", value=land_by)],
                                     soft=[], options=[], facts_owned={"default": "timeout"}),
                    f"LAND_BY = earliest offered slot {land_by}, connections ignored")
        turn = constraints.min_turnaround(ev.flight.dest)
        gate_arr = to_min(ev.eta_landing) + ev.taxi_in
        duty = to_hhmm(gate_arr + turn)
        notice = {"min_notice_min": constraints.GATE_CHANGE_NOTICE_MIN,
                  "decision_time": to_hhmm(constraints.decision_time_min(ev)),
                  "commit_time": to_hhmm(constraints.commit_time_min(ev)),
                  "assigned_gate": UNKNOWN_ASSIGNED_GATE}
        hard = [HardLimit(id="G_H1", rule="GATE_FREE_FROM", value={DEFAULT_REMOTE: to_hhmm(gate_arr)}),
                HardLimit(id="G_H2", rule="CREW_DUTY_LIMIT", value=duty),
                HardLimit(id="G_H3", rule="GATE_CHANGE_NOTICE", value=notice)]
        return (ConstraintPacket(**base, hard=hard, soft=[], options=[Option(type="GATE", value=DEFAULT_REMOTE)],
                                 facts_owned={"min_turn_min": turn, "default": "timeout"}),
                f"every gate unavailable except {DEFAULT_REMOTE}, CREW_DUTY_LIMIT = E + Ti + min_turn = {duty}, "
                f"and {DEFAULT_REMOTE} counts as a gate change needing "
                f"{constraints.GATE_CHANGE_NOTICE_MIN} min notice")

    # --- stages 3..4 ---------------------------------------------------------------------------
    def stage_negotiate(self) -> Iterator[LogEntry]:
        if AgentRole.ATC in self.missing:
            yield self._to_fallback("ATC_MISSING", txt.atc_missing_reason(self.atc_error),
                                    "ATC is missing, so no slot can be sequenced. Guard: `DECLARING` "
                                    "goes straight to `FALLBACK`.")
            return
        got = ", ".join(r.value for r in ROLES if r not in self.missing) or "none"
        yield self.coord("STATE", f"Packets received from {got}"
                         + (f"; defaults stand in for {', '.join(r.value for r in sorted(self.missing))}"
                            if self.missing else "") + ". Schema valid. Facts owned by each agent are "
                         "locked (owner wins on conflict). Moving to `SOLVING`.", "SOLVING", 3,
                         payload={"packets": sorted(r.value for r in self.packets)})
        if (yield from self._maybe_preempt()):
            return
        self.turn = FIRST_VOTE_TURN
        while self.outcome is None:
            if (late := self.wall.overshoot_reason()) is not None:
                yield self._to_fallback("TIME_BUDGET", late, "Live negotiation is over its wall-clock budget.")
                return
            yield from self._one_turn()
            if self.outcome is None and (yield from self._maybe_preempt()):
                return

    def _maybe_preempt(self) -> Iterator[LogEntry]:
        if self.should_yield is None or not self.should_yield():
            return False
        self.preempted = True
        yield self.coord("STATE", f"Pre-empted after turn {self.turn}: a higher-priority trigger needs "
                         "the airport. The current turn finished; this run stops here and is re-queued. "
                         "Nothing is committed.", "PREEMPTED", None,
                         payload={"event_id": self.event.event_id, "turn": self.turn, "reason": "should_yield"},
                         meta=self.wall.meta())
        return True

    def _one_turn(self) -> Iterator[LogEntry]:
        result = solve(self.packets, self.event.taxi_in, self.cuts, self.tried, self.stress, turn=self.turn)
        yield self._solver_entry(result)
        top = result.top
        if top is None:
            yield self._to_fallback("FEASIBLE_SET_EMPTY", txt.empty_set_reason(result, self.packets, self.turn),
                                    "No package satisfies every declared hard limit. Guard: feasible set "
                                    "empty, so `SOLVING` goes to `FALLBACK`.")
            return
        yield self.coord("SEND", txt.send_text(self.event, top, self.turn), "CHECKING", 4,
                         payload=top.model_dump(mode="json"))
        rejects: list[Decision] = []
        for role in ROLES:
            entries, decision = self._decide_one(role, top)
            yield from entries
            self.decisions.append(decision)
            if decision.verdict == Verdict.REJECT:
                rejects.append(decision)
        check = self._check(top)
        yield self.emit("checker", "CheckResult", txt.check_text(check), stage=4, turn=self.turn,
                        payload=check.model_dump(mode="json"))
        yield from self._resolve_turn(top, rejects, check)

    def _solver_entry(self, result: SolveResult) -> LogEntry:
        top, text = result.top, txt.solver_text(result, self.cuts)
        payload = {"package": top.model_dump(mode="json") if top else None,
                   "candidates": [c.model_dump(mode="json") for c in result.all],
                   "corrupted": result.corrupted, "corrupt_breaks": result.corrupt_breaks,
                   "cuts": [c.model_dump(mode="json") for c in self.cuts], "tried": sorted(self.tried)}
        return self.emit("solver", "ResolutionPackage", text, stage=3, turn=self.turn, payload=payload)

    def _decide_one(self, role: AgentRole, pkg: ResolutionPackage) -> tuple[list[LogEntry], Decision]:
        """Ask one agent to vote; returns its log entries and the effective decision."""
        agent, own = self.agents.get(role), self.packets[role]
        shared = self.event.model_copy(update={"turn": self.turn})
        try:
            if agent is None:
                raise AgentTimeout(role, "decide", "no agent configured")
            d = self._valid_decision(role, pkg, agent.decide(shared, pkg, private_for(role, self.private), own))
        except AGENT_FAILURES as exc:
            reject = self._timeout_reject(role, pkg, exc)
            self.timeout_rejects.append(reject)
            return [self._timeout_entry(role, reject, exc)], reject
        verdict = "ACCEPT " if d.verdict == Verdict.ACCEPT else f"REJECT `{d.reason_code.value}` citing `{d.cites}`. "
        entries = [self.emit(role.value.lower(), "Decision", verdict + d.rationale, stage=4, turn=self.turn,
                             payload=d.model_dump(mode="json"))]
        if d.verdict != Verdict.REJECT or d.cites in own.ids():
            return entries, d
        fixed = d.model_copy(update={"verdict": Verdict.ACCEPT, "reason_code": None, "cites": None,
                                     "rationale": d.rationale + " [objection: reject did not cite own constraint]"})
        entries.append(self.coord(
            "OBJECTION", f"{role.value} rejected citing `{d.cites}`, which is not in its own packet. A reject "
            "must cite its own constraint, so it is converted to ACCEPT with this objection logged.", "CHECKING", 4,
            payload={"original": d.model_dump(mode="json"), "converted": fixed.model_dump(mode="json"),
                     "objection": "Reject did not cite a declared constraint"}))
        return entries, fixed

    def _timeout_reject(self, role: AgentRole, pkg: ResolutionPackage, exc: Exception) -> Decision:
        code, cites = _TIMEOUT_REJECT[role]
        return Decision(event_id=pkg.event_id, package_id=pkg.package_id, agent=role, turn=self.turn,
                        verdict=Verdict.REJECT, reason_code=code, cites=cites,
                        rationale=f"{TIMEOUT_MARK} ({type(exc).__name__}); counted as REJECT")

    def _timeout_entry(self, role: AgentRole, d: Decision, exc: Exception) -> LogEntry:
        return self.coord("DEFAULT", f"{role.value} did not give a valid decision ({type(exc).__name__}: {exc}). "
                          "Most restrictive default: counted as REJECT, never as ACCEPT.", "CHECKING", 4,
                          payload={"role": role.value, "decision": d.model_dump(mode="json"), "timeout": True})

    def _valid_decision(self, role: AgentRole, pkg: ResolutionPackage, raw: object) -> Decision:
        d = raw if isinstance(raw, Decision) else Decision.model_validate(raw)
        if (d.event_id, d.package_id, d.agent) != (pkg.event_id, pkg.package_id, role):
            raise ValueError(f"decision identity mismatch: {d.event_id}/{d.package_id}/{d.agent.value}")
        return d.model_copy(update={"turn": self.turn})

    def _check(self, pkg: ResolutionPackage) -> CheckResult:
        return safe_check(pkg, self.event, self.private, self.world, self.live)

    def _resolve_turn(self, top: ResolutionPackage, rejects: list[Decision], check: CheckResult) -> Iterator[LogEntry]:
        late = self.wall.overshoot_reason()
        if late is not None:
            yield self._to_fallback("TIME_BUDGET", late, "Live negotiation is over its wall-clock budget, so "
                                    "this package is not committed.")
            return
        if not rejects and check.passed:
            self.outcome, self.final = "CONSENSUS", top
            yield self.coord("STATE", "All three ACCEPT and the checker passes. Guard satisfied: `CHECKING` "
                             f"to `COMMITTED` in {self.turn} turns.", "COMMITTED", 5,
                             payload={"package_id": top.package_id})
            return
        self.tried.add(package_key(top.slot, top.gate, top.runway))
        for d in rejects:
            if any(d is t for t in self.timeout_rejects):
                continue  # a timeout reject adds no cut; the package is marked tried
            cut = cut_from_reject(d, top, self.packets, self.event.taxi_in)
            if cut is not None:
                self.cuts.append(cut)
        reasons = txt.reject_summary(rejects, self.timeout_rejects, check)
        blocked = "" if check.passed else "Checker blocked this package, so it can never be committed. "
        if self.turn < config.MAX_TURNS:
            cut_ids = ", ".join(f"`{c.cites}`" for c in self.cuts) or "the rejected package (marked tried)"
            yield self.coord("STATE", f"{blocked}Rejected by {reasons}. Turn {self.turn} is under "
                             f"{config.MAX_TURNS}, so back to `SOLVING`. Reason codes become cuts: {cut_ids}.",
                             "SOLVING", 3, payload={"reasons": reasons, "cuts": [c.model_dump(mode="json") for c in self.cuts]})
            self.turn += 1
            return
        yield self._to_fallback("TURN_LIMIT", txt.turn_limit_reason(self.turn, reasons),
                                f"{blocked}Still rejected at turn {self.turn} ({reasons}). Guard: "
                                "`CHECKING` to `FALLBACK`. The rule-based plan takes over.")

    def _to_fallback(self, code: str, reason: str, text: str) -> LogEntry:
        """Enter FALLBACK; the log states why (CONSTRAINTS.md 6)."""
        self.outcome, self.fallback_reason = "FALLBACK", reason
        return self.coord("STATE", f"{text} Why: {reason}.", "FALLBACK", 5,
                          payload={"code": code, "reason": reason})

    # --- stage 5 -------------------------------------------------------------------------------
    def stage_outcome(self) -> Iterator[LogEntry]:
        used = self.outcome == "FALLBACK"
        plan = compute_fallback(self.event, self.private, self.airports, self.world, self.live, self.turn,
                                self.fallback_reason if used else None)
        yield self.coord("FallbackPlan", fallback_text(plan, used), None, 5,
                         payload={**plan.model_dump(mode="json"), "used": used})
        stamp = make_stamp(self.agents)
        record = build_record(self.event, self.outcome or "FALLBACK", self.final, plan, self.turn,
                              self.decisions, stamp, why=self.fallback_reason if used else None)
        yield self.coord("ConsensusRecord", record_text(record, self.event) + budget_text(self.event),
                         "AWAITING_SIGNOFF", 5, payload=record.model_dump(mode="json"), meta=self.wall.meta())


def run(event: DisruptionEvent, stress: Stress | None, agents: dict[AgentRole, Agent],
        ledger_view: LedgerView | None = None, should_yield: Callable[[], bool] | None = None, *,
        data: object | None = None, ledger_live: LedgerView | None = None,
        world: AirportWorld | None = None, dispatched_by: dict | None = None,
        clock: Clock | None = None) -> Iterator[LogEntry]:
    """Negotiate one event; yields LogEntry objects. The last entry's payload is the ConsensusRecord.

    `ledger_view` is the read-only snapshot the agents' private state is built from; the
    checker and the fallback read `ledger_live` (default: the same view). `should_yield` is
    polled between turns: when it returns True the current turn is finished, a PREEMPTED
    STATE entry is emitted and the generator stops (no ConsensusRecord). `data` is a DataStore
    (its airports table drives the divert); `dispatched_by` is {"seq", "hash"} of the DISPATCH
    entry and is written into the first entry. `clock` (seconds, default time.monotonic) measures
    the real run time, logged only in the final entry's non-hashed `meta` (TIME-7). An event whose
    earliest landing is too close to its decision time skips negotiation (TIME-5).
    """
    view = ledger_view if ledger_view is not None else EmptyLedgerView()
    live = ledger_live if ledger_live is not None else view
    world = world if world is not None else get_world(event.flight.dest, event.flight.date, event.injected)
    airports = getattr(data, "airports", None)
    if not isinstance(airports, pd.DataFrame) or airports.empty:
        airports = _default_airports()
    r = _Run(event, stress or Stress(), agents, view, live, world, airports, should_yield, clock)
    yield from r.stage_start(dispatched_by)
    no_time = deadline_reason(event)
    if no_time is not None:
        yield from r.stage_no_time(no_time)
        yield from r.stage_outcome()
        return
    yield from r.stage_declare()
    yield from r.stage_negotiate()
    if r.preempted:
        return
    yield from r.stage_outcome()


def run_all(event: DisruptionEvent, stress: Stress | None, agents: dict[AgentRole, Agent],
            ledger_view: LedgerView | None = None, **kwargs) -> list[LogEntry]:
    """Collect a whole run into a list (UI playback, tests)."""
    return list(run(event, stress, agents, ledger_view, **kwargs))
