"""LLM agents (HANDOVER 8.3): one Anthropic request per agent per step, structured via a tool.

Each agent has its own system prompt (prompts/{role}.md) and sees only the shared event, its own
private state and, when deciding, the package plus its own packet. Output is validated with
Pydantic; one retry carries the validation error; a second failure, an API timeout or an API error
raises AgentTimeout (the coordinator then applies the most restrictive default, HANDOVER 11.3).
The declaration audit compares declared hard limits with private state and never corrects them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ValidationError

from acacia import config
from acacia.agents.base import AgentPrivate, AgentTimeout, coerce_private
from acacia.models import (
    AgentRole, AtcPrivate, ConstraintPacket, Decision, DisruptionEvent, GroundPrivate, PilotPrivate,
    ResolutionPackage,
)
from acacia.timeutil import to_hhmm, to_min

logger = logging.getLogger(__name__)

Step = Literal["declare", "decide"]
DECLARE_TOOL = "submit_constraint_packet"
DECIDE_TOOL = "submit_decision"
MAX_TOKENS = 4096
EFFORT = "low"
MAX_ATTEMPTS = 2
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
# Models that reject a forced tool_choice ("tool"/"any") with a 400: use "auto" + prompt instruction.
NO_FORCED_TOOL_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-mythos-5-1")

_TOOLS: dict[str, tuple[str, type[BaseModel], str]] = {
    "declare": (DECLARE_TOOL, ConstraintPacket,
                "Submit your constraint packet: hard limits, soft preferences, options, owned facts."),
    "decide": (DECIDE_TOOL, Decision, "Submit your ACCEPT or REJECT verdict on the package."),
}


class InvalidOutput(ValueError):
    """The model's tool call was missing or did not validate."""

    def __init__(self, message: str, raw: Any = None) -> None:
        super().__init__(message)
        self.raw = raw


# --- prompts and requests ------------------------------------------------------------------------
@lru_cache(maxsize=None)
def system_prompt(role: AgentRole) -> str:
    path = PROMPTS_DIR / f"{AgentRole(role).value.lower()}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(f"system prompt for {AgentRole(role).value} missing at {path}") from exc


def prompt_file_hash(role: AgentRole) -> str:
    """Short hash of a role's prompt file, for the ConsensusRecord stamp."""
    return hashlib.sha256(system_prompt(role).encode()).hexdigest()[:12]


def tool_choice_for(model: str, tool_name: str) -> dict:
    if model.startswith(NO_FORCED_TOOL_MODELS):
        return {"type": "auto", "disable_parallel_tool_use": True}
    return {"type": "tool", "name": tool_name}


def tool_spec(step: Step) -> dict:
    name, model, description = _TOOLS[step]
    return {"name": name, "description": description, "input_schema": model.model_json_schema()}


def private_view(role: AgentRole, private: AgentPrivate | dict) -> dict:
    """The agent's own private state with times as "HH:MM", so it never has to convert minutes."""
    p = coerce_private(role, private)
    if isinstance(p, AtcPrivate):
        slots = sorted(p.free_slots)
        return {"first_available_slot": to_hhmm(p.first), "free_slots": [to_hhmm(s) for s in slots],
                "slot_demand": {to_hhmm(s): p.demand.get(s, 1) for s in slots},
                "other_traffic": [to_hhmm(t) for t in sorted(p.traffic)],
                "runway_closures": [{"from": to_hhmm(a), "until": to_hhmm(b)} for a, b in p.closures],
                "min_separation_min": p.min_separation}
    if isinstance(p, PilotPrivate):
        return {"land_by": to_hhmm(p.land_by), "connecting_pax": p.pax,
                "connect_by": to_hhmm(p.connect_by), "low_fuel": p.low_fuel}
    return {"gates": list(p.gates), "assigned_gate": p.assigned_gate,
            "gate_free_from": _gate_free_map(p), "failed_gates": list(p.failed),
            "min_turn_min": p.min_turn, "shift_end": to_hhmm(p.shift_end),
            "duty_limit": to_hhmm(p.duty_limit), "next_departure": to_hhmm(p.next_departure)}


