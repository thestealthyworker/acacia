"""Event dashboard panels shared by Single flight and Airport window mode (HANDOVER 13 items 2-7).

Renders from one event's visible log entries only, so it can never disagree with the channel.
Sign-off controls stay with each mode: single flight signs the in-memory run, window mode
signs through the dispatcher so the ledger commits or releases the holds.
"""

from __future__ import annotations

from collections.abc import Sequence

import streamlit as st

from acacia.models import DisruptionEvent, LogEntry
from ui.components import agent_cards, event_card, outcome_panel, protocol, solver_table
from ui.timeline import timeline_chart, timeline_data
from ui.view import build_view


def event_panels(entries: Sequence[LogEntry], event: DisruptionEvent) -> None:
    view = build_view(entries)
    event_card(event)
    protocol(view)
    agent_cards(view, event)
    solver_table(view, event)
    with st.container(border=True):
        st.markdown('<div class="ac-h">Timeline <span class="chip sim">simulated</span></div>', unsafe_allow_html=True)
        data = timeline_data(view, event)
        if data is None:
            st.caption("Appears once ATC declares its slots.")
        else:
            st.altair_chart(timeline_chart(data), width="stretch")
    outcome_panel(view)
