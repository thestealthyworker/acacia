"""Solver, stage 3 (HANDOVER 7, PLAN 3.2 and 3.6).

ISOLATION: `solve` takes only declared ConstraintPackets, the taxi-in time, cuts and the
packages already tried. It has no parameter for private state, so it cannot read it.
Every number comes from declared hard limits, soft preferences, options and facts_owned.

CONSTRAINTS.md 4-5: fuel utility spans land_by - E (E = the Pilot's declared `eta` fact, else
the P_S2 target); crew utility = max(0, 1 - 1.5 x overtime / 60). `score_breakdown` also carries
the informational keys `holding_fuel_kg` (when the Pilot declares `eta` and `burn_kg_per_h`) and
`overtime_cost_min` (when Ground declares a G_S1 shift-end target); they are never part of the
weighted score, which sums the POLICY keys only.

TIME-3/4: a slot before the ATC's declared A_H3 SLOT_LEAD earliest slot is discarded SLOT_LEAD,
and the G_H3 gate-change notice counts from the declared commit_time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from acacia.config import POLICY
from acacia.constraints import OVERTIME_COST_FACTOR, SPARE_HOLDING_MIN, time_budget_min
from acacia.models import (
    HHMM, AgentRole, ConstraintPacket, Decision, ReasonCode, ResolutionPackage, Stress, Verdict,
)
from acacia.timeutil import is_hhmm, to_hhmm, to_min

SOLVER_VERSION = "solver-0.4"
FUEL_SPAN_MIN = SPARE_HOLDING_MIN  # FUEL-5: used when the Pilot declares no earliest landing
CONNECTION_SPAN_MIN = 30
OVERTIME_SPAN_MIN = 60
OTHER_GATE_UTILITY = 0.6
NO_PREF_UTILITY = 1.0  # a preference nobody declared is neutral (constant for every package)
CORRUPT_NUDGE_MIN = 1  # corrupt fallback when no traffic is declared: a time off the sequence

Status = Literal["FEASIBLE", "DISCARDED", "CUT", "TRIED", "CORRUPTED"]


class Cut(BaseModel):
    """A constraint added after a REJECT (HANDOVER 7.5 soft cuts, PLAN 3.2 hard cuts).

    SOFT: utility on `pref` must be strictly above `min_utility`. SLOT: exclude `slot`.
    GATE: exclude `gate`. SLOT_FROM: exclude `slot` and later. PACKAGE: exclude slot+gate.
    """

    kind: Literal["SOFT", "SLOT", "GATE", "SLOT_FROM", "PACKAGE"]
    agent: AgentRole
    cites: str
    pref: str | None = None
    min_utility: float | None = None
    slot: HHMM | None = None
    gate: str | None = None


class Candidate(BaseModel):
    package: ResolutionPackage
    utilities: dict[str, float]  # by declared soft-pref id (A_S1, P_S1, ...)
    discard: list[ReasonCode] = Field(default_factory=list)
    status: Status
    cut_by: str | None = None


class SolveResult(BaseModel):
    top: ResolutionPackage | None
    all: list[Candidate]
    corrupted: bool = False
    corrupt_breaks: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _Decl:
    """Everything the solver reads, parsed from the declared packets."""

    event_id: str
    slots: tuple[tuple[int, int], ...]  # (slot, demand)
    gates: tuple[str, ...]
    land_by: int | None
    gate_free: dict[str, int | None]  # None = unavailable
    duty_limit: int | None
    crew_duty_end: int | None  # P_H2 FLIGHT_CREW_DUTY_END (gate arrival must not be later)
    notice: tuple[int, int, str] | None  # G_H3 (min notice, commit time, assigned gate), TIME-4
    min_turn: int
    prefs: dict[str, tuple[str, str, object]]  # id -> (metric, owner role value, target)
    traffic: tuple[int, ...]
    eta: int | None = None  # Pilot fact "eta" (earliest landing E)
    burn_kg_per_h: float | None = None  # Pilot fact "burn_kg_per_h" (holding burn)
    earliest_slot: int | None = None  # A_H3 SLOT_LEAD: commit time + slot lead (TIME-3)


def package_key(slot: str, gate: str) -> str:
    """Key of a tried package: "HH:MM|gate"."""
    return f"{slot}|{gate}"


def _hard(packet: ConstraintPacket | None, rule: str) -> object:
    if packet is None:
        return None
    return next((h.value for h in packet.hard if h.rule == rule), None)


def _parse(packets: dict[AgentRole, ConstraintPacket]) -> _Decl:
    atc, pil, gr = (packets.get(r) for r in (AgentRole.ATC, AgentRole.PILOT, AgentRole.GROUND))
    any_packet = next(iter(packets.values()), None)
    land_by, duty = _hard(pil, "LAND_BY"), _hard(gr, "CREW_DUTY_LIMIT")
    free_raw = _hard(gr, "GATE_FREE_FROM") or {}
    crew_end = _hard(pil, "FLIGHT_CREW_DUTY_END")
    prefs = {s.id: (s.metric, p.agent.value, s.target) for p in packets.values() for s in p.soft}
    return _Decl(
        event_id=any_packet.event_id if any_packet else "",
        slots=tuple((to_min(o.value), o.demand or 1) for o in (atc.options if atc else []) if o.type == "SLOT"),
        gates=tuple(o.value for o in (gr.options if gr else []) if o.type == "GATE"),
        land_by=to_min(land_by) if isinstance(land_by, str) else None,
        gate_free={g: (None if v == "unavailable" else to_min(v)) for g, v in dict(free_raw).items()},
        duty_limit=to_min(duty) if isinstance(duty, str) else None,
        crew_duty_end=to_min(crew_end) if isinstance(crew_end, str) else None,
        notice=_notice(_hard(gr, "GATE_CHANGE_NOTICE")),
        min_turn=int((gr.facts_owned if gr else {}).get("min_turn_min", 0)),
        prefs=prefs,
        traffic=tuple(to_min(t) for t in (atc.facts_owned if atc else {}).get("traffic", [])),
        eta=_fact_time(pil, "eta"),
        burn_kg_per_h=_fact_number(pil, "burn_kg_per_h"),
        earliest_slot=_earliest_slot(_hard(atc, "SLOT_LEAD")),
    )


def _fact_time(packet: ConstraintPacket | None, key: str) -> int | None:
    value = (packet.facts_owned if packet else {}).get(key)
    return to_min(value) if is_hhmm(value) else None


def _fact_number(packet: ConstraintPacket | None, key: str) -> float | None:
    value = (packet.facts_owned if packet else {}).get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _notice(value: object) -> tuple[int, int, str] | None:
    """Parse a declared G_H3 value into (min notice, commit time, assigned gate); None when it is
    missing or malformed. Notice counts from the declared commit_time (TIME-4); a packet that only
    declares decision_time gets the public negotiation + sign-off budget added."""
    if not isinstance(value, dict):
        return None
    decided, commit, gate = value.get("decision_time"), value.get("commit_time"), value.get("assigned_gate")
    need = _int(value.get("min_notice_min"))
    if need is None or not isinstance(gate, str):
        return None
    if is_hhmm(commit):
        return need, to_min(commit), gate
    if is_hhmm(decided):
        return need, to_min(decided) + time_budget_min(), gate
    return None


def _earliest_slot(value: object) -> int | None:
    """Parse a declared A_H3 SLOT_LEAD value: earliest_slot, else commit_time + slot_lead_min."""
    if not isinstance(value, dict):
        return None
    earliest, commit, lead = value.get("earliest_slot"), value.get("commit_time"), _int(value.get("slot_lead_min"))
    if is_hhmm(earliest):
        return to_min(earliest)
    if is_hhmm(commit) and lead is not None:
        return to_min(commit) + lead
    return None


# --- steps 2 and 3: hard filter and utilities -------------------------------------------------
def _discard(d: _Decl, slot: int, gate: str, taxi_in: int) -> list[ReasonCode]:
    why: list[ReasonCode] = []
    gate_arr = slot + taxi_in
    if d.earliest_slot is not None and slot < d.earliest_slot:
        why.append(ReasonCode.SLOT_LEAD)
    if d.land_by is not None and slot > d.land_by:
        why.append(ReasonCode.FUEL_LIMIT)
    if gate in d.gate_free:  # a declared free time binds every gate, remote stands too
        free = d.gate_free[gate]
        if free is None or gate_arr < free:
            why.append(ReasonCode.GATE_CONFLICT)
    crew_late = d.crew_duty_end is not None and gate_arr > d.crew_duty_end
    if crew_late or (d.duty_limit is not None and gate_arr + d.min_turn > d.duty_limit):
        why.append(ReasonCode.CREW_DUTY)
    if d.notice is not None:
        need, committed, assigned = d.notice
        if gate != assigned and gate_arr - committed < need:
            why.append(ReasonCode.GATE_CHANGE_NOTICE)
    return why


def _utility(d: _Decl, metric: str, target: object, slot: int, demand: int, gate: str,
             taxi_in: int) -> float:
    gate_arr = slot + taxi_in
    if metric == "slot_demand":
        return 1 - (demand - 1) / 3
    if metric == "gate_arrival_by":
        tgt = to_min(target) if isinstance(target, str) else None
        if tgt is None or gate_arr <= tgt:
            return 1.0
        return max(0.0, 1 - (gate_arr - tgt) / CONNECTION_SPAN_MIN)
    if metric == "fuel_margin":
        if d.land_by is None:
            return NO_PREF_UTILITY
        eta = d.eta if d.eta is not None else (to_min(target) if is_hhmm(target) else None)
        span = d.land_by - eta if eta is not None and eta < d.land_by else FUEL_SPAN_MIN
        return min(1.0, max(0.0, (d.land_by - slot) / span))
    if metric == "turnaround_done_by":
        overtime = _overtime(d, target, gate_arr) or 0
        return max(0.0, 1 - OVERTIME_COST_FACTOR * overtime / OVERTIME_SPAN_MIN)
    if metric == "keep_gate":
        return 1.0 if target in (None, gate) else OTHER_GATE_UTILITY
    raise ValueError(f"unknown soft-preference metric {metric!r}")


def _overtime(d: _Decl, shift_end: object, gate_arr: int) -> int | None:
    """Crew overtime past a declared shift end, or None when no shift end is declared."""
    if not is_hhmm(shift_end):
        return None
    return max(0, gate_arr + d.min_turn - to_min(shift_end))  # type: ignore[arg-type]


def _info(d: _Decl, slot: int, gate_arr: int) -> dict[str, float]:
    """Informational score_breakdown keys (CONSTRAINTS.md 4-5); never part of the score."""
    out: dict[str, float] = {}
    if d.eta is not None and d.burn_kg_per_h is not None:
        out["holding_fuel_kg"] = round(max(0, slot - d.eta) * d.burn_kg_per_h / 60.0, 1)
    shift = next((t for m, _, t in d.prefs.values() if m == "turnaround_done_by" and is_hhmm(t)), None)
    overtime = _overtime(d, shift, gate_arr)
    if overtime is not None:
        out["overtime_cost_min"] = round(OVERTIME_COST_FACTOR * overtime, 1)
    return out


def _utilities(d: _Decl, slot: int, demand: int, gate: str, taxi_in: int) -> dict[str, float]:
    return {pid: _utility(d, metric, target, slot, demand, gate, taxi_in)
            for pid, (metric, _, target) in d.prefs.items()}


def _by_metric(d: _Decl, utils: dict[str, float], metric: str) -> float:
    vals = [utils[pid] for pid, (m, _, _) in d.prefs.items() if m == metric]
    return min(vals) if vals else NO_PREF_UTILITY


def _breakdown(d: _Decl, utils: dict[str, float]) -> dict[str, float]:
    comp = {
        "throughput": _by_metric(d, utils, "slot_demand"),
        "connections": _by_metric(d, utils, "gate_arrival_by"),
        "fuel": _by_metric(d, utils, "fuel_margin"),
        "turnaround": 0.7 * _by_metric(d, utils, "turnaround_done_by")
        + 0.3 * _by_metric(d, utils, "keep_gate"),
    }
    return {k: round(POLICY[k] * u, 6) for k, u in comp.items()}


# --- step 5: cuts ----------------------------------------------------------------------------
def _cut_hit(cut: Cut, slot: int, gate: str, utils: dict[str, float]) -> bool:
    if cut.kind == "SOFT":
        u = utils.get(cut.pref or "")
        return u is not None and cut.min_utility is not None and not u > cut.min_utility
    if cut.kind == "SLOT":
        return cut.slot is not None and slot == to_min(cut.slot)
    if cut.kind == "GATE":
        return gate == cut.gate
    if cut.kind == "SLOT_FROM":
        return cut.slot is not None and slot >= to_min(cut.slot)
    return cut.slot is not None and slot == to_min(cut.slot) and gate == cut.gate


_HARD_CUT_KIND = {"A_H1": "SLOT", "A_H3": "SLOT", "G_H1": "GATE", "P_H1": "SLOT_FROM", "G_H2": "SLOT_FROM",
                  "P_H2": "SLOT_FROM", "G_H3": "GATE"}  # other hard ids cut the exact package


def cut_from_reject(decision: Decision, package: ResolutionPackage,
                    packets: dict[AgentRole, ConstraintPacket], taxi_in: int) -> Cut | None:
    """The cut a REJECT adds; None for an ACCEPT or a cite not in the agent's own packet."""
    own = packets.get(decision.agent)
    if decision.verdict != Verdict.REJECT or own is None or decision.cites not in own.ids():
        return None
    cites = decision.cites
    soft = {s.id for s in own.soft}
    base = {"agent": decision.agent, "cites": cites}
    if cites in soft:
        d = _parse(packets)
        slot = to_min(package.slot)
        demand = dict(d.slots).get(slot, 1)
        utils = _utilities(d, slot, demand, package.gate, taxi_in)
        return Cut(kind="SOFT", pref=cites, min_utility=utils.get(cites, 0.0), **base)
    kind = _HARD_CUT_KIND.get(cites, "PACKAGE")
    return Cut(kind=kind, slot=package.slot, gate=package.gate if kind in ("GATE", "PACKAGE") else None,
               **base)


