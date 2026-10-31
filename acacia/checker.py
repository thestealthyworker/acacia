"""Independent safety checker (HANDOVER 9, PLAN 2.A.1, CONSTRAINTS.md 2-3).

SEPARATION OF DUTIES: this module never imports from `acacia.solver` (a static test
enforces it). The solver reads only declared ConstraintPackets; the checker reads only
ground truth: the simulated private state, the shared `AirportWorld` and the *live* ledger
at check time (never the snapshot the agents saw). A package is safe only if every rule
passes; each rule returns pass plus a one-line detail string.

Workbook limits come from `acacia.constraints`: wake separation by leader/follower class at
140 kt (replacing the flat 3 min) and the hard 20 min gate-change notice. SEPARATION checks the
ATC's traffic snapshot and then `world.is_separated` against the world's traffic plus the live
ledger (other events' held or committed slots), so a slot booked nearby after ATC declared fails.
"""

from __future__ import annotations

from acacia import constraints
from acacia.config import MINUTES_PER_DAY
from acacia.models import (
    CheckResult, DisruptionEvent, EmptyLedgerView, LedgerView, PrivateState, ReasonCode, ResolutionPackage,
    RuleResult,
)
from acacia.timeutil import is_hhmm, to_hhmm, to_min
from acacia.world import OTHER_EVENT_CLASS, AirportWorld, separation_reach

CHECKER_VERSION = "checker-0.4"
REMOTE_PREFIX = "REMOTE"


def is_remote(gate: str) -> bool:
    return gate.upper().startswith(REMOTE_PREFIX)


def _result(rule: ReasonCode, passed: bool, detail: str) -> RuleResult:
    return RuleResult(rule=rule, passed=passed, detail=detail)


def _span(start: int, end: int) -> str:
    return f"[{to_hhmm(start)}, {to_hhmm(end)})"


def wake_reach() -> int:
    """Whole minutes that cover the largest wake minimum in the workbook table."""
    return separation_reach()


def _wake_gap(slot: int, own: str, t: int, other: str) -> tuple[float, float, str]:
    """(gap, required, pair text) between our slot and a movement at t of class `other`."""
    if t <= slot:
        return slot - t, constraints.wake_minutes(other, own), f"{other} ahead -> {own}"
    return t - slot, constraints.wake_minutes(own, other), f"{own} -> {other} behind"


def _wake_violation(slot: int, own: str, movements: dict[int, str]) -> str | None:
    """The first movement closer than its wake minimum, as text; None when all are separated."""
    for t in sorted(movements, key=lambda m: (abs(m - slot), m)):
        gap, need, pair = _wake_gap(slot, own, t, movements[t])
        if gap < need:
            return f"{to_hhmm(t)} ({pair}: {gap:g} min < {need:.2f} min)"
    return None


def _nearest(slot: int, traffic: dict[int, str]) -> dict[int, str]:
    """The nearest previous (<= slot) and next (> slot) traffic movement."""
    prev = [t for t in traffic if t <= slot]
    nxt = [t for t in traffic if t > slot]
    picks = ([max(prev)] if prev else []) + ([min(nxt)] if nxt else [])
    return {t: traffic[t] for t in picks}


def check_separation(slot: int, private: PrivateState, world: AirportWorld | None = None,
                     ledger_live: LedgerView | None = None,
                     exclude_event: str | None = None) -> RuleResult:
    """Free slot, wake minimum to the ATC's traffic, then the world's wake check (live ledger)."""
    snapshot = _separation_snapshot(slot, private)
    if not snapshot.passed or world is None or not hasattr(world, "is_separated"):
        return snapshot
    own = private.atc.own_class
    ok, detail = world.is_separated(slot, own, ledger_live or EmptyLedgerView(),
                                    exclude_event=exclude_event)
    if not ok:
        return _result(ReasonCode.SEPARATION, False,
                       f"{to_hhmm(slot)} breaks wake separation ({own}) on the live runway: {detail}.")
    return _result(ReasonCode.SEPARATION, True, snapshot.detail.rstrip(".") + f"; live runway {detail}.")


