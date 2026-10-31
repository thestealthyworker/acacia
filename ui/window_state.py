"""Airport-window session logic for the UI (HANDOVER 13 multi-trigger additions, FABLE_ADDENDUM 8).

No Streamlit here: building the data source (the SYNTHETIC fixture day when data/flights.csv is
absent), parsing a trigger payload list, running the Dispatcher, and the read-only rows the
window panels render (queue, impact components, capacity line, ledger). The dispatcher owns
every decision; this module only reads its state and forwards sign-off.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acacia import config, constraints
from acacia.agents.factory import AgentModeUnavailable, make_agents
from acacia.data import DataStore
from acacia.dispatcher import Dispatcher, DispatcherError
from acacia.ledger import Ledger
from acacia.log import verify_chain
from acacia.models import DisruptionEvent, ImpactAssessment, LogEntry, Stress, TriggerQueue
from acacia.trigger import TriggerError
from ui.components import storm_of

logger = logging.getLogger(__name__)

FIXTURE_PATH = config.ROOT_DIR / "tests" / "fixtures" / "multi_trigger_day.csv"
FIXTURE_AIRPORTS = config.DATA_DIR / "sample_airports.csv"
FIXTURE_DATE, FIXTURE_AIRPORT = "2015-03-12", "ATL"
DATA_DEFAULT_DATE = "2015-02-26"
DEFAULT_WINDOW = 180
SOURCE_FIXTURE, SOURCE_DATA = "fixture", "data"
IMPACT_PARTS = (  # breakdown key, label, css colour token
    ("knock_on_min", "knock-on min", "var(--ground)"),
    ("pax_connections_at_risk", "connection pax", "var(--pilot)"),
    ("late_night_legs", "late-night legs", "var(--bad)"),
    ("flights_affected", "flights affected", "var(--atc)"),
)


class WindowInputError(ValueError):
    """The window controls or the payload list could not be turned into triggers."""


# --- data source ------------------------------------------------------------------------------
def has_full_flights() -> bool:
    try:
        return (config.DATA_DIR / "flights.csv").is_file()
    except OSError:
        return False


def default_source() -> str:
    """The synthetic fixture day until the full flights.csv is added (HANDOVER 13, PLAN 1)."""
    return SOURCE_DATA if has_full_flights() else SOURCE_FIXTURE


def default_date(source: str) -> str:
    return FIXTURE_DATE if source == SOURCE_FIXTURE else DATA_DEFAULT_DATE


def load_store(source: str) -> DataStore:
    if source == SOURCE_FIXTURE:
        return DataStore(config.DATA_DIR, flights_path=FIXTURE_PATH, airports_path=FIXTURE_AIRPORTS)
    if source == SOURCE_DATA:
        return DataStore()
    raise WindowInputError(f"unknown data source {source!r}")


def source_label(source: str, data: DataStore) -> str:
    if source == SOURCE_FIXTURE:
        return f"synthetic fixture day ({FIXTURE_PATH.name}, fictional tails)"
    return data.flights_path.name


# --- payloads ---------------------------------------------------------------------------------
def parse_payloads(text: str) -> tuple[list[dict], tuple[str, str, int] | None]:
    """A JSON trigger list -> (single payloads, scan). Accepts a list, {"triggers": [...]}, a
    {"source": "scan", ...} object or one payload object (HANDOVER 4.4 shapes)."""
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WindowInputError(f"payload list is not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict) and raw.get("source") == "scan":
        try:
            return [], (str(raw["airport"]).upper(), str(raw["date"]), int(raw.get("window_min", DEFAULT_WINDOW)))
        except (KeyError, TypeError, ValueError) as exc:
            raise WindowInputError(f"scan payload needs airport, date and window_min: {exc}") from exc
    elif isinstance(raw, dict) and isinstance(raw.get("triggers"), list):
        items = raw["triggers"]
    elif isinstance(raw, dict):
        items = [raw]
    else:
        raise WindowInputError("payload list must be a JSON list or object")
    bad = [i for i, p in enumerate(items) if not isinstance(p, dict)]
    if bad or not items:
        raise WindowInputError(f"every trigger must be a JSON object (bad index {bad})" if bad else "no triggers")
    return [dict(p) for p in items], None


def example_payloads(source: str) -> str:
    """A ready-to-run payload list for the chosen source."""
    if source == SOURCE_FIXTURE:
        items = [{"source": "row_ref", "flight": f"DL{n}", "date": FIXTURE_DATE} for n in (201, 202, 203, 204, 205)]
    else:
        items = [{"source": "row_ref", "flight": "DL1639", "date": DATA_DEFAULT_DATE}]
    return json.dumps(items, indent=1)


# --- run --------------------------------------------------------------------------------------
@dataclass
class WindowRun:
    dispatcher: Dispatcher
    source: str
    label: str
    synthetic: bool
    mode: str
    entries: list[LogEntry] = field(default_factory=list)  # everything run() yielded
    done: bool = False
    error: str | None = None
    preempt_lowest: bool = False
    thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


def _merged(a: Stress, b: Stress) -> Stress:
    return Stress(**{k: bool(v or getattr(b, k)) for k, v in a.model_dump().items()})


def prepare(data: DataStore, source: str, mode: str, stress: Stress, *, scan: tuple[str, str, int] | None = None,
            payloads: list[dict] | None = None, preempt_lowest: bool = False) -> WindowRun:
    """Collect the triggers and apply the header stress to every one; nothing is negotiated yet."""
    try:
        agents = make_agents(mode.lower())
    except (AgentModeUnavailable, ValueError) as exc:
        raise WindowInputError(f"{mode} agents unavailable: {exc}") from exc
    date = scan[1] if scan else _payload_date(payloads)
    ledger = Ledger(date, runs_dir=Path(tempfile.mkdtemp(prefix="acacia-window-")))  # fresh: no holds
    d = Dispatcher(data, agents, ledger, config.IMPACT_POLICY)
    try:
        events = d.collect(payloads=payloads, scan=scan)
    except (TriggerError, DispatcherError, LookupError, ValueError) as exc:
        raise WindowInputError(str(exc)) from exc
    if not events:
        where = f"{scan[0]} on {scan[1]} within {scan[2]} min" if scan else "the payload list"
        raise WindowInputError(f"no qualifying triggers in {where}")
    for e in events:
        d.preempt(e.event_id, _merged(d.stress.get(e.event_id, Stress()), stress))  # before triage: stress only
    return WindowRun(dispatcher=d, source=source, label=source_label(source, data),
                     synthetic=source == SOURCE_FIXTURE, mode=mode, preempt_lowest=preempt_lowest)


def _payload_date(payloads: list[dict] | None) -> str:
    """The ledger date: the first payload's date (top level or inside "flight")."""
    for p in payloads or []:
        flight = p.get("flight")
        date = p.get("date") or (flight.get("date") if isinstance(flight, dict) else None)
        if date:
            return str(date)
    return FIXTURE_DATE


