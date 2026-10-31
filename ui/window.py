"""Airport window mode (HANDOVER 13 multi-trigger additions, FABLE_ADDENDUM 8).

Left: constraints & deviations, window controls (airport, date, window length, or a trigger
payload list), trigger queue (rank, flight, tier chip, impact bar by component, cluster,
deadline, status, and the trigger airport's workbook capacity and storm factor), impact graph,
booking ledger, and the selected event's dashboard with sign-off through the dispatcher.
Right: the orchestrator thread or one event's negotiation thread (ui/window_thread.py).
"""

from __future__ import annotations

import time

import streamlit as st

from acacia import triage
from acacia.models import QueueItem, Stress
from ui import runs
from ui import window_state as ws
from ui.constraints_panel import constraints_panel
from ui.dashboard import event_panels
from ui.impact_dot import DELAY_FILLS, impact_dot
from ui.theme import chip, esc, heading
from ui.window_thread import ORCH, THREAD_KEY, thread_panel

RUN_KEY, SELECTED_KEY, ERROR_KEY, SIGN_ERROR_KEY = "win_run_state", "win_selected", "win_error", "win_sign_error"
SCAN, PAYLOADS = "Scan airport window", "Trigger payload list"
POLL_S = 1.0  # LLM window runs drain in a thread; the page re-polls while it runs
STATUS_KIND = {"QUEUED": "muted", "NEGOTIATING": "info", "AWAITING_SIGNOFF": "warn", "APPROVED": "good",
               "REJECTED": "bad", "PREEMPTED": "bad"}


def _html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


@st.cache_resource(show_spinner="Loading flights…")
def _store(source: str):
    return ws.load_store(source)


# --- controls ---------------------------------------------------------------------------------
def _select(event_id: str) -> None:
    st.session_state[SELECTED_KEY] = event_id


def _open(event_id: str) -> None:
    """A queue row opens that event's dashboard and its negotiation thread."""
    _select(event_id)
    st.session_state[THREAD_KEY] = event_id


def _start(mode: str, stress: Stress) -> None:
    """Run button callback: read the controls, collect, drain; errors are shown, never raised."""
    s = st.session_state
    source = s.get("win_source", ws.default_source())
    st.session_state.pop(ERROR_KEY, None)
    try:
        scan, payloads = None, None
        if s.get("win_input", SCAN) == SCAN:
            scan = (str(s.get("win_airport", ws.FIXTURE_AIRPORT)).strip().upper(),
                    str(s.get(f"win_date_{source}", ws.default_date(source))).strip(),
                    int(s.get("win_window", ws.DEFAULT_WINDOW)))
        else:
            upload = s.get("win_upload")
            text = upload.getvalue().decode("utf-8") if upload is not None else s.get(f"win_payloads_{source}", "")
            payloads, scan = ws.parse_payloads(text)
        run = ws.prepare(_store(source), source, mode, stress, scan=scan, payloads=payloads or None,
                         preempt_lowest=bool(s.get("win_preempt", False)))
    except (ws.WindowInputError, UnicodeDecodeError, OSError, ValueError) as exc:
        st.session_state[ERROR_KEY] = str(exc)
        return
    ws.start(run)
    s[RUN_KEY] = run
    s[THREAD_KEY] = ORCH  # the channel opens on the orchestrator's own messages
    q = ws.queue(run)
    s[SELECTED_KEY] = q.items[0].event_id if q and q.items else None


def controls(mode: str, stress: Stress) -> None:
    with st.container(border=True):
        default = ws.default_source()
        options = (ws.SOURCE_FIXTURE, ws.SOURCE_DATA)
        source = st.radio("Data", options, index=options.index(default), key="win_source", horizontal=True,
                          format_func=lambda o: "Synthetic fixture day" if o == ws.SOURCE_FIXTURE else "Flights data",
                          help="The synthetic day (tests/fixtures/multi_trigger_day.csv) is used until "
                               "data/flights.csv is added.")
        synthetic = chip("synthetic", "sim") if source == ws.SOURCE_FIXTURE else ""
        _html(heading("Airport window", synthetic) + '<div class="ac-sub">'
              + esc(ws.source_label(source, _store(source))) + "</div>")
        how = st.radio("Triggers", (SCAN, PAYLOADS), key="win_input", horizontal=True, label_visibility="collapsed")
        if how == SCAN:
            a, b, c = st.columns(3)
            a.text_input("Airport", value=ws.FIXTURE_AIRPORT, key="win_airport")
            b.text_input("Date", value=ws.default_date(source), key=f"win_date_{source}")
            c.number_input("Window (min)", min_value=30, max_value=720, step=30, value=ws.DEFAULT_WINDOW,
                           key="win_window")
        else:
            st.text_area("Trigger payloads (JSON list, batch or scan object)", value=ws.example_payloads(source),
                         key=f"win_payloads_{source}", height=140)
            st.file_uploader("…or load a payload file", type=["json"], key="win_upload")
        a, b = st.columns([2.2, 1], vertical_alignment="center")
        a.checkbox("Scenario F: low fuel on the lowest-ranked trigger after the first dispatch", key="win_preempt")
        b.button("Run window", key="win_run", type="primary", width="stretch", on_click=_start, args=(mode, stress),
                 icon=":material/play_arrow:", help=f"{mode} agents; header stress toggles apply to every trigger")
        if st.session_state.get(ERROR_KEY):
            st.error(st.session_state[ERROR_KEY])


