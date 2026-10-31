"""Run lifecycle for the UI (PLAN 3.7): collect, then play back by an index in session state.

Rule and Replay runs are collected into a list at once. LLM runs execute in a background
thread that appends to the same list, which playback reads. One `RunState` per event_id,
kept in st.session_state by app.py; a new mode or stress set restarts that event's run.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from acacia import config
from acacia.agents.factory import AgentModeUnavailable, make_agents
from acacia.coordinator import run as run_coordinator
from acacia.log import verify_chain
from acacia.models import ConsensusRecord, DisruptionEvent, LogEntry, Stress
from acacia.signoff import SignOffError, last_record, sign_off

logger = logging.getLogger(__name__)

MODES = ("Rule", "LLM", "Replay")
SPEEDS = (0.25, 0.5, 1.0, 2.0)  # seconds per entry
DEFAULT_SPEED_IDX = 1


@dataclass
class RunState:
    key: tuple
    event: DisruptionEvent
    mode: str
    entries: list[LogEntry] = field(default_factory=list)
    done: bool = False
    error: str | None = None
    idx: int = 0
    playing: bool = True
    thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def visible(self) -> list[LogEntry]:
        return self.entries[: self.idx]

    @property
    def at_end(self) -> bool:
        return self.done and self.idx >= len(self.entries)


def has_recordings(event_id: str) -> bool:
    folder = config.RECORDINGS_DIR / event_id
    try:
        return folder.is_dir() and any(folder.glob("*.json"))
    except OSError:
        return False


def default_mode(event_id: str | None) -> str:
    return "Replay" if event_id and has_recordings(event_id) else "Rule"


def run_key(event: DisruptionEvent, mode: str, stress: Stress) -> tuple:
    return (event.event_id, mode, tuple(sorted(stress.model_dump().items())))


def _collect(state: RunState, stress: Stress, data: object) -> None:
    """Drain the coordinator generator into state.entries; errors end the run visibly."""
    try:
        agents = make_agents(state.mode.lower())
        for entry in run_coordinator(state.event, stress, agents, data=data):
            state.entries.append(entry)
    except AgentModeUnavailable as exc:
        state.error = f"{state.mode} agents unavailable: {exc}"
    except Exception as exc:  # noqa: BLE001 - surfaced in the UI, never swallowed
        logger.exception("run failed for %s", state.event.event_id)
        state.error = f"run failed: {type(exc).__name__}: {exc}"
    finally:
        state.done = True


def start_run(event: DisruptionEvent, mode: str, stress: Stress, data: object) -> RunState:
    if mode not in MODES:
        raise ValueError(f"unknown agent mode {mode!r}")
    state = RunState(key=run_key(event, mode, stress), event=event, mode=mode)
    if mode == "LLM":
        state.thread = threading.Thread(target=_collect, args=(state, stress, data), daemon=True,
                                        name=f"acacia-llm-{event.event_id}")
        state.thread.start()
    else:
        _collect(state, stress, data)
    return state


# --- playback ------------------------------------------------------------------------------
def step(state: RunState, n: int = 1) -> None:
    state.idx = min(len(state.entries), state.idx + n)
    if state.at_end:
        state.playing = False


def skip_to_end(state: RunState) -> None:
    state.idx = len(state.entries)
    state.playing = not state.done


def replay(state: RunState) -> None:
    state.idx, state.playing = 0, True


def chain_ok(entries: list[LogEntry]) -> bool:
    ok, _ = verify_chain(entries)
    return ok


# --- sign-off ------------------------------------------------------------------------------
def current_record(state: RunState) -> ConsensusRecord | None:
    try:
        return last_record(state.entries)
    except SignOffError:
        return None


def can_approve(record: ConsensusRecord | None) -> bool:
    if record is None or record.status != "AWAITING_SIGNOFF":
        return False
    return record.checker_pass is True or bool(record.package.get("divert"))


def apply_signoff(state: RunState, approve: bool, who: str) -> str | None:
    """Append a SignOff entry; returns an error message or None."""
    if not state.done:
        return "wait for the run to finish before signing off"
    record = current_record(state)
    if record is None:
        return "this run has no ConsensusRecord to sign off"
    try:
        entry, _ = sign_off(state.entries, record, approve, who)
    except (SignOffError, ValueError) as exc:
        return str(exc)
    state.entries.append(entry)
    state.idx = len(state.entries)
    return None
