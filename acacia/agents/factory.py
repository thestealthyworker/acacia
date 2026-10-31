"""Build the three agents for a run mode: "rule" (deterministic), "llm" (live), "replay"."""

from __future__ import annotations

import importlib
from typing import Literal

from acacia.agents.base import Agent
from acacia.agents.rule import RuleATC, RuleGround, RulePilot
from acacia.models import AgentRole

AgentMode = Literal["rule", "llm", "replay"]

_LAZY: dict[str, tuple[str, str]] = {
    "llm": ("acacia.agents.llm", "make_llm_agents"),
    "replay": ("acacia.agents.replay", "make_replay_agents"),
}


class AgentModeUnavailable(RuntimeError):
    """The module behind an agent mode is not installed or not built yet."""


def make_agents(mode: AgentMode = "rule", record: bool = False) -> dict[AgentRole, Agent]:
    """Return {role: agent} for `mode`. `record` is passed to the llm factory only."""
    if mode == "rule":
        return {AgentRole.ATC: RuleATC(), AgentRole.PILOT: RulePilot(), AgentRole.GROUND: RuleGround()}
    if mode not in _LAZY:
        raise ValueError(f"unknown agent mode {mode!r}; expected 'rule', 'llm' or 'replay'")
    module_name, func_name = _LAZY[mode]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name and module_name.startswith(exc.name):
            raise AgentModeUnavailable(
                f"agent mode {mode!r} needs {module_name}, which is not available; use --agents rule"
            ) from exc
        raise
    factory = getattr(module, func_name, None)
    if factory is None:
        raise AgentModeUnavailable(f"agent mode {mode!r}: {module_name} has no {func_name}()")
    agents = factory(record=record) if mode == "llm" else factory()
    _check_agents(mode, agents)
    return agents


def _check_agents(mode: str, agents: object) -> None:
    if not isinstance(agents, dict) or set(agents) != set(AgentRole):
        raise AgentModeUnavailable(f"agent mode {mode!r} must return one agent per role")