# --- queue ------------------------------------------------------------------------------------
def _impact_bar(run: ws.WindowRun, item: QueueItem, top: float) -> str:
    a = run.dispatcher.assessments[item.event_id]
    colours = {k: c for k, _, c in ws.IMPACT_PARTS}
    spans = "".join(f'<span style="width:{100 * w / top:.1f}%;background:{colours[k]}" '
                    f'title="{esc(label)} {raw:g} → {w:g}"></span>'
                    for k, label, raw, w in ws.impact_parts(a) if w > 0)
    return f'<div class="bar qbar">{spans}</div>'


def _row_html(run: ws.WindowRun, item: QueueItem, top: float) -> str:
    d = run.dispatcher
    event, a = d.events[item.event_id], d.assessments[item.event_id]
    tier = chip("SAFETY", "bad") if item.tier == 0 else chip("NORMAL", "muted")
    outcome = d.outcomes.get(item.event_id, "")
    parts = " · ".join(f"{label} {raw:g}" for _, label, raw, _ in ws.impact_parts(a))
    return (f'<div class="qrow"><span class="qrank">{item.rank}</span><b class="ac-mono">{esc(item.flight_no)}</b> '
            f'<span class="ac-sub">{esc(event.flight.origin)}→{esc(event.flight.dest)}</span> {tier}'
            f'<span title="{esc(a.tier_reason)}">{_impact_bar(run, item, top)}</span>'
            f'<span class="ac-mono">{item.impact_score:g}</span> {chip(item.cluster_id, "info")}'
            f'<span class="ac-mono">due {esc(item.decision_deadline)}</span> '
            f'{chip(item.status, STATUS_KIND.get(item.status, ""))}{chip(outcome, "muted") if outcome else ""}</div>'
            f'<div class="ac-sub ac-mono qcap">{esc(ws.capacity_line(event))} · {esc(parts)}</div>')


def _legend() -> str:
    return '<div class="legend">' + "".join(f'<span><i style="background:{c}"></i>{esc(lbl)}</span>'
                                            for _, lbl, c in ws.IMPACT_PARTS) + "</div>"


def _summary_chips(run: ws.WindowRun) -> str:
    p = ws.summary(run)
    if not p:
        return chip("running", "info") if run.running else ""
    k = p["knock_on_min"]
    return (chip(f"consensus rate {p['consensus_rate']:g}", "good") + chip(f"fallbacks {p['fallbacks']}", "warn")
            + chip(f"diverts {p['diverts']}", "muted") + chip(f"pre-empted {p['preempted']}", "muted")
            + chip(f"knock-on {k['do_nothing']:g} → {k['after_plans']:g} min", "info"))


def trigger_queue(run: ws.WindowRun) -> None:
    q = ws.queue(run)
    with st.container(border=True):
        sim = chip("synthetic", "sim") if run.synthetic else ""
        _html(heading("Trigger queue", sim + chip(f"{run.mode} agents", "info")))
        if q is None:
            st.caption("Triage has not finished yet.")
            return
        _html(f'<div class="ac-sub ac-mono">{esc(q.queue_id)} · {esc(q.airport or "multi-airport")} {esc(q.date)} '
              f'{esc(q.window)} · {len(q.items)} trigger(s) · ranked by tier, then impact</div>'
              f"<div>{_summary_chips(run)}</div>{_legend()}")
        top = max((it.impact_score for it in q.items), default=1.0) or 1.0
        selected = st.session_state.get(SELECTED_KEY)
        for it in q.items:
            with st.container(key=f"qrow-{'sel-' if it.event_id == selected else ''}{it.event_id}"):
                a, b = st.columns([6, 1], vertical_alignment="center")
                a.markdown(_row_html(run, it, top), unsafe_allow_html=True)
                b.button("Open", key=f"wq-{it.event_id}", on_click=_open, args=(it.event_id,), width="stretch",
                         help="Open this event's dashboard and negotiation thread")


