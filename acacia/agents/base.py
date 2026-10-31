"""Agent interface (HANDOVER 8.1).

Each agent receives only the shared event plus its own slice of private state
(AtcPrivate, PilotPrivate or GroundPrivate). Never pass one agent's private state or
packet to another agent.
"""

from __future__ import annotations

from typing import Protocol, Union, runtime_checkable

from acacia.models import (
    AgentRole, AtcPrivate, ConstraintPacket, Decision, DisruptionEvent, GroundPrivate,
    PilotPrivate, PrivateState, ResolutionPackage,
)

AgentPrivate = Union[AtcPrivate, PilotPrivate, GroundPrivate]

_PRIVATE_MODEL: dict[AgentRole, type[AgentPrivate]] = {
    AgentRole.ATC: AtcPrivate,
    AgentRole.PILOT: PilotPrivate,
    AgentRole.GROUND: GroundPrivate,
}


class AgentTimeout(Exception):
    """An agent did not answer in time, or gave invalid output twice (HANDOVER 8.3, 11.3).

    The coordinator applies the most restrictive default; a timeout never counts as ACCEPT.
    """

    def __init__(self, role: AgentRole | str, step: str, detail: str = "") -> None:
        self.role = AgentRole(role)
        self.step = step
        self.detail = detail
        super().__init__(f"{self.role.value} timed out on {step}" + (f": {detail}" if detail else ""))


@runtime_checkable
class Agent(Protocol):
    role: AgentRole

    def declare(self, event: DisruptionEvent, private: AgentPrivate) -> ConstraintPacket: ...

    def decide(
        self,
        event: DisruptionEvent,
        package: ResolutionPackage,
        private: AgentPrivate,
        own_packet: ConstraintPacket,
    ) -> Decision: ...


def private_for(role: AgentRole, state: PrivateState) -> AgentPrivate:
    """The one slice of private state an agent of `role` may see."""
    role = AgentRole(role)
    if role == AgentRole.ATC:
        return state.atc
    if role == AgentRole.PILOT:
        return state.pilot
    return state.ground


def coerce_private(role: AgentRole, private: AgentPrivate | dict) -> AgentPrivate:
    """Accept the typed model or its dict form; reject another role's state."""
    model = _PRIVATE_MODEL[AgentRole(role)]
    if isinstance(private, model):
        return private
    if isinstance(private, dict):
        return model.model_validate(private)
    raise TypeError(f"{AgentRole(role).value} agent expects {model.__name__}, got {type(private).__name__}")