def _gate_free_map(g: GroundPrivate) -> dict[str, str]:
    out = {}
    for gate in g.gates:
        free = g.free_from.get(gate)
        out[gate] = "unavailable" if gate in g.failed or free is None else to_hhmm(free)
    return out


def _derived(role: AgentRole, event: DisruptionEvent, package: ResolutionPackage,
             private: AgentPrivate | dict) -> dict:
    """Arithmetic done by code, not the model: gate arrival and turnaround end for the package."""
    gate_arr = to_min(package.slot) + event.taxi_in
    if role == AgentRole.PILOT:
        return {"gate_arrival": to_hhmm(gate_arr)}
    if role == AgentRole.GROUND:
        g = coerce_private(role, private)
        return {"gate_arrival": to_hhmm(gate_arr), "turnaround_end": to_hhmm(gate_arr + g.min_turn)}
    return {}


def _js(obj: Any) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, default=str)


def build_user_message(role: AgentRole, step: Step, event: DisruptionEvent, private: AgentPrivate | dict,
                       package: ResolutionPackage | None = None,
                       own_packet: ConstraintPacket | None = None) -> str:
    role = AgentRole(role)
    tool = _TOOLS[step][0]
    turn = event.turn if step == "declare" else package.turn  # type: ignore[union-attr]
    ident = f'event_id "{event.event_id}", agent "{role.value}", turn {turn}'
    if step == "decide":
        ident += f', package_id "{package.package_id}"'  # type: ignore[union-attr]
    parts = [f"Step: {step}. Call the tool {tool} exactly once. Use {ident}.",
             f"Shared event (JSON):\n{_js(event.model_dump(mode='json'))}",
             f"Your private state (simulated, yours only, times HH:MM):\n{_js(private_view(role, private))}"]
    if step == "decide":
        parts += [f"Package to vote on (JSON):\n{_js(package.model_dump(mode='json'))}",  # type: ignore[union-attr]
                  f"Your own declared packet (JSON):\n{_js(own_packet.model_dump(mode='json'))}",  # type: ignore[union-attr]
                  f"Computed for you from the package:\n{_js(_derived(role, event, package, private))}"]  # type: ignore[arg-type]
    return "\n\n".join(parts)


def build_request(role: AgentRole, step: Step, model: str, event: DisruptionEvent,
                  private: AgentPrivate | dict, package: ResolutionPackage | None = None,
                  own_packet: ConstraintPacket | None = None) -> dict:
    """The Messages API request body (no timeout). No temperature: current models reject it."""
    user = build_user_message(role, step, event, private, package, own_packet)
    return {"model": model, "max_tokens": MAX_TOKENS, "system": system_prompt(AgentRole(role)),
            "messages": [{"role": "user", "content": user}], "tools": [tool_spec(step)],
            "tool_choice": tool_choice_for(model, _TOOLS[step][0]), "output_config": {"effort": EFFORT}}


