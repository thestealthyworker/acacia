"""Per-runway arrival model (CONSTRAINTS.md 2, CAP-9, WAKE-4 AS_GIVEN).

Each arrival runway has its own grid (step 60 x n / R(t)), its own background traffic and wake
classes, and wake separation applies only between consecutive arrivals on the same runway.
Slots carry a runway id ("R1", "R2"); the ledger, solver, checker and fallback are per runway.
"""

from __future__ import annotations

import pytest

from acacia import checker, config, constraints, simulate, solver
from acacia.agents.factory import make_agents
from acacia.coordinator import run_all
from acacia.data import DataStore
from acacia.ledger import DoubleBookingError, Ledger
from acacia.models import (
    AgentRole, ConsensusRecord, EmptyLedgerView, Injected, ResolutionPackage, Stress,
)
from acacia.timeutil import to_hhmm, to_min
from acacia.trigger import to_event
from acacia.world import AirportWorld, clear_world_cache, get_world

DATE = "2015-02-26"
STORM = Injected(type="RUNWAY_CLOSED", ref="storm", start="12:00", end="13:00")
EMPTY = EmptyLedgerView()
DFW_SAMPLES = ("AA8", "AA23", "AA35")


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_world_cache()
    yield
    clear_world_cache()


class _RunwayHeldView(EmptyLedgerView):
    """Held runway slots keyed by (minute, runway), each owned by an event and of class Large."""

    def __init__(self, held: dict[tuple[int, str], str]) -> None:
        self.held = held

    def _owner(self, t: int, exclude_event: str | None, runway: str | None) -> str | None:
        for (minute, rwy), owner in self.held.items():
            if minute == t and owner != exclude_event and (runway is None or rwy == runway):
                return owner
        return None

    def slot_taken(self, airport, t, exclude_event=None, runway=None) -> bool:
        return self._owner(t, exclude_event, runway) is not None

    def slot_class(self, airport, t, exclude_event=None, runway=None):
        return "Large" if self.slot_taken(airport, t, exclude_event, runway) else None


def _gaps(points: list[int], lo: int, hi: int) -> list[int]:
    pts = [p for p in points if lo <= p < hi]
    return [b - a for a, b in zip(pts, pts[1:], strict=False)]


def _isolated(world: AirportWorld, runway: str, lo: int = 300) -> int:
    """A free grid point on `runway` with no traffic on that runway within the separation reach."""
    reach = 4
    for p in world.grid(runway):
        if p >= lo and not world.is_traffic(p, runway) and not world.traffic_times(p - reach, p + reach + 1, runway):
            return p
    raise AssertionError("no isolated point")


# --- capacity ----------------------------------------------------------------------------------
def test_arrival_runways_from_active_runways() -> None:
    assert constraints.arrival_runways("ATL") == 2  # 4 active
    assert constraints.arrival_runways("SFO") == 1  # 3 active
    assert constraints.arrival_runways("SLC") == 1  # 2 active
    assert constraints.arrival_runways("ZZZ") == 1  # unknown -> Non-hub, 1 active
    assert constraints.runway_ids("ATL") == ("R1", "R2")
    assert constraints.runway_ids("SFO") == ("R1",)


def test_registry_states_the_runway_assumption_and_same_runway_wake() -> None:
    rows = {item.id: item for item in constraints.REGISTRY}
    assert rows["CAP-9"].status == "ASSUMED" and "active_runways // 2" in rows["CAP-9"].used_value
    assert rows["WAKE-4"].status == "AS_GIVEN" and "same runway" in rows["WAKE-4"].used_value


def test_atl_nominal_spacing_is_2_67_min_per_runway() -> None:
    world = AirportWorld("ATL", DATE, [])
    assert world.runways() == ["R1", "R2"]
    for rwy in world.runways():
        grid = world.grid(rwy)
        assert grid == sorted(set(grid)) and all(0 <= p < config.MINUTES_PER_DAY for p in grid)
        assert set(_gaps(grid, 0, 1440)) <= {2, 3}
        span = grid[-1] - grid[0]
        assert abs(span / (len(grid) - 1) - 60 * 2 / 45) < 0.01  # 2.67 min
        assert len([p for p in grid if 600 <= p < 660]) in (22, 23)
    assert world.grid("R1")[:4] == [0, 3, 5, 8]  # 0, 2.67, 5.33, 8
    assert world.grid("R2")[:4] == [1, 4, 7, 9]  # staggered by 60 / R = 1.33 min
    merged = [p for p in world.grid() if 600 <= p < 660]
    assert len(merged) == 45  # the airport still lands R = 45/hr


