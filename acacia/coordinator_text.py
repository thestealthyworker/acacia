"""Readable text for coordinator log entries (wording modelled on docs/ui-reference.html).

Also builds the FALLBACK reason strings that CONSTRAINTS.md 6 requires ("the log states why"):
the run's FALLBACK STATE entry, the FallbackPlan and the ConsensusRecord carry the same string.
Pure functions only: nothing here decides anything.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from acacia import config, constraints
from acacia.models import (
    AgentRole, CheckResult, ConstraintPacket, Decision, DisruptionEvent, ResolutionPackage,
)
from acacia.solver import Cut, SolveResult
from acacia.timeutil import to_min


def role_name(role: AgentRole) -> str:
    return "ATC" if role == AgentRole.ATC else role.value.title()


def guard_text(event: DisruptionEvent) -> str:
    w, late = event.causes.weather, event.causes.late_aircraft
    parts = [f"weather {w} > {config.WEATHER_THRESHOLD}"] if w > config.WEATHER_THRESHOLD else []
    if late > config.LATE_AIRCRAFT_THRESHOLD:
        parts.append(f"late aircraft {late} > {config.LATE_AIRCRAFT_THRESHOLD}")
    return " and ".join(parts) or "payload-injected disruption"


def _hard(k: ConstraintPacket) -> dict:
    return {h.rule: h.value for h in k.hard}


def _wake_text(value: object) -> str:
    if not isinstance(value, dict):
        return "wake separation not declared"
    mins = [float(v) for v in (value.get("min_minutes") or {}).values() if isinstance(v, (int, float))]
    span = f", {min(mins):.1f} to {max(mins):.1f} min" if mins else ""
    return f"wake separation per the workbook table (own class {value.get('own_class', '?')}{span})"


def _atc_text(k: ConstraintPacket) -> str:
    hard = _hard(k)
    offers = ", ".join(o.value + (" (contested)" if (o.demand or 0) >= 3 else "") for o in k.options)
    sep = (_wake_text(hard["WAKE_SEPARATION"]) if "WAKE_SEPARATION" in hard
           else f"{hard.get('MIN_SEPARATION_MIN', '?')} min separation")
    return (f"Runway: {k.facts_owned.get('runway', 'n/a')}. I can offer {offers or 'no slots'}. "
            f"Hard: these slots only, {sep}.")


def _pilot_text(k: ConstraintPacket) -> str:
    hard = _hard(k)
    soft = "; ".join(f"{s.preference.lower()} (w {s.weight})" for s in k.soft) or "none"
    duty = f"; flight crew duty ends {hard['FLIGHT_CREW_DUTY_END']}" if "FLIGHT_CREW_DUTY_END" in hard else ""
    return f"Hard: must land by {hard.get('LAND_BY', '?')} (fuel){duty}. Soft: {soft}."


def _ground_text(k: ConstraintPacket) -> str:
    hard = _hard(k)
    gates = hard.get("GATE_FREE_FROM") or {}
    listed = ", ".join(f"{g} " + ("unavailable" if v == "unavailable" else f"free {v}") for g, v in gates.items())
    notice = hard.get("GATE_CHANGE_NOTICE")
    notice_txt = (f"; a gate change needs {notice.get('min_notice_min')} min notice "
                  f"(decided {notice.get('decision_time')})" if isinstance(notice, dict) else "")
    soft = "; ".join(s.preference.lower() for s in k.soft) or "none"
    return (f"Gates: {listed or 'none'}. Hard: crew duty limit {hard.get('CREW_DUTY_LIMIT', '?')}"
            f"{notice_txt}. Soft: {soft}.")


def packet_text(k: ConstraintPacket) -> str:
    if k.agent == AgentRole.ATC:
        return _atc_text(k)
    if k.agent == AgentRole.PILOT:
        return _pilot_text(k)
    return _ground_text(k)


def check_text(res: CheckResult) -> str:
    rules = "; ".join(("PASS " if r.passed else "FAIL ") + r.rule.value.lower().replace("_", " ")
                      + f" ({r.detail})" for r in res.rules)
    return (("PASS. " if res.passed else "FAIL. ") + rules
            + ". Verified against ground truth, independent of the agents and the solver.")


def holding(event: DisruptionEvent, slot: str) -> tuple[int, float]:
    """(holding minutes, holding fuel kg) for landing at `slot` (CONSTRAINTS.md 4, FUEL-4)."""
    eta, at = to_min(event.eta_landing), to_min(slot)
    wake = constraints.wake_class(event.distance)
    return max(0, at - eta), constraints.holding_fuel_kg(at, eta, wake)


def send_text(event: DisruptionEvent, pkg: ResolutionPackage, turn: int) -> str:
    mins, kg = holding(event, pkg.slot)
    return (f"Turn {turn} of {config.MAX_TURNS}. Sending `{pkg.package_id}` (land {pkg.slot}, gate "
            f"{pkg.gate}; holding {mins} min, {kg:,.0f} kg fuel) to all agents and to the checker.")


def solver_text(result: SolveResult, cuts: list[Cut]) -> str:
    top, n = result.top, len(result.all)
    disc = sum(c.status == "DISCARDED" for c in result.all)
    cut = sum(c.status == "CUT" for c in result.all)
    text = f"Enumerated {n} slot x gate packages. Discarded {disc} on declared hard limits" + (
        f", {cut} by reject cuts" if cuts else "") + ". "
    if top is None:
        return text + "Feasible set is empty."
    text += f"Top ranked: `{top.package_id}` {top.slot} + {top.gate}, score {top.score:.2f}"
    return text + (f" (corrupted: breaks {', '.join(result.corrupt_breaks)})." if result.corrupted else ".")


# --- FALLBACK reasons (CONSTRAINTS.md 6) -------------------------------------------------------------
def atc_missing_reason(error: str | None) -> str:
    return "ATC timeout: no runway slots were declared" + (f" ({error})" if error else "")


def empty_set_reason(result: SolveResult, packets: dict[AgentRole, ConstraintPacket], turn: int) -> str:
    """Why no package survived: the binding declared limit first, then the counts."""
    if not result.all:
        n_slots = sum(o.type == "SLOT" for o in getattr(packets.get(AgentRole.ATC), "options", []))
        n_gates = sum(o.type == "GATE" for o in getattr(packets.get(AgentRole.GROUND), "options", []))
        return f"feasible set empty: no packages to enumerate ({n_slots} slots x {n_gates} gates declared)"
    land_by = _hard(packets[AgentRole.PILOT]).get("LAND_BY") if AgentRole.PILOT in packets else None
    if land_by and all("FUEL_LIMIT" in {r.value for r in c.discard} for c in result.all):
        first = min(c.package.slot for c in result.all)
        return (f"feasible set empty: no separated slot before LAND_BY {land_by} "
                f"(earliest offered {first})")
    codes = Counter(r.value for c in result.all if c.status == "DISCARDED" for r in c.discard)
    cut_by = sorted({c.cut_by for c in result.all if c.status == "CUT" and c.cut_by})
    parts = []
    if codes:
        parts.append("discarded on " + ", ".join(f"{k} x{v}" for k, v in sorted(codes.items())))
    if cut_by:
        parts.append(f"{sum(c.status == 'CUT' for c in result.all)} cut by rejects citing {', '.join(cut_by)}")
    tried = sum(c.status == "TRIED" for c in result.all)
    if tried:
        parts.append(f"{tried} already voted down")
    return f"feasible set empty at turn {turn}: " + "; ".join(parts)


def reject_summary(rejects: Iterable[Decision], timeouts: Iterable[Decision], check: CheckResult) -> str:
    """e.g. 'Ground REJECT CREW_DUTY citing G_S1; Pilot timeout (counted as REJECT); checker FAIL (FUEL_LIMIT)'."""
    timed_out = {id(d) for d in timeouts}
    parts = [f"{role_name(d.agent)} timeout (counted as REJECT)" if id(d) in timed_out
             else f"{role_name(d.agent)} REJECT {d.reason_code.value} citing {d.cites}" for d in rejects]
    if not check.passed:
        parts.append("checker FAIL (" + ", ".join(r.rule.value for r in check.failed()) + ")")
    return "; ".join(parts)


def turn_limit_reason(turn: int, summary: str) -> str:
    return f"turn {turn}: {summary}"
