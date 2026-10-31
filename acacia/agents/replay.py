"""Replay agents: serve recorded LLM responses with no network (HANDOVER 8.3).

Recordings live at recordings/{event_id}/{role}-{step}-{turn}.json (written by LLMAgent with
record=True). The prompt is rebuilt from the same inputs and its hash compared with the recorded
one; a mismatch only warns, so a demo still runs after a prompt tweak. A recorded failure replays
as AgentTimeout, so the coordinator applies the same default as in the live run.
"""

from __future__ import annotations

import json
import logging
import warnings
from pathlib import Path

from pydantic import ValidationError

from acacia import config
from acacia.agents.base import AgentPrivate, AgentTimeout
from acacia.agents.llm import (
    Step, audit_declaration, build_request, prompt_file_hash, prompt_hash, recording_path,
)
from acacia.models import AgentRole, ConstraintPacket, Decision, DisruptionEvent, ResolutionPackage

logger = logging.getLogger(__name__)


class RecordingMissingError(LookupError):
    """No recording exists for this event, role, step and turn."""


class ReplayMismatchWarning(UserWarning):
    """The rebuilt prompt differs from the one that produced the recording."""


class ReplayAgent:
    """Implements the Agent protocol from recorded LLM responses."""

    def __init__(self, role: AgentRole, recordings_dir: Path | str | None = None) -> None:
        self.role = AgentRole(role)
        self.recordings_dir = Path(recordings_dir) if recordings_dir else config.RECORDINGS_DIR
        self.last_audit: list[dict] = []
        self.last_prompt_hash: str | None = None
        self.prompt_version = prompt_file_hash(self.role)

    def declare(self, event: DisruptionEvent, private: AgentPrivate | dict) -> ConstraintPacket:
        record = self._load(event.event_id, "declare", event.turn)
        request = build_request(self.role, "declare", record["model"], event, private)
        packet = self._serve(ConstraintPacket, record, request)
        self.last_audit = audit_declaration(self.role, packet, private)
        for note in self.last_audit:
            logger.warning("AUDIT %s", note["text"])
        return packet

    def decide(self, event: DisruptionEvent, package: ResolutionPackage, private: AgentPrivate | dict,
               own_packet: ConstraintPacket) -> Decision:
        record = self._load(event.event_id, "decide", package.turn)
        request = build_request(self.role, "decide", record["model"], event, private, package, own_packet)
        return self._serve(Decision, record, request)

    def _load(self, event_id: str, step: Step, turn: int) -> dict:
        path = recording_path(self.recordings_dir, event_id, self.role, step, turn)
        if not path.exists():
            raise RecordingMissingError(
                f"no recording for {self.role.value} {step} turn {turn} of {event_id} at {path}; "
                "run once with --agents llm --record, or use --agents rule")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecordingMissingError(f"recording {path} is unreadable: {exc}") from exc
        if not isinstance(record, dict) or "model" not in record or "prompt_hash" not in record:
            raise RecordingMissingError(f"recording {path} is malformed (needs model and prompt_hash)")
        record["_path"] = str(path)
        return record

    def _serve(self, model: type[ConstraintPacket] | type[Decision], record: dict, request: dict):
        step = "declare" if model is ConstraintPacket else "decide"
        self.last_prompt_hash = prompt_hash(request)
        if self.last_prompt_hash != record["prompt_hash"]:
            msg = (f"{self.role.value} {step}: prompt hash differs from recording {record['_path']}; "
                   "serving the recorded response anyway")
            logger.warning(msg)
            warnings.warn(msg, ReplayMismatchWarning, stacklevel=3)
        if record.get("result") is None:
            raise AgentTimeout(self.role, step, f"recorded failure: {record.get('error') or 'no result'}")
        try:
            return model.model_validate(record["result"])
        except ValidationError as exc:
            raise RecordingMissingError(f"recording {record['_path']} has an invalid result: {exc}") from exc


def make_replay_agents(recordings_dir: Path | str | None = None) -> dict[AgentRole, ReplayAgent]:
    return {role: ReplayAgent(role, recordings_dir=recordings_dir) for role in AgentRole}
