"""Right panel in Airport window mode (HANDOVER 13): orchestrator thread and event threads.

The channel opens on the dispatcher's own messages (TRIAGE, ImpactAssessment, TriggerQueue,
CLUSTER, DISPATCH, LEDGER, PREEMPT, SUMMARY) under the sender "Orchestrator", with its own
colour and filter chip. Each DISPATCH message links into that event's negotiation thread; a
pre-empted event shows every attempt, each its own verified hash chain.
"""

from __future__ import annotations

from collections.abc import Callable

import streamlit as st

from acacia.models import LogEntry
from ui import window_state as ws
from ui.channel import CHANNEL_HEIGHT, render_entry
from ui.theme import SINGLE_FLIGHT_SENDERS, chip, esc, sender_label

ORCH = "orchestrator"
THREAD_KEY, FILTER_KEY = "win_thread", "win_sender_filter"
WINDOW_SENDERS = (ORCH, *SINGLE_FLIGHT_SENDERS)


def _open_thread(event_id: str, select: Callable[[str], None]) -> None:
    st.session_state[THREAD_KEY] = event_id
    select(event_id)


def _thread_label(run: ws.WindowRun, key: str) -> str:
    if key == ORCH:
        return "Orchestrator thread"
    event = run.dispatcher.events.get(key)
    return f"{event.flight.code} negotiation" if event else key


def _thread_options(run: ws.WindowRun) -> list[str]:
    q = ws.queue(run)
    order = [it.event_id for it in q.items] if q else sorted(run.dispatcher.events)
    return [ORCH, *order]


def _header(run: ws.WindowRun, key: str, chains: list[list[LogEntry]]) -> str:
    ok = all(ws.chain_ok(c) for c in chains)
    badge = chip("chain verified", "good") if ok else chip("chain BROKEN", "bad")
    n = sum(len(c) for c in chains)
    who = "dispatcher log" if key == ORCH else key
    sim = chip("synthetic", "sim") if run.synthetic else ""
    return (f'<div class="ac-h">Agent channel {badge}{chip(f"{run.mode} agents", "info")}{sim}</div>'
            f'<div class="ac-sub ac-mono">{esc(who)} · {n} entries{"" if run.done else " · running"}</div>')


def _orchestrator(run: ws.WindowRun, keep: set[str], select: Callable[[str], None]) -> None:
    for e in ws.orchestrator_entries(run):
        if ORCH not in keep:
            break
        render_entry(e, key_prefix="o-")
        if e.type == "DISPATCH" and e.event_id in run.dispatcher.events:
            code = run.dispatcher.events[e.event_id].flight.code
            st.button(f"Open {code} negotiation thread", key=f"win-open-{e.seq}", icon=":material/forum:",
                      on_click=_open_thread, args=(e.event_id, select))


def _event(run: ws.WindowRun, event_id: str, keep: set[str]) -> None:
    segments = run.dispatcher.log_segments(event_id)
    for i, seg in enumerate(segments):
        if len(segments) > 1:
            last = "pre-empted" if i < len(segments) - 1 else "latest"
            st.markdown(f'<div class="ac-h">Attempt {i + 1} · {last}</div>', unsafe_allow_html=True)
        for e in seg:
            if e.who in keep:
                render_entry(e, key_prefix=f"{event_id[-8:]}-{i}-")


def thread_panel(run: ws.WindowRun | None, select: Callable[[str], None]) -> None:
    if run is None:
        st.markdown('<div class="ac-h">Agent channel</div><div class="ac-sub">Run an airport window to see the '
                    'orchestrator thread.</div>', unsafe_allow_html=True)
        return
    options = _thread_options(run)
    if st.session_state.get(THREAD_KEY) not in options:
        st.session_state[THREAD_KEY] = ORCH
    key = st.selectbox("Thread", options, key=THREAD_KEY, format_func=lambda k: _thread_label(run, k),
                       label_visibility="collapsed")
    chains = [ws.orchestrator_entries(run)] if key == ORCH else run.dispatcher.log_segments(key)
    st.markdown(_header(run, key, chains), unsafe_allow_html=True)
    chosen = st.pills("Senders", options=list(WINDOW_SENDERS), selection_mode="multi", default=list(WINDOW_SENDERS),
                      key=FILTER_KEY, format_func=sender_label, label_visibility="collapsed")
    keep = set(chosen or ())
    if run.error:
        st.error(run.error)
    with st.container(height=CHANNEL_HEIGHT, border=False):
        if key == ORCH:
            _orchestrator(run, keep, select)
        else:
            _event(run, key, keep)
        if run.running:
            st.markdown('<div class="composing">Dispatcher is negotiating…</div>', unsafe_allow_html=True)
