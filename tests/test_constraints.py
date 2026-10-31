"""CONSTRAINTS.md loader: workbook values reach the code unchanged."""

import pytest

from acacia import constraints as c


def test_atl_capacity_and_turnaround():
    lim = c.airport_limits("ATL")
    assert (lim.max_ops_per_hour, lim.min_turnaround_min, lim.spare_gates) == (90, 45, 3)
    assert c.arrival_rate("ATL") == pytest.approx(45.0)
    assert c.arrival_rate("ATL", "moderate") == pytest.approx(27.0)
    assert c.arrival_rate("ATL", "severe") == pytest.approx(18.0)


def test_medium_hub_turnaround_and_unknown_airport():
    assert c.min_turnaround("DCA") == 35
    unknown = c.airport_limits("ZZZ")
    assert unknown.assumed and unknown.size_class == "Non-hub"


@pytest.mark.parametrize("w,level", [(0, "none"), (15, "none"), (16, "moderate"), (45, "moderate"), (46, "severe")])
def test_storm_level(w, level):
    assert c.storm_level(w) == level


def test_wake_table_and_minutes():
    assert c.wake_nm("Heavy", "Small") == 6
    assert c.wake_nm("Large", "Small") == 4
    assert c.wake_nm("Large", "Large") == 3
    assert c.wake_minutes("Large", "Large") == pytest.approx(3 * 60 / 140)
    assert round(c.wake_minutes("Heavy", "Small"), 1) == 2.6


def test_fuel_rules():
    assert c.wake_class(2001) == "Heavy" and c.wake_class(547) == "Large"
    assert c.land_by(eta=731, air_time=125) == 731 + 45 + 6
    assert c.holding_fuel_kg(slot=741, eta=731, wake="Large") == pytest.approx(400.0)
    assert c.holding_fuel_kg(slot=700, eta=731, wake="Large") == 0


def test_registry_ids_unique_and_version_stable():
    ids = [i.id for i in c.REGISTRY]
    assert len(ids) == len(set(ids))
    assert len(c.constraints_version()) == 12


def test_crew_duty_end_from_scheduled_arrival_and_block_time() -> None:
    # arrive 12:00 (720), block 120 -> departs 10:00, report 09:00, 13 h -> 22:00
    assert c.crew_duty_end(720, 120) == 1320
    assert c.crew_duty_end(720, None) is None and c.crew_duty_end(720, 0) is None


def test_decision_time_is_shared_by_checker_and_agents(dl1639_row) -> None:
    from acacia import checker
    from acacia.agents import declared
    from acacia.triangulate import triangulate

    event = triangulate(dl1639_row)
    assert checker.decision_time_min is c.decision_time_min is declared.decision_time_min
    assert c.decision_time_min(event) == 12 * 60 + 1  # earliest injected start


def test_registry_lists_the_build_deviations() -> None:
    ids = [item.id for item in c.REGISTRY]
    assert len(ids) == len(set(ids))
    assert {"GATE-1", "GATE-2", "WAKE-7", "CAP-8", "FUEL-5", "CREW-1", "FUEL-6", "KIT-3", "KIT-4"} <= set(ids)
    assert all(item.status in ("AS_GIVEN", "ASSUMED", "CHANGED", "NOT_MODELLED") for item in c.REGISTRY)


def test_workbook_numbers_used_outside_the_loader_come_from_it() -> None:
    """Validator G3: no workbook number is re-typed outside acacia/constraints.py."""
    from acacia import solver

    assert solver.FUEL_SPAN_MIN == c.SPARE_HOLDING_MIN
    assert solver.OVERTIME_COST_FACTOR == c.OVERTIME_COST_FACTOR


def test_review_deviations_are_listed() -> None:
    """Validator FINAL: every rule change found in review has a REGISTRY row the UI renders."""
    rows = {item.id: item for item in c.REGISTRY}
    assert "injected" in rows["PLAY-1"].used_value
    assert "next free slot" in rows["KIT-3"].used_value
    assert rows["KIT-5"].status == "ASSUMED" and "0.6" in rows["KIT-5"].used_value
    assert "Heavy" in rows["WAKE-7"].used_value