def test_storm_factor_widens_each_runway_grid() -> None:
    moderate = AirportWorld("ATL", DATE, [STORM])  # 27/hr -> 4.44 min per runway
    severe = AirportWorld("ATL", DATE, [STORM.model_copy(update={"severity": "severe"})])  # 6.67 min
    for rwy in ("R1", "R2"):
        assert set(_gaps(moderate.grid(rwy), 730, 770)) <= {4, 5}
        assert len([p for p in moderate.grid(rwy) if 720 <= p < 780]) in (13, 14)
        assert set(_gaps(severe.grid(rwy), 730, 770)) <= {6, 7}
        assert len([p for p in severe.grid(rwy) if 720 <= p < 780]) in (9, 10)


def test_single_arrival_runway_keeps_the_airport_rate() -> None:
    world = AirportWorld("SFO", DATE, [])  # 60 movements -> 30 arrivals/hr on one runway
    assert world.runways() == ["R1"]
    assert set(_gaps(world.grid("R1"), 0, 1440)) == {2}


# --- traffic and wake separation per runway -------------------------------------------------------
def test_traffic_is_drawn_per_runway() -> None:
    world = AirportWorld("ATL", DATE, [])
    for rwy in world.runways():
        grid = world.grid(rwy)
        share = sum(world.is_traffic(p, rwy) for p in grid) / len(grid)
        assert 0.45 < share < 0.65
        assert all(world.traffic_class(p, rwy) in ("Heavy", "Large", "Small")
                   for p in world.traffic_times(0, 1440, rwy))
    r1 = [world.is_traffic(p, "R1") for p in world.grid("R1")[:200]]
    r2 = [world.is_traffic(p, "R2") for p in world.grid("R2")[:200]]
    assert r1 != r2  # each runway has its own seeded draw


def test_wake_applies_only_within_a_runway() -> None:
    world = AirportWorld("ATL", DATE, [])
    t = _isolated(world, "R1")
    assert world.is_separated(t, "Large", EMPTY, runway="R1")[0]
    other_runway = _RunwayHeldView({(t - 1, "R2"): "EV-2", (t + 1, "R2"): "EV-3"})
    assert world.is_separated(t, "Large", other_runway, runway="R1")[0]
    same_runway = _RunwayHeldView({(t - 1, "R1"): "EV-2"})
    assert not world.is_separated(t, "Large", same_runway, runway="R1")[0]
    assert world.is_separated(t, "Large", same_runway, runway="R1", exclude_event="EV-2")[0]


def test_wake_boundary_exactly_at_the_minimum_passes(monkeypatch) -> None:
    world = AirportWorld("ATL", DATE, [])
    t = _isolated(world, "R2")
    monkeypatch.setattr(constraints, "wake_minutes", lambda leader, follower: 2.0)
    assert world.is_separated(t, "Large", _RunwayHeldView({(t - 2, "R2"): "EV-2"}), runway="R2")[0]
    assert world.is_separated(t, "Large", _RunwayHeldView({(t + 2, "R2"): "EV-2"}), runway="R2")[0]
    assert not world.is_separated(t, "Large", _RunwayHeldView({(t - 1, "R2"): "EV-2"}), runway="R2")[0]


def test_free_slots_per_runway_are_separated_on_their_runway() -> None:
    world = AirportWorld("ATL", DATE, [STORM])
    for rwy in world.runways():
        slots = world.free_slots_after(700, 6, EMPTY, own_class="Heavy", runway=rwy)
        assert len(slots) == 6 and slots == sorted(slots)
        assert all(p in world.grid(rwy) and not world.is_traffic(p, rwy) for p in slots)
        assert all(world.is_separated(p, "Heavy", EMPTY, runway=rwy)[0] for p in slots)


def test_atl_and_dfw_offer_realistic_numbers_of_separated_slots() -> None:
    """The merged-stream model left ~6-7 separated free points per hour against 45 nominal."""
    for airport in ("ATL", "DFW"):
        world = AirportWorld(airport, DATE, [])
        per_hour = 0
        for rwy in world.runways():
            pts = [p for p in world.grid(rwy) if 600 <= p < 720 and not world.is_traffic(p, rwy)]
            per_hour += sum(world.is_separated(p, "Large", EMPTY, runway=rwy)[0] for p in pts) / 2
        assert per_hour >= 15, (airport, per_hour)


