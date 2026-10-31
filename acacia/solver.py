"""Solver, stage 3 (HANDOVER 7, PLAN 3.2 and 3.6).

ISOLATION: `solve` takes only declared ConstraintPackets, the taxi-in time, cuts and the
packages already tried. It has no parameter for private state, so it cannot read it.
Every number comes from declared hard limits, soft preferences and options.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from acacia.config import POLICY
from acacia.models import (
    HHMM, AgentRole, ConstraintPacket, Decision, ReasonCode, ResolutionPackage, Stress, Verdict,
)
from acacia.timeutil import to_hhmm, to_min

SOLVER_VERSION = "solver-0.1"
FUEL_SPAN_MIN = 45
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
    min_turn: int
    prefs: dict[str, tuple[str, str, object]]  # id -> (metric, owner role value, target)
    traffic: tuple[int, ...]


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
    prefs = {s.id: (s.metric, p.agent.value, s.target) for p in packets.values() for s in p.soft}
    return _Decl(
        event_id=any_packet.event_id if any_packet else "",
        slots=tuple((to_min(o.value), o.demand or 1) for o in (atc.options if atc else []) if o.type == "SLOT"),
        gates=tuple(o.value for o in (gr.options if gr else []) if o.type == "GATE"),
        land_by=to_min(land_by) if isinstance(land_by, str) else None,
        gate_free={g: (None if v == "unavailable" else to_min(v)) for g, v in dict(free_raw).items()},
        duty_limit=to_min(duty) if isinstance(duty, str) else None,
        min_turn=int((gr.facts_owned if gr else {}).get("min_turn_min", 0)),
        prefs=prefs,
        traffic=tuple(to_min(t) for t in (atc.facts_owned if atc else {}).get("traffic", [])),
    )


# --- steps 2 and 3: hard filter and utilities -------------------------------------------------
def _discard(d: _Decl, slot: int, gate: str, taxi_in: int) -> list[ReasonCode]:
    why: list[ReasonCode] = []
    gate_arr = slot + taxi_in
    if d.land_by is not None and slot > d.land_by:
        why.append(ReasonCode.FUEL_LIMIT)
    if gate in d.gate_free:  # a declared free time binds every gate, remote stands too
        free = d.gate_free[gate]
        if free is None or gate_arr < free:
            why.append(ReasonCode.GATE_CONFLICT)
    if d.duty_limit is not None and gate_arr + d.min_turn > d.duty_limit:
        why.append(ReasonCode.CREW_DUTY)
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
        return min(1.0, max(0.0, (d.land_by - slot) / FUEL_SPAN_MIN))
    if metric == "turnaround_done_by":
        tgt = to_min(target) if isinstance(target, str) else None
        overtime = 0 if tgt is None else max(0, gate_arr + d.min_turn - tgt)
        return 1.0 if overtime == 0 else max(0.0, 1 - overtime / OVERTIME_SPAN_MIN)
    if metric == "keep_gate":
        return 1.0 if target in (None, gate) else OTHER_GATE_UTILITY
    raise ValueError(f"unknown soft-preference metric {metric!r}")


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


_HARD_CUT_KIND = {"A_H1": "SLOT", "G_H1": "GATE", "P_H1": "SLOT_FROM", "G_H2": "SLOT_FROM"}


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
    pkg = ResolutionPackage(event_id=d.event_id, package_id=pid, turn=turn, slot=to_hhmm(slot),
                            gate=gate, score=round(sum(bd.values()), 6), score_breakdown=bd,
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
