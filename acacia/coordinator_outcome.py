"""Coordinator stage 5 helpers: the always-computed fallback, the ConsensusRecord and its stamp.

Split out of coordinator.py to keep both files small. No AI here: the fallback is the FCFS
rule plan (HANDOVER 10), checked by the independent checker unless it is a divert.

The record's `package` dict always states the outcome the challenge asks for (CONSTRAINTS.md 6):
the revised arrival (slot, gate, gate_arrival) or the divert flight plan, plus holding minutes
and holding_fuel_kg (CONSTRAINTS.md 4), a one-line `flight_plan` and, for a FALLBACK, its reason.
"""

from __future__ import annotations

import hashlib

import pandas as pd

from acacia import checker, config, constraints
from acacia.agents.base import Agent
from acacia.coordinator_text import holding
from acacia.fallback import plan_fallback
from acacia.log import canonical_json
from acacia.models import (
    AgentRole, CheckResult, ConsensusRecord, Decision, DisruptionEvent, FallbackPlan, LedgerView,
    PrivateState, ReasonCode, ResolutionPackage,
)
from acacia.solver import SOLVER_VERSION
from acacia.timeutil import to_hhmm, to_min
from acacia.world import AirportWorld

COORDINATOR_VERSION = "coordinator-0.2"
CHECKER_VERSION = getattr(checker, "CHECKER_VERSION", "checker-0.1")
RULE_AGENTS_VERSION = "rule-stand-in-0.1"
FALLBACK_PACKAGE_ID = "FB"
REFERENCE_REASON = "Reference only: consensus reached, plan kept for delta_vs_fallback."


def safe_check(pkg: ResolutionPackage, event: DisruptionEvent, private: PrivateState,
               world: AirportWorld, live: LedgerView) -> CheckResult:
    """The checker's verdict; a package it cannot evaluate fails (never passes by default)."""
    try:
        return checker.check(pkg, event, private, world, live)
    except (ValueError, KeyError, TypeError) as exc:
        return CheckResult(event_id=event.event_id, package_id=pkg.package_id, passed=False,
                           rules=[{"rule": ReasonCode.SEPARATION, "passed": False,
                                   "detail": f"checker could not evaluate the package: {exc}"}])


def compute_fallback(event: DisruptionEvent, private: PrivateState, airports: pd.DataFrame,
                     world: AirportWorld, live: LedgerView, turn: int, why: str | None) -> FallbackPlan:
    """The FCFS fallback plan; a non-divert plan carries its checker result.

    `why` is the reason the run fell back (None when consensus was reached: reference plan).
    The plan's own rationale follows it, so FallbackPlan.reason explains both.
    """
    plan = plan_fallback(event, private, airports, world=world, ledger_view=live)
    lead = why.rstrip(".") + "." if why else REFERENCE_REASON
    plan = plan.model_copy(update={"reason": f"{lead} Plan: {plan.reason}".strip()})
    if plan.divert or plan.slot is None or plan.gate is None:
        return plan
    pkg = ResolutionPackage(event_id=event.event_id, package_id=FALLBACK_PACKAGE_ID, turn=turn,
                            slot=plan.slot, gate=plan.gate, score=0.0, score_breakdown={})
    return plan.model_copy(update={"check": safe_check(pkg, event, private, world, live)})


def fallback_text(plan: FallbackPlan, used: bool) -> str:
    if plan.divert:
        where = f"{plan.alternate} ({plan.alternate_distance_mi} mi)" if plan.alternate else "no alternate found"
        body = f"DIVERT to {where}. {plan.reason}"
    else:
        verdict = "" if plan.check is None else f" Checker {'PASS' if plan.check.passed else 'FAIL'}."
        body = f"Fallback plan: land {plan.slot}, gate {plan.gate}. {plan.reason}{verdict}"
    if used and not plan.divert and plan.check is not None and not plan.check.passed:
        body += " The fallback fails the checker: manual handling required."
    return body


def _gate_arrival(event: DisruptionEvent, slot: str | None) -> int | None:
    return None if slot is None else to_min(slot) + event.taxi_in


def _landing(event: DisruptionEvent, slot: str, gate: str) -> dict:
    """Revised-arrival fields shared by the consensus and the non-divert fallback package."""
    mins, kg = holding(event, slot)
    wake = constraints.wake_class(event.distance)
    arr = to_hhmm(to_min(slot) + event.taxi_in)
    return {"slot": slot, "gate": gate, "gate_arrival": arr, "divert": False, "holding_min": mins,
            "holding_fuel_kg": kg, "wake_class": wake, "burn_kg_per_h": constraints.burn_kg_per_h(wake),
            "flight_plan": f"land {event.flight.dest} {slot}, gate {gate}, at gate {arr}"}


