"""UI smoke tests (HANDOVER 13): the Streamlit app loads, and DL1639 in Rule mode reaches an outcome.

Also covers the pure UI helpers: the view derived from log entries, the constraints registry
panel grouping, the timeline data and the sign-off path. All offline with rule agents.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from acacia import config, constraints
from acacia.agents.factory import make_agents
from acacia.coordinator import run_all
from acacia.data import DataStore
from acacia.models import Stress
from acacia.trigger import to_event
from ui import runs
from ui.constraints_panel import group_by_sheet, registry_html, status_counts
from ui.timeline import timeline_chart, timeline_data
from ui.view import build_view

APP = str(Path(__file__).resolve().parent.parent / "app.py")
DL1639_KEY = "strip-DL1639-2015-02-26-DCA-ATL"
DL1639_SEL_KEY = "strip-sel-DL1639-2015-02-26-DCA-ATL"  # preselected as the demo at rest
TIMEOUT_S = 60


@pytest.fixture(scope="module")
def data() -> DataStore:
    return DataStore()


@pytest.fixture(scope="module")
def dl1639(data: DataStore):
    return to_event({"source": "row_ref", "flight": "DL1639", "date": "2015-02-26"}, data)


@pytest.fixture(scope="module")
def entries(dl1639, data):
    return run_all(dl1639, Stress(), make_agents("rule"), data=data)


def _all_markdown(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


@pytest.fixture(autouse=True)
def _no_recordings(tmp_path, monkeypatch):
    """UI tests run against an empty recordings folder so the default agent mode is Rule and the
    rule-agent expectations hold whether or not LLM recordings are committed."""
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path / "recordings")


def test_app_loads_without_exception() -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT_S).run()
    assert not at.exception
    text = _all_markdown(at)
    assert "Constraints &amp; deviations" in text or any("Constraints & deviations" in e.label for e in at.expander)


def test_pick_dl1639_rule_mode_skip_to_end_shows_outcome() -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT_S).run()
    at.button(key=DL1639_SEL_KEY).click().run()
    assert not at.exception
    assert at.radio(key="agent_mode").value == "Rule"
    at.button(key="pb_skip").click().run()
    assert not at.exception
    text = _all_markdown(at)
    assert "AWAITING_SIGNOFF" in text
    assert "CONSENSUS" in text or "FALLBACK" in text
    assert "chain verified" in text


def test_approve_appends_signoff(entries, dl1639) -> None:
    state = runs.RunState(key=("k",), event=dl1639, mode="Rule", entries=list(entries), done=True,
                          idx=len(entries))
    record = runs.current_record(state)
    assert record is not None and runs.can_approve(record) == (record.checker_pass is True)
    assert runs.apply_signoff(state, approve=False, who="duty manager") is None
    assert state.entries[-1].type == "SignOff"
    assert runs.chain_ok(state.entries)
    assert runs.apply_signoff(state, approve=True, who="duty manager") is not None  # already signed off


def test_signoff_refused_while_running(entries, dl1639) -> None:
    state = runs.RunState(key=("k",), event=dl1639, mode="LLM", entries=list(entries[:3]), done=False, idx=3)
    assert runs.apply_signoff(state, approve=True, who="x") is not None


def test_view_from_entries(entries) -> None:
    view = build_view(entries)
    assert view.state == "AWAITING_SIGNOFF"
    assert set(view.packets) == {"ATC", "PILOT", "GROUND"}
    assert view.record is not None and view.record["outcome"] in ("CONSENSUS", "FALLBACK")
    assert view.turn <= 3
    partial = build_view(entries[:5])
    assert partial.record is None and partial.state == "DECLARING"


def test_view_shows_holding_fuel_and_overtime(entries) -> None:
    view = build_view(entries)
    for cand in view.solver["candidates"]:
        sb = cand["package"]["score_breakdown"]
        if cand["status"] != "DISCARDED":
            assert "holding_fuel_kg" in sb and "overtime_cost_min" in sb


def test_registry_grouped_by_sheet_covers_every_row() -> None:
    groups = group_by_sheet(constraints.REGISTRY)
    assert sum(len(v) for v in groups.values()) == len(constraints.REGISTRY)
    assert list(groups)[0] == "airport_capacity"
    counts = status_counts(constraints.REGISTRY)
    assert set(counts) == {"AS_GIVEN", "ASSUMED", "CHANGED", "NOT_MODELLED"}
    html = registry_html(constraints.REGISTRY)
    for item in constraints.REGISTRY:
        assert item.id in html


def test_timeline_data_and_chart(entries, dl1639) -> None:
    view = build_view(entries)
    data = timeline_data(view, dl1639)
    assert data is not None and data.lanes[0] == "Runway"
    assert {"fuel limit", "historical arrival"} <= set(data.limits["kind"])
    assert "offered slot" in set(data.marks["kind"])
    timeline_chart(data).to_dict()  # valid Vega-Lite spec
    assert timeline_data(build_view(entries[:3]), dl1639) is None


def test_default_mode_without_recordings() -> None:
    assert runs.default_mode("EVT-NOPE0000") == "Rule"
    assert runs.default_mode(None) == "Rule"


# --- airport window mode (HANDOVER 13 multi-trigger additions, scenario E/F) ------------------------
from ui import window_state as ws  # noqa: E402
from ui.impact_dot import fill_for, impact_dot  # noqa: E402

FIXTURE_CODES = {"DL201", "DL202", "DL203", "DL204", "DL205"}


@pytest.fixture(scope="module")
def fixture_store() -> DataStore:
    return ws.load_store(ws.SOURCE_FIXTURE)


@pytest.fixture(scope="module")
def window_run(fixture_store) -> ws.WindowRun:
    run = ws.prepare(fixture_store, ws.SOURCE_FIXTURE, "Rule", Stress(),
                     scan=(ws.FIXTURE_AIRPORT, ws.FIXTURE_DATE, ws.DEFAULT_WINDOW))
    return ws.start(run)


def test_default_source_is_synthetic_fixture_without_flights_csv() -> None:
    if ws.has_full_flights():
        pytest.skip("data/flights.csv present")
    assert ws.default_source() == ws.SOURCE_FIXTURE
    assert ws.default_date(ws.SOURCE_FIXTURE) == ws.FIXTURE_DATE
    assert "synthetic" in ws.source_label(ws.SOURCE_FIXTURE, ws.load_store(ws.SOURCE_FIXTURE))


def test_parse_payloads_shapes() -> None:
    items, scan = ws.parse_payloads(ws.example_payloads(ws.SOURCE_FIXTURE))
    assert scan is None and len(items) == 5
    items, scan = ws.parse_payloads('{"source": "scan", "airport": "atl", "date": "2015-03-12", "window_min": 90}')
    assert items == [] and scan == ("ATL", "2015-03-12", 90)
    items, _ = ws.parse_payloads('{"source": "batch", "triggers": [{"flight": "DL201", "date": "2015-03-12"}]}')
    assert items == [{"flight": "DL201", "date": "2015-03-12"}]
    for bad in ("not json", "[1, 2]", "[]", "3"):
        with pytest.raises(ws.WindowInputError):
            ws.parse_payloads(bad)


def test_window_run_drains_fixture_queue(window_run) -> None:
    assert window_run.done and window_run.error is None and window_run.synthetic
    q = ws.queue(window_run)
    assert q is not None and {it.flight_no for it in q.items} == FIXTURE_CODES
    assert [it.rank for it in q.items] == [1, 2, 3, 4, 5]
    assert all(it.status in ("AWAITING_SIGNOFF", "REJECTED") for it in q.items)
    orch = ws.orchestrator_entries(window_run)
    assert ws.chain_ok(orch) and orch[-1].type == "SUMMARY" and ws.summary(window_run) is not None
    for it in q.items:
        seg = ws.event_entries(window_run, it.event_id)
        assert seg and ws.chain_ok(seg)


def test_impact_parts_sum_to_score_and_capacity_line(window_run) -> None:
    d = window_run.dispatcher
    for eid, a in d.assessments.items():
        parts = ws.impact_parts(a)
        assert [p[0] for p in parts] == list(config.IMPACT_POLICY)
        assert sum(p[3] for p in parts) == pytest.approx(a.impact_score)
        line = ws.capacity_line(d.events[eid])
        assert line.startswith("ATL 90 mvts/h · 45 arr/h")
        storm = ws.storm_of(d.events[eid])
        assert (f"×{constraints.STORM_FACTOR[storm]:g}" in line)


def test_impact_dot_rotation_solid_connection_dashed(window_run) -> None:
    from acacia import triage

    d = window_run.dispatcher
    eid = next(e for e, ev in d.events.items() if ev.flight.code == "DL201")
    a, node = d.assessments[eid], triage.trigger_node(d.events[eid], d.graph)
    dot = impact_dot(a, node, d.graph, "DL201")
    assert dot.startswith("digraph") and "rankdir=LR" in dot and "penwidth=3" in dot
    rot = [x for x in a.downstream if x.edge_type == "ROTATION"]
    assert rot and f"slack {rot[0].slack_min} · +{rot[0].propagated_delay_min}" in dot
    for c in (x for x in a.downstream if x.edge_type == "CONNECTION"):
        assert f"{c.pax_at_risk} pax" in dot and "style=dashed" in dot
    assert dot == impact_dot(a, node, d.graph, "DL201")  # deterministic
    assert fill_for(0) != fill_for(45) and fill_for(61) == fill_for(500)
    assert "not in graph" in impact_dot(a, None, None)


def test_ledger_rows_and_window_signoff(fixture_store) -> None:
    run = ws.start(ws.prepare(fixture_store, ws.SOURCE_FIXTURE, "Rule", Stress(),
                              scan=(ws.FIXTURE_AIRPORT, ws.FIXTURE_DATE, ws.DEFAULT_WINDOW)))
    rows = ws.ledger_rows(run)
    assert rows and {r["status"] for r in rows} == {"HELD"}
    held = rows[0]["event_id"]
    assert ws.sign_off(run, held, approve=False, who="") is not None  # who is required
    assert ws.sign_off(run, held, approve=False, who="duty manager") is None
    assert {r["status"] for r in ws.ledger_rows(run) if r["event_id"] == held} == {"RELEASED"}
    assert run.dispatcher.status[held] == "REJECTED"
    assert ws.sign_off(run, held, approve=True, who="duty manager") is not None  # already decided
    assert ws.chain_ok(ws.orchestrator_entries(run))


def test_window_scenario_f_preempts(fixture_store) -> None:
    run = ws.start(ws.prepare(fixture_store, ws.SOURCE_FIXTURE, "Rule", Stress(), preempt_lowest=True,
                              scan=(ws.FIXTURE_AIRPORT, ws.FIXTURE_DATE, ws.DEFAULT_WINDOW)))
    assert run.error is None
    assert any(e.type == "PREEMPT" for e in run.entries)
    assert ws.queue(run).items[0].tier == 0


def test_window_payload_list_and_empty_scan(fixture_store) -> None:
    items, _ = ws.parse_payloads(ws.example_payloads(ws.SOURCE_FIXTURE))
    run = ws.start(ws.prepare(fixture_store, ws.SOURCE_FIXTURE, "Rule", Stress(), payloads=items[:2]))
    assert {it.flight_no for it in ws.queue(run).items} == {"DL201", "DL202"}
    with pytest.raises(ws.WindowInputError):
        ws.prepare(fixture_store, ws.SOURCE_FIXTURE, "Rule", Stress(), scan=("LAX", ws.FIXTURE_DATE, 30))


def _window_app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT_S).run()
    at.radio(key="view_mode").set_value("Airport window").run()
    assert not at.exception
    return at


def test_app_window_mode_loads_and_runs_fixture_queue() -> None:
    at = _window_app()
    assert "synthetic" in _all_markdown(at)
    at.button(key="win_run").click().run()
    assert not at.exception
    text = _all_markdown(at)
    for code in FIXTURE_CODES:
        assert code in text
    assert "SAFETY" in text or "NORMAL" in text
    assert "mvts/h" in text and "×" in text  # each trigger's airport capacity and storm factor
    assert "Orchestrator" in text and "DISPATCH" in text
    assert at.get("graphviz_chart")
    assert any("Booking ledger" in e.label for e in at.expander)


def test_app_window_open_row_and_dispatch_link() -> None:
    at = _window_app()
    at.button(key="win_run").click().run()
    row = next(b for b in at.button if b.key and b.key.startswith("wq-"))
    row.click().run()
    assert not at.exception
    text = _all_markdown(at)
    assert "AWAITING_SIGNOFF" in text or "CONSENSUS" in text or "FALLBACK" in text
    opened = row.key.removeprefix("wq-")
    assert at.selectbox(key="win_thread").value == opened  # the row opened that event's thread
    at.selectbox(key="win_thread").set_value("orchestrator").run()
    link = next(b for b in at.button if b.key and b.key.startswith("win-open-"))
    link.click().run()
    assert not at.exception
    assert at.selectbox(key="win_thread").value != "orchestrator"  # DISPATCH links into the event thread


# --- QA fixes: ready demo, verdict banner, playback on pick, auto-scroll, compact constraints -----------
from ui import autoscroll  # noqa: E402
from ui.timeline import COLOURS, LEGEND_LABEL_LIMIT_PX  # noqa: E402
from ui.verdict import verdict_html  # noqa: E402


def _expander(at: AppTest, label: str):
    return next(e for e in at.expander if label in e.label)


def test_default_demo_shows_verdict_and_outcome_at_rest() -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT_S).run()
    assert not at.exception
    assert at.radio(key="agent_mode").value == runs.default_mode(at.session_state["selected_event"])
    assert at.button(key=DL1639_SEL_KEY)  # DL1639 is the preselected demo
    text = _all_markdown(at)
    assert 'class="verdict"' in text and "land ATL" in text and 'href="#ac-outcome"' in text
    assert 'id="ac-outcome"' in text and "AWAITING_SIGNOFF" in text
    assert "CONSENSUS" in text or "FALLBACK" in text
    assert "16 of 16 entries" in text and "Code decides" in text
    demo = at.session_state["runs"][at.session_state["selected_event"]]
    assert demo.at_end and not demo.playing  # played to the end: no timer at rest


def test_constraints_collapsed_with_chip_summary_visible() -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT_S).run()
    assert _expander(at, "Constraints & deviations").proto.expanded is False
    assert "ac-cons-sum" in _all_markdown(at) and "CHANGED" in _all_markdown(at)


def test_picking_another_flight_starts_playback_timer() -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT_S).run()
    other = next(b for b in at.button if b.key and b.key.startswith("strip-") and not b.key.startswith("strip-sel-"))
    other.click().run()
    assert not at.exception
    state = at.session_state["runs"][at.session_state["selected_event"]]
    assert state.event.flight.code in other.key and state.playing and state.idx >= 1
    assert at.session_state["_ticking"]  # run_every is set on the first rerun after the pick (no stall)
    assert "Negotiating" in _all_markdown(at)


def test_window_mode_runs_and_shows_verdict_for_opened_event() -> None:
    at = _window_app()
    at.button(key="win_run").click().run()
    assert not at.exception
    next(b for b in at.button if b.key and b.key.startswith("wq-")).click().run()
    assert not at.exception
    assert 'class="verdict"' in _all_markdown(at)


def test_verdict_html_only_for_finished_runs(entries) -> None:
    assert verdict_html(entries[:5]) is None
    html = verdict_html(entries)
    assert html is not None and "#ac-outcome" in html and "turns" in html
    assert "kg fuel" in html or "divert" in html


def test_autoscroll_script_targets_anchor_and_token() -> None:
    assert 'id="ac-scroll-channel"' in autoscroll.anchor_html("channel")
    js = autoscroll.script("channel", "EVT-1:3")
    assert '"ac-scroll-channel"' in js and '"EVT-1:3"' in js and "scrollHeight" in js
    assert "catch (e) { return; }" in js  # degrades when the parent page is unreachable
    assert autoscroll.script("a b", "t") == autoscroll.script("a-b", "t")


def test_timeline_legend_labels_fit() -> None:
    assert "turnaround (under vote)" in COLOURS
    assert LEGEND_LABEL_LIMIT_PX >= 200


def test_default_demo_shows_the_arrival_runway() -> None:
    """CAP-9: the plan's runway is shown in the verdict banner, the outcome rows and the solver table."""
    at = AppTest.from_file(APP, default_timeout=TIMEOUT_S).run()
    assert not at.exception
    text = _all_markdown(at)
    assert re.search(r"land ATL \d\d:\d\d on R[12]", text), "flight plan names the runway"
    assert "<th>Runway</th>" in text
    assert re.search(r"<dt>Runway</dt><dd>R[12]</dd>", text), "outcome has a Runway row"
