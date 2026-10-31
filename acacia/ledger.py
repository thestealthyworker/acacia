"""Booking ledger for runway slots and gates (FABLE_ADDENDUM 7, HANDOVER 18.5).

The single source of truth for bookings. Only the dispatcher writes to it. State lives in
memory and is appended, one line per change, to `runs/ledger-{date}.jsonl`; an existing file
is replayed on startup. Gate intervals are half-open [start, end), so back-to-back use of one
gate is allowed. A hold never silently overwrites: any conflict raises `DoubleBookingError`
and nothing is written.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from acacia import config
from acacia.models import LedgerView, Reservation
from acacia.timeutil import to_hhmm, to_min

ACTIVE = ("HELD", "COMMITTED")


class DoubleBookingError(ValueError):
    """The slot or gate interval is already HELD or COMMITTED by another event."""


class LedgerStateError(ValueError):
    """An invalid reservation transition (for example commit of a released event)."""


def _is_active(res: Reservation) -> bool:
    return res.status in ACTIVE


def _gate_overlaps(res: Reservation, start: int, end: int) -> bool:
    return to_min(res.start) < end and start < to_min(res.end)


class _View:
    """Read-only view over a reservation list (frozen copy or the live list)."""

    def __init__(self, rows: list[Reservation]) -> None:
        self._rows = rows

    def _active(self, airport: str, kind: str, exclude_event: str | None) -> list[Reservation]:
        return [
            r for r in self._rows
            if _is_active(r) and r.airport == airport and r.resource_type == kind
            and r.event_id != exclude_event
        ]

    def slot_taken(self, airport: str, t: int, exclude_event: str | None = None) -> bool:
        return any(to_min(r.start) == t for r in self._active(airport, "RUNWAY_SLOT", exclude_event))

    def gate_busy(
        self, airport: str, gate: str, start: int, end: int, exclude_event: str | None = None
    ) -> bool:
        return any(
            r.resource_id == gate and _gate_overlaps(r, start, end)
            for r in self._active(airport, "GATE", exclude_event)
        )


class Ledger:
    """Append-only booking ledger for one date."""

    def __init__(self, date: str, runs_dir: Path | None = None) -> None:
        self.date = date
        self._dir = Path(runs_dir) if runs_dir is not None else config.RUNS_DIR
        self.path = self._dir / f"ledger-{date}.jsonl"
        self._rows: list[Reservation] = []
        self._replay()

    # --- persistence ------------------------------------------------------------------------
    def _replay(self) -> None:
        if not self.path.exists():
            return
        by_id: dict[str, int] = {}
        for lineno, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                res = Reservation.model_validate(record["reservation"])
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{self.path}:{lineno}: bad ledger line: {exc}") from exc
            if res.res_id in by_id:
                self._rows[by_id[res.res_id]] = res
            else:
                by_id[res.res_id] = len(self._rows)
                self._rows.append(res)

    def _append(self, op: str, res: Reservation) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        line = {"ts": datetime.now(timezone.utc).isoformat(), "op": op, "reservation": res.model_dump()}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")

    # --- views ------------------------------------------------------------------------------
    def view(self) -> LedgerView:
        """Frozen snapshot: later holds are invisible to it."""
        return _View([r.model_copy() for r in self._rows])

    def live(self) -> LedgerView:
        """Always-current view (the checker uses this)."""
        return _View(self._rows)

    def reservations_for(self, event_id: str) -> list[Reservation]:
        return [r.model_copy() for r in self._rows if r.event_id == event_id]

    def all_reservations(self) -> list[Reservation]:
        return [r.model_copy() for r in self._rows]

    # --- writes -----------------------------------------------------------------------------
    def hold(
        self, event_id: str, airport: str, slot: int, gate: str, gate_start: int, gate_end: int
    ) -> list[Reservation]:
        """HELD runway slot at `slot` and gate [gate_start, gate_end). Raises on conflict."""
        self._validate_hold(event_id, airport, slot, gate, gate_start, gate_end)
        if any(_is_active(r) for r in self._rows if r.event_id == event_id):
            raise LedgerStateError(f"{event_id} already holds reservations; release first")
        live = self.live()
        if live.slot_taken(airport, slot, exclude_event=event_id):
            raise DoubleBookingError(f"slot {airport} {to_hhmm(slot)} is already booked")
        if live.gate_busy(airport, gate, gate_start, gate_end, exclude_event=event_id):
            raise DoubleBookingError(
                f"gate {airport} {gate} {to_hhmm(gate_start)}-{to_hhmm(gate_end)} overlaps a booking"
            )
        end_wire = min(gate_end, config.DAY_END)  # HH:MM cannot express 24:00
        created = [
            self._add(event_id, "RUNWAY_SLOT", airport, "RWY", slot, slot),
            self._add(event_id, "GATE", airport, gate, gate_start, end_wire),
        ]
        return [r.model_copy() for r in created]

    def _add(self, event_id: str, kind: str, airport: str, rid: str, start: int, end: int) -> Reservation:
        """Create a HELD reservation with the next sequence number, store and persist it."""
        seq = len(self._rows) + 1
        res = Reservation(
            res_id=f"R-{event_id}-{kind}-{seq}", resource_type=kind, airport=airport,
            resource_id=rid, start=to_hhmm(start), end=to_hhmm(end), event_id=event_id, status="HELD",
        )
        self._rows.append(res)
        self._append("hold", res)
        return res

    @staticmethod
    def _validate_hold(event_id: str, airport: str, slot: int, gate: str, start: int, end: int) -> None:
        if not event_id or not airport or not gate:
            raise ValueError("event_id, airport and gate must be non-empty")
        for name, value in (("slot", slot), ("gate_start", start)):
            if not 0 <= value <= config.DAY_END:
                raise ValueError(f"{name} must be within the day (0..{config.DAY_END}), got {value}")
        if not start < end <= config.MINUTES_PER_DAY:
            raise ValueError(f"gate interval must satisfy start < end <= 1440, got [{start}, {end})")

    def _transition(self, event_id: str, from_status: str, to_status: str, op: str) -> None:
        targets = [r for r in self._rows if r.event_id == event_id and r.status == from_status]
        if not targets:
            raise LedgerStateError(f"{event_id}: no {from_status} reservations to {op}")
        for res in targets:
            res.status = to_status
            self._append(op, res)

    def commit(self, event_id: str) -> None:
        """HELD -> COMMITTED (human approve)."""
        self._transition(event_id, "HELD", "COMMITTED", "commit")

    def release(self, event_id: str) -> None:
        """HELD -> RELEASED (reject or pre-emption)."""
        self._transition(event_id, "HELD", "RELEASED", "release")

    def cancel(self, event_id: str) -> None:
        """COMMITTED -> RELEASED (explicit cancel; unused in the MVP)."""
        self._transition(event_id, "COMMITTED", "RELEASED", "cancel")
