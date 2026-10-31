"""Deterministic rule-based agents (HANDOVER 8.2), ported from the prototype's declare/decide.

Red lines are private (never declared): Pilot rejects when connections are fully lost
(gate arrival >= connect_by + 30, PLAN 3.3) and pax >= 25; Ground rejects when crew
overtime max(0, slot + Ti + min_turn - shift_end) exceeds 20 min (PLAN 3.4).
Hard limits follow CONSTRAINTS.md: A_H2 WAKE_SEPARATION, A_H3 SLOT_LEAD (TIME-3), P_H2 FLIGHT_CREW_DUTY_END (when the
duty end falls inside the day) and G_H3 GATE_CHANGE_NOTICE (notice counted from the commit
time, TIME-4), with the same values the LLM
agents declare (acacia.agents.declared). Pilot also states the facts `eta` and `burn_kg_per_h`
(the solver's fuel span and holding-fuel report); Ground states `spare_gates` and `shift_start`.
"""

from __future__ import annotations

from acacia.agents.base import AgentPrivate, coerce_private
from acacia.agents.declared import crew_duty_hhmm, gate_notice_value, slot_lead_value, wake_value
from acacia.constraints import (
    GATE_CHANGE_NOTICE_MIN, GROUND_OVERTIME_RED_LINE_MIN, burn_kg_per_h, commit_time_min,
)
from acacia.models import (
    AgentRole, AtcPrivate, ConstraintPacket, Decision, DisruptionEvent, GroundPrivate, HardLimit,
    Option, PilotPrivate, ReasonCode, ResolutionPackage, SoftPref, Verdict,
)
from acacia.timeutil import to_hhmm, to_min

PILOT_RED_LINE_PAX = 25
CONNECTION_LOSS_MIN = 30  # connections utility hits 0 at connect_by + 30
GROUND_RED_LINE_OVERTIME = GROUND_OVERTIME_RED_LINE_MIN  # CREW-3, single source in constraints
TRAFFIC_BEFORE_MIN = 30   # declared traffic window around the free slots
TRAFFIC_AFTER_MIN = 60


def _base(event: DisruptionEvent, package: ResolutionPackage, role: AgentRole) -> dict:
    return {"event_id": event.event_id, "package_id": package.package_id, "agent": role,
            "turn": package.turn}


def _accept(base: dict, rationale: str) -> Decision:
    return Decision(**base, verdict=Verdict.ACCEPT, rationale=rationale)


def _reject(base: dict, code: ReasonCode, cites: str, rationale: str) -> Decision:
    return Decision(**base, verdict=Verdict.REJECT, reason_code=code, cites=cites, rationale=rationale)


def is_remote(gate: str) -> bool:
    return gate.upper().startswith("REMOTE")


class RuleATC:
    role = AgentRole.ATC

    def declare(self, event: DisruptionEvent, private: AgentPrivate | dict) -> ConstraintPacket:
        a: AtcPrivate = coerce_private(self.role, private)  # type: ignore[assignment]
        slots = sorted(a.free_slots)
        return ConstraintPacket(
            event_id=event.event_id, agent=self.role, turn=event.turn,
            hard=[HardLimit(id="A_H1", rule="SLOT_IN_SEQUENCE", value=[to_hhmm(s) for s in slots]),
                  HardLimit(id="A_H2", rule="WAKE_SEPARATION", value=wake_value(a.own_class)),
                  HardLimit(id="A_H3", rule="SLOT_LEAD", value=slot_lead_value(event))],
            soft=[SoftPref(id="A_S1", preference="Use low-demand slots to protect sector throughput",
                           weight=0.7, metric="slot_demand")],
            options=[Option(type="SLOT", value=to_hhmm(s), demand=a.demand.get(s, 1)) for s in slots],
            facts_owned={"runway": _runway_text(a), "traffic": _traffic_window(a, slots)},
        )

    def decide(self, event: DisruptionEvent, package: ResolutionPackage,
               private: AgentPrivate | dict, own_packet: ConstraintPacket) -> Decision:
        a: AtcPrivate = coerce_private(self.role, private)  # type: ignore[assignment]
        base = _base(event, package, self.role)
        slot = to_min(package.slot)
        if slot not in a.free_slots:
            return _reject(base, ReasonCode.SEPARATION, "A_H1",
                           f"{package.slot} is not a free slot in my sequence.")
        contested = ", though it is a contested slot" if a.demand.get(slot, 1) >= 3 else ""
        return _accept(base, f"Slot {package.slot} fits the sequence{contested}.")


