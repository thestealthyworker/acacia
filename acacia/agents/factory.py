"""Build the three agents for a run mode: "rule" (deterministic), "llm" (live), "replay"."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Literal

from acacia import config

from acacia.agents.base import Agent
from acacia.agents.rule import RuleATC, RuleGround, RulePilot
from acacia.models import AgentRole, Stress

AgentMode = Literal["rule", "llm", "replay"]

_LAZY: dict[str, tuple[str, str]] = {
    "llm": ("acacia.agents.llm", "make_llm_agents"),
    "replay": ("acacia.agents.replay", "make_replay_agents"),
}


class AgentModeUnavailable(RuntimeError):
    """The module behind an agent mode is not installed or not built yet."""


STRESS_FLAGS = ("low_fuel", "tight_crew", "corrupt_solver", "declared_emergency")


def variant_tag(stress: Stress | None) -> str:
    """Recording variant for a stress combination: "base", or the active flags joined by "+".
    LLM answers to an identical prompt can differ between runs, so each scenario keeps its own
    recordings and replay never mixes them."""
    flags = [f for f in STRESS_FLAGS if stress is not None and getattr(stress, f, False)]
    return "+".join(flags) if flags else "base"


def recordings_dir_for(stress: Stress | None) -> Path:
    return config.RECORDINGS_DIR / variant_tag(stress)


def make_agents(mode: AgentMode = "rule", record: bool = False,
                stress: Stress | None = None) -> dict[AgentRole, Agent]:
    """Return {role: agent} for `mode`. `record` is passed to the llm factory only; `stress` picks
    the recordings variant folder for llm (recording) and replay."""
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
    folder = recordings_dir_for(stress)
    agents = factory(record=record, recordings_dir=folder) if mode == "llm" else factory(recordings_dir=folder)
    _check_agents(mode, agents)
    return agents


def _check_agents(mode: str, agents: object) -> None:
    if not isinstance(agents, dict) or set(agents) != set(AgentRole):
        raise AgentModeUnavailable(f"agent mode {mode!r} must return one agent per role")