def _divert(event: DisruptionEvent, plan: FallbackPlan) -> dict:
    dist = f" ({plan.alternate_distance_mi} mi)" if plan.alternate_distance_mi is not None else ""
    to = plan.alternate or "manual alternate (none with coordinates)"
    return {"divert": True, "alternate": plan.alternate, "alternate_distance_mi": plan.alternate_distance_mi,
            "holding_min": None, "holding_fuel_kg": None,
            "flight_plan": f"divert {event.flight.origin}-{event.flight.dest} to {to}{dist}; "
                           f"no holding at {event.flight.dest}"}


def build_record(event: DisruptionEvent, outcome: str, final: ResolutionPackage | None,
                 plan: FallbackPlan, turns_used: int, decisions: list[Decision], stamp: dict,
                 why: str | None = None) -> ConsensusRecord:
    """ConsensusRecord for the run. checker_pass is True for CONSENSUS (only reachable on PASS)."""
    fb_arr = None if plan.divert else _gate_arrival(event, plan.slot)
    if outcome == "CONSENSUS" and final is not None:
        plan_arr = _gate_arrival(event, final.slot)
        package = {**final.model_dump(mode="json"), **_landing(event, final.slot, final.gate)}
        checker_pass: bool | None = True
    elif plan.divert or plan.slot is None or plan.gate is None:
        plan_arr, checker_pass = None, None
        package = {"package_id": FALLBACK_PACKAGE_ID, **_divert(event, plan), "fallback_reason": why}
    else:
        plan_arr = fb_arr
        checker_pass = plan.check.passed if plan.check is not None else None
        package = {"package_id": FALLBACK_PACKAGE_ID, **_landing(event, plan.slot, plan.gate),
                   "fallback_reason": why}
    hist = to_min(event.hist_arrival)
    return ConsensusRecord(
        event_id=event.event_id, outcome=outcome, package=package, turns_used=turns_used,
        decisions=decisions, checker_pass=checker_pass,
        delta_vs_history_min=None if plan_arr is None else hist - plan_arr,
        delta_vs_fallback_min=None if plan_arr is None or fb_arr is None else fb_arr - plan_arr,
        stamp=stamp,
    )


def _rel(n: int | None) -> str:
    if n is None:
        return "n/a"
    return "same time" if n == 0 else f"{abs(n)} min {'earlier' if n > 0 else 'later'}"


def record_text(record: ConsensusRecord, event: DisruptionEvent) -> str:
    p = record.package
    why = f" ({p['fallback_reason']})" if p.get("fallback_reason") else ""
    head = f"Outcome {record.outcome} after {record.turns_used} turns{why}."
    if p.get("divert"):
        body = f"Flight plan: {p['flight_plan']}."
    else:
        body = (f"Revised arrival: land {event.flight.dest} {p['slot']}, gate {p['gate']}, at gate "
                f"{p['gate_arrival']} vs {event.hist_arrival} historically "
                f"({_rel(record.delta_vs_history_min)}, illustrative). Holding {p['holding_min']} min, "
                f"{p['holding_fuel_kg']:,.0f} kg fuel at {p['burn_kg_per_h']:,} kg/h.")
        if record.outcome == "FALLBACK":
            body += f" Checker {'PASS' if record.checker_pass else 'FAIL: manual handling required'}."
    return f"{head} {body} Awaiting human sign-off before execution."


def _short(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def make_stamp(agents: dict[AgentRole, Agent]) -> dict:
    """Versions behind the decision: policy, solver, checker, model, prompt hashes, constraints."""
    models = {getattr(a, "model", None) for a in agents.values()} - {None}
    prompts = {r.value: getattr(a, "prompt_version", None) for r, a in agents.items()
               if getattr(a, "prompt_version", None)}
    kinds = sorted({type(a).__name__ for a in agents.values()})
    return {
        "policy": config.POLICY_VERSION,
        "policy_weights_hash": _short(canonical_json(config.POLICY)),
        "solver": SOLVER_VERSION,
        "checker": CHECKER_VERSION,
        "coordinator": COORDINATOR_VERSION,
        "constraints": constraints.constraints_version(),
        "agents": ",".join(kinds),
        "model": ",".join(sorted(models)) if models else RULE_AGENTS_VERSION,
        "prompt_hashes": prompts,
        "seed_salt_hash": _short(config.SEED_SALT),
    }
