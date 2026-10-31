"""UI smoke tests (HANDOVER 13): the Streamlit app loads, and DL1639 in Rule mode reaches an outcome.

Also covers the pure UI helpers: the view derived from log entries, the constraints registry
panel grouping, the timeline data and the sign-off path. All offline with rule agents.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from acacia import constraints
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


def test_app_loads_without_exception() -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT_S).run()
    assert not at.exception
    text = _all_markdown(at)
    assert "Constraints &amp; deviations" in text or any("Constraints & deviations" in e.label for e in at.expander)


def test_pick_dl1639_rule_mode_skip_to_end_shows_outcome() -> None:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT_S).run()
    at.button(key=DL1639_KEY).click().run()
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
