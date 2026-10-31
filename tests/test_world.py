"""FABLE_ADDENDUM 4 / HANDOVER 18.2: the shared airport world (runway grid, gate pool, failures).

Covers determinism, workbook arrival capacity and storm spacing (CONSTRAINTS.md 2), wake
separation against traffic and held ledger slots, ledger-aware free slots and gate_free_from.
"""

from __future__ import annotations

import pytest

from acacia import config, constraints
from acacia.models import EmptyLedgerView, Injected
from acacia.timeutil import to_hhmm
from acacia.world import AirportWorld, clear_world_cache, get_world, separation_reach

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
def _gaps(grid: list[int], lo: int, hi: int) -> list[int]:
    pts = [p for p in grid if lo <= p < hi]
    return [b - a for a, b in zip(pts, pts[1:], strict=False)]


def test_grid_follows_workbook_arrival_capacity_and_storm_factor() -> None:
    """CONSTRAINTS.md 2: ATL 90 movements/hr x 50% = 45 arrivals/hr, x0.6 in a moderate storm
    (27/hr), x0.4 in a severe one (18/hr). Per-runway model (CAP-9): two arrival runways, each
    stepping 60 x 2 / R (2.67 / 4.44 / 6.67 min), staggered, so the merged grid lands R per hour."""
    world = AirportWorld("ATL", DATE, [CLOSURE])
    grid = world.grid()  # merged points of R1 and R2
    assert grid[0] == 0 and all(0 <= p < config.MINUTES_PER_DAY for p in grid)
    assert grid == sorted(set(grid))
    assert grid[:7] == [0, 1, 3, 4, 5, 7, 8]  # R1 0, 2.67, 5.33, 8 and R2 1.33, 4, 6.67 rounded
    assert len([p for p in grid if 600 <= p < 660]) == 45  # nominal hour
    assert [len([p for p in world.grid(r) if 720 <= p < 780]) for r in world.runways()] == [14, 14]
    assert all(set(_gaps(world.grid(r), 730, 780)) <= {4, 5} for r in world.runways())
    severe = AirportWorld("ATL", DATE, [CLOSURE.model_copy(update={"severity": "severe"})])
    assert len([p for p in severe.grid() if 720 <= p < 780]) == 18
    assert all(set(_gaps(severe.grid(r), 730, 780)) <= {6, 7} for r in severe.runways())
    assert world.storm_at(720) == "moderate" and severe.storm_at(779) == "severe"
    assert world.storm_at(780) == "none"
    assert world.closure_at(720) and world.closure_at(779) and not world.closure_at(780)
    assert world.closures() == [(720, 780)]


def test_medium_hub_grid_uses_its_own_capacity() -> None:
    world = AirportWorld("SFO", DATE, [])  # 60 movements/hr -> 30 arrivals/hr, step 2
    assert set(_gaps(world.grid(), 0, 1440)) == {2}


def test_traffic_wake_classes_are_seeded_and_near_the_mix() -> None:
    world = AirportWorld("ATL", DATE, [])
    again = AirportWorld("ATL", DATE, [])
    grid = world.grid()
    assert [world.traffic_class(p) for p in grid] == [again.traffic_class(p) for p in grid]
    traffic = world.traffic_times(0, config.MINUTES_PER_DAY)
    assert all(world.traffic_class(p) in ("Heavy", "Large", "Small") for p in traffic)
    share = {c: sum(world.traffic_class(p) == c for p in traffic) / len(traffic) for c in ("Heavy", "Large", "Small")}
    assert 0.11 < share["Heavy"] < 0.19 and 0.75 < share["Large"] < 0.85 and 0.02 < share["Small"] < 0.08
    assert world.traffic_class(2) is None  # not a grid point
    assert all(world.traffic_class(p) is None for p in grid if not world.is_traffic(p))


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
    eligible = [p for p in world.grid() if p >= 700 and not world.is_traffic(p) and p != slots[0]
                and world.is_separated(p, "Large", taken)[0]]
    assert again[0] == eligible[0]


def test_free_slots_after_end_of_day_is_empty() -> None:
    world = AirportWorld("ATL", DATE, [])
    assert world.free_slots_after(world.grid()[-1] + 1, 4, EmptyLedgerView()) == []
    assert world.free_slots_after(config.MINUTES_PER_DAY, 4, EmptyLedgerView()) == []
    assert world.free_slots_after(0, 0, EmptyLedgerView()) == []


