"""Independent safety checker (HANDOVER 9, PLAN 2.A.1).

SEPARATION OF DUTIES: this module never imports from `acacia.solver` (a static test
enforces it). The solver reads only declared ConstraintPackets; the checker reads only
ground truth: the simulated private state, the shared `AirportWorld` and the *live* ledger
at check time (never the snapshot the agents saw). A package is safe only if every rule
passes; each rule returns pass plus a one-line detail string.
"""

from __future__ import annotations

from acacia.models import (
    CheckResult, DisruptionEvent, LedgerView, PrivateState, ReasonCode, ResolutionPackage,
    RuleResult,
)
from acacia.timeutil import is_hhmm, to_hhmm, to_min
from acacia.world import AirportWorld

REMOTE_PREFIX = "REMOTE"


def is_remote(gate: str) -> bool:
    return gate.upper().startswith(REMOTE_PREFIX)


def _result(rule: ReasonCode, passed: bool, detail: str) -> RuleResult:
    return RuleResult(rule=rule, passed=passed, detail=detail)


def _span(start: int, end: int) -> str:
    return f"[{to_hhmm(start)}, {to_hhmm(end)})"


def check_separation(slot: int, private: PrivateState) -> RuleResult:
    atc = private.atc
    if slot not in atc.free_slots:
        free = ", ".join(to_hhmm(s) for s in sorted(atc.free_slots)) or "none"
        return _result(ReasonCode.SEPARATION, False,
                       f"{to_hhmm(slot)} is not an ATC free slot (free: {free}).")
    near = [t for t in atc.traffic if abs(slot - t) < atc.min_separation]
    if near:
        return _result(ReasonCode.SEPARATION, False,
                       f"{to_hhmm(slot)} is within {atc.min_separation} min of traffic at "
                       f"{to_hhmm(near[0])}.")
    gap = min((abs(slot - t) for t in atc.traffic), default=None)
    gap_text = f"nearest traffic {gap} min away" if gap is not None else "no traffic nearby"
    return _result(ReasonCode.SEPARATION, True,
                   f"{to_hhmm(slot)} is a free slot; {gap_text} (>= {atc.min_separation}).")


def check_fuel(slot: int, private: PrivateState) -> RuleResult:
    land_by = private.pilot.land_by
    passed = slot <= land_by
    sign = "<=" if passed else ">"
    return _result(ReasonCode.FUEL_LIMIT, passed,
                   f"slot {to_hhmm(slot)} {sign} fuel limit {to_hhmm(land_by)} "
                   f"(margin {land_by - slot} min).")


def _gate_blockers(world: AirportWorld, gate: str, start: int, end: int) -> list[str]:
    """Reasons the world says [start, end) on gate is not usable (busy or failed)."""
    reasons = [f"background busy {_span(s, e)}" for s, e in world.busy_intervals(gate)
               if s < end and start < e]
    reasons += [f"out of service {_span(s, e)}" for s, e in world.failed_intervals(gate)
                if s < end and start < e]
    return reasons


def check_gate(slot: int, gate: str, event: DisruptionEvent, private: PrivateState,
               world: AirportWorld) -> RuleResult:
    if is_remote(gate):
        return _result(ReasonCode.GATE_CONFLICT, True, f"{gate} is a remote stand (always passes).")
    if gate not in world.gates():
        return _result(ReasonCode.GATE_CONFLICT, False, f"{gate} is not a gate at {world.airport}.")
    g = private.ground
    start = slot + event.taxi_in
    end = start + g.min_turn
    if gate in g.failed or (gate in g.free_from and g.free_from[gate] is None):
        return _result(ReasonCode.GATE_CONFLICT, False, f"{gate} is out of service.")
    free_from = g.free_from.get(gate)
    if free_from is not None and start < free_from:
        return _result(ReasonCode.GATE_CONFLICT, False,
                       f"gate arrival {to_hhmm(start)} is before {gate} is free at {to_hhmm(free_from)}.")
    blockers = _gate_blockers(world, gate, start, end)
    if blockers:
        return _result(ReasonCode.GATE_CONFLICT, False,
                       f"{gate} turnaround {_span(start, end)} overlaps " + "; ".join(blockers) + ".")
    return _result(ReasonCode.GATE_CONFLICT, True,
                   f"{gate} is clear for the full turnaround {_span(start, end)}.")


def check_crew(slot: int, event: DisruptionEvent, private: PrivateState) -> RuleResult:
    g = private.ground
    done = slot + event.taxi_in + g.min_turn
    passed = done <= g.duty_limit
    sign = "<=" if passed else ">"
    return _result(ReasonCode.CREW_DUTY, passed,
                   f"turnaround done {to_hhmm(done)} {sign} crew duty limit {to_hhmm(g.duty_limit)}.")


def check_resources(slot: int, gate: str, event: DisruptionEvent, private: PrivateState,
                    ledger_live: LedgerView, airport: str) -> RuleResult:
    """Live ledger: no other event's runway slot within separation, no gate overlap."""
    me = event.event_id
    sep = private.atc.min_separation
    clash = [t for t in range(slot - sep + 1, slot + sep)
             if ledger_live.slot_taken(airport, t, exclude_event=me)]
    if clash:
        return _result(ReasonCode.RESOURCE_CONFLICT, False,
                       f"runway slot {to_hhmm(clash[0])} at {airport} is booked by another event "
                       f"(within {sep} min of {to_hhmm(slot)}).")
    start = slot + event.taxi_in
    end = start + private.ground.min_turn
    if ledger_live.gate_busy(airport, gate, start, end, exclude_event=me):
        return _result(ReasonCode.RESOURCE_CONFLICT, False,
                       f"{gate} {_span(start, end)} overlaps another event's gate booking.")
    return _result(ReasonCode.RESOURCE_CONFLICT, True,
                   f"no ledger conflict for {to_hhmm(slot)} and {gate} {_span(start, end)}.")


def check(package: ResolutionPackage, event: DisruptionEvent, private: PrivateState,
          world: AirportWorld, ledger_live: LedgerView, airport: str | None = None) -> CheckResult:
    """Run every rule against ground truth and the live ledger. Overall pass = all rules pass."""
    if package.event_id != event.event_id:
        raise ValueError(f"package {package.package_id} is for {package.event_id}, not {event.event_id}")
    if not is_hhmm(package.slot):
        raise ValueError(f"package slot must be 'HH:MM', got {package.slot!r}")
    airport = (airport or event.flight.dest).upper()
    slot, gate = to_min(package.slot), package.gate
    rules = [
        check_separation(slot, private),
        check_fuel(slot, private),
        check_gate(slot, gate, event, private, world),
        check_crew(slot, event, private),
        check_resources(slot, gate, event, private, ledger_live, airport),
    ]
    return CheckResult(event_id=event.event_id, package_id=package.package_id,
                       passed=all(r.passed for r in rules), rules=rules)
