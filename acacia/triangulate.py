"""Stage 0: turn one flight row into a DisruptionEvent (HANDOVER 4.3)."""

from __future__ import annotations

import hashlib

from acacia import config, constraints
from acacia.data import Row, earliest_landing, num, row_date, taxi_in
from acacia.models import Causes, DisruptionEvent, FlightRef, Injected, ScenarioType
from acacia.timeutil import parse_hhmm, to_hhmm

RUNWAY_REF = "storm ({level}): arrival capacity x{factor:g}"  # CONSTRAINTS.md 2
EQUIPMENT_REF = "jet bridge out at one gate"


def classify(weather: int, late_aircraft: int) -> ScenarioType:
    """COMBINED if W > 15 and L > 20; WEATHER if W > 15; else KNOCK_ON."""
    is_weather = weather > config.WEATHER_THRESHOLD
    if is_weather and late_aircraft > config.LATE_AIRCRAFT_THRESHOLD:
        return ScenarioType.COMBINED
    return ScenarioType.WEATHER if is_weather else ScenarioType.KNOCK_ON


def make_event_id(airline: str, flight_no: str, date: str, origin: str, dest: str) -> str:
    """"EVT-" + first 8 hex chars (upper case) of sha256("{airline}{flight_no}-{date}-{origin}{dest}")."""
    key = f"{airline}{flight_no}-{date}-{origin}{dest}"
    return "EVT-" + hashlib.sha256(key.encode()).hexdigest()[:8].upper()


def _clamp_day(t: int) -> int:
    return max(0, min(config.DAY_END, t))


def injected_for(event_id: str, eta: int, weather: int) -> list[Injected]:
    """RUNWAY_CLOSED storm window when W > 15 (end seeded rand 0..10), else EQUIPMENT_FAILURE.

    The storm keeps type RUNWAY_CLOSED and carries its severity (CONSTRAINTS.md 2)."""
    severity = None
    if weather > config.WEATHER_THRESHOLD:
        extra = config.seeded_rng(config.seed_key(event_id, "event")).randint(0, 10)
        severity = constraints.storm_level(weather)
        ref = RUNWAY_REF.format(level=severity, factor=constraints.STORM_FACTOR[severity])
        start, end, kind = eta - 10, eta + weather + extra, "RUNWAY_CLOSED"
    else:
        start, end, kind, ref = eta - 20, eta + 180, "EQUIPMENT_FAILURE", EQUIPMENT_REF
    return [Injected(type=kind, ref=ref, start=to_hhmm(_clamp_day(start)), end=to_hhmm(_clamp_day(end)),
                     severity=severity)]


def flight_ref(row: Row) -> FlightRef:
    airline = str(row.get("AIRLINE", "")).strip().upper()
    number = str(num(row, "FLIGHT_NUMBER"))
    tail = str(row.get("TAIL_NUMBER") or "").strip() or "UNKNOWN"
    return FlightRef(
        airline=airline, flight_no=number, tail=tail,
        origin=str(row.get("ORIGIN_AIRPORT", "")).strip().upper(),
        dest=str(row.get("DESTINATION_AIRPORT", "")).strip().upper(),
        date=row_date(row),
    )


def triangulate(row: Row) -> DisruptionEvent:
    """Build the DisruptionEvent for a flight row. Raises ValueError if scheduled arrival is blank."""
    sched = parse_hhmm(row.get("SCHEDULED_ARRIVAL"))
    eta = earliest_landing(row)
    if sched is None or eta is None:
        raise ValueError("row has no SCHEDULED_ARRIVAL; cannot triangulate")
    flight = flight_ref(row)
    delay = num(row, "ARRIVAL_DELAY")
    actual = parse_hhmm(row.get("ARRIVAL_TIME"))
    causes = Causes(
        weather=num(row, "WEATHER_DELAY"), late_aircraft=num(row, "LATE_AIRCRAFT_DELAY"),
        air_system=num(row, "AIR_SYSTEM_DELAY"), airline=num(row, "AIRLINE_DELAY"),
        security=num(row, "SECURITY_DELAY"),
    )
    event_id = make_event_id(flight.airline, flight.flight_no, flight.date, flight.origin, flight.dest)
    return DisruptionEvent(
        event_id=event_id,
        scenario_type=classify(causes.weather, causes.late_aircraft),
        flight=flight,
        sched_arrival=to_hhmm(sched),
        hist_arrival=to_hhmm(actual if actual is not None else sched + delay),
        hist_delay_min=delay,
        eta_landing=to_hhmm(eta),
        taxi_in=taxi_in(row),
        distance=num(row, "DISTANCE"),
        causes=causes,
        injected=injected_for(event_id, eta, causes.weather),
        air_time=_positive(row, "AIR_TIME"),
        scheduled_time=_positive(row, "SCHEDULED_TIME"),
    )


def _positive(row: Row, column: str) -> int | None:
    """A positive whole-minute column value, else None (blank or zero)."""
    value = num(row, column)
    return value if value > 0 else None