# --- wake separation (CONSTRAINTS.md 2, WAKE-1..4, WAKE-7) ---------------------------------
class _HeldView(EmptyLedgerView):
    """A view with held runway slots per event id, honouring exclude_event; every held slot
    carries wake class `cls` (None: no stored class)."""

    def __init__(self, held: dict[int, str], cls: str | None = "Large") -> None:
        self.held = held
        self.cls = cls

    def slot_taken(self, airport: str, t: int, exclude_event: str | None = None) -> bool:
        owner = self.held.get(t)
        return owner is not None and owner != exclude_event

    def slot_class(self, airport: str, t: int, exclude_event: str | None = None) -> str | None:
        return self.cls if self.slot_taken(airport, t, exclude_event) else None


def _isolated_point(world: AirportWorld, lo: int = 300) -> int:
    """A free grid point >= lo with no traffic within the separation reach on either side."""
    reach = separation_reach()
    for p in world.grid():
        if p < lo or world.is_traffic(p):
            continue
        if not world.traffic_times(p - reach, p + reach + 1):
            return p
    raise AssertionError("fixture world has no isolated free point")


def test_separation_reach_covers_the_widest_workbook_pair() -> None:
    assert separation_reach() == 4  # Super -> Small 8 nm = 3.43 min at 140 kt
    assert all(constraints.wake_minutes(a, b) <= separation_reach()
               for a in ("Super", "Heavy", "Large", "Small") for b in ("Heavy", "Large", "Small"))


def test_is_separated_uses_workbook_minutes_against_held_slots() -> None:
    world = AirportWorld("SFO", DATE, [])
    t = _isolated_point(world)
    ok, detail = world.is_separated(t, "Large", EmptyLedgerView())
    assert ok and "separated" in detail
    # another event's held Large slot (WAKE-7): Large -> Large needs 3 nm = 1.29 min
    assert not world.is_separated(t, "Large", _HeldView({t - 1: "EV-2"}))[0]
    assert world.is_separated(t, "Large", _HeldView({t - 2: "EV-2"}))[0]
    assert not world.is_separated(t, "Large", _HeldView({t + 1: "EV-2"}))[0]
    assert world.is_separated(t, "Large", _HeldView({t + 2: "EV-2"}))[0]
    # Large leader -> Small follower needs 4 nm = 1.71 min; Small -> Large only 3 nm
    assert not world.is_separated(t, "Small", _HeldView({t - 1: "EV-2"}))[0]
    assert world.is_separated(t, "Small", _HeldView({t - 2: "EV-2"}))[0]
    # a held slot at t itself is 0 min apart
    ok, detail = world.is_separated(t, "Large", _HeldView({t: "EV-2"}))
    assert not ok and "need" in detail


def test_is_separated_boundary_exactly_at_the_minimum_passes(monkeypatch) -> None:
    world = AirportWorld("SFO", DATE, [])
    t = _isolated_point(world)
    monkeypatch.setattr(constraints, "wake_minutes", lambda leader, follower: 2.0)
    assert world.is_separated(t, "Large", _HeldView({t - 2: "EV-2"}))[0]
    assert world.is_separated(t, "Large", _HeldView({t + 2: "EV-2"}))[0]
    assert not world.is_separated(t, "Large", _HeldView({t - 1: "EV-2"}))[0]
    assert not world.is_separated(t, "Large", _HeldView({t + 1: "EV-2"}))[0]


def test_is_separated_ignores_the_excluded_events_own_slots() -> None:
    world = AirportWorld("SFO", DATE, [])
    t = _isolated_point(world)
    view = _HeldView({t - 1: "EV-1"})
    assert not world.is_separated(t, "Large", view)[0]
    assert world.is_separated(t, "Large", view, exclude_event="EV-1")[0]


def test_is_separated_against_traffic_classes() -> None:
    """Every traffic point is a neighbour with its own seeded class; a point right next to
    traffic (1 min) is never separated, since the smallest minimum is 1.29 min."""
    world = AirportWorld("ATL", DATE, [])
    traffic = world.traffic_times(600, 700)
    for p in traffic:
        for own in ("Large", "Heavy"):
            assert not world.is_separated(p + 1, own, EmptyLedgerView())[0]
            assert not world.is_separated(p - 1, own, EmptyLedgerView())[0]
    t = _isolated_point(world)
    assert world.is_separated(t, "Heavy", EmptyLedgerView())[0]