# --- graph and ledger -------------------------------------------------------------------------
def impact_graph(run: ws.WindowRun, event_id: str) -> None:
    d = run.dispatcher
    a, event = d.assessments.get(event_id), d.events[event_id]
    with st.container(border=True):
        _html(heading("Impact graph", chip(event.flight.code, "info") + (chip("synthetic", "sim") if run.synthetic else "")))
        if a is None or d.graph is None:
            st.caption("Appears after triage.")
            return
        node = triage.trigger_node(event, d.graph)
        st.graphviz_chart(impact_dot(a, node, d.graph, event.flight.code), width="stretch")
        fills = "".join(f'<span><i style="background:{c}"></i>{esc(lbl)}</span>' for _, c, lbl in DELAY_FILLS)
        _html(f'<div class="legend">propagated delay {fills} · solid = rotation (slack · delay) · dashed = '
              f'connection (pax) · outlined = trigger</div><div class="ac-sub">Tier {a.tier}: {esc(a.tier_reason)}</div>')


def booking_ledger(run: ws.WindowRun) -> None:
    rows = ws.ledger_rows(run)
    with st.expander(f"Booking ledger · {len(rows)} reservation(s)", expanded=False):
        if not rows:
            st.caption("No holds yet. Holds are made when an event ends COMMITTED or in a non-divert FALLBACK.")
        else:
            st.dataframe(rows, hide_index=True, column_order=("flight", "type", "airport", "resource", "start",
                                                              "end", "status", "res_id"))


# --- event ------------------------------------------------------------------------------------
def _sign(run: ws.WindowRun, event_id: str, approve: bool, who_key: str) -> None:
    error = ws.sign_off(run, event_id, approve, st.session_state.get(who_key, ""))
    if error:
        st.session_state[SIGN_ERROR_KEY] = error


def window_signoff(run: ws.WindowRun, event_id: str) -> None:
    d = run.dispatcher
    record = d.records.get(event_id)
    if record is None:
        return
    pending = run.done and d.status.get(event_id) == "AWAITING_SIGNOFF"
    who_key = f"win_who_{event_id}"
    a, b, c = st.columns([1.6, 1, 1], vertical_alignment="bottom")
    a.text_input("Signed off by", value="Duty manager", key=who_key)
    b.button("Approve", key="win_approve", type="primary", width="stretch", on_click=_sign,
             args=(run, event_id, True, who_key), disabled=not (pending and runs.can_approve(record)),
             help="Commits this event's ledger holds. Only a checker-passed plan (or a divert) can be approved.")
    c.button("Reject", key="win_reject", width="stretch", on_click=_sign, args=(run, event_id, False, who_key),
             disabled=not pending, help="Releases this event's ledger holds for the next event.")
    if st.session_state.get(SIGN_ERROR_KEY):
        st.error(st.session_state.pop(SIGN_ERROR_KEY))


def selected_event(run: ws.WindowRun, event_id: str) -> None:
    entries = ws.event_entries(run, event_id)
    attempts = len(run.dispatcher.log_segments(event_id))
    if attempts > 1:
        st.caption(f"Attempt {attempts}: {attempts - 1} earlier attempt(s) were pre-empted; their logs are kept "
                   "in the event thread.")
    event_panels(entries, run.dispatcher.events[event_id])
    window_signoff(run, event_id)


# --- page -------------------------------------------------------------------------------------
def _left(run: ws.WindowRun | None, mode: str, stress: Stress) -> None:
    constraints_panel()
    controls(mode, stress)
    if run is None:
        st.info("Choose an airport window (or a payload list) and press Run window. The dispatcher triages, "
                "clusters and negotiates every trigger in rank order.")
        return
    if run.error:
        st.error(run.error)
    trigger_queue(run)
    eid = st.session_state.get(SELECTED_KEY)
    if eid not in run.dispatcher.events:
        return
    impact_graph(run, eid)
    booking_ledger(run)
    selected_event(run, eid)


def render_window(mode: str, stress: Stress) -> None:
    run: ws.WindowRun | None = st.session_state.get(RUN_KEY)
    left, right = st.columns([1.45, 1], gap="medium")
    with left:
        _left(run, mode, stress)
    with right:
        thread_panel(run, _select)
    if run is not None and run.running:
        time.sleep(POLL_S)
        st.rerun()
