"""HANDOVER 14 tests 16, 18, 19, 20 (FABLE_ADDENDUM 6 and 10): propagation, score, tier, rank, clusters."""

from __future__ import annotations

import hashlib

import pytest

from acacia import config
from acacia.data import DataStore
from acacia.graph import ImpactGraph
from acacia.models import DisruptionEvent, ImpactAssessment, Stress
from acacia.timeutil import to_min
from acacia.triangulate import triangulate
from acacia.triage import assess, cluster, propagate, rank, safety_tier, single_cluster_id
from tests.conftest import FIXTURES

DATE = "2015-03-10"
L1 = "DL901-2015-03-10-ATLDCA-0800"
L2 = "DL902-2015-03-10-DCAATL-1050"
L3 = "DL903-2015-03-10-ATLMCO-1340"
L4 = "DL904-2015-03-10-MCOATL-1545"
C1 = "DL2000-2015-03-10-ATLBOS-1340"
CALM = Stress()


def _pax(a: str, b: str) -> int:
    return config.seeded_rng(config.seed_key("pax", a, b)).randint(2, 15)


def _event(store: DataStore, flight_no: str, origin: str, dest: str) -> DisruptionEvent:
    return triangulate(store.find_flight("DL", flight_no, DATE, origin, dest))


@pytest.fixture(scope="module")
def graph(rotation_store: DataStore) -> ImpactGraph:
    return ImpactGraph.build(rotation_store.rows_for_date(DATE), DATE)


@pytest.fixture(scope="module")
def l1_event(rotation_store: DataStore) -> DisruptionEvent:
    event = _event(rotation_store, "901", "ATL", "DCA")
    assert event.hist_delay_min == 60
    return event


# --- test 16: slack and propagation -----------------------------------------------------------------
def test_16_propagation_on_fixture_chain(graph: ImpactGraph) -> None:
    out = propagate(graph, L1, 60)
    rot = [d for d in out if d.edge_type == "ROTATION"]
    assert [(d.node_id, d.depth, d.slack_min, d.propagated_delay_min) for d in rot] == [
        (L2, 1, 15, 45), (L3, 2, 5, 40), (L4, 3, 10, 30)]  # CONSTRAINTS.md 3 slacks 15/5/10
    assert all(not d.late_night for d in rot)
    con = [d for d in out if d.edge_type == "CONNECTION"]
    assert len(con) == 1
    c = con[0]
    assert (c.node_id, c.depth, c.gap_min, c.propagated_delay_min) == (C1, 2, 50, 45)
    assert c.pax_at_risk == _pax(L2, C1)
    assert c.slack_min is None


def test_16_stops_at_zero_delay_with_input_10(graph: ImpactGraph) -> None:
    out = propagate(graph, L1, 10)
    assert [(d.edge_type, d.node_id, d.propagated_delay_min) for d in out] == [("ROTATION", L2, 0)]


def test_16_max_depth_and_negative_slack_clamped(graph: ImpactGraph) -> None:
    shallow = propagate(graph, L1, 60, max_depth=1)
    assert [d.node_id for d in shallow if d.edge_type == "ROTATION"] == [L2]
    assert [d.node_id for d in shallow if d.edge_type == "CONNECTION"] == [C1]
    big = propagate(graph, L1, 1000)
    assert [d.propagated_delay_min for d in big if d.edge_type == "ROTATION"] == [985, 980, 970]
    assert propagate(graph, L4, 60) == []
    assert propagate(graph, "not-a-node", 60) == []


def test_16_connection_breaks_only_when_delay_exceeds_gap_minus_mct(graph: ImpactGraph) -> None:
    # L2 delay 15 -> gap 50 - 35 = 15, not strictly greater: holds (chain L2=15, L3=10, L4=0)
    out = propagate(graph, L1, 30)
    assert [(d.edge_type, d.propagated_delay_min) for d in out] == [
        ("ROTATION", 15), ("ROTATION", 10), ("ROTATION", 0)]
    # L2 delay 16: breaks
    out = propagate(graph, L1, 31)
    assert [d.node_id for d in out if d.edge_type == "CONNECTION"] == [C1]