# --- steps 1, 4 and 6: enumerate, score, rank ---------------------------------------------------
def _enumerate(d: _Decl, taxi_in: int, turn: int, cuts: list[Cut], tried: set[str]) -> list[Candidate]:
    out: list[Candidate] = []
    n = 0
    for slot, demand in d.slots:
        for gate in d.gates:
            n += 1
            out.append(_candidate(d, f"P{n}", slot, demand, gate, taxi_in, turn, cuts, tried))
    return out


def _candidate(d: _Decl, pid: str, slot: int, demand: int, gate: str, taxi_in: int, turn: int,
               cuts: list[Cut], tried: set[str]) -> Candidate:
    utils = _utilities(d, slot, demand, gate, taxi_in)
    bd = _breakdown(d, utils)
    why = _discard(d, slot, gate, taxi_in)
    score = round(sum(bd.values()), 6)  # weighted POLICY keys only
    pkg = ResolutionPackage(event_id=d.event_id, package_id=pid, turn=turn, slot=to_hhmm(slot),
                            gate=gate, score=score, score_breakdown={**bd, **_info(d, slot, slot + taxi_in)},
                            satisfies_hard=not why)
    cut = next((c for c in cuts if _cut_hit(c, slot, gate, utils)), None)
    status: Status = "DISCARDED" if why else "CUT" if cut else (
        "TRIED" if package_key(pkg.slot, gate) in tried else "FEASIBLE")
    return Candidate(package=pkg, utilities=utils, discard=why, status=status,
                     cut_by=cut.cites if cut and not why else None)