def _separation_snapshot(slot: int, private: PrivateState) -> RuleResult:
    atc = private.atc
    if slot not in atc.free_slots:
        free = ", ".join(to_hhmm(s) for s in sorted(atc.free_slots)) or "none"
        return _result(ReasonCode.SEPARATION, False,
                       f"{to_hhmm(slot)} is not an ATC free slot (free: {free}).")
    traffic = {t: atc.traffic_classes.get(t, OTHER_EVENT_CLASS) for t in atc.traffic}
    near = _nearest(slot, traffic)
    violation = _wake_violation(slot, atc.own_class, near)
    if violation:
        return _result(ReasonCode.SEPARATION, False,
                       f"{to_hhmm(slot)} breaks wake separation with traffic at {violation}.")
    gap = min((abs(slot - t) for t in near), default=None)
    gap_text = f"nearest traffic {gap} min away" if gap is not None else "no traffic nearby"
    return _result(ReasonCode.SEPARATION, True,
                   f"{to_hhmm(slot)} is a free slot; {gap_text}, wake table met ({atc.own_class}).")


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
    """Ground crew duty limit for the turnaround, and the flight crew's 13 h duty end (P_H2)."""
    g = private.ground
    arrival = slot + event.taxi_in
    done = arrival + g.min_turn
    passed = done <= g.duty_limit
    sign = "<=" if passed else ">"
    detail = f"turnaround done {to_hhmm(done)} {sign} crew duty limit {to_hhmm(g.duty_limit)}"
    crew_end = private.pilot.crew_duty_end
    if crew_end is not None:
        crew_ok = arrival <= crew_end
        passed = passed and crew_ok
        end_text = to_hhmm(crew_end) if 0 <= crew_end < MINUTES_PER_DAY else f"{crew_end} min (after today)"
        detail += f"; gate arrival {to_hhmm(arrival)} {'<=' if crew_ok else '>'} flight crew duty end {end_text}"
    return _result(ReasonCode.CREW_DUTY, passed, detail + ".")


def check_resources(slot: int, gate: str, event: DisruptionEvent, private: PrivateState,
                    ledger_live: LedgerView, airport: str) -> RuleResult:
    """Live ledger: no other event's runway slot inside the wake minimum, no gate overlap."""
    me = event.event_id
    reach = wake_reach()
    held = {t: OTHER_EVENT_CLASS for t in range(slot - reach, slot + reach + 1)
            if ledger_live.slot_taken(airport, t, exclude_event=me)}
    violation = _wake_violation(slot, private.atc.own_class, held)
    if violation:
        return _result(ReasonCode.RESOURCE_CONFLICT, False,
                       f"runway slot {violation} at {airport} is booked by another event "
                       f"inside the wake minimum of {to_hhmm(slot)}.")
    start = slot + event.taxi_in
    end = start + private.ground.min_turn
    if ledger_live.gate_busy(airport, gate, start, end, exclude_event=me):
        return _result(ReasonCode.RESOURCE_CONFLICT, False,
                       f"{gate} {_span(start, end)} overlaps another event's gate booking.")
    return _result(ReasonCode.RESOURCE_CONFLICT, True,
                   f"no ledger conflict for {to_hhmm(slot)} and {gate} {_span(start, end)}.")


decision_time_min = constraints.decision_time_min  # shared default (CONSTRAINTS.md 3)


def check_gate_change_notice(slot: int, gate: str, event: DisruptionEvent,
                             private: PrivateState) -> RuleResult:
    """A gate other than the assigned one (REMOTE-* included) needs >= 20 min notice."""
    assigned = private.ground.assigned_gate
    if gate == assigned:
        return _result(ReasonCode.GATE_CHANGE_NOTICE, True, f"{gate} is the assigned gate; no change.")
    decided, arrival = decision_time_min(event), slot + event.taxi_in
    notice, need = arrival - decided, constraints.GATE_CHANGE_NOTICE_MIN
    passed = notice >= need
    sign = ">=" if passed else "<"
    return _result(ReasonCode.GATE_CHANGE_NOTICE, passed,
                   f"gate change {assigned} -> {gate}: notice {notice} min {sign} {need} min "
                   f"(decided {to_hhmm(decided)}, gate arrival {to_hhmm(arrival)}).")


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
        check_separation(slot, private, world, ledger_live, exclude_event=event.event_id),
        check_fuel(slot, private),
        check_gate(slot, gate, event, private, world),
        check_crew(slot, event, private),
        check_resources(slot, gate, event, private, ledger_live, airport),
        check_gate_change_notice(slot, gate, event, private),
    ]
    return CheckResult(event_id=event.event_id, package_id=package.package_id,
                       passed=all(r.passed for r in rules), rules=rules)