def _runway_text(a: AtcPrivate) -> str:
    if not a.closures:
        return "both runways open"
    return "single runway until " + to_hhmm(max(end for _, end in a.closures))


def _traffic_window(a: AtcPrivate, slots: list[int]) -> list[str]:
    if not slots:
        return []
    lo, hi = slots[0] - TRAFFIC_BEFORE_MIN, slots[-1] + TRAFFIC_AFTER_MIN
    return [to_hhmm(t) for t in sorted(a.traffic) if lo <= t <= hi]


class RulePilot:
    role = AgentRole.PILOT

    def declare(self, event: DisruptionEvent, private: AgentPrivate | dict) -> ConstraintPacket:
        p: PilotPrivate = coerce_private(self.role, private)  # type: ignore[assignment]
        return ConstraintPacket(
            event_id=event.event_id, agent=self.role, turn=event.turn,
            hard=_pilot_hard(p),
            soft=[SoftPref(id="P_S1", weight=0.7, metric="gate_arrival_by",
                           preference=f"At gate by {to_hhmm(p.connect_by)} to protect {p.pax} "
                                      "connecting passengers",
                           target=to_hhmm(p.connect_by), stake=p.pax),
                  SoftPref(id="P_S2", preference="Maximise fuel margin at landing", weight=0.3,
                           metric="fuel_margin", target=event.eta_landing)],
            options=[],
            facts_owned={"fuel_endurance_until": to_hhmm(p.land_by), "connecting_pax": p.pax,
                         "eta": event.eta_landing, "burn_kg_per_h": pilot_burn(p)},
        )

    def decide(self, event: DisruptionEvent, package: ResolutionPackage,
               private: AgentPrivate | dict, own_packet: ConstraintPacket) -> Decision:
        p: PilotPrivate = coerce_private(self.role, private)  # type: ignore[assignment]
        base = _base(event, package, self.role)
        slot = to_min(package.slot)
        gate_arr = slot + event.taxi_in
        if slot > p.land_by:
            return _reject(base, ReasonCode.FUEL_LIMIT, "P_H1",
                           f"Landing at {package.slot} is past my fuel limit of {to_hhmm(p.land_by)}.")
        if p.crew_duty_end is not None and gate_arr > p.crew_duty_end:
            return _reject(base, ReasonCode.CREW_DUTY, "P_H2",
                           f"Gate arrival {to_hhmm(gate_arr)} is past the flight crew duty limit.")
        if gate_arr >= p.connect_by + CONNECTION_LOSS_MIN and p.pax >= PILOT_RED_LINE_PAX:
            return _reject(base, ReasonCode.CONNECTION_RISK, "P_S1",
                           f"Gate arrival {to_hhmm(gate_arr)} strands all {p.pax} connecting passengers "
                           f"(need {to_hhmm(p.connect_by)}).")
        status = "protected" if gate_arr <= p.connect_by else "partly at risk"
        return _accept(base, f"Fuel margin {p.land_by - slot} min; connections {status}.")


def pilot_burn(p: PilotPrivate) -> int:
    """Holding burn kg/h (FUEL-4): the private value, else the workbook rate for the wake class."""
    return p.burn_kg_per_h if p.burn_kg_per_h is not None else burn_kg_per_h(p.wake_class)


def _ground_facts(g: GroundPrivate) -> dict:
    facts: dict = {"min_turn_min": g.min_turn, "next_leg_departure": to_hhmm(g.next_departure),
                   "spare_gates": g.spare_gates}
    if g.shift_start is not None:
        facts["shift_start"] = to_hhmm(g.shift_start)
    return facts