def _rank(cands: list[Candidate]) -> list[Candidate]:
    order = {"FEASIBLE": 0, "CUT": 1, "TRIED": 2, "DISCARDED": 3, "CORRUPTED": 4}
    return sorted(cands, key=lambda c: (order[c.status], -c.package.score, int(c.package.package_id[1:])))


def solve(packets: dict[AgentRole, ConstraintPacket], taxi_in: int, cuts: list[Cut], tried: set[str],
          stress: Stress | None = None, turn: int = 2) -> SolveResult:
    """Rank slot x gate packages from declarations only; top is None when nothing is feasible."""
    d = _parse(packets)
    ranked = _rank(_enumerate(d, taxi_in, turn, list(cuts), set(tried)))
    top = next((c.package for c in ranked if c.status == "FEASIBLE"), None)
    result = SolveResult(top=top, all=ranked)
    if stress is not None and stress.corrupt_solver and turn == 2:
        return _corrupt(d, result, taxi_in)
    return result


# --- corrupt_solver stress (safety demo only) ---------------------------------------------------
def _corrupt(d: _Decl, result: SolveResult, taxi_in: int) -> SolveResult:
    bad = next((c for c in result.all if c.status == "DISCARDED"), None)
    if bad is not None:
        lie = bad.package.model_copy(update={"satisfies_hard": True})
        return result.model_copy(update={"top": lie, "corrupted": True,
                                          "corrupt_breaks": [r.value for r in bad.discard]})
    if result.top is None:
        return result
    slot = to_min(result.top.slot)
    declared = {s for s, _ in d.slots}
    later = sorted(t for t in d.traffic if t > slot and t not in declared)
    occupied = later[0] if later else slot + CORRUPT_NUDGE_MIN
    moved = result.top.model_copy(update={
        "package_id": result.top.package_id + "x", "slot": to_hhmm(occupied), "satisfies_hard": True})
    cand = Candidate(package=moved, utilities={}, discard=[ReasonCode.SEPARATION], status="CORRUPTED")
    return result.model_copy(update={"top": moved, "corrupted": True, "corrupt_breaks": ["SEPARATION"],
                                     "all": [*result.all, cand]})