def test_16_late_night_flag(graph: ImpactGraph) -> None:
    out = propagate(graph, L3, 500)  # L4 departs 15:45 + 490 = 23:55
    assert out[0].node_id == L4 and out[0].propagated_delay_min == 490 and out[0].late_night


# --- test 18: deterministic impact -------------------------------------------------------------------
def test_18_assess_fixture_formula(graph: ImpactGraph, l1_event: DisruptionEvent) -> None:
    a = assess(l1_event, graph, land_by=800, first_slot=700, stress=CALM, policy=config.IMPACT_POLICY)
    pax = _pax(L2, C1)
    assert a.breakdown == {"knock_on_min": 115.0, "pax_connections_at_risk": float(pax),
                           "late_night_legs": 0.0, "flights_affected": 3.0}
    assert a.impact_score == 115 + 3 * pax + 0 + 30
    assert a.input_delay_min == 60 and a.tier == 1 and a.tier_reason == "normal"
    assert a.decision_deadline == l1_event.eta_landing
    assert a.graph_version == graph.version and a.policy_version == config.IMPACT_POLICY_VERSION
    assert a.cluster_id == single_cluster_id(l1_event.event_id)
    assert len(a.downstream) == 4


def test_18_same_inputs_give_identical_assessment_and_order(
    rotation_store: DataStore, l1_event: DisruptionEvent
) -> None:
    l3 = _event(rotation_store, "903", "ATL", "MCO")

    def run() -> list[ImpactAssessment]:
        g = ImpactGraph.build(rotation_store.rows_for_date(DATE), DATE)
        return rank([assess(e, g, 800, 700, CALM, config.IMPACT_POLICY) for e in (l3, l1_event)])

    first, second = run(), run()
    assert [a.model_dump() for a in first] == [a.model_dump() for a in second]
    assert [a.event_id for a in first] == [l1_event.event_id, l3.event_id]


def test_18_trigger_not_in_graph_propagates_nothing(graph: ImpactGraph, l1_event: DisruptionEvent) -> None:
    synthetic = l1_event.model_copy(update={"flight": l1_event.flight.model_copy(update={"flight_no": "999"})})
    a = assess(synthetic, graph, 800, 700, CALM, config.IMPACT_POLICY)
    assert a.downstream == [] and a.impact_score == 0.0
    assert "not in graph" in a.tier_reason


def test_18_policy_weights_are_read_from_the_policy_dict(graph: ImpactGraph, l1_event: DisruptionEvent) -> None:
    policy = {**config.IMPACT_POLICY, "knock_on_min": 2.0, "pax_connections_at_risk": 0.0}
    a = assess(l1_event, graph, 800, 700, CALM, policy)
    assert a.impact_score == 2 * 115 + 30


# --- safety tier ------------------------------------------------------------------------------------
def test_safety_tier_rules(l1_event: DisruptionEvent) -> None:
    assert safety_tier(l1_event, 710, 700, CALM) == (0, "FUEL_MARGIN <= 10 min to first slot")
    assert safety_tier(l1_event, 711, 700, CALM) == (1, "normal")
    assert safety_tier(l1_event, 800, 700, Stress(low_fuel=True)) == (0, "LOW_FUEL stress")
    assert safety_tier(l1_event, 800, 700, Stress(declared_emergency=True)) == (0, "DECLARED_EMERGENCY")
    assert safety_tier(l1_event, 705, 700, Stress(low_fuel=True))[1].startswith("FUEL_MARGIN")


# --- test 19: priority ordering ------------------------------------------------------------------------
def _a(event_id: str, tier: int, score: float, deadline: str) -> ImpactAssessment:
    return ImpactAssessment(event_id=event_id, tier=tier, tier_reason="t", impact_score=score,
                            breakdown={}, downstream=[], input_delay_min=0, decision_deadline=deadline,
                            cluster_id="CL-x", graph_version="v", policy_version="impact-0.1")


def test_19_tier0_outranks_tier1_then_score_deadline_event_id() -> None:
    items = [_a("EVT-D", 1, 900.0, "10:00"), _a("EVT-C", 0, 1.0, "12:00"), _a("EVT-B", 1, 50.0, "11:00"),
             _a("EVT-A", 1, 50.0, "11:00"), _a("EVT-E", 1, 50.0, "09:00"), _a("EVT-F", 0, 1.0, "08:00")]
    assert [a.event_id for a in rank(items)] == ["EVT-F", "EVT-C", "EVT-D", "EVT-E", "EVT-A", "EVT-B"]
    assert rank([]) == []


