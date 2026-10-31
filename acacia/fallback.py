"""Deterministic rule-based fallback plan (HANDOVER 10). No AI involved.

1. Slot: the earliest ATC free slot (first-come-first-served). When the world is given, only
   slots still wake-separated on the live runway count (`world.is_separated`, CONSTRAINTS.md 2).
2. If there is none, or it is past land_by: DIVERT to the nearest airport by haversine
   distance from the destination, excluding the destination, the origin and airports with
   blank coordinates.
3. Otherwise gate: the assigned gate when it is clear (no gate change, so no notice needed),
   else the non-failed gate with the earliest free time that is free by the gate arrival
   time, else a remote stand. When the world is given, a gate must be clear for the
   full turnaround [gate arrival, gate arrival + min_turn) (PLAN 2.A.1). When a ledger view is
   given, the remote stand is the first REMOTE-k (k = 1..REMOTE_STANDS) not booked for that
   interval, so two fallback plans never share one stand.
4. Gate-change notice (CONSTRAINTS.md 3, HARD): a gate other than the assigned one, remote
   stands included, needs gate arrival - decision time >= 20 min. When the earliest slot has
   no gate that meets it, the next ATC free slot (still <= land_by) is tried; if none does,
   the earliest slot is kept and the reason says so (the checker then fails the plan).

The coordinator runs the checker on a non-divert plan; this module never imports it.
"""

from __future__ import annotations

import math

from typing import TYPE_CHECKING

import pandas as pd

from acacia import constraints
from acacia.models import DisruptionEvent, EmptyLedgerView, FallbackPlan, LedgerView, PrivateState
from acacia.timeutil import to_hhmm

if TYPE_CHECKING:
    from acacia.world import AirportWorld

EARTH_RADIUS_MI = 3958.8
REMOTE_STAND = "REMOTE-1"
REMOTE_STANDS = 9


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(a))


