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
