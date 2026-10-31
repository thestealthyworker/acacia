"""HANDOVER 14 tests 21 (ledger-level property) and 22 (ledger lifecycle), FABLE_ADDENDUM 7."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from acacia.ledger import DoubleBookingError, Ledger, LedgerStateError
from acacia.models import LedgerView, Reservation
from acacia.timeutil import to_min

DATE = "2015-02-26"


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(DATE, runs_dir=tmp_path)


def _hold(ledger: Ledger, event: str, slot: int, gate: str = "A5", start: int = 740, end: int = 775):
    return ledger.hold(event, "ATL", slot, gate, start, end)


# --- test 22: lifecycle ------------------------------------------------------------------------
def test_hold_creates_two_held_reservations(ledger: Ledger) -> None:
    res = _hold(ledger, "EVT-1", 730)
    assert [r.resource_type for r in res] == ["RUNWAY_SLOT", "GATE"]
    slot, gate = res
    assert slot.start == slot.end == "12:10" and slot.resource_id == "RWY"
    assert gate.start == "12:20" and gate.end == "12:55" and gate.resource_id == "A5"
    assert all(r.status == "HELD" and r.event_id == "EVT-1" and r.airport == "ATL" for r in res)
    assert slot.res_id == "R-EVT-1-RUNWAY_SLOT-1" and gate.res_id == "R-EVT-1-GATE-2"
    assert ledger.reservations_for("EVT-1") == res
    assert all(isinstance(r, Reservation) for r in res)


def test_views_satisfy_protocol_and_snapshot_is_frozen(ledger: Ledger) -> None:
    snapshot = ledger.view()
    live = ledger.live()
    assert isinstance(snapshot, LedgerView) and isinstance(live, LedgerView)
    _hold(ledger, "EVT-1", 730)
    assert not snapshot.slot_taken("ATL", 730)
    assert not snapshot.gate_busy("ATL", "A5", 740, 775)
    assert live.slot_taken("ATL", 730)
    assert live.gate_busy("ATL", "A5", 740, 775)
    assert ledger.view().slot_taken("ATL", 730)
    # exclude_event ignores the event's own bookings; other airports are unaffected
    assert not live.slot_taken("ATL", 730, exclude_event="EVT-1")
    assert not live.gate_busy("ATL", "A5", 740, 775, exclude_event="EVT-1")
    assert not live.slot_taken("DCA", 730) and not live.gate_busy("DCA", "A5", 740, 775)


def test_double_booking_is_refused_atomically(ledger: Ledger) -> None:
    _hold(ledger, "EVT-1", 730, gate="A5", start=740, end=775)
    with pytest.raises(DoubleBookingError):
        _hold(ledger, "EVT-2", 730, gate="A7", start=900, end=930)  # same slot
    with pytest.raises(DoubleBookingError):
        _hold(ledger, "EVT-2", 734, gate="A5", start=770, end=800)  # overlapping gate interval
    assert ledger.reservations_for("EVT-2") == []
    assert not ledger.live().slot_taken("ATL", 734)
    # back-to-back gate use is fine (half-open intervals)
    res = _hold(ledger, "EVT-2", 734, gate="A5", start=775, end=810)
    assert all(r.status == "HELD" for r in res)


def test_other_airport_does_not_conflict(ledger: Ledger) -> None:
    _hold(ledger, "EVT-1", 730)
    ledger.hold("EVT-2", "DCA", 730, "A5", 740, 775)
    assert ledger.live().slot_taken("DCA", 730) and ledger.live().slot_taken("ATL", 730)


def test_commit_release_and_reuse(ledger: Ledger) -> None:
    _hold(ledger, "EVT-1", 730)
    ledger.commit("EVT-1")
    assert {r.status for r in ledger.reservations_for("EVT-1")} == {"COMMITTED"}
    assert ledger.live().slot_taken("ATL", 730)
    with pytest.raises(LedgerStateError):
        ledger.commit("EVT-1")  # already committed
    with pytest.raises(LedgerStateError):
        ledger.release("EVT-1")  # COMMITTED -> RELEASED needs cancel
    ledger.cancel("EVT-1")
    assert {r.status for r in ledger.reservations_for("EVT-1")} == {"RELEASED"}

    _hold(ledger, "EVT-2", 730)
    ledger.release("EVT-2")
    assert {r.status for r in ledger.reservations_for("EVT-2")} == {"RELEASED"}
    assert not ledger.live().slot_taken("ATL", 730)
    assert not ledger.live().gate_busy("ATL", "A5", 740, 775)
    # a released slot and gate are available to the next event
    res = _hold(ledger, "EVT-3", 730)
    assert all(r.status == "HELD" for r in res)
    with pytest.raises(LedgerStateError):
        ledger.release("EVT-2")  # nothing held any more
    with pytest.raises(LedgerStateError):
        ledger.commit("EVT-9")  # unknown event
    with pytest.raises(LedgerStateError):
        _hold(ledger, "EVT-3", 900)  # already has active reservations


def test_hold_validates_inputs(ledger: Ledger) -> None:
    with pytest.raises(ValueError):
        ledger.hold("EVT-1", "ATL", 730, "A5", 775, 740)  # end before start
    with pytest.raises(ValueError):
        ledger.hold("EVT-1", "ATL", -1, "A5", 740, 775)
    with pytest.raises(ValueError):
        ledger.hold("", "ATL", 730, "A5", 740, 775)
    assert ledger.reservations_for("EVT-1") == []


def test_jsonl_is_appended_and_replayed(tmp_path: Path) -> None:
    ledger = Ledger(DATE, runs_dir=tmp_path)
    _hold(ledger, "EVT-1", 730)
    _hold(ledger, "EVT-2", 734, gate="A7")
    ledger.commit("EVT-1")
    ledger.release("EVT-2")
    path = tmp_path / f"ledger-{DATE}.jsonl"
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["op"] for line in lines] == ["hold", "hold", "hold", "hold", "commit", "commit", "release", "release"]
    assert all({"ts", "op", "reservation"} <= set(line) for line in lines)

    replayed = Ledger(DATE, runs_dir=tmp_path)
    assert replayed.reservations_for("EVT-1") == ledger.reservations_for("EVT-1")
    assert replayed.reservations_for("EVT-2") == ledger.reservations_for("EVT-2")
    assert replayed.live().slot_taken("ATL", 730) and not replayed.live().slot_taken("ATL", 734)
    # sequence numbers continue after replay
    (slot, _) = _hold(replayed, "EVT-3", 738, gate="A9")
    assert slot.res_id == "R-EVT-3-RUNWAY_SLOT-5"
    assert len(path.read_text().splitlines()) == 10


def test_corrupt_jsonl_raises(tmp_path: Path) -> None:
    (tmp_path / f"ledger-{DATE}.jsonl").write_text('{"op": "hold"}\nnot json\n')
    with pytest.raises(ValueError):
        Ledger(DATE, runs_dir=tmp_path)


# --- test 21: property, ledger level -------------------------------------------------------------
def _no_double_booking(ledger: Ledger) -> None:
    active = [r for r in ledger.all_reservations() if r.status in ("HELD", "COMMITTED")]
    slots = [r for r in active if r.resource_type == "RUNWAY_SLOT"]
    gates = [r for r in active if r.resource_type == "GATE"]
    for i, a in enumerate(slots):
        for b in slots[i + 1:]:
            assert not (a.airport == b.airport and a.start == b.start and a.event_id != b.event_id), (a, b)
    for i, a in enumerate(gates):
        for b in gates[i + 1:]:
            if a.airport != b.airport or a.resource_id != b.resource_id or a.event_id == b.event_id:
                continue
            overlap = to_min(a.start) < to_min(b.end) and to_min(b.start) < to_min(a.end)
            assert not overlap, (a, b)


_op = st.one_of(
    st.tuples(
        st.just("hold"),
        st.integers(1, 5),            # event number
        st.sampled_from(["ATL", "DCA"]),
        st.integers(0, 6),            # slot index (x4 minutes)
        st.sampled_from(["A1", "A2"]),
        st.integers(0, 60),           # gate start
        st.integers(1, 40),           # gate length
    ),
    st.tuples(st.just("commit"), st.integers(1, 5)),
    st.tuples(st.just("release"), st.integers(1, 5)),
)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops=st.lists(_op, max_size=25))
def test_random_hold_sequences_never_double_book(ops: list[tuple]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(DATE, runs_dir=Path(tmp))
        for op in ops:
            try:
                if op[0] == "hold":
                    _, n, airport, slot, gate, start, length = op
                    ledger.hold(f"EVT-{n}", airport, 700 + slot * 4, gate, 700 + start, 700 + start + length)
                elif op[0] == "commit":
                    ledger.commit(f"EVT-{op[1]}")
                else:
                    ledger.release(f"EVT-{op[1]}")
            except (DoubleBookingError, LedgerStateError):
                pass
            _no_double_booking(ledger)
        # the JSONL replay reproduces the same state
        assert Ledger(DATE, runs_dir=Path(tmp)).all_reservations() == ledger.all_reservations()


def test_runway_slot_stores_wake_class_and_replays_it(tmp_path: Path) -> None:
    """WAKE-7: the slot carries the flight's class; the gate row carries none; replay keeps it."""
    ledger = Ledger(DATE, runs_dir=tmp_path)
    slot, gate = ledger.hold("EVT-1", "ATL", 730, "A5", 740, 775, wake_class="Heavy")
    assert slot.wake_class == "Heavy" and gate.wake_class is None
    view = Ledger(DATE, runs_dir=tmp_path).live()
    assert view.slot_class("ATL", 730) == "Heavy"
    assert view.slot_class("ATL", 730, exclude_event="EVT-1") is None
    assert view.slot_class("ATL", 731) is None


def test_slot_without_a_stored_class_reads_heavy(ledger: Ledger) -> None:
    _hold(ledger, "EVT-1", 730)
    assert ledger.live().slot_class("ATL", 730) == "Heavy"