def _coord(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _coords(airports: pd.DataFrame) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for code, lat, lon in zip(airports["IATA_CODE"], airports["LATITUDE"], airports["LONGITUDE"]):
        la, lo = _coord(lat), _coord(lon)
        if isinstance(code, str) and code.strip() and la is not None and lo is not None:
            out[code.strip().upper()] = (la, lo)
    return out


def nearest_alternate(airports: pd.DataFrame, dest: str, origin: str) -> tuple[str | None, float | None]:
    """(IATA code, distance in miles) of the nearest airport to `dest`, or (None, None)."""
    required = {"IATA_CODE", "LATITUDE", "LONGITUDE"}
    if not required.issubset(airports.columns):
        raise ValueError(f"airports table needs columns {sorted(required)}")
    coords = _coords(airports)
    home = coords.get(dest.upper())
    if home is None:
        return None, None
    excluded = {dest.upper(), origin.upper()}
    best = min(
        ((haversine_mi(*home, *xy), code) for code, xy in coords.items() if code not in excluded),
        default=None,
    )
    if best is None:
        return None, None
    return best[1], round(best[0], 1)


def _fits(world: AirportWorld | None, view: LedgerView, gate: str, start: int, min_turn: int) -> bool:
    """True when the world says gate is clear for [start, start + min_turn); True without a world."""
    if world is None:
        return True
    try:
        return world.gate_free_from(gate, start, view, duration=min_turn) == start
    except KeyError:
        return False


def _notice_ok(event: DisruptionEvent, gate_arr: int) -> bool:
    return gate_arr - constraints.decision_time_min(event) >= constraints.GATE_CHANGE_NOTICE_MIN


def _gate_clear(private: PrivateState, gate: str, gate_arr: int, world: AirportWorld | None,
                view: LedgerView) -> bool:
    g = private.ground
    free = g.free_from.get(gate)
    return (gate not in g.failed and free is not None and free <= gate_arr
            and _fits(world, view, gate, gate_arr, g.min_turn))


def _pick_gate(event: DisruptionEvent, private: PrivateState, gate_arr: int,
               world: AirportWorld | None, view: LedgerView, changes_ok: bool = True) -> str | None:
    """The assigned gate if clear for the turnaround, else the earliest-free clear gate, or None.

    With changes_ok False (gate-change notice too short) only the assigned gate qualifies."""
    g = private.ground
    if g.assigned_gate in g.gates and _gate_clear(private, g.assigned_gate, gate_arr, world, view):
        return g.assigned_gate
    if not changes_ok:
        return None
    others = sorted((g.free_from[x], i, x) for i, x in enumerate(g.gates)
                    if x != g.assigned_gate and g.free_from.get(x) is not None)
    return next((x for _, _, x in others if _gate_clear(private, x, gate_arr, world, view)), None)


def _separated_slots(event: DisruptionEvent, private: PrivateState, world: AirportWorld | None,
                     view: LedgerView) -> list[int]:
    """ATC free slots, earliest first, still wake-separated on the live runway when a world is given."""
    slots = sorted(private.atc.free_slots)
    if world is None or not hasattr(world, "is_separated"):
        return slots
    own = private.atc.own_class
    return [s for s in slots if world.is_separated(s, own, view, exclude_event=event.event_id)[0]]


def _pick_remote(event: DisruptionEvent, gate_arr: int, min_turn: int, view: LedgerView | None) -> str:
    """REMOTE-1, or with a ledger view the first remote stand not booked for the turnaround."""
    if view is None:
        return REMOTE_STAND
    airport = event.flight.dest.upper()
    for k in range(1, REMOTE_STANDS + 1):
        stand = f"REMOTE-{k}"
        if not view.gate_busy(airport, stand, gate_arr, gate_arr + min_turn, exclude_event=event.event_id):
            return stand
    return REMOTE_STAND


def _divert(event: DisruptionEvent, airports: pd.DataFrame, why: str) -> FallbackPlan:
    code, dist = nearest_alternate(airports, event.flight.dest, event.flight.origin)
    where = f"nearest alternate {code} ({dist} mi)" if code else "no alternate with coordinates found"
    return FallbackPlan(event_id=event.event_id, divert=True, alternate=code,
                        alternate_distance_mi=dist, reason=f"{why} DIVERT: {where}.")


def plan_fallback(event: DisruptionEvent, private: PrivateState, airports_df: pd.DataFrame,
                  world: AirportWorld | None = None,
                  ledger_view: LedgerView | None = None) -> FallbackPlan:
    """The FCFS fallback plan for one event, from ground-truth private state.

    Pass the airport `world` and the live `ledger_view` so the plan passes the checker's full
    turnaround and remote-stand checks; without them the HANDOVER 10 start-time rule applies.
    """
    view = ledger_view if ledger_view is not None else EmptyLedgerView()
    slots = _separated_slots(event, private, world, view)
    land_by = private.pilot.land_by
    if not slots or slots[0] > land_by:
        return _divert(event, airports_df, _no_slot_reason(private, slots, land_by))
    slot = slots[0]
    for candidate in (s for s in slots if s <= land_by):
        picked = _gate_for_slot(event, private, candidate, world, view, ledger_view)
        if picked is not None:
            gate, remote = picked
            later = "" if candidate == slot else (
                f" Slot {to_hhmm(candidate)} instead of {to_hhmm(slot)} to meet the "
                f"{constraints.GATE_CHANGE_NOTICE_MIN} min gate-change notice.")
            return FallbackPlan(event_id=event.event_id, slot=to_hhmm(candidate), gate=gate,
                                reason="First-come-first-served slot, " + _gate_text(private, gate, remote)
                                       + later)
    gate_arr = slot + event.taxi_in
    gate = _pick_gate(event, private, gate_arr, world, view) or _pick_remote(
        event, gate_arr, private.ground.min_turn, ledger_view)
    return FallbackPlan(event_id=event.event_id, slot=to_hhmm(slot), gate=gate,
                        reason=f"First-come-first-served slot; no gate meets the "
                               f"{constraints.GATE_CHANGE_NOTICE_MIN} min gate-change notice "
                               "before the fuel limit.")


def _no_slot_reason(private: PrivateState, slots: list[int], land_by: int) -> str:
    """Why no slot works (CONSTRAINTS.md 6), for the divert reason."""
    if not slots:
        lost = " (the declared ones are no longer wake-separated)" if private.atc.free_slots else ""
        return f"Feasible set empty: no separated free runway slot{lost} before LAND_BY {to_hhmm(land_by)}."
    return (f"Feasible set empty: earliest separated slot {to_hhmm(slots[0])} is past "
            f"LAND_BY {to_hhmm(land_by)} (fuel limit).")


def _gate_text(private: PrivateState, gate: str, remote: bool) -> str:
    if remote:
        return "no gate clear for the turnaround, remote stand."
    if gate == private.ground.assigned_gate:
        return "assigned gate clear for the turnaround."
    return "earliest free gate (assigned gate not clear)."


def _gate_for_slot(event: DisruptionEvent, private: PrivateState, slot: int,
                   world: AirportWorld | None, view: LedgerView,
                   ledger_view: LedgerView | None) -> tuple[str, bool] | None:
    """(gate, is_remote) for this slot honouring the gate-change notice, or None."""
    gate_arr = slot + event.taxi_in
    changes_ok = _notice_ok(event, gate_arr)
    gate = _pick_gate(event, private, gate_arr, world, view, changes_ok)
    if gate is not None:
        return gate, False
    if not changes_ok:
        return None
    return _pick_remote(event, gate_arr, private.ground.min_turn, ledger_view), True