def _pilot_hard(p: PilotPrivate) -> list[HardLimit]:
    hard = [HardLimit(id="P_H1", rule="LAND_BY", value=to_hhmm(p.land_by))]
    duty = crew_duty_hhmm(p)
    if duty is not None:
        hard.append(HardLimit(id="P_H2", rule="FLIGHT_CREW_DUTY_END", value=duty))
    return hard


class RuleGround:
    role = AgentRole.GROUND

    def declare(self, event: DisruptionEvent, private: AgentPrivate | dict) -> ConstraintPacket:
        g: GroundPrivate = coerce_private(self.role, private)  # type: ignore[assignment]
        usable = [x for x in g.gates if _usable(g, x)]
        return ConstraintPacket(
            event_id=event.event_id, agent=self.role, turn=event.turn,
            hard=[HardLimit(id="G_H1", rule="GATE_FREE_FROM",
                            value={x: (to_hhmm(g.free_from[x]) if _usable(g, x) else "unavailable")
                                   for x in g.gates}),
                  HardLimit(id="G_H2", rule="CREW_DUTY_LIMIT", value=to_hhmm(g.duty_limit)),
                  HardLimit(id="G_H3", rule="GATE_CHANGE_NOTICE", value=gate_notice_value(event, g))],
            soft=[SoftPref(id="G_S1", weight=0.6, metric="turnaround_done_by", target=to_hhmm(g.shift_end),
                           preference=f"Finish turnaround before crew shift ends {to_hhmm(g.shift_end)} "
                                      "(no overtime)"),
                  SoftPref(id="G_S2", weight=0.4, metric="keep_gate", target=g.assigned_gate,
                           preference=f"Keep assigned gate {g.assigned_gate}")],
            options=[Option(type="GATE", value=x) for x in usable],
            facts_owned=_ground_facts(g),
        )

    def decide(self, event: DisruptionEvent, package: ResolutionPackage,
               private: AgentPrivate | dict, own_packet: ConstraintPacket) -> Decision:
        g: GroundPrivate = coerce_private(self.role, private)  # type: ignore[assignment]
        base = _base(event, package, self.role)
        gate_arr = to_min(package.slot) + event.taxi_in
        done = gate_arr + g.min_turn
        if not _gate_ready(g, package.gate, gate_arr):
            return _reject(base, ReasonCode.GATE_CONFLICT, "G_H1",
                           f"Gate {package.gate} is not available at {to_hhmm(gate_arr)}.")
        notice = gate_arr - commit_time_min(event)  # TIME-4: counted from the commit time
        if package.gate != g.assigned_gate and notice < GATE_CHANGE_NOTICE_MIN:
            return _reject(base, ReasonCode.GATE_CHANGE_NOTICE, "G_H3",
                           f"Changing to {package.gate} gives only {notice} min notice "
                           f"(need {GATE_CHANGE_NOTICE_MIN}).")
        if done > g.duty_limit:
            return _reject(base, ReasonCode.CREW_DUTY, "G_H2",
                           f"Turnaround would breach the crew duty limit {to_hhmm(g.duty_limit)}.")
        overtime = max(0, done - g.shift_end)
        if overtime > GROUND_RED_LINE_OVERTIME:
            return _reject(base, ReasonCode.CREW_DUTY, "G_S1",
                           f"Turnaround ends {to_hhmm(done)}, {overtime} min of crew overtime.")
        return _accept(base, f"Gate {package.gate} ready; turnaround done by {to_hhmm(done)}.")


def _usable(g: GroundPrivate, gate: str) -> bool:
    return gate not in g.failed and g.free_from.get(gate) is not None


def _gate_ready(g: GroundPrivate, gate: str, gate_arr: int) -> bool:
    if is_remote(gate):
        return True
    if not _usable(g, gate):
        return False
    return gate_arr >= g.free_from[gate]  # type: ignore[operator]
