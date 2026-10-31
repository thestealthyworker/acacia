"""Tests 21 (dispatcher side), 23 and scenarios E and F (HANDOVER 12, 14) on the synthetic
multi-trigger day (tests/fixtures/multi_trigger_day.csv). Rule agents, offline, fixed salt."""

from __future__ import annotations

from pathlib import Path

import pytest

from acacia import config
from acacia.agents.factory import make_agents
from acacia.data import DataStore
from acacia.dispatcher import Dispatcher, DispatcherError
from acacia.ledger import DoubleBookingError, Ledger
from acacia.log import verify_chain
from acacia.models import ConsensusRecord, LogEntry, Stress, TriggerQueue
from acacia.timeutil import to_min

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "multi_trigger_day.csv"
DATE = "2015-03-12"
AIRPORT = "ATL"
WINDOW = 180
TRIGGERS = {"DL201", "DL202", "DL203", "DL204", "DL205"}


@pytest.fixture(scope="module")
def store() -> DataStore:
    return DataStore(ROOT / "data", flights_path=FIXTURE, airports_path=ROOT / "data" / "sample_airports.csv")


def _dispatcher(store: DataStore, tmp_path: Path, stress: dict[str, Stress] | None = None) -> Dispatcher:
    return Dispatcher(store, make_agents("rule"), Ledger(DATE, runs_dir=tmp_path), config.IMPACT_POLICY, stress)


def _run(d: Dispatcher) -> list[LogEntry]:
    return list(d.run())


def _queue(entries: list[LogEntry]) -> TriggerQueue:
    return TriggerQueue.model_validate(next(e for e in entries if e.type == "TriggerQueue").payload)


# --- collect -----------------------------------------------------------------------------------------
def test_scan_collects_every_qualifying_trigger_once(store: DataStore, tmp_path: Path) -> None:
    d = _dispatcher(store, tmp_path)
    events = d.collect(scan=(AIRPORT, DATE, WINDOW))
    assert {e.flight.code for e in events} == TRIGGERS
    again = d.collect(scan=(AIRPORT, DATE, WINDOW))
    assert again == [] and len(d.events) == 5


def test_payloads_and_scan_dedupe_by_event_id(store: DataStore, tmp_path: Path) -> None:
    d = _dispatcher(store, tmp_path)
    first = d.collect(payloads=[{"flight": "DL201", "date": DATE, "stress": {"tight_crew": True}}])
    assert len(first) == 1 and d.stress[first[0].event_id].tight_crew is True
    more = d.collect(scan=(AIRPORT, DATE, WINDOW))
    assert len(more) == 4 and len(d.events) == 5


def test_run_without_events_raises(store: DataStore, tmp_path: Path) -> None:
    with pytest.raises(DispatcherError):
        list(_dispatcher(store, tmp_path).run())


# --- scenario E ------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def scenario_e(store: DataStore, tmp_path_factory) -> tuple[Dispatcher, list[LogEntry]]:
    d = _dispatcher(store, tmp_path_factory.mktemp("ledger"))
    d.collect(scan=(AIRPORT, DATE, WINDOW))
    return d, _run(d)


def test_e_states_and_orchestrator_chain(scenario_e) -> None:
    d, entries = scenario_e
    orch = [e for e in entries if e.who == "orchestrator"]
    states = [e.state for e in orch if e.type == "STATE"]
    assert states == ["COLLECTING", "TRIAGING", "CLUSTERING", "DISPATCHING", "DRAINED"]
    assert verify_chain(orch) == (True, None)
    assert orch[-1].type == "SUMMARY"
    assert all(e.event_id for e in orch if e.type in ("ImpactAssessment", "DISPATCH", "LEDGER"))


def test_e_queue_ranked_by_tier_then_impact(scenario_e) -> None:
    d, entries = scenario_e
    q = _queue(entries)
    assert q.airport == AIRPORT and q.date == DATE and len(q.items) == 5
    assert [it.rank for it in q.items] == list(range(1, 6))
    # clusters run one after another; inside a cluster the rank key (tier, -impact, deadline, id) is sorted
    by_cluster: dict[str, list[tuple]] = {}
    for it in q.items:
        by_cluster.setdefault(it.cluster_id, []).append(
            (it.tier, -it.impact_score, to_min(it.decision_deadline), it.event_id))
    assert len(by_cluster) == 2 and all(keys == sorted(keys) for keys in by_cluster.values())
    assert [it.cluster_id for it in q.items] == sorted(
        (it.cluster_id for it in q.items), key=lambda c: min(by_cluster[c]))  # best member orders the clusters
    assert all(it.status == "QUEUED" for it in q.items)


