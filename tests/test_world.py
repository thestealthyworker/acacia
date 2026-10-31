"""FABLE_ADDENDUM 4 / HANDOVER 18.2: the shared airport world (runway grid, gate pool, failures).

Covers determinism, closure spacing, ledger-aware free slots and gate_free_from with a duration.
"""

from __future__ import annotations

import pytest

from acacia import config
from acacia.models import EmptyLedgerView, Injected
from acacia.timeutil import to_hhmm
from acacia.world import AirportWorld, clear_world_cache, get_world

DATE = "2015-02-26"
CLOSURE = Injected(type="RUNWAY_CLOSED", ref="rwy", start="12:00", end="13:00")
FAILURE = Injected(type="EQUIPMENT_FAILURE", ref="jet bridge", start="10:00", end="11:00")


class _SlotView(EmptyLedgerView):
    """A view that reports the given runway minutes as taken."""

    def __init__(self, taken: set[int]) -> None:
        self.taken = taken

    def slot_taken(self, airport: str, t: int, exclude_event: str | None = None) -> bool:
        return t in self.taken


class _GateView(EmptyLedgerView):
    """A view that reports one gate booking [start, end)."""

    def __init__(self, gate: str, start: int, end: int) -> None:
        self.gate, self.start, self.end = gate, start, end

    def gate_busy(
        self, airport: str, gate: str, start: int, end: int, exclude_event: str | None = None
    ) -> bool:
        return gate == self.gate and start < self.end and self.start < end


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_world_cache()
    yield
    clear_world_cache()


def _busy_gate(world: AirportWorld) -> tuple[str, int, int]:
    """First gate with a busy interval that starts after 02:00 and ends before 22:00."""
    for gate in world.gates():
        for start, end in world.busy_intervals(gate):
            if start >= 120 and end <= 1320:
                return gate, start, end
    raise AssertionError("fixture world has no usable busy interval")


# --- determinism and caching ----------------------------------------------------------------
def test_world_is_deterministic() -> None:
    a = AirportWorld("ATL", DATE, [CLOSURE, FAILURE])
    b = AirportWorld("ATL", DATE, [FAILURE, CLOSURE])
    assert a.grid() == b.grid()
    assert [a.is_traffic(p) for p in a.grid()] == [b.is_traffic(p) for p in b.grid()]
    assert a.gates() == b.gates()
    assert all(a.busy_intervals(g) == b.busy_intervals(g) for g in a.gates())
    assert all(a.failed_intervals(g) == b.failed_intervals(g) for g in a.gates())


def test_salt_changes_the_world(monkeypatch) -> None:
    base = [p for p in AirportWorld("ATL", DATE, []).grid() if AirportWorld("ATL", DATE, []).is_traffic(p)]
    monkeypatch.setattr(config, "SEED_SALT", "another-salt")
    other_world = AirportWorld("ATL", DATE, [])
    other = [p for p in other_world.grid() if other_world.is_traffic(p)]
    assert base != other


def test_get_world_caches_per_airport_date_and_injected() -> None:
    w1 = get_world("ATL", DATE, [CLOSURE])
    assert get_world("ATL", DATE, [CLOSURE]) is w1
    assert get_world("ATL", DATE, [CLOSURE, FAILURE]) is not w1
    assert get_world("DCA", DATE, [CLOSURE]) is not w1
    assert get_world("ATL", "2015-02-27", [CLOSURE]) is not w1


# --- runway grid -------------------------------------------------------------------------------
def test_grid_spacing_is_4_outside_and_8_inside_closures() -> None:
    world = AirportWorld("ATL", DATE, [CLOSURE])
    grid = world.grid()
    assert grid[0] == 0 and all(0 <= p < config.MINUTES_PER_DAY for p in grid)
    for a, b in zip(grid, grid[1:], strict=False):
        assert b - a == (8 if world.closure_at(a) else 4), (a, b)
    assert world.closure_at(720) and world.closure_at(779) and not world.closure_at(780)
    assert world.closures() == [(720, 780)]


def test_closures_are_a_union_of_runway_closed_events() -> None:
    world = AirportWorld("ATL", DATE, [
        CLOSURE,
        Injected(type="RUNWAY_CLOSED", ref="rwy", start="12:30", end="13:30"),
        Injected(type="RUNWAY_CLOSED", ref="rwy", start="15:00", end="15:10"),
        FAILURE,
    ])
    assert world.closures() == [(720, 810), (900, 910)]


def test_traffic_share_is_about_55_percent() -> None:
    world = AirportWorld("ATL", DATE, [])
    grid = world.grid()
    share = sum(world.is_traffic(p) for p in grid) / len(grid)
    assert 0.45 < share < 0.65
    assert world.traffic_times(600, 660) == [p for p in grid if 600 <= p < 660 and world.is_traffic(p)]


