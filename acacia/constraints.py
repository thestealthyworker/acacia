"""Challenge operational constraints (CONSTRAINTS.md): the single access point for workbook limits.

Source: docs/challenge2_operational_constraints.xlsx, exported to data/challenge/*.csv.
No other module reads those CSVs or hard-codes these numbers.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from acacia import config
from acacia.timeutil import to_min

if TYPE_CHECKING:
    from acacia.models import DisruptionEvent

CHALLENGE_DIR: Path = config.DATA_DIR / "challenge"
FILES = ("airport_capacity.csv", "wake_separation.csv", "fuel_and_crew.csv", "rules_of_play.csv")

# --- stated assumptions (CONSTRAINTS.md; each one is a REGISTRY row) ----------------------------
ARRIVAL_SHARE = 0.5                      # CAP-2
STORM_FACTOR = {"none": 1.0, "moderate": 0.6, "severe": 0.4}  # CAP-3
SEVERE_WEATHER_DELAY_MIN = 45            # CAP-3: severe if WEATHER_DELAY > 45
APPROACH_SPEED_KT = 140.0                # WAKE-2
HEAVY_DISTANCE_MI = 2000                 # WAKE-3
REMOTE_STANDS = 9                        # GATE-2: REMOTE-1..REMOTE_STANDS
UNKNOWN_SLOT_WAKE_CLASS = "Heavy"        # WAKE-7: a held slot with no stored class (most restrictive)
TRAFFIC_CLASS_MIX = (("Heavy", 0.15), ("Large", 0.80), ("Small", 0.05))  # WAKE-3
BACKGROUND_LOAD = 0.55                   # CAP-7
WORLD_GATE_POOL = 8                      # CAP-4
FINAL_RESERVE_MIN = 30                   # FUEL-1
SPARE_HOLDING_MIN = 45                   # FUEL-2 (above final reserve)
CONTINGENCY_SHARE = 0.05                 # FUEL-3
BURN_KG_PER_H = {"narrowbody": 2400, "widebody": 6500}  # FUEL-4
FLIGHT_CREW_MAX_DUTY_MIN = 13 * 60       # CREW-1
REPORT_BEFORE_DEP_MIN = 60               # CREW-1 (ASSUMED)
GROUND_SHIFT_MIN = 8 * 60                # CREW-2
OVERTIME_COST_FACTOR = 1.5               # CREW-2
GROUND_MAX_OVERTIME_MIN = 45             # CREW-3
GROUND_OVERTIME_RED_LINE_MIN = 20        # CREW-3
# KIT-6: tight_crew stress sets shift_end = first offered slot + Ti + min_turn - this, so the
# earliest offered slot carries exactly this much crew overtime: over the red line (G_S1 reject),
# within the hard limit (no G_H2 discard).
TIGHT_CREW_SLACK_MIN = 25
GATE_CHANGE_NOTICE_MIN = 20              # GATE-1
DEFAULT_DECISION_LEAD_MIN = 30           # GATE-1: decision time = E - 30 when nothing injected
NEGOTIATION_BUDGET_MIN = 3               # TIME-1: 3 turns x 1 min of simulated negotiation time
SIGNOFF_BUDGET_MIN = 2                   # TIME-2: human sign-off time after the record is ready
SLOT_LEAD_MIN = 5                        # TIME-3: earliest landing slot = commit time + 5 min

Storm = Literal["none", "moderate", "severe"]


@dataclass(frozen=True)
class AirportLimits:
    iata: str
    size_class: str
    active_runways: int
    gates: int
    max_ops_per_hour: int
    min_turnaround_min: int
    spare_gates: int
    assumed: bool = False  # True when the airport is missing and Non-hub values are used


@dataclass(frozen=True)
class ConstraintItem:
    id: str
    sheet: str
    rule: str
    workbook_value: str
    used_value: str
    status: Literal["AS_GIVEN", "ASSUMED", "CHANGED", "NOT_MODELLED"]
    note: str = ""


def _read(name: str) -> list[dict[str, str]]:
    path = CHALLENGE_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"challenge constraint file missing: {path}")
    with path.open(newline="") as fh:
        return [row for row in csv.DictReader(fh) if any(v for v in row.values())]


@lru_cache(maxsize=1)
def _airports() -> dict[str, AirportLimits]:
    out: dict[str, AirportLimits] = {}
    for r in _read("airport_capacity.csv"):
        code = (r.get("IATA_CODE") or "").strip()
        if not code:
            continue
        out[code] = AirportLimits(
            iata=code, size_class=r["size_class"], active_runways=int(float(r["active_runways"])),
            gates=int(float(r["gates"])), max_ops_per_hour=int(float(r["max_runway_ops_per_hour"])),
            min_turnaround_min=int(float(r["min_turnaround_min"])),
            spare_gates=int(float(r["spare_gates_at_any_time"])),
        )
    return out


@lru_cache(maxsize=1)
def _nonhub_default() -> AirportLimits:
    for lim in _airports().values():
        if lim.size_class == "Non-hub":
            return lim
    raise ValueError("airport_capacity.csv has no Non-hub row for the unknown-airport default")


def airport_limits(iata: str) -> AirportLimits:
    """Workbook limits for an airport; unknown airports get the Non-hub values (ASSUMED)."""
    lim = _airports().get(iata)
    if lim is not None:
        return lim
    d = _nonhub_default()
    return AirportLimits(iata, d.size_class, d.active_runways, d.gates, d.max_ops_per_hour,
                         d.min_turnaround_min, d.spare_gates, assumed=True)


def min_turnaround(iata: str) -> int:
    return airport_limits(iata).min_turnaround_min


def storm_level(weather_delay_min: int) -> Storm:
    if weather_delay_min <= 15:
        return "none"
    return "severe" if weather_delay_min > SEVERE_WEATHER_DELAY_MIN else "moderate"


def arrival_rate(iata: str, storm: Storm = "none") -> float:
    """Arrivals per hour: max ops x arrival share x storm factor."""
    return airport_limits(iata).max_ops_per_hour * ARRIVAL_SHARE * STORM_FACTOR[storm]


def wake_class(distance_miles: int | float) -> str:
    return "Heavy" if distance_miles > HEAVY_DISTANCE_MI else "Large"


def _norm_class(label: str) -> str:
    return label.split("(")[0].strip()


@lru_cache(maxsize=1)
def _wake_table() -> tuple[dict[tuple[str, str], int], int]:
    table: dict[tuple[str, str], int] = {}
    default_nm = 3
    for r in _read("wake_separation.csv"):
        leader, follower = (r.get("leader") or "").strip(), (r.get("follower") or "").strip()
        nm = int(float(r["min_separation_nm"]))
        if leader.lower().startswith("any other"):
            default_nm = nm
        elif leader and follower:
            table[(_norm_class(leader), _norm_class(follower))] = nm
    return table, default_nm


def wake_nm(leader: str, follower: str) -> int:
    table, default_nm = _wake_table()
    return table.get((leader, follower), default_nm)


def wake_minutes(leader: str, follower: str) -> float:
    """Minimum arrival spacing in minutes at the assumed approach speed."""
    return wake_nm(leader, follower) * 60.0 / APPROACH_SPEED_KT


def burn_kg_per_h(wake: str) -> int:
    return BURN_KG_PER_H["widebody" if wake in ("Heavy", "Super") else "narrowbody"]


def contingency_min(air_time: int | None, scheduled_time: int | None = None) -> int:
    base = air_time if air_time else max(0, (scheduled_time or 0) - 20)
    return round(CONTINGENCY_SHARE * base)


def land_by(eta: int, air_time: int | None, scheduled_time: int | None = None) -> int:
    """Latest landing keeping the 30 min final reserve intact (CONSTRAINTS.md 4)."""
    return eta + SPARE_HOLDING_MIN + contingency_min(air_time, scheduled_time)


def holding_fuel_kg(slot: int, eta: int, wake: str) -> float:
    return round(max(0, slot - eta) * burn_kg_per_h(wake) / 60.0, 1)


def decision_time_min(event: DisruptionEvent) -> int:
    """Gate-change notice reference (GATE-1): the event's decision_time, else the earliest
    injected disruption start, else E - 30 (ASSUMED). Shared by solver inputs, agents and checker."""
    if event.decision_time:
        return to_min(event.decision_time)
    if event.injected:
        return min(to_min(i.start) for i in event.injected)
    return to_min(event.eta_landing) - DEFAULT_DECISION_LEAD_MIN


def time_budget_min() -> int:
    """Simulated minutes between the decision time and the commit time (TIME-1 + TIME-2)."""
    return NEGOTIATION_BUDGET_MIN + SIGNOFF_BUDGET_MIN


def commit_time_for(decision_time: int) -> int:
    """Commit time for a decision time in minutes: decision + negotiation + sign-off budget."""
    return decision_time + time_budget_min()


def commit_time_min(event: DisruptionEvent) -> int:
    """When the plan is approved and can execute (TIME-1, TIME-2): decision_time + 3 + 2 min.
    The clock no longer freezes at decision_time; slots and the gate-change notice count from here."""
    return commit_time_for(decision_time_min(event))


def earliest_slot_min(event: DisruptionEvent) -> int:
    """Earliest landing slot that can still be offered (TIME-3): commit_time + 5 min."""
    return commit_time_min(event) + SLOT_LEAD_MIN


def has_time_to_negotiate(event: DisruptionEvent) -> bool:
    """Deadline guard (TIME-5): E - decision_time must cover the whole time budget."""
    return to_min(event.eta_landing) - decision_time_min(event) >= time_budget_min()


def crew_duty_end(sched_arrival: int, scheduled_time: int | None) -> int | None:
    """Flight-crew duty end on the destination clock (CREW-1): report = scheduled departure - 60,
    where scheduled departure = scheduled arrival - SCHEDULED_TIME; None without a block time."""
    if not scheduled_time or scheduled_time <= 0:
        return None
    return sched_arrival - scheduled_time - REPORT_BEFORE_DEP_MIN + FLIGHT_CREW_MAX_DUTY_MIN


@lru_cache(maxsize=1)
def constraints_version() -> str:
    h = hashlib.sha256()
    for name in FILES:
        h.update((CHALLENGE_DIR / name).read_bytes())
    return h.hexdigest()[:12]


REGISTRY: tuple[ConstraintItem, ...] = (
    ConstraintItem("CAP-1", "airport_capacity", "Runway movements per hour", "per airport (ATL 90)", "as given", "AS_GIVEN"),
    ConstraintItem("CAP-2", "airport_capacity", "Arrival share of movements", "not stated", "50%", "ASSUMED"),
    ConstraintItem("CAP-3", "airport_capacity", "Storm capacity factor", "not stated",
                   "x0.6 moderate / x0.4 severe; severe if WEATHER_DELAY > 45", "ASSUMED"),
    ConstraintItem("CAP-4", "airport_capacity", "Gates", "per airport (ATL 150)", "modelled pool of 8 per airport",
                   "CHANGED", "tractable model; spare-gate rule still applied"),
    ConstraintItem("CAP-5", "airport_capacity", "Spare gates at any time", "per airport (ATL 3)",
                   "Ground options = assigned + spare", "AS_GIVEN"),
    ConstraintItem("CAP-6", "airport_capacity", "Minimum turnaround", "per airport (ATL 45)",
                   "as given everywhere, incl. impact-graph slack", "AS_GIVEN"),
    ConstraintItem("CAP-7", "airport_capacity", "Background runway load", "not stated", "55% of capacity points", "ASSUMED"),
    ConstraintItem("WAKE-1", "wake_separation", "Arrival wake separation", "nm by leader/follower", "as given", "AS_GIVEN"),
    ConstraintItem("WAKE-2", "wake_separation", "nm to minutes", "not stated", "140 kt (3 nm = 1.3 min)", "ASSUMED"),
    ConstraintItem("WAKE-3", "wake_separation", "Aircraft wake class", "not in flights.csv",
                   "Heavy if distance > 2000 mi else Large; traffic 15/80/5 Heavy/Large/Small", "ASSUMED"),
    ConstraintItem("WAKE-4", "wake_separation", "Same-runway scope", "per runway",
                   "applied across the merged arrival stream", "CHANGED", "conservative"),
    ConstraintItem("WAKE-5", "wake_separation", "Super (A380) pairs", "6-8 nm", "no A380 in data", "NOT_MODELLED"),
    ConstraintItem("WAKE-6", "wake_separation", "Departure min_time_s", "60-180 s", "arrivals only", "NOT_MODELLED"),
    ConstraintItem("FUEL-1", "fuel_and_crew", "Final fuel reserve", "30 min holding", "never planned below", "AS_GIVEN"),
    ConstraintItem("FUEL-2", "fuel_and_crew", "Spare fuel on arrival", "45 min holding",
                   "45 min above the final reserve", "ASSUMED", "interpretation of 'spare'"),
    ConstraintItem("FUEL-3", "fuel_and_crew", "Contingency fuel", "5% of trip fuel", "5% of AIR_TIME in minutes", "ASSUMED"),
    ConstraintItem("FUEL-4", "fuel_and_crew", "Holding fuel burn", "2,400 / 6,500 kg/h",
                   "as given; widebody = Heavy class", "AS_GIVEN"),
    ConstraintItem("CREW-1", "fuel_and_crew", "Flight crew max duty", "13 h from report",
                   "report = scheduled departure - 60 of this leg (SCHEDULED_ARRIVAL - SCHEDULED_TIME, "
                   "destination clock); declared and checked only when the end falls inside the day",
                   "ASSUMED", "earlier legs of the crew's day are not in the data"),
    ConstraintItem("CREW-2", "fuel_and_crew", "Ground crew shift", "8 h; overtime 1.5x", "as given", "AS_GIVEN"),
    ConstraintItem("CREW-3", "fuel_and_crew", "Ground max overtime", "not stated",
                   "45 min hard; reject over 20 min (red line)", "ASSUMED"),
    ConstraintItem("GATE-1", "fuel_and_crew", "Gate-change notice", ">= 20 min before arrival",
                   "HARD; decision time = disruption start or E - 30; notice counted from the commit time "
                   "(TIME-4)", "ASSUMED", "limit as given, decision time assumed"),
    ConstraintItem("GATE-2", "fuel_and_crew", "Remote stands", "not stated",
                   "REMOTE-1..9, each one bookable stand; a remote stand counts as a gate change",
                   "ASSUMED"),
    ConstraintItem("WAKE-7", "wake_separation", "Wake class of other negotiated flights", "not stated",
                   "the class stored on the held runway slot (wake_class(distance)); Heavy when none is stored",
                   "ASSUMED", "unknown class read as Heavy, the most restrictive class produced"),
    ConstraintItem("CAP-8", "airport_capacity", "Storm severity when not given", "not stated",
                   "moderate (x0.6) unless the injected event says severe", "ASSUMED"),
    ConstraintItem("FUEL-5", "fuel_and_crew", "Fuel utility span", "not stated",
                   "(land_by - slot) / (land_by - E); 45 min when E is not declared", "ASSUMED"),
    ConstraintItem("PLAY-1", "rules_of_play", "Qualifying input", "WEATHER_DELAY > 15 or LATE_AIRCRAFT_DELAY > 20",
                   "as given, plus earliest landing 01:00-21:30; an explicit injected event waives the "
                   "delay-cause test", "CHANGED", "no midnight crossing in the MVP; injected events force a trigger"),
    ConstraintItem("PLAY-2", "rules_of_play", "Turn and 3-turn limit", "3 turns", "turn 1 declare, turns 2-3 vote", "AS_GIVEN"),
    ConstraintItem("PLAY-3", "rules_of_play", "Fallback logs why", "required", "reason in FallbackPlan and log", "AS_GIVEN"),
    ConstraintItem("KIT-1", "kit", "Private agent state", "not given",
                   "slots, pax, gates, crew shift simulated with a fixed seed", "ASSUMED", "labelled simulated"),
    ConstraintItem("KIT-2", "kit", "Minimum connection time", "not given", "35 min", "ASSUMED"),
    ConstraintItem("FUEL-6", "fuel_and_crew", "Holding fuel on a divert", "not stated",
                   "not modelled: the aircraft diverts at once, holding_fuel_kg shown as none", "ASSUMED"),
    ConstraintItem("KIT-3", "kit", "Fallback gate choice", "kit: earliest free gate",
                   "assigned gate first, then earliest free gate, then a remote stand; if no gate meets "
                   "the 20 min notice at the earliest slot, the next free slot <= land_by is used", "CHANGED",
                   "keeping the assigned gate needs no 20 min gate-change notice"),
    ConstraintItem("KIT-4", "kit", "Ground timeout default", "kit: every gate unavailable except REMOTE-1",
                   "as kit; REMOTE-1 counts as a gate change needing 20 min notice; workbook turnaround",
                   "ASSUMED", "most restrictive reading"),
    ConstraintItem("KIT-5", "kit", "ATC arrival queue before the first slot", "not stated",
                   "round(0.6 x WEATHER_DELAY) min when WEATHER_DELAY > 15, else seeded 3-8 min",
                   "ASSUMED", "long weather delays push the first slot past land_by, so they divert"),
    ConstraintItem("KIT-6", "kit", "Tight-crew stress shift end", "kit: first + Ti + min_turn - 6",
                   f"first offered slot + Ti + min_turn - {TIGHT_CREW_SLACK_MIN} "
                   f"({TIGHT_CREW_SLACK_MIN} min overtime at the earliest slot: over the "
                   f"{GROUND_OVERTIME_RED_LINE_MIN} min red line, within the {GROUND_MAX_OVERTIME_MIN} min hard limit)",
                   "ASSUMED", "demo stress lever for scenario D; the kit's 6 min gave about 7 min overtime, "
                   "under the red line, and drifted when the first slot moved"),
    ConstraintItem("TIME-1", "rules_of_play", "Negotiation time budget", "not stated",
                   "3 min of simulated time (3 turns x 1 min); the clock no longer freezes at the decision time",
                   "ASSUMED", "worst case live: about 9 AI calls x 20 s plus one retry each"),
    ConstraintItem("TIME-2", "rules_of_play", "Human sign-off time", "not stated",
                   "2 min; commit time = decision time + 3 + 2", "ASSUMED"),
    ConstraintItem("TIME-3", "rules_of_play", "Slot lead after commit", "not stated",
                   "landing slots at or after commit time + 5 min (ATC, solver, checker, fallback)", "ASSUMED",
                   "an approved plan never offers a slot already in the past"),
    ConstraintItem("TIME-4", "fuel_and_crew", "Gate-change notice reference", "before arrival",
                   "measured from the commit time, not the decision time", "ASSUMED",
                   "notice is only real once the plan is approved"),
    ConstraintItem("TIME-5", "rules_of_play", "Deadline guard", "not stated",
                   "E - decision time < 5 min budget: no negotiation, straight to the checked fallback",
                   "ASSUMED"),
    ConstraintItem("TIME-6", "rules_of_play", "Shared clock across triggers", "not stated",
                   "each dispatched event's decision time >= the previous dispatched event's commit time",
                   "ASSUMED", "later triggers see later decision times"),
    ConstraintItem("TIME-7", "rules_of_play", "Live wall-clock overshoot", "not stated",
                   "LLM mode only: a negotiation longer than 3 min of real time falls back with the reason "
                   "logged; wall time is logged outside the hash chain", "ASSUMED",
                   "rule and replay runs stay deterministic"),
)