def test_e_shared_runway_window_clusters(scenario_e) -> None:
    d, entries = scenario_e
    clusters = [e for e in entries if e.type == "CLUSTER"]
    members = [{d.events[i].flight.code for i in c.payload["event_ids"]} for c in clusters]
    assert {"DL201", "DL202", "DL203"} in members and {"DL204", "DL205"} in members
    assert all(any("shared runway window ATL" in r for r in c.payload["reasons"]) for c in clusters)
    first = clusters[0].payload["event_ids"][0]
    assert first == _queue(entries).items[0].event_id  # clusters ordered by best member


def test_e_assessments_show_downstream_chain(scenario_e) -> None:
    d, entries = scenario_e
    by_code = {d.events[e.event_id].flight.code: e.payload for e in entries if e.type == "ImpactAssessment"}
    assert set(by_code) == TRIGGERS
    rot = [x for x in by_code["DL201"]["downstream"] if x["edge_type"] == "ROTATION"]
    assert [x["propagated_delay_min"] for x in rot] == [25, 15]   # slack 15 (ATL 45), then 10 (MCO 35)
    assert by_code["DL201"]["breakdown"]["knock_on_min"] == 40
    assert any(x["edge_type"] == "CONNECTION" for x in by_code["DL201"]["downstream"])
    assert "knock-on 40 min" in next(e.text for e in entries if e.type == "ImpactAssessment"
                                     and d.events[e.event_id].flight.code == "DL201")


def test_e_negotiated_in_rank_order_with_fresh_views(scenario_e) -> None:
    d, entries = scenario_e
    dispatched = [e.event_id for e in entries if e.type == "DISPATCH"]
    assert dispatched == [it.event_id for it in _queue(entries).items]
    for eid in dispatched:
        seg = d.logs[eid]
        assert seg[0].type == "STATE" and seg[0].dispatched_by is not None
        dispatch = next(e for e in entries if e.type == "DISPATCH" and e.event_id == eid)
        assert seg[0].dispatched_by == {"seq": dispatch.seq, "hash": dispatch.hash}
        assert verify_chain(seg) == (True, None)
        assert seg[-1].type == "ConsensusRecord" and d.status[eid] == "AWAITING_SIGNOFF"


def test_e_no_double_booking_across_events(scenario_e) -> None:
    d, entries = scenario_e
    rows = [r for r in d.ledger.all_reservations() if r.status in ("HELD", "COMMITTED")]
    slots = [(r.airport, r.start) for r in rows if r.resource_type == "RUNWAY_SLOT"]
    assert len(slots) == len(set(slots))
    gates = [r for r in rows if r.resource_type == "GATE"]
    for i, a in enumerate(gates):
        for b in gates[i + 1:]:
            if a.resource_id == b.resource_id and a.airport == b.airport:
                assert to_min(a.end) <= to_min(b.start) or to_min(b.end) <= to_min(a.start)
    booked = {r.event_id for r in rows}
    for eid, rec in d.records.items():
        held = eid in booked
        assert held == (not rec.package.get("divert") and rec.checker_pass is not False)
    assert len(booked) >= 3  # the fixture day books most of its triggers


def test_e_summary_counts(scenario_e) -> None:
    d, entries = scenario_e
    s = entries[-1].payload
    assert entries[-1].type == "SUMMARY"
    assert sum(s["outcomes"].values()) == 5
    assert s["knock_on_min"]["do_nothing"] == sum(a.breakdown["knock_on_min"] for a in d.assessments.values())
    assert 0 <= s["consensus_rate"] <= 1
    assert s["knock_on_min"]["after_plans"] >= 0


def test_e_is_deterministic(store: DataStore, tmp_path: Path, scenario_e) -> None:
    _, entries = scenario_e
    d2 = _dispatcher(store, tmp_path)
    d2.collect(scan=(AIRPORT, DATE, WINDOW))
    again = _run(d2)
    assert [e.hash for e in entries if e.who == "orchestrator"] == [e.hash for e in again if e.who == "orchestrator"]


# --- sign-off and ledger lifecycle -------------------------------------------------------------------
def test_sign_off_commits_or_releases(store: DataStore, tmp_path: Path) -> None:
    d = _dispatcher(store, tmp_path)
    d.collect(scan=(AIRPORT, DATE, WINDOW))
    _run(d)
    held = [eid for eid in d.records if any(r.status == "HELD" for r in d.ledger.reservations_for(eid))]
    assert len(held) >= 2
    approve, reject = held[0], held[1]
    entry = d.sign_off(approve, True, "duty manager")
    assert entry.type == "SignOff" and d.status[approve] == "APPROVED"
    assert {r.status for r in d.ledger.reservations_for(approve)} == {"COMMITTED"}
    assert verify_chain(d.log_segments(approve)[-1]) == (True, None)
    d.sign_off(reject, False, "duty manager")
    assert d.status[reject] == "REJECTED"
    assert {r.status for r in d.ledger.reservations_for(reject)} == {"RELEASED"}
    ledger_entries = [e for e in d.entries if e.type == "LEDGER" and e.event_id in (approve, reject)]
    assert any("commit" in e.text.lower() for e in ledger_entries)
    assert any("release" in e.text.lower() for e in ledger_entries)
    with pytest.raises(DispatcherError):
        d.sign_off(approve, True, "duty manager")


