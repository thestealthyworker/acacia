"""Right panel (HANDOVER 13): agent channel with chain badge, sender filter and playback.

One st.chat_message per visible log entry. Playback controls mutate the RunState through
callbacks; a change that starts or stops the timer asks app.py for a full rerun, because
the fragment's run_every is fixed until then.
"""

from __future__ import annotations

from collections.abc import Sequence

import streamlit as st

from acacia.models import LogEntry
from ui import runs
from ui.runs import RunState
from ui.theme import SINGLE_FLIGHT_SENDERS, chip, colour, esc, sender_avatar, sender_label
from ui.view import next_speaker

FULL_RERUN = "_acacia_full_rerun"
SPEED_KEY = "speed_idx"
FILTER_KEY = "sender_filter"
CHANNEL_HEIGHT = 760


def request_full_rerun() -> None:
    st.session_state[FULL_RERUN] = True


def _toggle_play(state: RunState) -> None:
    state.playing = not state.playing
    request_full_rerun()


def _step(state: RunState) -> None:
    state.playing = False
    runs.step(state)
    request_full_rerun()


def _skip(state: RunState) -> None:
    runs.skip_to_end(state)
    request_full_rerun()


def _replay(state: RunState) -> None:
    runs.replay(state)
    request_full_rerun()


def playback_controls(state: RunState) -> None:
    c1, c2, c3, c4 = st.columns(4)
    label, icon = ("Pause", ":material/pause:") if state.playing else ("Play", ":material/play_arrow:")
    c1.button(label, key="pb_play", icon=icon, on_click=_toggle_play, args=(state,), width="stretch",
              disabled=state.at_end, help="Play or pause the channel")
    c2.button("Step", key="pb_step", icon=":material/skip_next:", on_click=_step, args=(state,),
              disabled=state.idx >= len(state.entries), width="stretch", help="Show the next entry")
    c3.button("End", key="pb_skip", icon=":material/fast_forward:", on_click=_skip, args=(state,),
              width="stretch", help="Skip to end")
    c4.button("Replay", key="pb_replay", icon=":material/restart_alt:", on_click=_replay, args=(state,),
              width="stretch", help="Replay from the first entry")


def speed_select() -> None:
    st.selectbox("Speed", options=list(range(len(runs.SPEEDS))), key=SPEED_KEY, label_visibility="collapsed",
                 index=runs.DEFAULT_SPEED_IDX,
                 format_func=lambda i: f"{runs.SPEEDS[i]:g} s per message", on_change=request_full_rerun)


def _header(state: RunState, visible: Sequence[LogEntry]) -> str:
    ok = runs.chain_ok(list(visible))
    badge = chip("chain verified", "good") if ok else chip("chain BROKEN", "bad")
    mode = chip(f"{state.mode} agents", "info")
    return (f'<div class="ac-h">Agent channel {badge}{mode}</div><div class="ac-sub ac-mono">{esc(state.event.event_id)} · '
            f'{len(visible)} of {len(state.entries)} entries{"" if state.done else " · running"}</div>')


def render_entry(e: LogEntry, key_prefix: str = "") -> None:
    """One chat message; `key_prefix` keeps keys unique when several hash chains share a panel."""
    with st.container(key=f"msg-{e.who}-{key_prefix}{e.seq}"):
        with st.chat_message(e.who, avatar=sender_avatar(e.who)):
            bits = [f'<b style="color:{colour(e.who)}">{esc(sender_label(e.who))}</b>', chip(e.type, "muted")]
            if e.state:
                bits.append(chip(e.state, "info"))
            if e.turn:
                bits.append(f"<span>T{e.turn}</span>")
            if e.who == "coordinator" and e.type in ("STATE", "BROADCAST", "SEND", "OBJECTION", "DEFAULT", "AUDIT"):
                bits.append(chip("reasoning", "warn"))
            st.markdown(f'<div class="msg-head">{"".join(bits)}</div>', unsafe_allow_html=True)
            st.markdown(e.text)
            with st.expander("Payload JSON", expanded=False):
                st.json(e.payload, expanded=False)
            st.markdown(f'<div class="msg-foot">#{e.seq} · {esc(e.hash[:12])}</div>', unsafe_allow_html=True)


def channel(state: RunState | None) -> None:
    if state is None:
        st.markdown('<div class="ac-h">Agent channel</div><div class="ac-sub">Pick a disrupted flight to start a '
                    'negotiation.</div>', unsafe_allow_html=True)
        return
    visible = state.visible
    a, b = st.columns([2.2, 1], vertical_alignment="center")
    a.markdown(_header(state, visible), unsafe_allow_html=True)
    with b:
        speed_select()
    playback_controls(state)
    chosen = st.pills("Senders", options=list(SINGLE_FLIGHT_SENDERS), selection_mode="multi",
                      default=list(SINGLE_FLIGHT_SENDERS), key=FILTER_KEY, format_func=sender_label,
                      label_visibility="collapsed")
    keep = set(chosen or ())
    if state.error:
        st.error(state.error)
    with st.container(height=CHANNEL_HEIGHT, border=False):
        for e in visible:
            if e.who in keep:
                render_entry(e)
        if state.running and state.idx >= len(state.entries):
            who = next_speaker(state.entries) or "Agents"
            st.markdown(f'<div class="composing">{esc(who)} is composing…</div>', unsafe_allow_html=True)