def test_free_slots_skip_traffic_taken_and_earlier_points() -> None:
    world = AirportWorld("ATL", DATE, [CLOSURE])
    slots = world.free_slots_after(700, 4, EmptyLedgerView())
    assert len(slots) == 4 and slots == sorted(slots) and all(p >= 700 for p in slots)
    assert not any(world.is_traffic(p) for p in slots)
    assert all(p in world.grid() for p in slots)
    # the same call is reproducible, and a different t uses its own skip rng
    assert world.free_slots_after(700, 4, EmptyLedgerView()) == slots

    taken = _SlotView({slots[0]})
    again = world.free_slots_after(700, 4, taken)
    assert slots[0] not in again and len(again) == 4
    # the first eligible point is always returned; skips only happen between returned slots
    eligible = [p for p in world.grid() if p >= 700 and not world.is_traffic(p) and p != slots[0]]
    assert again[0] == eligible[0]


def test_free_slots_after_end_of_day_is_empty() -> None:
    world = AirportWorld("ATL", DATE, [])
    assert world.free_slots_after(1439, 4, EmptyLedgerView()) == []
    assert world.free_slots_after(config.MINUTES_PER_DAY, 4, EmptyLedgerView()) == []
    assert world.free_slots_after(0, 0, EmptyLedgerView()) == []


# --- gate pool ---------------------------------------------------------------------------------
def test_gate_pool_naming_and_busy_intervals() -> None:
    atl = AirportWorld("ATL", DATE, [])
    dca = AirportWorld("DCA", DATE, [])
    assert len(atl.gates()) == 8 and len(set(atl.gates())) == 8
    assert all(g.startswith("A") for g in atl.gates()) and all(g.startswith("D") for g in dca.gates())
    numbers = [int(g[1:]) for g in atl.gates()]
    assert numbers == sorted(numbers) and all(1 <= n <= 30 for n in numbers)
    for gate in atl.gates():
        busy = atl.busy_intervals(gate)
        assert 1 <= len(busy) <= 4
        assert all(300 <= s < e for s, e in busy)
        assert all(busy[i][1] <= busy[i + 1][0] for i in range(len(busy) - 1))  # merged, sorted


def test_gate_free_from_respects_background_busy_and_duration() -> None:
    world = AirportWorld("ATL", DATE, [])
    gate, start, end = _busy_gate(world)
    view = EmptyLedgerView()
    t = start - 10
    # 30 min from t would run into the busy interval, so the answer is at or after its end
    assert world.gate_free_from(gate, t, view) >= end
    assert world.gate_free_from(gate, t, view, duration=5) == t
    inside = world.gate_free_from(gate, start + 1, view)
    assert inside is not None and inside >= end
    # the returned time never starts a window that overlaps a busy interval
    free = world.gate_free_from(gate, t, view, duration=45)
    assert all(not (free < e and s < free + 45) for s, e in world.busy_intervals(gate))


def test_gate_free_from_respects_ledger_bookings() -> None:
    world = AirportWorld("ATL", DATE, [])
    gate = world.gates()[0]
    t = world.gate_free_from(gate, 60, EmptyLedgerView())
    assert t is not None
    booked = _GateView(gate, t, t + 40)
    later = world.gate_free_from(gate, t, booked)
    assert later is not None and later >= t + 40
    assert not booked.gate_busy("ATL", gate, later, later + config.GATE_MIN_FREE)


def test_equipment_failure_takes_one_gate_out() -> None:
    world = AirportWorld("ATL", DATE, [FAILURE])
    failed = [g for g in world.gates() if world.failed_intervals(g)]
    assert len(failed) == 1
    gate = failed[0]
    assert world.failed_intervals(gate) == [(600, 660)]
    assert world.is_failed(gate, 600) and world.is_failed(gate, 659) and not world.is_failed(gate, 660)
    assert world.gate_free_from(gate, 630, EmptyLedgerView()) is None
    before = world.gate_free_from(gate, 540, EmptyLedgerView(), duration=120)
    assert before is not None and before >= 660
    assert AirportWorld("ATL", DATE, [FAILURE]).gates().index(gate) == world.gates().index(gate)


def test_gate_free_from_rejects_unknown_gate_and_runs_out_at_day_end() -> None:
    world = AirportWorld("ATL", DATE, [])
    with pytest.raises(KeyError):
        world.gate_free_from("Z99", 0, EmptyLedgerView())
    assert world.gate_free_from(world.gates()[0], 1430, EmptyLedgerView()) is None
    assert world.gate_free_from(world.gates()[0], 1400, EmptyLedgerView(), duration=40) == 1400


def test_injected_times_round_trip() -> None:
    world = AirportWorld("ATL", DATE, [CLOSURE])
    (start, end), = world.closures()
    assert to_hhmm(start) == CLOSURE.start and to_hhmm(end) == CLOSURE.end