def prompt_hash(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()


def recording_path(root: Path | str, event_id: str, role: AgentRole, step: Step, turn: int) -> Path:
    safe_event = re.sub(r"[^A-Za-z0-9_.-]", "_", event_id)
    return Path(root) / safe_event / f"{AgentRole(role).value.lower()}-{step}-{turn}.json"


# --- declaration audit ---------------------------------------------------------------------------
def _expected_limits(role: AgentRole, private: AgentPrivate | dict) -> list[tuple[str, str, str, Any]]:
    """(kind, id, rule, expected value) per declared limit the solver relies on."""
    p = coerce_private(role, private)
    if isinstance(p, AtcPrivate):
        return [("hard", "A_H1", "SLOT_IN_SEQUENCE", [to_hhmm(s) for s in sorted(p.free_slots)]),
                ("hard", "A_H2", "MIN_SEPARATION_MIN", p.min_separation)]
    if isinstance(p, PilotPrivate):
        return [("hard", "P_H1", "LAND_BY", to_hhmm(p.land_by))]
    return [("hard", "G_H1", "GATE_FREE_FROM", _gate_free_map(p)),
            ("hard", "G_H2", "CREW_DUTY_LIMIT", to_hhmm(p.duty_limit)),
            ("fact", "min_turn_min", "facts_owned", p.min_turn)]


def _same(declared: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return isinstance(declared, list) and sorted(map(str, declared)) == sorted(map(str, expected))
    if isinstance(expected, dict):
        return (isinstance(declared, dict) and set(map(str, declared)) == set(expected)
                and all(str(declared[k]).strip() == v for k, v in expected.items()))
    if isinstance(expected, int):
        try:
            return not isinstance(declared, bool) and int(declared) == expected and float(declared) == expected
        except (TypeError, ValueError):
            return False
    return isinstance(declared, str) and declared.strip() == expected


def audit_declaration(role: AgentRole, packet: ConstraintPacket, private: AgentPrivate | dict) -> list[dict]:
    """AUDIT notes for declared hard limits that disagree with private state. Nothing is corrected."""
    role = AgentRole(role)
    hard = {h.id: h.value for h in packet.hard}
    notes = []
    for kind, limit_id, rule, expected in _expected_limits(role, private):
        source = hard if kind == "hard" else packet.facts_owned
        present = limit_id in source
        declared = source.get(limit_id)
        if present and _same(declared, expected):
            continue
        what = f"declared {_js(declared)}" if present else "not declared"
        notes.append({"agent": role.value, "id": limit_id, "rule": rule, "declared": declared,
                      "expected": expected,
                      "text": f"{role.value} {limit_id} {rule} {what}; private state says {_js(expected)} "
                              "(not corrected; the checker uses ground truth)."})
    return notes


# --- the agent -------------------------------------------------------------------------------------
def _block_field(block: Any, name: str) -> Any:
    return block.get(name) if isinstance(block, dict) else getattr(block, name, None)


def _dump_response(response: Any) -> dict:
    blocks = []
    for block in getattr(response, "content", None) or []:
        kind = _block_field(block, "type")
        item: dict[str, Any] = {"type": kind}
        if kind == "tool_use":
            item.update(name=_block_field(block, "name"), input=_block_field(block, "input"))
        elif kind == "text":
            item["text"] = _block_field(block, "text")
        blocks.append(item)
    return {"content": blocks, "stop_reason": getattr(response, "stop_reason", None),
            "model": getattr(response, "model", None)}


def _tool_input(dumped: dict, tool_name: str) -> dict:
    for block in dumped["content"]:
        if block["type"] == "tool_use" and block.get("name") == tool_name:
            if not isinstance(block.get("input"), dict):
                raise InvalidOutput(f"{tool_name} input is not a JSON object", block.get("input"))
            return block["input"]
    raise InvalidOutput(f"no {tool_name} tool call in the response (stop_reason={dumped['stop_reason']})")


def parse_output(step: Step, raw: dict, expected: dict) -> ConstraintPacket | Decision:
    """Validate a tool input and check its identity fields (event, agent, turn, package)."""
    model = _TOOLS[step][1]
    try:
        obj = model.model_validate(raw)
    except ValidationError as exc:
        raise InvalidOutput(str(exc), raw) from exc
    wrong = [f"{k}={_js(getattr(obj, k))} (expected {_js(v)})"
             for k, v in expected.items() if getattr(obj, k) != v]
    if wrong:
        raise InvalidOutput("wrong identity fields: " + ", ".join(wrong), raw)
    return obj  # type: ignore[return-value]


def _retry_request(request: dict, error: InvalidOutput, tool_name: str) -> dict:
    text = (f"{request['messages'][0]['content']}\n\nYour previous {tool_name} call was invalid: {error}\n"
            f"Previous input: {_js(error.raw)}\nCall {tool_name} again with a corrected, complete input.")
    return {**request, "messages": [{"role": "user", "content": text}]}


class LLMAgent:
    """An ATC, Pilot or Ground agent backed by one Claude request per step."""

    def __init__(self, role: AgentRole, client: Any = None, model: str | None = None, record: bool = False,
                 recordings_dir: Path | str | None = None, timeout: float | None = None) -> None:
        self.role = AgentRole(role)
        self.model = model or config.MODEL
        self.record = record
        self.recordings_dir = Path(recordings_dir) if recordings_dir else config.RECORDINGS_DIR
        self.timeout = timeout if timeout is not None else config.LLM_TIMEOUT_S
        self._client = client if client is not None else default_client(self.timeout)
        self.last_audit: list[dict] = []
        self.last_prompt_hash: str | None = None
        self.prompt_version = prompt_file_hash(self.role)

    def declare(self, event: DisruptionEvent, private: AgentPrivate | dict) -> ConstraintPacket:
        request = build_request(self.role, "declare", self.model, event, private)
        expected = {"event_id": event.event_id, "agent": self.role, "turn": event.turn}
        packet = self._run("declare", request, expected, event.event_id, event.turn)
        self.last_audit = audit_declaration(self.role, packet, private)  # type: ignore[arg-type]
        for note in self.last_audit:
            logger.warning("AUDIT %s", note["text"])
        return packet  # type: ignore[return-value]

    def decide(self, event: DisruptionEvent, package: ResolutionPackage, private: AgentPrivate | dict,
               own_packet: ConstraintPacket) -> Decision:
        request = build_request(self.role, "decide", self.model, event, private, package, own_packet)
        expected = {"event_id": event.event_id, "package_id": package.package_id, "agent": self.role,
                    "turn": package.turn}
        return self._run("decide", request, expected, event.event_id, package.turn)  # type: ignore[return-value]

    def _run(self, step: Step, request: dict, expected: dict, event_id: str,
             turn: int) -> ConstraintPacket | Decision:
        tool_name = _TOOLS[step][0]
        self.last_prompt_hash = prompt_hash(request)
        log = {"event_id": event_id, "role": self.role.value, "step": step, "turn": turn,
               "model": self.model, "prompt_hash": self.last_prompt_hash, "exchanges": [],
               "result": None, "error": None}
        try:
            result = self._attempts(step, request, expected, tool_name, log)
        except AgentTimeout as exc:
            log["error"] = str(exc)
            self._save(log)
            raise
        log["result"] = result.model_dump(mode="json")
        self._save(log)
        return result

    def _attempts(self, step: Step, request: dict, expected: dict, tool_name: str,
                  log: dict) -> ConstraintPacket | Decision:
        current = request
        for attempt in range(1, MAX_ATTEMPTS + 1):
            dumped = self._call(step, current, log)
            try:
                return parse_output(step, _tool_input(dumped, tool_name), expected)
            except InvalidOutput as err:
                log["exchanges"][-1]["invalid"] = str(err)
                logger.info("%s %s attempt %d invalid: %s", self.role.value, step, attempt, err)
                if attempt == MAX_ATTEMPTS:
                    raise AgentTimeout(self.role, step, f"invalid output twice: {err}") from err
                current = _retry_request(request, err, tool_name)
        raise AgentTimeout(self.role, step, "no attempts made")  # unreachable with MAX_ATTEMPTS >= 1

    def _call(self, step: Step, request: dict, log: dict) -> dict:
        exchange: dict[str, Any] = {"request": request}
        log["exchanges"].append(exchange)
        try:
            response = self._client.messages.create(**request, timeout=self.timeout)
        except anthropic.APIConnectionError as exc:  # includes APITimeoutError
            exchange["error"] = f"{type(exc).__name__}: {exc}"
            raise AgentTimeout(self.role, step, exchange["error"]) from exc
        except anthropic.APIStatusError as exc:
            exchange["error"] = f"{type(exc).__name__} {exc.status_code}: {exc.message}"
            raise AgentTimeout(self.role, step, exchange["error"]) from exc
        exchange["response"] = _dump_response(response)
        return exchange["response"]

    def _save(self, log: dict) -> None:
        if not self.record:
            return
        path = recording_path(self.recordings_dir, log["event_id"], self.role, log["step"], log["turn"])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")
        except OSError:
            logger.exception("could not write recording %s", path)


def default_client(timeout: float) -> anthropic.Anthropic:
    """Live client. max_retries=0 keeps each call inside the per-call timeout (HANDOVER 8.3)."""
    return anthropic.Anthropic(timeout=timeout, max_retries=0)


def make_llm_agents(record: bool = False, client: Any = None, model: str | None = None,
                    recordings_dir: Path | str | None = None) -> dict[AgentRole, LLMAgent]:
    """One LLMAgent per role, sharing one client (built from the environment when None)."""
    shared = client if client is not None else default_client(config.LLM_TIMEOUT_S)
    return {role: LLMAgent(role, client=shared, model=model, record=record, recordings_dir=recordings_dir)
            for role in AgentRole}
