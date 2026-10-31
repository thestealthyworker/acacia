"""Acacia Streamlit UI (HANDOVER 13, PLAN 3.7): single-flight negotiation dashboard + agent channel.

    streamlit run app.py

Left column: verdict banner (or a how-it-works note while a run plays), disrupted flights,
constraints & deviations (chips visible, table collapsed), event card, protocol, declared
constraints, solver packages, timeline, outcome and sign-off. Right column: the agent channel,
which follows its newest message. Both render from the same visible slice of one log entry
list held in st.session_state, keyed by event_id. Playback advances that slice from a timed
st.fragment. A new session opens on a ready demo: DL1639 played to the end. On phones the
theme CSS reorders the panels: verdict and flights, channel, dashboard, then constraints.
"""

from __future__ import annotations

import logging

import streamlit as st

from acacia.data import DataStore
from acacia.models import DisruptionEvent, Stress
from acacia.trigger import TriggerError, to_event
from ui import runs
from ui.channel import FULL_RERUN, SPEED_KEY, channel, request_full_rerun
from ui.constraints_panel import constraints_panel
from ui.dashboard import event_panels
from ui.theme import CSS, esc
from ui.verdict import verdict_html
from ui.window import render_window

logger = logging.getLogger(__name__)

LARGE_TABLE_ROWS = 200_000  # above this, the flight list needs a date (full flights.csv)
DEFAULT_DATE = "2015-02-26"
DEMO_FLIGHT, DEMO_DATE, DEMO_KEY = "DL1639", "2015-02-26", "_demo_seeded"  # the ready demo at rest
MAX_STRIPS = 40
RUNS_KEY, SELECTED_KEY, MODE_KEY = "runs", "selected_event", "agent_mode"
STRESS_KEYS = (("low_fuel", "Low fuel"), ("tight_crew", "Tight crew"), ("corrupt_solver", "Corrupt solver"))


# --- data -------------------------------------------------------------------------------------
@st.cache_resource(show_spinner="Loading flights…")
def load_data() -> DataStore:
    return DataStore()


@st.cache_resource(show_spinner="Finding disrupted flights…")
def qualifying_events(date: str | None) -> dict[str, DisruptionEvent]:
    """event_id -> DisruptionEvent for every qualifying row (read-only, shared across sessions)."""
    data = load_data()
    out: dict[str, DisruptionEvent] = {}
    for _, row in data.qualifying_rows(date=date).iterrows():
        try:
            event = to_event({"source": "row_ref", "row": row.to_dict()}, data)
            out[event.event_id] = event
        except (TriggerError, ValueError, LookupError) as exc:
            logger.warning("skipped row %s%s: %s", row.get("AIRLINE"), row.get("FLIGHT_NUMBER"), exc)
    return out


def is_large() -> bool:
    return len(load_data().flights) > LARGE_TABLE_ROWS


def strip_key(e: DisruptionEvent, selected: bool) -> str:
    f = e.flight
    return f"strip-{'sel-' if selected else ''}{f.code}-{f.date}-{f.origin}-{f.dest}"


# --- header -----------------------------------------------------------------------------------
def current_stress() -> Stress:
    return Stress(**{k: bool(st.session_state.get(f"stress_{k}", False)) for k, _ in STRESS_KEYS})


def header() -> str:
    st.markdown(CSS, unsafe_allow_html=True)
    if MODE_KEY not in st.session_state:
        st.session_state[MODE_KEY] = runs.default_mode(st.session_state.get(SELECTED_KEY))
    a, b, c = st.columns([1.4, 1.6, 1.3], vertical_alignment="center")
    with a:
        st.markdown('<p class="ac-brand">Acacia · disruption negotiation</p><span class="ac-sub">ATC, Pilot and '
                    'Ground agents argue; code decides; a human signs off.</span>', unsafe_allow_html=True)
    with b, st.container(key="ac-stress"):  # one row on phones (theme CSS)
        cols = st.columns(len(STRESS_KEYS))
        for col, (k, label) in zip(cols, STRESS_KEYS):
            col.toggle(label, key=f"stress_{k}", on_change=request_full_rerun)
    with c:
        st.radio("Agents", runs.MODES, key=MODE_KEY, horizontal=True, on_change=request_full_rerun,
                 help="Replay serves recorded LLM answers offline; Rule is the deterministic stand-in.")
        view_mode = st.radio("View", ("Single flight", "Airport window"), key="view_mode", horizontal=True,
                             label_visibility="collapsed")
    return view_mode


