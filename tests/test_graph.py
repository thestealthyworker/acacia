"""HANDOVER 14 tests 15 and 17 (FABLE_ADDENDUM 5): rotation and connection edges of the impact graph.

Plus blank/UNKNOWN tails, duplicate node ids, cancelled legs, bad airport codes and midnight.
"""

from __future__ import annotations

import pandas as pd
import pytest

from acacia import config
from acacia.data import FLIGHT_COLUMNS, DataStore
from acacia.graph import ImpactGraph, node_id_for

DATE = "2015-03-10"
L1 = "DL901-2015-03-10-ATLDCA-0800"
L2 = "DL902-2015-03-10-DCAATL-1050"
L3 = "DL903-2015-03-10-ATLMCO-1340"
L4 = "DL904-2015-03-10-MCOATL-1545"
C1 = "DL2000-2015-03-10-ATLBOS-1340"


def _row(**over: object) -> dict[str, str]:
    base = {c: "" for c in FLIGHT_COLUMNS}
    base.update(YEAR="2015", MONTH="3", DAY="10", DAY_OF_WEEK="2", AIRLINE="DL", DISTANCE="500",
                DIVERTED="0", CANCELLED="0")
    base.update({k: str(v) for k, v in over.items()})
    return base


def _frame(rows: list[dict[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(FLIGHT_COLUMNS), dtype=str)


def _leg(fn: str, tail: str, o: str, d: str, dep: str, arr: str, **over: object) -> dict[str, str]:
    return _row(FLIGHT_NUMBER=fn, TAIL_NUMBER=tail, ORIGIN_AIRPORT=o, DESTINATION_AIRPORT=d,
                SCHEDULED_DEPARTURE=dep, SCHEDULED_ARRIVAL=arr, **over)


@pytest.fixture(scope="module")
def graph(rotation_store: DataStore) -> ImpactGraph:
    return ImpactGraph.build(rotation_store.rows_for_date(DATE), DATE)


# --- test 15: rotation edges ---------------------------------------------------------------------
def test_15_rotation_chain_and_slack_on_fixture(graph: ImpactGraph) -> None:
    assert graph.rotation_next(L1) == (L2, 15)
    assert graph.rotation_next(L2) == (L3, 15)
    assert graph.rotation_next(L3) == (L4, 10)
    assert graph.rotation_next(L4) is None
    assert graph.rotation_next(C1) is None  # other tail, single leg


def test_15_node_ids_and_find(graph: ImpactGraph) -> None:
    assert set(graph.nodes) == {L1, L2, L3, L4, C1}
    assert graph.find("DL", "901", "ATL", "DCA") == L1
    assert graph.find("DL", "DL901", "ATL", "DCA", sched_dep=480) == L1  # minutes, not hhmm
    assert graph.find("DL", "901", "ATL", "DCA", sched_dep=800) is None
    assert graph.find("UA", "901", "ATL", "DCA") is None
    node = graph.node(L1)
    assert node["tail"] == "N900ZZ" and node["sched_dep"] == 480 and node["sched_arr"] == 600
    assert node["distance"] == 547 and node["arrival_delay"] == 60 and node["late_aircraft_delay"] == 0
    with pytest.raises(KeyError):
        graph.node("nope")


def test_15_no_edge_when_destination_differs_from_next_origin() -> None:
    rows = [_leg("1", "N1", "ATL", "DCA", "0800", "1000"), _leg("2", "N1", "BOS", "ATL", "1100", "1300")]
    g = ImpactGraph.build(_frame(rows), DATE)
    assert g.rotation_next(node_id_for("DL", "1", DATE, "ATL", "DCA", 800)) is None


def test_15_no_edge_across_dates() -> None:
    rows = [_leg("1", "N1", "ATL", "DCA", "0800", "1000"),
            _leg("2", "N1", "DCA", "ATL", "1100", "1300", DAY="11")]
    g = ImpactGraph.build(_frame(rows), DATE)
    assert len(g.nodes) == 1
    assert g.skipped["other_date"] == 1
    assert g.rotation_next(node_id_for("DL", "1", DATE, "ATL", "DCA", 800)) is None


def test_15_blank_and_unknown_tails_get_no_rotation_edges() -> None:
    rows = [_leg("1", "", "ATL", "DCA", "0800", "1000"), _leg("2", "", "DCA", "ATL", "1100", "1300"),
            _leg("3", "UNKNOWN", "ATL", "DCA", "0800", "1000"),
            _leg("4", "UNKNOWN", "DCA", "ATL", "1100", "1300")]
    g = ImpactGraph.build(_frame(rows), DATE)
    assert len(g.nodes) == 4
    assert all(g.rotation_next(n) is None for n in g.nodes)


def test_15_tolerance_minus_30_and_broken_chain() -> None:
    a = _leg("1", "N1", "ATL", "DCA", "0800", "1000")
    ok = _leg("2", "N1", "DCA", "ATL", "0930", "1130")  # dep = arr - 30: allowed, slack 0
    g = ImpactGraph.build(_frame([a, ok]), DATE)
    assert g.rotation_next(node_id_for("DL", "1", DATE, "ATL", "DCA", 800)) == (
        node_id_for("DL", "2", DATE, "DCA", "ATL", 930), 0)
    bad = _leg("2", "N1", "DCA", "ATL", "0929", "1130")  # 31 min early: broken chain
    g = ImpactGraph.build(_frame([a, bad]), DATE)
    assert g.rotation_next(node_id_for("DL", "1", DATE, "ATL", "DCA", 800)) is None


def test_15_slack_uses_min_turn_by_distance() -> None:
    a = _leg("1", "N1", "LAX", "JFK", "0800", "1600", DISTANCE="2475")
    b = _leg("2", "N1", "JFK", "LAX", "1730", "2100")
    g = ImpactGraph.build(_frame([a, b]), DATE)
    nxt = g.rotation_next(node_id_for("DL", "1", DATE, "LAX", "JFK", 800))
    assert nxt is not None and nxt[1] == 90 - config.min_turn(2475)


def test_15_at_most_one_successor_sorted_by_sched_dep_then_file_order() -> None:
    a = _leg("1", "N1", "ATL", "DCA", "0800", "1000")
    later = _leg("3", "N1", "DCA", "ATL", "1400", "1600")
    earlier = _leg("2", "N1", "DCA", "ATL", "1100", "1300")
    g = ImpactGraph.build(_frame([a, later, earlier]), DATE)
    assert g.rotation_next(node_id_for("DL", "1", DATE, "ATL", "DCA", 800)) == (
        node_id_for("DL", "2", DATE, "DCA", "ATL", 1100), 25)


def test_cancelled_diverted_and_blank_time_rows_are_skipped_and_counted() -> None:
    rows = [_leg("1", "N1", "ATL", "DCA", "0800", "1000"),
            _leg("2", "N1", "DCA", "ATL", "1100", "1300", CANCELLED="1"),
            _leg("3", "N1", "ATL", "MCO", "1500", "1700"),
            _leg("4", "N2", "ATL", "MCO", "1500", "", ),
            _leg("5", "N2", "ATL", "MCO", "", "1700"),
            _leg("6", "N3", "ATL", "MCO", "1500", "1700", DIVERTED="1"),
            _leg("7", "N3", "12345", "MCO", "1500", "1700")]
    g = ImpactGraph.build(_frame(rows), DATE)
    assert set(g.nodes) == {node_id_for("DL", "1", DATE, "ATL", "DCA", 800),
                            node_id_for("DL", "3", DATE, "ATL", "MCO", 1500)}
    assert g.skipped == {"cancelled": 1, "diverted": 1, "blank_time": 2, "bad_airport": 1,
                         "other_date": 0}
    # the cancelled leg is not in the graph, so the chain ends at leg 1 (leg 3 departs ATL, not DCA)
    assert g.rotation_next(node_id_for("DL", "1", DATE, "ATL", "DCA", 800)) is None


def test_duplicate_node_ids_get_file_order_suffixes() -> None:
    dup = _leg("1", "N1", "ATL", "DCA", "0800", "1000")
    rows = [dup, dict(dup, TAIL_NUMBER="N2"), dict(dup, TAIL_NUMBER="N3")]
    g = ImpactGraph.build(_frame(rows), DATE)
    base = node_id_for("DL", "1", DATE, "ATL", "DCA", 800)
    assert list(g.nodes) == [base, base + "-2", base + "-3"]
    assert g.node(base + "-2")["tail"] == "N2"
    assert g.find("DL", "1", "ATL", "DCA") == base


def test_midnight_departure_2400_parses_to_zero_and_sorts_first() -> None:
    a = _leg("1", "N1", "ATL", "DCA", "2400", "0200")
    b = _leg("2", "N1", "DCA", "ATL", "0300", "0500")
    g = ImpactGraph.build(_frame([a, b]), DATE)
    first = node_id_for("DL", "1", DATE, "ATL", "DCA", 2400)
    assert first.endswith("-2400") and g.node(first)["sched_dep"] == 0
    assert g.rotation_next(first) == (node_id_for("DL", "2", DATE, "DCA", "ATL", 300), 25)


# --- test 17: connection edges --------------------------------------------------------------------
def test_17_fixture_connection_window_and_seeded_pax(graph: ImpactGraph) -> None:
    conns = graph.connections(L2)
    assert [(n, gap) for n, gap, _ in conns] == [(C1, 50)]  # L3 is on the same tail: excluded
    pax = conns[0][2]
    assert pax == config.seeded_rng(config.seed_key("pax", L2, C1)).randint(2, 15)
    assert 2 <= pax <= 15
    assert graph.connections(L1) == []  # L2 is same tail; nothing else leaves DCA
    assert graph.connections(L3) == [] and graph.connections(C1) == []


def test_17_window_boundaries_35_and_120() -> None:
    a = _leg("1", "N1", "ATL", "DCA", "0800", "1000")
    rows = [a,
            _leg("2", "N2", "DCA", "BOS", "1034", "1200"),  # 34: too tight
            _leg("3", "N3", "DCA", "BOS", "1035", "1200"),  # 35: ok
            _leg("4", "N4", "DCA", "BOS", "1200", "1300"),  # 120: ok
            _leg("5", "N5", "DCA", "BOS", "1201", "1300"),  # 121: too long
            _leg("6", "N6", "DCA", "BOS", "1100", "1200", AIRLINE="UA"),  # other airline
            _leg("7", "N7", "ATL", "BOS", "1100", "1200")]  # other origin
    g = ImpactGraph.build(_frame(rows), DATE)
    a_id = node_id_for("DL", "1", DATE, "ATL", "DCA", 800)
    assert [(n, gap) for n, gap, _ in g.connections(a_id)] == [
        (node_id_for("DL", "3", DATE, "DCA", "BOS", 1035), 35),
        (node_id_for("DL", "4", DATE, "DCA", "BOS", 1200), 120)]


def test_17_fanout_cap_keeps_five_smallest_gaps_tiebreak_by_id() -> None:
    a = _leg("1", "N1", "ATL", "DCA", "0800", "1000")
    outs = [_leg(str(10 + i), f"N{10 + i}", "DCA", "BOS", "1100", "1200") for i in range(4)]  # gap 60
    outs += [_leg("30", "N30", "DCA", "BOS", "1040", "1200"),  # gap 40
             _leg("31", "N31", "DCA", "BOS", "1140", "1300")]  # gap 100: dropped by the cap
    g = ImpactGraph.build(_frame([a, *outs]), DATE)
    conns = g.connections(node_id_for("DL", "1", DATE, "ATL", "DCA", 800))
    assert len(conns) == config.CONNECTION_FANOUT
    assert [gap for _, gap, _ in conns] == [40, 60, 60, 60, 60]
    ids = [n for n, _, _ in conns[1:]]
    assert ids == sorted(ids)


def test_version_depends_on_date_and_rows_used(graph: ImpactGraph, rotation_store: DataStore) -> None:
    again = ImpactGraph.build(rotation_store.rows_for_date(DATE), DATE)
    assert graph.version == again.version and len(graph.version) == 12
    other = ImpactGraph.build(rotation_store.rows_for_date(DATE).iloc[:4], DATE)
    assert other.version != graph.version


def test_build_on_sample_data_is_quick_and_deterministic(sample_store: DataStore) -> None:
    date = sample_store.date_range[0]
    rows = sample_store.rows_for_date(date)
    g1 = ImpactGraph.build(rows, date)
    g2 = ImpactGraph.build(rows, date)
    assert g1.nodes == g2.nodes and g1.rotation == g2.rotation and g1.conns == g2.conns
    assert g1.version == g2.version
