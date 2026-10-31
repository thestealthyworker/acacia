"""Declared hard-limit values shared by the rule and LLM agents (CONSTRAINTS.md 2-4).

Anthropic-free, so rule agents and the declaration audit build identical values from the
same private state. The solver reads these values from the packets; the checker re-derives
them from ground truth through `acacia.constraints`.
"""

from __future__ import annotations

from acacia import config, constraints
from acacia.models import DisruptionEvent, GroundPrivate, PilotPrivate
from acacia.timeutil import to_hhmm

WAKE_CLASSES = ("Heavy", "Large", "Small")
decision_time_min = constraints.decision_time_min


def wake_value(own_class: str) -> dict:
    """A_H2 WAKE_SEPARATION value: workbook minutes for every pair with this flight as leader or follower."""
    pairs = {f"{lead}>{follow}": round(constraints.wake_minutes(lead, follow), 1)
             for c in WAKE_CLASSES for lead, follow in ((c, own_class), (own_class, c))}
    return {"table": "workbook", "own_class": own_class, "min_minutes": dict(sorted(pairs.items()))}


def gate_notice_value(event: DisruptionEvent, g: GroundPrivate) -> dict:
    """G_H3 GATE_CHANGE_NOTICE value (hard, CONSTRAINTS.md 3); notice counts from commit_time (TIME-4)."""
    return {"min_notice_min": constraints.GATE_CHANGE_NOTICE_MIN,
            "decision_time": to_hhmm(decision_time_min(event)),
            "commit_time": to_hhmm(constraints.commit_time_min(event)), "assigned_gate": g.assigned_gate}


def slot_lead_value(event: DisruptionEvent) -> dict:
    """A_H3 SLOT_LEAD value (hard, TIME-3): no landing slot before commit_time + 5 min."""
    return {"commit_time": to_hhmm(constraints.commit_time_min(event)),
            "slot_lead_min": constraints.SLOT_LEAD_MIN,
            "earliest_slot": to_hhmm(constraints.earliest_slot_min(event))}


def crew_duty_hhmm(p: PilotPrivate) -> str | None:
    """P_H2 FLIGHT_CREW_DUTY_END as "HH:MM" when the duty end falls inside the day, else None
    (it cannot bind today, and HH:MM cannot express a later time)."""
    end = p.crew_duty_end
    if end is None or not 0 <= end < config.MINUTES_PER_DAY:
        return None
    return to_hhmm(end)