# --- test 20: clustering ---------------------------------------------------------------------------------
def _assess_all(events: list[DisruptionEvent], g: ImpactGraph) -> list[ImpactAssessment]:
    return rank([assess(e, g, 800, 700, CALM, config.IMPACT_POLICY) for e in events])


def test_20_rotation_link_clusters_and_unrelated_stays_apart(
    rotation_store: DataStore, graph: ImpactGraph, l1_event: DisruptionEvent
) -> None:
    l3 = _event(rotation_store, "903", "ATL", "MCO")
    c1 = _event(rotation_store, "2000", "ATL", "BOS")
    assessments = _assess_all([c1, l3, l1_event], graph)
    clusters = cluster([c1, l3, l1_event], assessments, graph)
    assert [c.event_ids for c in clusters] == [[l1_event.event_id, l3.event_id], [c1.event_id]]
    joint = clusters[0]
    assert joint.reasons == ["rotation N900ZZ"]
    expected = "CL-" + hashlib.sha256("|".join(sorted([l1_event.event_id, l3.event_id])).encode()).hexdigest()[:8]
    assert joint.cluster_id == expected
    assert clusters[1].cluster_id == single_cluster_id(c1.event_id) and clusters[1].reasons == []


def test_20_connection_link_clusters(rotation_store: DataStore, graph: ImpactGraph) -> None:
    l2 = _event(rotation_store, "902", "DCA", "ATL")
    c1 = _event(rotation_store, "2000", "ATL", "BOS")
    clusters = cluster([l2, c1], _assess_all([l2, c1], graph), graph)
    assert len(clusters) == 1 and clusters[0].reasons == [f"connection {L2}->{C1}"]


def test_20_shared_runway_window_clusters_within_60_min() -> None:
    store = DataStore(FIXTURES.parent.parent / "data", flights_path=FIXTURES / "cluster_day.csv",
                      airports_path=FIXTURES.parent.parent / "data" / "sample_airports.csv")
    g = ImpactGraph.build(store.rows_for_date(DATE), DATE)
    e101, e102 = _event(store, "101", "DCA", "ATL"), _event(store, "102", "BOS", "ATL")
    e103, e104 = _event(store, "103", "MCO", "BOS"), _event(store, "104", "DCA", "BOS")
    assert abs(to_min(e101.eta_landing) - to_min(e102.eta_landing)) <= config.CLUSTER_WINDOW_MIN
    assert abs(to_min(e103.eta_landing) - to_min(e104.eta_landing)) > config.CLUSTER_WINDOW_MIN
    events = [e104, e103, e102, e101]
    clusters = cluster(events, _assess_all(events, g), g)
    members = [sorted(c.event_ids) for c in clusters]
    assert sorted([e101.event_id, e102.event_id]) in members
    assert [e103.event_id] in members and [e104.event_id] in members
    atl = next(c for c in clusters if len(c.event_ids) == 2)
    lo, hi = sorted([e101.eta_landing, e102.eta_landing])
    assert atl.reasons == [f"shared runway window ATL {lo}-{hi}"]
    # members are in rank order and clusters are ordered by their best member
    ranked = [a.event_id for a in _assess_all(events, g)]
    flat = [e for c in clusters for e in c.event_ids]
    assert sorted(flat, key=ranked.index) == flat
    assert [ranked.index(c.event_ids[0]) for c in clusters] == sorted(ranked.index(c.event_ids[0]) for c in clusters)


def test_20_cluster_is_deterministic_regardless_of_input_order(
    rotation_store: DataStore, graph: ImpactGraph, l1_event: DisruptionEvent
) -> None:
    l3 = _event(rotation_store, "903", "ATL", "MCO")
    one = cluster([l1_event, l3], _assess_all([l1_event, l3], graph), graph)
    two = cluster([l3, l1_event], _assess_all([l3, l1_event], graph), graph)
    assert [c.model_dump() for c in one] == [c.model_dump() for c in two]