# --- run state --------------------------------------------------------------------------------
def _pick(event_id: str) -> None:
    st.session_state[SELECTED_KEY] = event_id
    st.session_state[MODE_KEY] = runs.default_mode(event_id)
    request_full_rerun()


def active_run(events: dict[str, DisruptionEvent]) -> runs.RunState | None:
    event = events.get(st.session_state.get(SELECTED_KEY, ""))
    if event is None:
        return None
    mode, stress = st.session_state.get(MODE_KEY, "Rule"), current_stress()
    store: dict[str, runs.RunState] = st.session_state.setdefault(RUNS_KEY, {})
    state = store.get(event.event_id)
    if state is None or state.key != runs.run_key(event, mode, stress):
        state = runs.start_run(event, mode, stress, load_data())
        store[event.event_id] = state
    return state


def demo_event_id(events: dict[str, DisruptionEvent]) -> str | None:
    return next((e.event_id for e in events.values()
                 if e.flight.code == DEMO_FLIGHT and e.flight.date == DEMO_DATE), None)


def seed_demo(events: dict[str, DisruptionEvent]) -> None:
    """Once per session: preselect DL1639 (default agent mode) and play it to the end (QA fix 5)."""
    s = st.session_state
    if s.get(DEMO_KEY):
        return
    s[DEMO_KEY] = True
    event_id = demo_event_id(events)
    if s.get(SELECTED_KEY) or event_id is None:
        return
    s[SELECTED_KEY] = event_id
    s.setdefault(MODE_KEY, runs.default_mode(event_id))
    state = active_run(events)
    if state is not None:
        runs.skip_to_end(state)


def tick_interval(events: dict[str, DisruptionEvent]) -> float | None:
    state = st.session_state.get(RUNS_KEY, {}).get(st.session_state.get(SELECTED_KEY, ""))
    if state is None or not state.playing or state.at_end:
        return None
    return runs.SPEEDS[st.session_state.get(SPEED_KEY, runs.DEFAULT_SPEED_IDX)]


# --- left column ------------------------------------------------------------------------------
def _matches(e: DisruptionEvent, q: str) -> bool:
    f = e.flight
    hay = f"{f.code} {f.origin} {f.dest} {f.date} {e.scenario_type.value}".lower()
    return all(part in hay for part in q.lower().split())


def flight_list(events: dict[str, DisruptionEvent]) -> None:
    with st.container(border=True):
        if is_large():
            st.text_input("Date", key="flight_date", value=DEFAULT_DATE, on_change=request_full_rerun,
                          help="Full flights.csv: qualifying rows are listed one date at a time.")
        q = st.text_input("Search", key="flight_search", placeholder="flight, airport or date",
                          label_visibility="collapsed")
        hits = [e for e in events.values() if _matches(e, q or "")]
        st.markdown(f'<div class="ac-h">Disrupted flights <span class="ac-mono">{len(hits)} qualifying'
                    f'{f" (showing {MAX_STRIPS})" if len(hits) > MAX_STRIPS else ""}</span></div>'
                    '<div class="ac-sub">Pick a flight to start a new negotiation.</div>', unsafe_allow_html=True)
        selected = st.session_state.get(SELECTED_KEY)
        with st.container(height=230, border=False):
            if not hits:
                st.caption("No qualifying flight matches.")
            for e in hits[:MAX_STRIPS]:
                f = e.flight
                label = (f"{f.code}  {f.origin}→{f.dest}  {f.date}  sched {e.sched_arrival} · actual "
                         f"{e.hist_arrival}  +{e.hist_delay_min} min  {e.scenario_type.value}")
                st.button(label, key=strip_key(e, e.event_id == selected), width="stretch",
                          on_click=_pick, args=(e.event_id,))


def _signoff(state: runs.RunState, approve: bool, who_key: str) -> None:
    error = runs.apply_signoff(state, approve, st.session_state.get(who_key, ""))
    if error:
        st.session_state["signoff_error"] = error