def drain(run: WindowRun) -> None:
    """Run the dispatcher to DRAINED; scenario F flags low fuel on the lowest-ranked trigger
    right after the first DISPATCH (HANDOVER 12 F). Errors end the run visibly."""
    d, lowest, fired = run.dispatcher, None, False
    try:
        for entry in d.run():
            run.entries.append(entry)
            if entry.type == "TriggerQueue":
                lowest = TriggerQueue.model_validate(entry.payload).items[-1].event_id
            if run.preempt_lowest and not fired and entry.type == "DISPATCH" and lowest and entry.event_id != lowest:
                d.preempt(lowest, _merged(d.stress[lowest], Stress(low_fuel=True)))
                fired = True
    except Exception as exc:  # noqa: BLE001 - surfaced in the UI, never swallowed
        logger.exception("window run failed")
        run.error = f"window run failed: {type(exc).__name__}: {exc}"
    finally:
        run.done = True


def start(run: WindowRun) -> WindowRun:
    """Rule and Replay drain at once; LLM drains in a daemon thread the UI polls."""
    if run.mode == "LLM":
        run.thread = threading.Thread(target=drain, args=(run,), daemon=True, name="acacia-llm-window")
        run.thread.start()
    else:
        drain(run)
    return run


# --- read-only rows ---------------------------------------------------------------------------
def queue(run: WindowRun) -> TriggerQueue | None:
    d = run.dispatcher
    if not d.assessments or d.graph is None:
        return None
    return d.queue()


def impact_parts(a: ImpactAssessment, policy: dict[str, float] | None = None) -> list[tuple[str, str, float, float]]:
    """(key, label, raw value, weighted score) per breakdown component; weighted sums to the score."""
    w = policy or config.IMPACT_POLICY
    return [(k, label, float(a.breakdown.get(k, 0.0)), float(a.breakdown.get(k, 0.0)) * w[k])
            for k, label, _ in IMPACT_PARTS]


def capacity_line(event: DisruptionEvent) -> str:
    """The trigger airport's workbook capacity and storm factor (CAP-1..3), e.g.
    "ATL 90 mvts/h · 45 arr/h · storm moderate ×0.6 → 27 arr/h"."""
    dest = event.flight.dest
    lim = constraints.airport_limits(dest)
    storm = storm_of(event)
    nominal = constraints.arrival_rate(dest, "none")
    base = f"{dest} {lim.max_ops_per_hour} mvts/h · {nominal:g} arr/h"
    if lim.assumed:
        base += " (Non-hub, ASSUMED)"
    if storm == "none":
        return f"{base} · no storm ×{constraints.STORM_FACTOR['none']:g}"
    return f"{base} · storm {storm} ×{constraints.STORM_FACTOR[storm]:g} → {constraints.arrival_rate(dest, storm):g} arr/h"


def event_entries(run: WindowRun, event_id: str) -> list[LogEntry]:
    """The event's latest negotiation attempt (a pre-empted event has earlier segments)."""
    segments = run.dispatcher.log_segments(event_id)
    return segments[-1] if segments else []


def orchestrator_entries(run: WindowRun) -> list[LogEntry]:
    return list(run.dispatcher.entries)


def chain_ok(entries: list[LogEntry]) -> bool:
    ok, _ = verify_chain(entries)
    return ok


def ledger_rows(run: WindowRun) -> list[dict[str, str]]:
    d = run.dispatcher
    return [{"flight": d.events[r.event_id].flight.code if r.event_id in d.events else r.event_id,
             "type": r.resource_type, "airport": r.airport, "resource": r.resource_id, "start": r.start,
             "end": r.end, "status": r.status, "res_id": r.res_id, "event_id": r.event_id}
            for r in d.ledger.all_reservations()]


def summary(run: WindowRun) -> dict | None:
    last = next((e for e in reversed(run.dispatcher.entries) if e.type == "SUMMARY"), None)
    return last.payload if last else None


def sign_off(run: WindowRun, event_id: str, approve: bool, who: str) -> str | None:
    """Forward a human decision to the dispatcher (commits or releases the holds); error text or None."""
    if not run.done:
        return "wait for the window run to finish before signing off"
    if not who.strip():
        return "enter who is signing off"
    try:
        run.dispatcher.sign_off(event_id, approve, who)
    except (DispatcherError, ValueError) as exc:
        return str(exc)
    return None