def test_double_booking_on_hold_retries_once_then_manual(store: DataStore, tmp_path: Path, monkeypatch) -> None:
    d = _dispatcher(store, tmp_path)
    d.collect(payloads=[{"flight": "DL201", "date": DATE}])
    real_hold = d.ledger.hold
    calls: list[int] = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise DoubleBookingError("simulated: world changed mid-run")
        return real_hold(*args, **kwargs)

    monkeypatch.setattr(d.ledger, "hold", flaky)
    entries = _run(d)
    eid = next(iter(d.events))
    assert [e.type for e in entries if e.type == "DISPATCH"] == ["DISPATCH", "DISPATCH"]
    assert any(e.type == "LEDGER" and "DoubleBookingError" in e.text for e in entries)
    assert len(calls) == 2 and d.status[eid] == "AWAITING_SIGNOFF"
    assert len(d.log_segments(eid)) == 2

    d2 = _dispatcher(store, tmp_path / "b")
    d2.collect(payloads=[{"flight": "DL201", "date": DATE}])
    monkeypatch.setattr(d2.ledger, "hold", lambda *a, **k: (_ for _ in ()).throw(DoubleBookingError("always")))
    entries2 = _run(d2)
    eid2 = next(iter(d2.events))
    assert d2.outcomes[eid2] == "MANUAL" and d2.status[eid2] == "REJECTED"
    assert entries2[-1].payload["outcomes"].get("MANUAL") == 1


# --- scenario F / test 23 --------------------------------------------------------------------------------
def test_f_preemption_requeues_without_losing_the_log(store: DataStore, tmp_path: Path) -> None:
    d = _dispatcher(store, tmp_path)
    d.collect(scan=(AIRPORT, DATE, WINDOW))
    gen = d.run()
    entries: list[LogEntry] = []
    lowest: str | None = None
    first_dispatched: str | None = None
    for e in gen:
        entries.append(e)
        if e.type == "TriggerQueue":
            lowest = TriggerQueue.model_validate(e.payload).items[-1].event_id
        if e.type == "DISPATCH" and first_dispatched is None:
            first_dispatched = e.event_id
            d.preempt(lowest, Stress(low_fuel=True))
    assert lowest is not None and first_dispatched is not None and lowest != first_dispatched
    assert d.assessments[lowest].tier == 0 and "FUEL" in d.assessments[lowest].tier_reason
    preempt = next(e for e in entries if e.type == "PREEMPT")
    assert preempt.payload["preempted"] == first_dispatched and preempt.payload["by"] == lowest
    dispatched = [e.event_id for e in entries if e.type == "DISPATCH"]
    assert dispatched[0] == first_dispatched and dispatched[1] == lowest
    assert dispatched.count(first_dispatched) == 2  # re-queued and run again
    segments = d.log_segments(first_dispatched)
    assert len(segments) == 2
    assert segments[0][-1].state == "PREEMPTED" and segments[0][-1].event_id == first_dispatched
    assert all(verify_chain(s) == (True, None) for s in segments)
    assert segments[1][-1].type == "ConsensusRecord"
    assert all(d.status[eid] == "AWAITING_SIGNOFF" for eid in d.events)
    assert d.preempted[first_dispatched] == 1
    assert ConsensusRecord.model_validate(d.records[lowest].model_dump()).outcome == "FALLBACK"
    tier0_items = [it for it in d.queue().items if it.event_id == lowest]
    assert tier0_items[0].rank == 1
    orch = [e for e in entries if e.who == "orchestrator"]
    assert verify_chain(orch) == (True, None)
    assert any(e.type == "TRIAGE" and e.event_id == lowest for e in orch)
    assert not any(r.status == "HELD" for r in d.ledger.reservations_for(lowest))  # low fuel diverts


def test_preempt_unknown_event_raises(store: DataStore, tmp_path: Path) -> None:
    d = _dispatcher(store, tmp_path)
    with pytest.raises(KeyError):
        d.preempt("EVT-NOPE", Stress(low_fuel=True))