def _wake_ok(world: AirportWorld, slot: int, own: str, held: dict[int, str]) -> bool:
    reach = separation_reach()
    others = {p: world.traffic_class(p) for p in world.traffic_times(slot - reach, slot + reach + 1)}
    others.update({p: "Large" for p in held if abs(p - slot) <= reach})
    for p, cls in others.items():
        if p <= slot and slot - p < constraints.wake_minutes(cls, own):
            return False
        if p > slot and p - slot < constraints.wake_minutes(own, cls):
            return False
    return True


@pytest.mark.parametrize("own", ["Large", "Heavy"])
def test_free_slots_never_violate_wake_against_traffic_or_held_slots(own: str) -> None:
    world = AirportWorld("ATL", DATE, [CLOSURE])
    plain = world.free_slots_after(700, 8, EmptyLedgerView(), own_class=own)
    assert len(plain) == 8 and all(_wake_ok(world, s, own, {}) for s in plain)
    held = {plain[0] + 1: "EV-9", plain[3]: "EV-9"}
    view = _HeldView(held)
    slots = world.free_slots_after(700, 8, view, own_class=own)
    assert len(slots) == 8 and plain[0] not in slots and plain[3] not in slots
    assert all(_wake_ok(world, s, own, held) for s in slots)
    assert all(world.is_separated(s, own, view)[0] for s in slots)
    assert all(not world.is_traffic(s) and not view.slot_taken("ATL", s) for s in slots)


def test_free_slots_default_class_is_large_and_heavy_is_a_subset_of_separated_points() -> None:
    world = AirportWorld("ATL", DATE, [])
    assert world.free_slots_after(600, 4, EmptyLedgerView()) == world.free_slots_after(600, 4, EmptyLedgerView(), "Large")
    # Heavy needs at least the Large minimum except behind a Heavy leader (4 nm vs 5 nm), so a
    # Heavy-separated point with no Heavy leader within reach is Large-separated too (per runway).
    reach = separation_reach()
    for rwy in world.runways():
        heavy_ok = [p for p in world.grid(rwy) if 600 <= p < 700 and not world.is_traffic(p, rwy)
                    and world.is_separated(p, "Heavy", EmptyLedgerView(), runway=rwy)[0]
                    and "Heavy" not in {world.traffic_class(q, rwy) for q in range(p - reach, p)}]
        assert heavy_ok and all(world.is_separated(p, "Large", EmptyLedgerView(), runway=rwy)[0]
                                for p in heavy_ok)


# --- gate pool ---------------------------------------------------------------------------------
def test_gate_pool_naming_and_busy_intervals() -> None:
    atl = AirportWorld("ATL", DATE, [])
    dca = AirportWorld("DCA", DATE, [])
    assert len(atl.gates()) == constraints.WORLD_GATE_POOL == 8  # CAP-4
    assert len(set(atl.gates())) == constraints.WORLD_GATE_POOL
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


def test_held_heavy_slot_uses_its_stored_class() -> None:
    """Review fix (WAKE-7): a Large 2 min behind a held Heavy fails (Heavy -> Large 5 nm = 2.14 min)."""
    world = AirportWorld("SFO", DATE, [])
    t = _isolated_point(world)
    assert world.is_separated(t, "Large", _HeldView({t - 2: "EV-2"}, cls="Large"))[0]
    ok, detail = world.is_separated(t, "Large", _HeldView({t - 2: "EV-2"}, cls="Heavy"))
    assert not ok and "Heavy" in detail
    assert world.is_separated(t, "Large", _HeldView({t - 3: "EV-2"}, cls="Heavy"))[0]


def test_held_slot_without_a_class_reads_as_heavy() -> None:
    world = AirportWorld("SFO", DATE, [])
    t = _isolated_point(world)
    assert not world.is_separated(t, "Large", _HeldView({t - 2: "EV-2"}, cls=None))[0]
    assert not world.is_separated(t, "Large", _SlotOnlyView({t - 2: "EV-2"}))[0]


class _SlotOnlyView:
    """A legacy view with slot_taken only (no slot_class)."""

    def __init__(self, held: dict[int, str]) -> None:
        self.held = held

    def slot_taken(self, airport: str, t: int, exclude_event: str | None = None) -> bool:
        owner = self.held.get(t)
        return owner is not None and owner != exclude_event

    def gate_busy(self, airport: str, gate: str, start: int, end: int, exclude_event: str | None = None) -> bool:
        return False
