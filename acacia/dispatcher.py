"""Dispatcher: the orchestrator above the coordinator (FABLE_ADDENDUM 3 and 8, HANDOVER 18.6).

COLLECTING -> TRIAGING -> CLUSTERING -> DISPATCHING -> DRAINED. Triggers are collected first;
the airport world is built ONCE per destination from every collected trigger's injected events
(PLAN 2.A.2); triage uses the constraint-based `land_by` and first separated slot from
`simulate`; events run in rank order, each with a fresh ledger view. Only this module writes
the ledger (HELD on COMMITTED or non-divert FALLBACK; sign-off commits or releases). The
orchestrator log is its own hash chain (`who = "orchestrator"`); each DISPATCH entry's seq and
hash go into the started event's first entry as `dispatched_by`. `preempt` may be called
between the entries `run` yields. Nothing here prints or calls an LLM.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence

from acacia import constraints, coordinator, simulate, triage, trigger
from acacia import dispatcher_text as txt
from acacia.graph import ImpactGraph
from acacia.ledger import DoubleBookingError, Ledger, LedgerStateError
from acacia.log import LogChain
from acacia.models import (
    Cluster, ConsensusRecord, DisruptionEvent, ImpactAssessment, Injected, LogEntry, Stress, TriggerQueue,
)
from acacia.signoff import SignOffError, sign_off as human_sign_off
from acacia.timeutil import to_hhmm, to_min
from acacia.world import AirportWorld, get_world

log = logging.getLogger(__name__)

OUTCOME_CONSENSUS, OUTCOME_FALLBACK, OUTCOME_DIVERT, OUTCOME_MANUAL = "CONSENSUS", "FALLBACK", "DIVERT", "MANUAL"
MAX_HOLD_ATTEMPTS = 2  # one re-run after a DoubleBookingError (FABLE 8.4)


class DispatcherError(RuntimeError):
    """The dispatcher was asked for something its state does not allow."""


class Dispatcher:
    """One multi-trigger session for one date (FABLE_ADDENDUM 3 interface)."""

    def __init__(self, data, agents, ledger: Ledger, policy: dict, stress: dict[str, Stress] | None = None) -> None:
        self.data, self.agents, self.ledger, self.policy = data, agents, ledger, dict(policy)
        self.stress: dict[str, Stress] = dict(stress or {})
        self.events: dict[str, DisruptionEvent] = {}
        self.assessments: dict[str, ImpactAssessment] = {}
        self.clusters: list[Cluster] = []
        self.status: dict[str, str] = {}
        self.outcomes: dict[str, str] = {}
        self.records: dict[str, ConsensusRecord] = {}
        self.logs: dict[str, list[LogEntry]] = {}
        self.preempted: dict[str, int] = {}
        self.graph: ImpactGraph | None = None
        self.worlds: dict[str, AirportWorld] = {}
        self.entries: list[LogEntry] = []
        self._chain = LogChain()
        self._outbox: list[LogEntry] = []
        self._order: list[str] = []
        self._scan: tuple[str, str, int] | None = None
        self._negotiating: str | None = None
        self._preempt_by: str | None = None
        self._hold_attempts: dict[str, int] = {}

    # --- log -------------------------------------------------------------------------------------
    def _emit(self, type_: str, text: str, *, event_id: str | None = None, state: str | None = None,
              payload: dict | None = None) -> LogEntry:
        raw = self._chain.append("orchestrator", type_, text, state=state, payload=payload)
        entry = raw.model_copy(update={"event_id": event_id})
        self.entries.append(entry)
        self._outbox.append(entry)
        return entry

    def _flush(self) -> Iterator[LogEntry]:
        while self._outbox:
            yield self._outbox.pop(0)

    def log_segments(self, event_id: str) -> list[list[LogEntry]]:
        """An event's log split into its runs (a pre-empted event has one segment per attempt)."""
        segments: list[list[LogEntry]] = []
        for entry in self.logs.get(event_id, []):
            if entry.seq == 1:
                segments.append([])
            segments[-1].append(entry)
        return segments

    # --- collect ---------------------------------------------------------------------------------
    def collect(self, payloads: Sequence[dict] | None = None,
                scan: tuple[str, str, int] | None = None) -> list[DisruptionEvent]:
        """New events from payloads (trigger.to_event) and/or a scan; duplicates by event_id dropped."""
        found: list[tuple[DisruptionEvent, Stress]] = []
        for payload in payloads or []:
            event = trigger.to_event(payload, self.data)
            found.append((event, trigger.stress_for(payload, self.stress.get(event.event_id))))
        if scan is not None:
            airport, date, window = scan
            self._scan = (airport.upper(), date, int(window))
            for event in trigger.scan_events(self.data, {"airport": airport, "date": date, "window_min": window}):
                found.append((event, self.stress.get(event.event_id, Stress())))
        new: list[DisruptionEvent] = []
        for event, stress in found:
            if event.event_id in self.events:
                continue
            self.events[event.event_id] = event
            self.stress[event.event_id] = stress
            self.status[event.event_id] = "QUEUED"
            self.logs[event.event_id] = []
            self.preempted[event.event_id] = 0
            new.append(event)
        return new

    def _date(self) -> str:
        dates = sorted({e.flight.date for e in self.events.values()})
        if len(dates) != 1:
            raise DispatcherError(f"all triggers must share one date, got {dates}")
        return dates[0]

    def _sorted_events(self) -> list[DisruptionEvent]:
        return sorted(self.events.values(), key=lambda e: (to_min(e.eta_landing), e.event_id))

    # --- run -------------------------------------------------------------------------------------
    def run(self) -> Iterator[LogEntry]:
        """Yield orchestrator and per-event entries; each is tagged with its event_id."""
        if not self.events:
            raise DispatcherError("nothing collected: call collect() with payloads or a scan first")
        codes = ", ".join(e.flight.code for e in self._sorted_events())
        self._emit("STATE", f"Collected {len(self.events)} qualifying trigger(s): {codes}. Deduplicated by "
                   "event_id. Moving to `TRIAGING`.", state="COLLECTING",
                   payload={"event_ids": sorted(self.events), "scan": list(self._scan) if self._scan else None})
        yield from self._flush()
        yield from self._triage()
        yield from self._cluster()
        yield from self._dispatch_all()
        self._emit("STATE", "Every trigger is awaiting sign-off, rejected for manual handling or diverted. "
                   "Queue drained.", state="DRAINED", payload={"status": dict(sorted(self.status.items()))})
        text, payload = self._summary()
        self._emit("SUMMARY", text, payload=payload)
        yield from self._flush()

    # --- triage ----------------------------------------------------------------------------------
    def _triage(self) -> Iterator[LogEntry]:
        date = self._date()
        self.graph = ImpactGraph.build(self.data.rows_for_date(date), date)
        self._build_worlds(date)
        airports = ", ".join(f"{ap} ({len(w.closures())} storm window(s))" for ap, w in sorted(self.worlds.items()))
        self._emit("STATE", f"Impact graph {self.graph.version} for {date}: {len(self.graph.nodes)} flights, "
                   f"{len(self.graph.rotation)} rotation edges. Airport world built once from every collected "
                   f"trigger's injected events: {airports}. Triaging with the constraint-based land_by and "
                   "first separated slot.", state="TRIAGING",
                   payload={"graph_version": self.graph.version, "airports": sorted(self.worlds)})
        view = self.ledger.view()
        for event in self._sorted_events():
            a = self._assess(event, view)
            self.assessments[event.event_id] = a
            self._emit("ImpactAssessment", txt.assessment_text(event, a), event_id=event.event_id,
                       payload=a.model_dump(mode="json"))
        yield from self._flush()

    def _build_worlds(self, date: str) -> None:
        by_dest: dict[str, list[Injected]] = {}
        for event in self._sorted_events():
            by_dest.setdefault(event.flight.dest.upper(), []).extend(event.injected)
        self.worlds = {dest: get_world(dest, date, inj) for dest, inj in by_dest.items()}

    def _assess(self, event: DisruptionEvent, view) -> ImpactAssessment:
        if self.graph is None:
            raise DispatcherError("triage has not run yet")
        stress = self.stress.get(event.event_id, Stress())
        world = self.worlds[event.flight.dest.upper()]
        land_by, first = simulate.land_by_and_first_slot(event, stress, world, view)
        return triage.assess(event, self.graph, land_by, first, stress, self.policy)

    # --- cluster and queue -----------------------------------------------------------------------
    def _cluster(self) -> Iterator[LogEntry]:
        self.clusters = triage.cluster(list(self.events.values()), list(self.assessments.values()), self.graph)
        for c in self.clusters:
            for eid in c.event_ids:
                self.assessments[eid] = self.assessments[eid].model_copy(update={"cluster_id": c.cluster_id})
        self._emit("STATE", f"{len(self.clusters)} cluster(s) from {len(self.events)} trigger(s); clusters "
                   "ordered by their best-ranked member, members in rank order (tier, then impact).",
                   state="CLUSTERING", payload={"clusters": [c.cluster_id for c in self.clusters]})
        for c in self.clusters:
            codes = ", ".join(self.events[e].flight.code for e in c.event_ids)
            why = "; ".join(c.reasons) or "no shared resource (singleton)"
            self._emit("CLUSTER", f"Cluster {c.cluster_id}: {codes}. Why: {why}.", payload=c.model_dump(mode="json"))
        self._reorder()
        q = self.queue()
        head = q.items[0]
        self._emit("TriggerQueue", f"Queue of {len(q.items)}: ranked by safety tier first (a gate, not a weight), "
                   f"then impact, deadline, event_id. {self.events[head.event_id].flight.code} first: tier "
                   f"{head.tier}, impact {head.impact_score}.", payload=q.model_dump(mode="json"))
        self._emit("STATE", "Dispatching clusters in order; each event gets a fresh ledger view.",
                   state="DISPATCHING", payload={"order": list(self._order)})
        yield from self._flush()

    def _rank_key(self, eid: str) -> tuple:
        a = self.assessments[eid]
        return (a.tier, -a.impact_score, to_min(a.decision_deadline), eid)

    def _reorder(self) -> None:
        """Tier 0 first (any cluster), then cluster order, then rank within the cluster."""
        cluster_idx = {eid: i for i, c in enumerate(self.clusters) for eid in c.event_ids}
        self._order = sorted(self.events, key=lambda e: (self.assessments[e].tier, cluster_idx.get(e, 0),
                                                         self._rank_key(e)))

    def queue(self) -> TriggerQueue:
        return txt.build_queue(self._date(), self._scan[0] if self._scan else None, self._order, self.events,
                               self.assessments, self.status, self.graph.version if self.graph else "")

    # --- dispatch --------------------------------------------------------------------------------
    def _next_queued(self) -> str | None:
        return next((eid for eid in self._order if self.status[eid] == "QUEUED"), None)

    def _dispatch_all(self) -> Iterator[LogEntry]:
        while (eid := self._next_queued()) is not None:
            yield from self._dispatch_one(eid)

    def _dispatch_one(self, eid: str) -> Iterator[LogEntry]:
        event, stress = self.events[eid], self.stress[eid]
        if self._preempt_by == eid:
            self._preempt_by = None
        self.status[eid], self._negotiating = "NEGOTIATING", eid
        a = self.assessments[eid]
        again = " (re-run)" if self.preempted[eid] or self._hold_attempts.get(eid) else ""
        d = self._emit("DISPATCH", f"Dispatching {event.flight.code}{again}: rank {self._order.index(eid) + 1}, "
                       f"tier {a.tier}, impact {a.impact_score}, cluster {a.cluster_id}. Fresh ledger view "
                       f"with {len(self.ledger.all_reservations())} reservation(s) so far.", event_id=eid,
                       state="NEGOTIATING", payload={"event_id": eid, "stress": stress.model_dump(),
                                                     "cluster_id": a.cluster_id, "tier": a.tier})
        yield from self._flush()
        dispatched_by = {"seq": d.seq, "hash": d.hash}
        last = yield from self._negotiate(event, stress, dispatched_by)
        self._negotiating = None
        if last.type != "ConsensusRecord":
            yield from self._after_preempted(eid, last)
            return
        record = ConsensusRecord.model_validate(last.payload)
        self.records[eid] = record
        yield from self._book(eid, record)

    def _negotiate(self, event: DisruptionEvent, stress: Stress, dispatched_by: dict) -> Iterator[LogEntry]:
        eid = event.event_id
        gen = coordinator.run(event, stress, self.agents, self.ledger.view(),
                              should_yield=lambda: self._preempt_by is not None and self._preempt_by != eid,
                              data=self.data, ledger_live=self.ledger.live(),
                              world=self.worlds[event.flight.dest.upper()], dispatched_by=dispatched_by)
        last: LogEntry | None = None
        for entry in gen:
            self.logs[eid].append(entry)
            last = entry
            yield entry
            yield from self._flush()
        if last is None:
            raise DispatcherError(f"coordinator produced no entries for {eid}")
        return last

    def _after_preempted(self, eid: str, last: LogEntry) -> Iterator[LogEntry]:
        by = self._preempt_by
        self._release_quietly(eid)
        self.preempted[eid] += 1
        self.status[eid] = "QUEUED"
        self._reorder()
        kept = len(self.logs[eid])
        who = self.events[by].flight.code if by else "a tier 0 trigger"
        why = self.assessments[by].tier_reason if by else ""
        self._emit("PREEMPT", txt.preempt_text(self.events[eid].flight.code, who, why, last.turn, kept,
                                               self._order.index(eid) + 1), event_id=eid, state="PREEMPTED",
                   payload={"preempted": eid, "by": by, "turn": last.turn, "entries_kept": kept,
                            "order": list(self._order)})
        yield from self._flush()

    # --- booking ---------------------------------------------------------------------------------
    def _book(self, eid: str, record: ConsensusRecord) -> Iterator[LogEntry]:
        event, pkg = self.events[eid], record.package
        self._hold_attempts[eid] = self._hold_attempts.get(eid, 0) + 1
        if pkg.get("divert"):
            self.status[eid], self.outcomes[eid] = "AWAITING_SIGNOFF", OUTCOME_DIVERT
            self._emit("LEDGER", f"{event.flight.code}: FALLBACK divert to {pkg.get('alternate')}; nothing to "
                       "book at the destination. Awaiting sign-off.", event_id=eid,
                       payload={"op": "none", "outcome": record.outcome, "divert": True})
        elif pkg.get("slot") is None or pkg.get("gate") is None or record.checker_pass is not True:
            self._mark_manual(eid, f"plan has no bookable slot/gate or the checker failed it ({pkg.get('package_id')})")
        else:
            yield from self._hold(eid, record)
        yield from self._flush()

    def _hold(self, eid: str, record: ConsensusRecord) -> Iterator[LogEntry]:
        event, pkg = self.events[eid], record.package
        slot, gate = to_min(pkg["slot"]), str(pkg["gate"])
        start = slot + event.taxi_in
        end = min(start + constraints.min_turnaround(event.flight.dest), 1440)
        try:
            held = self.ledger.hold(eid, event.flight.dest.upper(), slot, gate, start, end,
                                    wake_class=constraints.wake_class(event.distance))
        except LedgerStateError as exc:  # the event already holds (a replayed --keep-ledger ledger)
            self._mark_manual(eid, f"hold refused, LedgerStateError: {exc}")
            return
        except DoubleBookingError as exc:
            self._release_quietly(eid)
            self._emit("LEDGER", f"{event.flight.code}: hold refused, DoubleBookingError: {exc}. The world changed "
                       "mid-run; nothing was written.", event_id=eid, payload={"op": "hold", "error": str(exc)})
            if self._hold_attempts[eid] < MAX_HOLD_ATTEMPTS:
                self.status[eid] = "QUEUED"
                yield from self._flush()
                yield from self._dispatch_one(eid)
            else:
                self._mark_manual(eid, "hold refused twice (DoubleBookingError)")
            return
        self.status[eid] = "AWAITING_SIGNOFF"
        self.outcomes[eid] = OUTCOME_CONSENSUS if record.outcome == "CONSENSUS" else OUTCOME_FALLBACK
        self._emit("LEDGER", txt.hold_text(event.flight.code, record.outcome, pkg["slot"], gate, to_hhmm(start),
                                           to_hhmm(end), constraints.min_turnaround(event.flight.dest)), event_id=eid,
                   payload={"op": "hold", "outcome": record.outcome, "reservations": [r.model_dump() for r in held],
                            "impact": self.assessments[eid].model_dump(mode="json")})

    def _mark_manual(self, eid: str, why: str) -> None:
        self.status[eid], self.outcomes[eid] = "REJECTED", OUTCOME_MANUAL
        self._emit("LEDGER", f"{self.events[eid].flight.code}: marked for manual handling: {why}.", event_id=eid,
                   payload={"op": "manual", "reason": why})

    def _release_quietly(self, eid: str) -> None:
        try:
            self.ledger.release(eid)
        except LedgerStateError:
            pass  # nothing held

    # --- pre-emption -----------------------------------------------------------------------------
    def preempt(self, event_id: str, stress: Stress) -> None:
        """Re-triage one queued event with new stress; a tier 0 result jumps the queue (FABLE 8.5)."""
        if event_id not in self.events:
            raise KeyError(f"unknown event {event_id}")
        self.stress[event_id] = stress
        if event_id not in self.assessments:
            return  # before triage: the new stress is used when triage runs
        old = self.assessments[event_id]
        new = self._assess(self.events[event_id], self.ledger.view()).model_copy(update={"cluster_id": old.cluster_id})
        self.assessments[event_id] = new
        code, status = self.events[event_id].flight.code, self.status[event_id]
        queued = status == "QUEUED"
        if queued:
            self._reorder()
        jumps = queued and new.tier == 0 and old.tier != 0
        busy = self.events[self._negotiating].flight.code if self._negotiating else None
        if jumps and self._negotiating:
            self._preempt_by = event_id
        rank = self._order.index(event_id) + 1 if queued else None
        self._emit("TRIAGE", txt.triage_text(code, stress, old, new, status, rank, busy, jumps), event_id=event_id,
                   payload={"tier": new.tier, "tier_reason": new.tier_reason, "status": status,
                            "order": list(self._order), "preempts": busy if jumps else None})

    # --- sign-off ---------------------------------------------------------------------------------
    def sign_off(self, event_id: str, approve: bool, who: str) -> LogEntry:
        """Human decision: commit the holds on approve, release on reject. Returns the SignOff entry."""
        record = self.records.get(event_id)
        if record is None or self.status.get(event_id) != "AWAITING_SIGNOFF":
            raise DispatcherError(f"{event_id} is not awaiting sign-off (status {self.status.get(event_id)})")
        try:
            entry, updated = human_sign_off(self.log_segments(event_id)[-1], record, approve, who)
        except SignOffError as exc:
            raise DispatcherError(str(exc)) from exc
        self.logs[event_id].append(entry)
        self.records[event_id] = updated
        self.status[event_id] = "APPROVED" if approve else "REJECTED"
        op = "commit" if approve else "release"
        try:
            (self.ledger.commit if approve else self.ledger.release)(event_id)
            what = f"reservations {op}ted" if approve else "reservations released"
        except LedgerStateError:
            what = "no reservations to " + op
        self._emit("LEDGER", f"{self.events[event_id].flight.code}: {updated.status} by {who.strip()}; {what}.",
                   event_id=event_id, state=updated.status, payload={"op": op, "who": who.strip(), "approved": approve})
        return entry

    # --- summary ---------------------------------------------------------------------------------
    def _planned_delay(self, eid: str) -> int:
        rec = self.records.get(eid)
        if rec is None or rec.package.get("divert") or rec.package.get("gate_arrival") is None:
            return self.events[eid].hist_delay_min  # divert or manual: no improvement claimed
        return max(0, to_min(rec.package["gate_arrival"]) - to_min(self.events[eid].sched_arrival))

    def _summary(self) -> tuple[str, dict]:
        counts: dict[str, int] = {}
        for eid in self.events:
            outcome = self.outcomes.get(eid, OUTCOME_MANUAL)
            counts[outcome] = counts.get(outcome, 0) + 1
        before = sum(a.breakdown["knock_on_min"] for a in self.assessments.values())
        after = 0.0
        for eid, event in self.events.items():
            node = triage.trigger_node(event, self.graph)
            if node is not None:
                after += triage.breakdown_of(triage.propagate(self.graph, node, self._planned_delay(eid)))["knock_on_min"]
        decided = len(self.records)
        rate = round(counts.get(OUTCOME_CONSENSUS, 0) / decided, 3) if decided else 0.0
        fallbacks = counts.get(OUTCOME_FALLBACK, 0) + counts.get(OUTCOME_DIVERT, 0)
        payload = {"outcomes": dict(sorted(counts.items())), "consensus_rate": rate, "fallbacks": fallbacks,
                   "diverts": counts.get(OUTCOME_DIVERT, 0), "manual": counts.get(OUTCOME_MANUAL, 0),
                   "knock_on_min": {"do_nothing": before, "after_plans": after},
                   "preempted": sum(self.preempted.values()), "reservations": len(self.ledger.all_reservations())}
        return txt.summary_text(payload), payload
