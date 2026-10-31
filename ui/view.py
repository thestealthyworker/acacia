"""Derive what the dashboard shows from the visible log entries (pure, no Streamlit).

Both panels render from the same entry list, so the dashboard can never disagree with the
agent channel: every card below reads only from `LogEntry.payload`s seen so far.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from acacia.models import LogEntry

ROLE_WHO = {"ATC": "atc", "PILOT": "pilot", "GROUND": "ground"}


@dataclass
class View:
    event: dict | None = None
    packets: dict[str, dict] = field(default_factory=dict)  # role value -> ConstraintPacket payload
    defaults: dict[str, str] = field(default_factory=dict)  # role value -> default text
    solver: dict | None = None  # latest ResolutionPackage entry payload
    sent: dict | None = None  # package under vote (SEND payload)
    sent_ids: list[str] = field(default_factory=list)
    votes: dict[str, dict] = field(default_factory=dict)  # role value -> latest Decision payload
    check: dict | None = None  # latest CheckResult payload
    fallback: dict | None = None  # FallbackPlan payload (with "used")
    fallback_reason: str | None = None
    record: dict | None = None  # ConsensusRecord payload (or a SignOff's updated record)
    signoff: dict | None = None
    state: str = "IDLE"
    stage: int = -1
    turn: int = 0
    audits: list[str] = field(default_factory=list)


def _apply_state(v: View, e: LogEntry) -> None:
    if e.state:
        v.state = e.state
    if e.stage is not None:
        v.stage = max(v.stage, e.stage)
    if e.turn is not None:
        v.turn = max(v.turn, e.turn)


def _apply_payload(v: View, e: LogEntry) -> None:
    p = e.payload
    if e.type == "DisruptionEvent":
        v.event = p
    elif e.type == "ConstraintPacket":
        v.packets[p.get("agent", e.who.upper())] = p
    elif e.type == "DEFAULT" and "packet" in p:
        v.packets[p["role"]] = p["packet"]
        v.defaults[p["role"]] = e.text
    elif e.type == "ResolutionPackage":
        v.solver = p
    elif e.type == "SEND":
        v.sent = p
        v.sent_ids.append(p.get("package_id", ""))
        v.votes = {}
    elif e.type == "Decision":
        v.votes[p.get("agent", e.who.upper())] = p
    elif e.type == "DEFAULT" and "decision" in p:
        v.votes[p["role"]] = p["decision"]
    elif e.type == "CheckResult":
        v.check = p
    elif e.type == "STATE" and e.state == "FALLBACK":
        v.fallback_reason = p.get("reason")
    elif e.type == "FallbackPlan":
        v.fallback = p
    elif e.type == "ConsensusRecord":
        v.record = p
    elif e.type == "SignOff":
        v.signoff = p.get("signoff")
        v.record = p.get("record", v.record)
    elif e.type == "AUDIT":
        v.audits.append(e.text)


def build_view(entries: Sequence[LogEntry]) -> View:
    v = View()
    for e in entries:
        _apply_state(v, e)
        _apply_payload(v, e)
    return v


def hard(packet: dict | None, rule: str) -> dict | None:
    """The first hard limit with this rule name in a packet payload."""
    for h in (packet or {}).get("hard", []):
        if h.get("rule") == rule:
            return h
    return None


def soft(packet: dict | None, metric: str) -> dict | None:
    for s in (packet or {}).get("soft", []):
        if s.get("metric") == metric:
            return s
    return None


def options(packet: dict | None, kind: str) -> list[dict]:
    return [o for o in (packet or {}).get("options", []) if o.get("type") == kind]


def next_speaker(entries: Sequence[LogEntry]) -> str | None:
    """Who the channel is waiting on (LLM mode "<agent> is composing...")."""
    if not entries:
        return "Scenario builder"
    last = entries[-1]
    order = ("ATC", "PILOT", "GROUND")
    if last.type == "BROADCAST" or (last.type == "SEND"):
        return "ATC agent"
    if last.type in ("ConstraintPacket", "Decision", "DEFAULT", "OBJECTION", "AUDIT"):
        role = (last.payload.get("agent") or last.payload.get("role") or last.who.upper())
        if role in order and order.index(role) < 2:
            return f"{order[order.index(role) + 1].title()} agent"
        return "Coordinator" if last.type != "Decision" else "Checker"
    return None