def signoff_controls(state: runs.RunState) -> None:
    record = runs.current_record(state)
    if record is None:
        return
    who_key = f"who_{state.event.event_id}"
    a, b, c = st.columns([1.6, 1, 1], vertical_alignment="bottom")
    a.text_input("Signed off by", value="Duty manager", key=who_key)
    pending = record.status == "AWAITING_SIGNOFF" and state.at_end
    b.button("Approve", key="signoff_approve", type="primary", width="stretch", on_click=_signoff,
             args=(state, True, who_key), disabled=not (pending and runs.can_approve(record)),
             help="Only a checker-passed plan (or a divert) can be approved.")
    c.button("Reject", key="signoff_reject", width="stretch", on_click=_signoff, args=(state, False, who_key),
             disabled=not pending)
    if st.session_state.get("signoff_error"):
        st.error(st.session_state.pop("signoff_error"))


def dashboard(state: runs.RunState) -> None:
    event_panels(state.visible, state.event)
    signoff_controls(state)


HOW_IT_WORKS = ('<ol class="ac-how"><li>ATC, Pilot and Ground agents argue over a landing slot and a gate.</li>'
                '<li>Code decides: the solver proposes, the checker verifies; three turns at most.</li>'
                '<li>A human signs off before anything executes.</li></ol>')


def status_panel(state: runs.RunState | None) -> None:
    """Top of the dashboard: the verdict once the run ends (QA fix 3), else its progress."""
    banner = verdict_html(state.visible) if state is not None and state.at_end else None
    if banner:
        flight = state.event.flight
        st.markdown(f'<div class="ac-h">Verdict <span class="ac-mono">{esc(flight.code)} {esc(flight.origin)}→'
                    f'{esc(flight.dest)} {esc(flight.date)} · {esc(state.mode)} agents</span></div>{banner}',
                    unsafe_allow_html=True)
    elif state is not None:
        st.markdown(f'<div class="ac-progress">Negotiating {esc(state.event.flight.code)} · entry {state.idx} of '
                    f'{len(state.entries)}{"" if state.done else "+"} — the verdict appears here when the run '
                    'ends.</div>', unsafe_allow_html=True)
    st.markdown(HOW_IT_WORKS, unsafe_allow_html=True)


# --- body -------------------------------------------------------------------------------------
def single_flight_body(events: dict[str, DisruptionEvent]) -> None:
    if st.session_state.pop(FULL_RERUN, False):
        st.rerun(scope="app")
    state = active_run(events)
    if state is not None and state.playing:
        runs.step(state)
    with st.container(key="ac-body"):
        left, right = st.columns([1.45, 1], gap="medium")
        with left:
            with st.container(key="ac-l-top"):
                status_panel(state)
                flight_list(events)
            with st.container(key="ac-l-cons"):
                constraints_panel()
            with st.container(key="ac-l-dash"):
                if state is None:
                    st.info("Pick a disrupted flight to start a negotiation.")
                else:
                    dashboard(state)
        with right, st.container(key="ac-r-chan"):
            channel(state)
    if state is not None and state.at_end and tick_interval(events) is None and st.session_state.pop("_ticking", False):
        st.rerun(scope="app")  # stop the timer once playback has finished


def render_window_mode() -> None:
    """Airport window mode (HANDOVER 13 multi-trigger additions, FABLE_ADDENDUM 8): ui/window.py.

    Uses the header's agent mode and stress toggles; the synthetic fixture day when flights.csv
    is absent. Playback is not timed here: the dispatcher drains the whole queue at once.
    """
    st.session_state.pop(FULL_RERUN, None)
    render_window(st.session_state.get(MODE_KEY, "Rule"), current_stress())


def main() -> None:
    st.set_page_config(page_title="Acacia", page_icon=":material/flight_land:", layout="wide")
    single = st.session_state.get("view_mode", "Single flight") == "Single flight"
    date = st.session_state.get("flight_date", DEFAULT_DATE) if single and is_large() else None
    events = qualifying_events(date) if single else {}
    if single:
        seed_demo(events)  # before header(), which shows the seeded agent mode
    view_mode = header()
    if view_mode == "Airport window":
        render_window_mode()
        return
    if not single:  # switched to Single flight in this run
        events = qualifying_events(st.session_state.get("flight_date", DEFAULT_DATE) if is_large() else None)
    active_run(events)  # QA fix 1: create a newly picked flight's run before the timer interval is read
    interval = tick_interval(events)
    st.session_state["_ticking"] = interval is not None
    st.fragment(single_flight_body, run_every=interval)(events)


main()