# --- ledger -----------------------------------------------------------------------------------------
def test_no_double_booking_per_runway(tmp_path) -> None:
    ledger = Ledger(DATE, runs_dir=tmp_path)
    held = ledger.hold("EV-1", "ATL", 730, "A2", 740, 785, wake_class="Large", runway="R1")
    assert held[0].resource_id == "RWY-R1"
    ledger.hold("EV-2", "ATL", 730, "A3", 740, 785, wake_class="Large", runway="R2")  # other runway: fine
    with pytest.raises(DoubleBookingError):
        ledger.hold("EV-3", "ATL", 730, "A4", 740, 785, runway="R1")
    live = ledger.live()
    assert live.slot_taken("ATL", 730, runway="R1") and live.slot_taken("ATL", 730, runway="R2")
    assert not live.slot_taken("ATL", 731, runway="R1")
    assert live.slot_class("ATL", 730, runway="R2") == "Large"
    assert live.slot_taken("ATL", 730)  # no runway given: any runway


def test_legacy_hold_without_runway_blocks_every_runway(tmp_path) -> None:
    ledger = Ledger(DATE, runs_dir=tmp_path)
    ledger.hold("EV-1", "ATL", 730, "A2", 740, 785)
    assert ledger.live().slot_taken("ATL", 730, runway="R2")
    with pytest.raises(DoubleBookingError):
        ledger.hold("EV-2", "ATL", 730, "A3", 740, 785, runway="R1")


# --- determinism --------------------------------------------------------------------------------------
def test_world_is_deterministic_and_seeded_per_runway(monkeypatch) -> None:
    keys: list[str] = []
    real = config.seeded_rng

    def spy(key: str):
        keys.append(key)
        return real(key)

    monkeypatch.setattr(config, "seeded_rng", spy)
    a = AirportWorld("ATL", DATE, [STORM])
    b = AirportWorld("ATL", DATE, [STORM])
    for rwy in ("R1", "R2"):
        assert a.grid(rwy) == b.grid(rwy)
        assert [a.traffic_class(p, rwy) for p in a.grid(rwy)] == [b.traffic_class(p, rwy) for p in b.grid(rwy)]
    a.free_slots_after(700, 4, EMPTY, runway="R2")
    assert all(k.startswith(config.SEED_SALT) for k in keys)
    assert any(k.endswith("|R1") for k in keys) and any(k.endswith("|R2") for k in keys)


# --- simulate, solver, checker, fallback ----------------------------------------------------------------
def _atl_event(dl1639_row):
    from acacia.triangulate import triangulate
    return triangulate(dl1639_row)


def test_atc_offers_slots_per_runway(dl1639_row) -> None:
    event = _atl_event(dl1639_row)
    world = get_world("ATL", event.flight.date, event.injected)
    atc = simulate.build_private(event, Stress(), world, EMPTY).atc
    assert set(atc.free_by_runway) == {"R1", "R2"}
    for rwy, slots in atc.free_by_runway.items():
        assert slots and all(s >= atc.first for s in slots)
        assert all(world.is_separated(s, atc.own_class, EMPTY, runway=rwy)[0] for s in slots)
        assert set(atc.demand_by_runway[rwy]) == set(slots)
    assert atc.free_slots == sorted({s for v in atc.free_by_runway.values() for s in v})
    packet = make_agents("rule")[AgentRole.ATC].declare(event, atc)
    assert {(o.value, o.runway) for o in packet.options} == {
        (to_hhmm(s), r) for r, v in atc.free_by_runway.items() for s in v}


def test_solver_enumerates_slot_x_runway_x_gate(dl1639_row) -> None:
    event = _atl_event(dl1639_row)
    world = get_world("ATL", event.flight.date, event.injected)
    private = simulate.build_private(event, Stress(), world, EMPTY)
    agents = make_agents("rule")
    packets = {r: agents[r].declare(event, getattr(private, r.value.lower())) for r in AgentRole}
    result = solver.solve(packets, event.taxi_in, [], set())
    n_slots = sum(len(v) for v in private.atc.free_by_runway.values())
    n_gates = len(packets[AgentRole.GROUND].options)
    assert len(result.all) == n_slots * n_gates
    assert {c.package.runway for c in result.all} == {"R1", "R2"}
    assert solver.package_key("12:30", "A2", "R1") != solver.package_key("12:30", "A2", "R2")


