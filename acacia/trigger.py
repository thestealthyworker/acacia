"""Trigger payload adapter -> DisruptionEvent (HANDOVER 4.4).

Accepted shapes (detected by keys, extra keys ignored):
  row_ref   {"flight": {"airline", "flight_number"|"flight_no", "date", "origin"?, "dest"?}}
            or {"flight": "DL1639", "date": "2015-02-26"}
  raw row   {"row": {31 CSV fields}} or the 31 CSV fields at the top level
  explicit  any of the above plus "injected", "overrides", "declared_emergency", "stress"
  event     {"event": {full DisruptionEvent}}
  batch     {"triggers": [...]} or a list (to_events / to_triggers only)
  scan      {"source": "scan", "airport", "date", "window_min", "start"?} (to_events / to_triggers)
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from acacia import config
from acacia.data import FLIGHT_COLUMNS, DataStore, earliest_landing, in_window, num, qualifies_base
from acacia.models import DisruptionEvent, Injected, Stress
from acacia.timeutil import to_hhmm, to_min
from acacia.triangulate import triangulate

_FLIGHT_CODE_RE = re.compile(r"^([A-Z0-9]{2})(\d{1,5})$")
_LOCKED_FIELDS = {"event_id", "flight"}
STRESS_FLAGS = ("low_fuel", "tight_crew", "corrupt_solver", "declared_emergency")


class TriggerError(ValueError):
    """A payload that cannot become a valid, qualifying DisruptionEvent."""


# --- single payload ------------------------------------------------------------------------
def to_event(payload: Mapping[str, Any], data: DataStore, stress: Stress | None = None) -> DisruptionEvent:
    """One payload -> one validated DisruptionEvent. Raises TriggerError with a clear message."""
    if not isinstance(payload, Mapping):
        raise TriggerError(f"payload must be an object, got {type(payload).__name__}")
    if stress is not None and not isinstance(stress, Stress):
        raise TriggerError("stress must be a Stress model")
    has_injected = bool(payload.get("injected"))
    try:
        if isinstance(payload.get("event"), Mapping):
            base = DisruptionEvent.model_validate(payload["event"])
            row = None
        else:
            row = _resolve_row(payload, data)
            base = triangulate(row)
        event = _apply_overrides(base, payload)
    except ValidationError as exc:
        raise TriggerError(f"invalid trigger payload: {exc}") from exc
    except (LookupError, ValueError) as exc:
        if isinstance(exc, TriggerError):
            raise
        raise TriggerError(str(exc)) from exc
    _check_qualifies(row, event, has_injected)
    return event


def stress_for(payload: Mapping[str, Any], base: Stress | None = None) -> Stress:
    """Stress flags: base OR payload["stress"] OR top-level declared_emergency."""
    flags = (base or Stress()).model_dump()
    raw = payload.get("stress") if isinstance(payload, Mapping) else None
    if isinstance(raw, Mapping):
        for key in STRESS_FLAGS:
            flags[key] = flags[key] or bool(raw.get(key, False))
    if isinstance(payload, Mapping) and payload.get("declared_emergency"):
        flags["declared_emergency"] = True
    return Stress(**flags)


# --- many payloads ---------------------------------------------------------------------------
def to_triggers(
    payload: Any, data: DataStore, stress: Stress | None = None, skip_invalid: bool = False
) -> list[tuple[DisruptionEvent, Stress]]:
    """Expand batch/scan/list/single payloads into (event, stress) pairs."""
    if isinstance(payload, list):
        return _batch(payload, data, stress, skip_invalid)
    if not isinstance(payload, Mapping):
        raise TriggerError(f"payload must be an object or a list, got {type(payload).__name__}")
    if payload.get("source") == "scan":
        events = scan_events(data, payload)
        return [(e, stress_for(payload, stress)) for e in events]
    if isinstance(payload.get("triggers"), list):
        return _batch(payload["triggers"], data, stress_for(payload, stress), skip_invalid)
    return [(to_event(payload, data, stress), stress_for(payload, stress))]


def to_events(
    payload: Any, data: DataStore, stress: Stress | None = None, skip_invalid: bool = False
) -> list[DisruptionEvent]:
    return [event for event, _ in to_triggers(payload, data, stress, skip_invalid)]


def _batch(
    items: list, data: DataStore, stress: Stress | None, skip_invalid: bool
) -> list[tuple[DisruptionEvent, Stress]]:
    out: list[tuple[DisruptionEvent, Stress]] = []
    errors: list[str] = []
    for i, item in enumerate(items):
        try:
            out.extend(to_triggers(item, data, stress, skip_invalid))
        except TriggerError as exc:
            errors.append(f"trigger {i}: {exc}")
    if errors and not skip_invalid:
        raise TriggerError("; ".join(errors))
    return out


def scan_events(data: DataStore, payload: Mapping[str, Any]) -> list[DisruptionEvent]:
    """Every qualifying row landing at `airport` on `date` within [start, start + window).

    Without "start" the window opens at the earliest qualifying landing.
    """
    airport, date = payload.get("airport"), payload.get("date")
    if not airport or not date:
        raise TriggerError("scan payload needs airport and date")
    window = window_minutes(payload.get("window_min", payload.get("window", 180)))
    rows = [r.to_dict() for _, r in data.qualifying_rows(date=str(date), airport=str(airport)).iterrows()]
    timed = sorted(((earliest_landing(r), r) for r in rows), key=lambda p: p[0])
    if not timed:
        return []
    start = to_min(payload["start"]) if payload.get("start") else timed[0][0]
    picked = [triangulate(r) for eta, r in timed if start <= eta < start + window]
    return sorted(picked, key=lambda e: (to_min(e.eta_landing), e.event_id))


def window_minutes(raw: Any) -> int:
    """180, "180", "180m" or "3h" -> minutes."""
    text = str(raw).strip().lower()
    try:
        if text.endswith("h"):
            return int(float(text[:-1]) * 60)
        return int(float(text.removesuffix("min").removesuffix("m")))
    except ValueError as exc:
        raise TriggerError(f"invalid window {raw!r}") from exc


# --- helpers -----------------------------------------------------------------------------------
def _resolve_row(payload: Mapping[str, Any], data: DataStore) -> dict[str, str]:
    if isinstance(payload.get("row"), Mapping):
        return _raw_row(payload["row"])
    if "AIRLINE" in payload and "FLIGHT_NUMBER" in payload:
        return _raw_row(payload)
    flight = payload.get("flight")
    if flight is None:
        raise TriggerError("payload has no flight reference, raw row or event")
    airline, number, date, origin, dest = _flight_ref(flight, payload)
    return data.find_flight(airline, number, date, origin=origin, dest=dest)


def _raw_row(raw: Mapping[str, Any]) -> dict[str, str]:
    missing = [c for c in FLIGHT_COLUMNS if c not in raw]
    if missing:
        raise TriggerError(f"raw row is missing columns: {missing}")
    return {c: "" if raw[c] is None else str(raw[c]).strip() for c in FLIGHT_COLUMNS}


def _flight_ref(flight: Any, payload: Mapping[str, Any]) -> tuple[str, str, str, str | None, str | None]:
    if isinstance(flight, str):
        match = _FLIGHT_CODE_RE.match(flight.strip().upper())
        if match is None:
            raise TriggerError(f"invalid flight code {flight!r}")
        ref: Mapping[str, Any] = {"airline": match.group(1), "flight_number": match.group(2)}
        ref = {**payload, **ref}
    elif isinstance(flight, Mapping):
        ref = flight
    else:
        raise TriggerError("flight must be an object or a code like 'DL1639'")
    airline = ref.get("airline")
    number = ref.get("flight_number", ref.get("flight_no", ref.get("number")))
    date = ref.get("date", payload.get("date"))
    if not airline or number in (None, "") or not date:
        raise TriggerError("flight reference needs airline, flight_number and date")
    dest = ref.get("dest", ref.get("destination"))
    return str(airline).upper(), str(number), str(date), ref.get("origin"), dest


def _apply_overrides(event: DisruptionEvent, payload: Mapping[str, Any]) -> DisruptionEvent:
    merged = event.model_dump()
    recorded = list(event.overrides)
    overrides = payload.get("overrides") or {}
    if not isinstance(overrides, Mapping):
        raise TriggerError("overrides must be an object of DisruptionEvent fields")
    for key, value in overrides.items():
        if key in _LOCKED_FIELDS or key not in DisruptionEvent.model_fields or key == "overrides":
            raise TriggerError(f"field {key!r} cannot be overridden")
        merged[key] = value
        recorded.append(key)
    if payload.get("injected"):
        merged["injected"] = [Injected.model_validate(i).model_dump() for i in payload["injected"]]
        recorded.append("injected")
    if payload.get("declared_emergency"):
        recorded.append("declared_emergency")
    merged["overrides"] = list(dict.fromkeys(recorded))
    return DisruptionEvent.model_validate(merged)


def _check_qualifies(row: Mapping[str, Any] | None, event: DisruptionEvent, has_injected: bool) -> None:
    """Cancelled/diverted and the 01:00..21:30 window always apply; an explicit injected event
    waives only the delay-cause test (DECISIONS.md)."""
    code = f"{event.flight.code} {event.flight.date}"
    if row is not None:
        if num(row, "CANCELLED") or num(row, "DIVERTED"):
            raise TriggerError(f"{code} is cancelled or diverted; never a trigger")
        if not has_injected and not qualifies_base(row):
            raise TriggerError(
                f"{code} does not qualify: needs WEATHER_DELAY > 15 or LATE_AIRCRAFT_DELAY > 20 "
                f"(got {num(row, 'WEATHER_DELAY')}, {num(row, 'LATE_AIRCRAFT_DELAY')}); "
                "add an explicit injected event to force it"
            )
    elif not has_injected and not event.injected and not _causes_qualify(event):
        raise TriggerError(
            f"{code} does not qualify: needs weather > 15 or late_aircraft > 20 "
            "or an explicit injected event"
        )
    eta = to_min(event.eta_landing)
    if not in_window(eta):
        raise TriggerError(
            f"{code} earliest landing {to_hhmm(eta)} is outside the 01:00-21:30 MVP window"
        )


def _causes_qualify(event: DisruptionEvent) -> bool:
    return (
        event.causes.weather > config.WEATHER_THRESHOLD
        or event.causes.late_aircraft > config.LATE_AIRCRAFT_THRESHOLD
    )
