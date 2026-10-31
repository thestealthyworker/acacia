"""Reasoning text and the TriggerQueue payload for the orchestrator log (HANDOVER 18.6: every
dispatcher `text` explains its reasoning in one or two lines). Pure builders, no state."""

from __future__ import annotations

import hashlib

from acacia.models import DisruptionEvent, ImpactAssessment, QueueItem, Stress, TriggerQueue
from acacia.timeutil import to_hhmm, to_min


def build_queue(date: str, scan_airport: str | None, order: list[str], events: dict[str, DisruptionEvent],
                assessments: dict[str, ImpactAssessment], status: dict[str, str], graph_version: str) -> TriggerQueue:
    """TriggerQueue in dispatch order; queue_id = "Q-" + sha256(date|airport|sorted ids)[:8]."""
    etas = [to_min(e.eta_landing) for e in events.values()]
    dests = {e.flight.dest for e in events.values()}
    items = [QueueItem(rank=i + 1, event_id=eid, flight_no=events[eid].flight.code, tier=assessments[eid].tier,
                       impact_score=assessments[eid].impact_score, cluster_id=assessments[eid].cluster_id,
                       decision_deadline=assessments[eid].decision_deadline, status=status[eid])
             for i, eid in enumerate(order)]
    key = f"{date}|{scan_airport}|{'|'.join(sorted(events))}"
    return TriggerQueue(queue_id="Q-" + hashlib.sha256(key.encode()).hexdigest()[:8].upper(),
                        airport=scan_airport or (dests.pop() if len(dests) == 1 else None), date=date,
                        window=f"{to_hhmm(min(etas))}-{to_hhmm(max(etas))}", items=items,
                        graph_version=graph_version, policy_version=next(iter(assessments.values())).policy_version)


def assessment_text(event: DisruptionEvent, a: ImpactAssessment) -> str:
    b = a.breakdown
    legs = int(b["flights_affected"])
    return (f"{event.flight.code}: tier {a.tier} ({a.tier_reason}), impact {a.impact_score} "
            f"(knock-on {int(b['knock_on_min'])} min over {legs} leg{'s' if legs != 1 else ''}, "
            f"{int(b['pax_connections_at_risk'])} passengers at risk, {int(b['late_night_legs'])} late-night legs).")


def triage_text(code: str, stress: Stress, old: ImpactAssessment, new: ImpactAssessment, status: str,
                rank: int | None, busy: str | None, jumps: bool) -> str:
    head = f"{code} re-triaged with {stress.model_dump()}: tier {old.tier} -> {new.tier} ({new.tier_reason}). "
    if status != "QUEUED":
        return head + f"It is {status}, so the queue is unchanged."
    tail = f"{busy} yields after its current turn." if jumps and busy else "no run is interrupted."
    return head + f"Moves to rank {rank}; " + tail


def preempt_text(code: str, by: str, why: str, turn: int | None, kept: int, rank: int) -> str:
    return (f"{code} pre-empted by {by} ({why}) after turn {turn}: its {kept} log entries are kept, holds "
            f"released, re-queued at rank {rank}. {by} is dispatched next.")


def hold_text(code: str, outcome: str, slot: str, gate: str, start: str, end: str, turn: int,
              runway: str | None = None) -> str:
    on = f" on {runway}" if runway else ""
    return (f"{code}: {outcome} plan HELD: runway slot {slot}{on}, gate {gate} {start}-{end} (turnaround {turn} "
            "min). Awaiting human sign-off.")


def summary_text(p: dict) -> str:
    before, after = p["knock_on_min"]["do_nothing"], p["knock_on_min"]["after_plans"]
    return (f"Drained: {', '.join(f'{v} {k}' for k, v in p['outcomes'].items())}. Consensus rate "
            f"{p['consensus_rate']:.0%}, {p['fallbacks']} fallback(s) incl. {p['diverts']} divert(s). Knock-on "
            f"{before:.0f} min if nothing is done vs {after:.0f} min after the plans (re-propagated with each "
            "planned delay).")
