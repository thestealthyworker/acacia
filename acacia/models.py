"""Frozen message contracts (HANDOVER 5 and 18.3), simulated private-state models and the
LedgerView protocol shared by both workstreams.

Times are "HH:MM" strings on the wire (validated by `HHMM`) and int minutes in the private
state models. Change nothing here without a DECISIONS.md line and a note to the other stream.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import AfterValidator, BaseModel, Field, model_validator

from acacia.timeutil import is_hhmm


def _check_hhmm(value: str) -> str:
    if not is_hhmm(value):
        raise ValueError(f"time must be 'HH:MM' (00:00..23:59), got {value!r}")
    return value


HHMM = Annotated[str, AfterValidator(_check_hhmm)]


# --- enums -------------------------------------------------------------------------------
class ScenarioType(str, Enum):
    WEATHER = "WEATHER"
    KNOCK_ON = "KNOCK_ON"
    COMBINED = "COMBINED"


class AgentRole(str, Enum):
    ATC = "ATC"
    PILOT = "PILOT"
    GROUND = "GROUND"


class Verdict(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


class ReasonCode(str, Enum):
    FUEL_LIMIT = "FUEL_LIMIT"
    GATE_CONFLICT = "GATE_CONFLICT"
    SEPARATION = "SEPARATION"
    CREW_DUTY = "CREW_DUTY"
    CONNECTION_RISK = "CONNECTION_RISK"
    RESOURCE_CONFLICT = "RESOURCE_CONFLICT"  # ADDED: checker only, ledger double booking


# --- event --------------------------------------------------------------------------------
class FlightRef(BaseModel):
    airline: str
    flight_no: str  # number only, e.g. "1639"; `code` gives "DL1639"
    tail: str
    origin: str
    dest: str
    date: str  # "YYYY-MM-DD"

    @property
    def code(self) -> str:
        return f"{self.airline}{self.flight_no}"


class Causes(BaseModel):
    weather: int = 0
    late_aircraft: int = 0
    air_system: int = 0
    airline: int = 0
    security: int = 0


class Injected(BaseModel):
    type: Literal["RUNWAY_CLOSED", "EQUIPMENT_FAILURE"]
    ref: str
    start: HHMM
    end: HHMM


class Stress(BaseModel):
    low_fuel: bool = False
    tight_crew: bool = False
    corrupt_solver: bool = False
    declared_emergency: bool = False  # ADDED: payload-declared emergency -> tier 0


class DisruptionEvent(BaseModel):
    event_id: str
    scenario_type: ScenarioType
    flight: FlightRef
    sched_arrival: HHMM
    hist_arrival: HHMM
    hist_delay_min: int
    eta_landing: HHMM  # ADDED: earliest landing E (HANDOVER 4.3)
    taxi_in: int  # ADDED
    distance: int  # ADDED: drives turnaround
    causes: Causes
    injected: list[Injected]
    turn: int = 1
    overrides: list[str] = Field(default_factory=list)  # ADDED: fields set by the payload


# --- negotiation contracts ---------------------------------------------------------------------
class HardLimit(BaseModel):
    id: str
    rule: str
    value: Any


class SoftPref(BaseModel):
    id: str
    preference: str
    weight: float
    metric: Literal["slot_demand", "gate_arrival_by", "fuel_margin", "turnaround_done_by", "keep_gate"]
    target: Any = None
    stake: int | None = None


class Option(BaseModel):
    type: Literal["SLOT", "GATE", "ROUTE"]
    value: str
    demand: int | None = None


class ConstraintPacket(BaseModel):
    """facts_owned convention: Ground declares "min_turn_min" so the solver can use it."""

    event_id: str
    agent: AgentRole
    turn: int
    hard: list[HardLimit]
    soft: list[SoftPref]
    options: list[Option]
    facts_owned: dict = Field(default_factory=dict)

    def ids(self) -> set[str]:
        return {h.id for h in self.hard} | {s.id for s in self.soft}


class ResolutionPackage(BaseModel):
    event_id: str
    package_id: str
    turn: int
    slot: HHMM
    gate: str
    route_change: str | None = None
    score: float
    score_breakdown: dict[str, float]  # throughput, connections, fuel, turnaround
    satisfies_hard: bool = True


class Decision(BaseModel):
    event_id: str
    package_id: str
    agent: AgentRole
    turn: int
    verdict: Verdict
    reason_code: ReasonCode | None = None
    cites: str | None = None  # ADDED: id of the declared limit or preference behind a REJECT
    rationale: str

    @model_validator(mode="after")
    def _reject_needs_reason(self) -> "Decision":
        if self.verdict == Verdict.REJECT:
            if self.reason_code is None:
                raise ValueError("a REJECT must have a reason_code")
            if not self.cites:
                raise ValueError("a REJECT must cite a declared constraint id")
        return self


# --- triage contracts (HANDOVER 18.3) ------------------------------------------------------
class DownstreamImpact(BaseModel):
    node_id: str
    edge_type: Literal["ROTATION", "CONNECTION"]
    depth: int
    slack_min: int | None = None
    gap_min: int | None = None
    propagated_delay_min: int
    pax_at_risk: int = 0
    late_night: bool = False


class ImpactAssessment(BaseModel):
    event_id: str
    tier: Literal[0, 1]
    tier_reason: str
    impact_score: float
    breakdown: dict[str, float]  # knock_on_min, pax_connections_at_risk, late_night_legs, flights_affected
    downstream: list[DownstreamImpact]
    input_delay_min: int
    decision_deadline: HHMM
    cluster_id: str
    graph_version: str
    policy_version: str


class QueueItem(BaseModel):
    rank: int
    event_id: str
    flight_no: str
    tier: int
    impact_score: float
    cluster_id: str
    decision_deadline: HHMM
    status: Literal["QUEUED", "NEGOTIATING", "AWAITING_SIGNOFF", "APPROVED", "REJECTED", "PREEMPTED"]


class TriggerQueue(BaseModel):
    queue_id: str
    airport: str | None
    date: str
    window: str
    items: list[QueueItem]
    graph_version: str
    policy_version: str


class Cluster(BaseModel):
    cluster_id: str
    event_ids: list[str]  # priority order
    reasons: list[str]


class Reservation(BaseModel):
    res_id: str
    resource_type: Literal["RUNWAY_SLOT", "GATE"]
    airport: str
    resource_id: str  # "RWY" or a gate id
    start: HHMM
    end: HHMM  # slot: start == end
    event_id: str
    status: Literal["HELD", "COMMITTED", "RELEASED"]


# --- outcome --------------------------------------------------------------------------------
class ConsensusRecord(BaseModel):
    event_id: str
    outcome: Literal["CONSENSUS", "FALLBACK"]
    package: dict
    turns_used: int
    decisions: list[Decision]
    checker_pass: bool | None
    delta_vs_history_min: int | None = None
    delta_vs_fallback_min: int | None = None  # ADDED
    status: Literal["AWAITING_SIGNOFF", "APPROVED", "REJECTED"] = "AWAITING_SIGNOFF"  # ADDED
    signed_off_by: str | None = None
    stamp: dict = Field(default_factory=dict)  # ADDED: policy, solver, checker, model, prompt hashes
    impact: ImpactAssessment | None = None  # ADDED (18.3)
    reservations: list[str] = Field(default_factory=list)  # ADDED (18.3): res_ids


class RuleResult(BaseModel):
    rule: ReasonCode
    passed: bool
    detail: str


class CheckResult(BaseModel):
    event_id: str
    package_id: str
    passed: bool  # overall: every rule passed
    rules: list[RuleResult]

    def failed(self) -> list[RuleResult]:
        return [r for r in self.rules if not r.passed]


class FallbackPlan(BaseModel):
    event_id: str
    slot: HHMM | None = None
    gate: str | None = None
    divert: bool = False
    alternate: str | None = None  # IATA code of the divert alternate
    alternate_distance_mi: float | None = None  # haversine from the destination, statute miles
    check: CheckResult | None = None  # None for a divert
    reason: str = ""


class SignOff(BaseModel):
    event_id: str
    approved: bool
    who: str
    note: str | None = None
    ts: str | None = None


LogWho = Literal[
    "coordinator", "builder", "atc", "pilot", "ground", "solver", "checker", "human", "orchestrator"
]
LogType = Literal[
    "STATE", "DisruptionEvent", "BROADCAST", "ConstraintPacket", "AUDIT", "ResolutionPackage",
    "SEND", "Decision", "CheckResult", "FallbackPlan", "ConsensusRecord", "SignOff",
    "OBJECTION", "DEFAULT", "ERROR",
    "TRIAGE", "ImpactAssessment", "TriggerQueue", "CLUSTER", "DISPATCH", "LEDGER", "PREEMPT",
    "SUMMARY",
]


class LogEntry(BaseModel):
    seq: int
    ts: str
    who: LogWho
    type: LogType
    state: str | None = None
    stage: int | None = Field(default=None, ge=0, le=5)
    turn: int | None = None
    text: str
    payload: dict = Field(default_factory=dict)
    prev_hash: str
    hash: str
    event_id: str | None = None  # ADDED: tags entries in multi-trigger streams
    dispatched_by: dict | None = None  # ADDED: {"seq": int, "hash": str} of the DISPATCH entry


# --- ledger view (HANDOVER 18.5) -------------------------------------------------------------
@runtime_checkable
class LedgerView(Protocol):
    def slot_taken(self, airport: str, t: int, exclude_event: str | None = None) -> bool: ...

    def gate_busy(
        self, airport: str, gate: str, start: int, end: int, exclude_event: str | None = None
    ) -> bool: ...


class EmptyLedgerView:
    """A ledger with no bookings: single-event runs and tests."""

    def slot_taken(self, airport: str, t: int, exclude_event: str | None = None) -> bool:
        return False

    def gate_busy(
        self, airport: str, gate: str, start: int, end: int, exclude_event: str | None = None
    ) -> bool:
        return False


# --- simulated private state (HANDOVER 6; int minutes) ------------------------------------------
class AtcPrivate(BaseModel):
    first: int
    free_slots: list[int]
    demand: dict[int, int]
    traffic: list[int]
    closures: list[tuple[int, int]]
    min_separation: int = 3


class PilotPrivate(BaseModel):
    land_by: int
    pax: int
    connect_by: int
    low_fuel: bool = False


class GroundPrivate(BaseModel):
    gates: list[str]
    assigned_gate: str
    free_from: dict[str, int | None]  # None = out of service
    failed: list[str]
    min_turn: int
    shift_end: int
    duty_limit: int
    next_departure: int


class PrivateState(BaseModel):
    atc: AtcPrivate
    pilot: PilotPrivate
    ground: GroundPrivate