def test_checker_verifies_the_package_runway(dl1639_row) -> None:
    event = _atl_event(dl1639_row)
    world = get_world("ATL", event.flight.date, event.injected)
    private = simulate.build_private(event, Stress(), world, EMPTY)
    rwy, slots = next(iter(private.atc.free_by_runway.items()))
    other = "R2" if rwy == "R1" else "R1"
    pkg = ResolutionPackage(event_id=event.event_id, package_id="P1", turn=2, slot=to_hhmm(slots[0]),
                            gate=private.ground.assigned_gate, score=1.0, score_breakdown={}, runway=rwy)
    ok = checker.check_separation(slots[0], private, world, EMPTY, event.event_id, runway=rwy)
    assert ok.passed, ok.detail
    if slots[0] not in private.atc.free_by_runway.get(other, []):
        bad = checker.check_separation(slots[0], private, world, EMPTY, event.event_id, runway=other)
        assert not bad.passed
    assert not checker.check_separation(slots[0], private, world, EMPTY, event.event_id, runway="R9").passed
    # a slot booked by another event right next to ours on the same runway fails the live check
    live = _RunwayHeldView({(slots[0] + 1, rwy): "EV-OTHER"})
    result = checker.check(pkg, event, private, world, live)
    assert not result.passed
    assert {r.rule.value for r in result.failed()} >= {"SEPARATION"}
    # ...but on the other runway it does not
    live_other = _RunwayHeldView({(slots[0] + 1, other): "EV-OTHER"})
    sep = checker.check_separation(slots[0], private, world, live_other, event.event_id, runway=rwy)
    assert sep.passed and checker.check_resources(
        slots[0], pkg.gate, event, private, live_other, "ATL", runway=rwy).passed


def test_fallback_is_fcfs_across_runways(dl1639_row) -> None:
    import pandas as pd

    from acacia.fallback import plan_fallback
    event = _atl_event(dl1639_row)
    world = get_world("ATL", event.flight.date, event.injected)
    private = simulate.build_private(event, Stress(), world, EMPTY)
    airports = pd.read_csv(config.DATA_DIR / "sample_airports.csv", dtype=str, keep_default_na=False)
    plan = plan_fallback(event, private, airports, world=world, ledger_view=EMPTY)
    earliest = min((s, r) for r, v in private.atc.free_by_runway.items() for s in v)
    assert not plan.divert and plan.runway is not None
    assert (to_min(plan.slot), plan.runway) >= earliest
    assert to_min(plan.slot) in private.atc.free_by_runway[plan.runway]


# --- regression: the DFW sample rows (validator finding) -----------------------------------------------
@pytest.fixture(scope="module")
def sample() -> DataStore:
    return DataStore(config.DATA_DIR, use_sample=True)


@pytest.mark.parametrize("code", DFW_SAMPLES)
def test_dfw_sample_rows_have_separated_slots_before_land_by(sample, code) -> None:
    event = to_event({"flight": code, "date": "2015-01-01"}, sample)
    world = get_world(event.flight.dest, event.flight.date, event.injected)
    private = simulate.build_private(event, Stress(), world, EMPTY)
    early = [(s, r) for r, v in private.atc.free_by_runway.items() for s in v if s <= private.pilot.land_by]
    assert early, f"{code}: no separated slot before land_by {to_hhmm(private.pilot.land_by)}"
    assert all(world.is_separated(s, private.atc.own_class, EMPTY, runway=r)[0] for s, r in early)


@pytest.mark.parametrize("code", DFW_SAMPLES)
def test_dfw_sample_rows_do_not_divert_without_stress(sample, code) -> None:
    event = to_event({"flight": code, "date": "2015-01-01"}, sample)
    entries = run_all(event, Stress(), make_agents("rule"), EMPTY, data=sample)
    record = ConsensusRecord.model_validate(entries[-1].payload)
    assert not record.package.get("divert"), record.package.get("fallback_reason")
    assert record.package.get("runway") in ("R1", "R2") and record.checker_pass is True
    assert "no separated slot" not in (record.package.get("fallback_reason") or "")


def test_rule_and_llm_atc_declare_the_same_per_runway_values(dl1639_row) -> None:
    from acacia.agents.llm import audit_declaration, expected_declaration, private_view
    event = _atl_event(dl1639_row)
    world = get_world("ATL", event.flight.date, event.injected)
    atc = simulate.build_private(event, Stress(), world, EMPTY).atc
    packet = make_agents("rule")[AgentRole.ATC].declare(event, atc)
    a_h1 = next(h for h in packet.hard if h.id == "A_H1").value
    assert a_h1 == {r: [to_hhmm(s) for s in v] for r, v in sorted(atc.free_by_runway.items())}
    assert ("hard", "A_H1", "SLOT_IN_SEQUENCE", a_h1) in expected_declaration(AgentRole.ATC, atc, event)
    assert audit_declaration(AgentRole.ATC, packet, atc, event) == []
    view = private_view(AgentRole.ATC, atc)
    assert view["arrival_runways"] == ["R1", "R2"]
    assert {(o["value"], o["runway"]) for o in view["slot_options"]} == {(o.value, o.runway) for o in packet.options}
    assert set(packet.facts_owned["traffic_by_runway"]) == {"R1", "R2"}
