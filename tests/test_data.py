"""HANDOVER 14 tests 1 and 2: parsing, qualifying filter, classification; DataStore queries."""

import pytest

from acacia import config
from acacia.data import (
    DataStore, earliest_landing, in_window, normalise_flight_no, num, qualifies_base, qualifying,
)
from acacia.models import ScenarioType
from acacia.triangulate import classify


# --- test 1: parsing --------------------------------------------------------------------------
def test_blank_causes_are_zero(make_row) -> None:
    row = make_row(WEATHER_DELAY="", LATE_AIRCRAFT_DELAY="", AIRLINE_DELAY="", SECURITY_DELAY="")
    assert num(row, "WEATHER_DELAY") == 0
    assert num(row, "LATE_AIRCRAFT_DELAY") == 0
    assert num({}, "AIR_SYSTEM_DELAY") == 0


def test_num_rejects_garbage(make_row) -> None:
    with pytest.raises(ValueError):
        num(make_row(WEATHER_DELAY="lots"), "WEATHER_DELAY")


# --- test 2: qualifying and classification ----------------------------------------------------
@pytest.mark.parametrize(
    "weather, late, expected",
    [("15", "0", False), ("16", "0", True), ("0", "20", False), ("0", "21", True),
     ("15", "20", False), ("16", "21", True), ("", "", False)],
)
def test_qualifying_edges(make_row, weather: str, late: str, expected: bool) -> None:
    row = make_row(WEATHER_DELAY=weather, LATE_AIRCRAFT_DELAY=late)
    assert qualifies_base(row) is expected
    assert qualifying(row) is expected  # DL1639 lands 11:41..12:11, inside the window


@pytest.mark.parametrize("col", ["CANCELLED", "DIVERTED"])
def test_cancelled_or_diverted_never_qualifies(make_row, col: str) -> None:
    assert not qualifying(make_row(**{col: "1"}))


@pytest.mark.parametrize(
    "weather, late, expected",
    [(16, 21, ScenarioType.COMBINED), (16, 20, ScenarioType.WEATHER), (15, 21, ScenarioType.KNOCK_ON),
     (0, 21, ScenarioType.KNOCK_ON), (30, 0, ScenarioType.WEATHER), (15, 20, ScenarioType.KNOCK_ON)],
)
def test_classification_edges(weather: int, late: int, expected: ScenarioType) -> None:
    assert classify(weather, late) == expected


def test_time_window_bounds() -> None:
    assert not in_window(59)
    assert in_window(60)
    assert in_window(1290)
    assert not in_window(1291)
    assert not in_window(None)


def test_window_applies_to_qualifying(make_row) -> None:
    # S=21:50, Ti=10, L=21 -> E = 1310-10+21 = 1321 (22:01) > 21:30
    late_row = make_row(SCHEDULED_ARRIVAL="2150", WHEELS_ON="2359", ARRIVAL_TIME="2359")
    assert earliest_landing(late_row) == 1321
    assert qualifies_base(late_row) and not qualifying(late_row)


# --- DataStore ----------------------------------------------------------------------------------
def test_store_falls_back_to_sample(tmp_path, sample_store: DataStore) -> None:
    assert sample_store.flights_path.name == "sample_flights.csv"
    assert len(sample_store.flights) == 6
    assert sample_store.date_range == ("2015-01-01", "2015-02-26")
    assert sample_store.skipped_airport_rows == 0
    assert sample_store.airport("ATL") is not None


def test_store_missing_files(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        DataStore(tmp_path)


def test_store_skips_non_iata_rows(tmp_path, sample_store: DataStore) -> None:
    src = sample_store.flights_path.read_text().splitlines()
    bad = src[1].replace(",DFW,HNL,", ",10397,HNL,")
    path = tmp_path / "flights.csv"
    path.write_text("\n".join([src[0], bad, *src[2:]]) + "\n")
    store = DataStore(sample_store.data_dir, flights_path=path)
    assert store.skipped_airport_rows == 1
    assert len(store.flights) == 5


def test_find_flight(sample_store: DataStore) -> None:
    row = sample_store.find_flight("DL", "1639", "2015-02-26")
    assert row["ORIGIN_AIRPORT"] == "DCA" and row["DESTINATION_AIRPORT"] == "ATL"
    assert sample_store.find_flight("dl", "DL1639", "2015-02-26", origin="DCA", dest="ATL") == row
    with pytest.raises(LookupError):
        sample_store.find_flight("DL", "1639", "2015-02-27")
    with pytest.raises(LookupError):
        sample_store.find_flight("DL", "1639", "2015-02-26", dest="ORD")


def test_find_flight_ambiguous(tmp_path, sample_store: DataStore) -> None:
    src = sample_store.flights_path.read_text().splitlines()
    dup = src[6].replace(",DCA,ATL,", ",ATL,MCO,")
    path = tmp_path / "flights.csv"
    path.write_text("\n".join([*src, dup]) + "\n")
    store = DataStore(sample_store.data_dir, flights_path=path)
    with pytest.raises(ValueError, match="ambiguous"):
        store.find_flight("DL", "1639", "2015-02-26")
    assert store.find_flight("DL", "1639", "2015-02-26", origin="ATL")["DESTINATION_AIRPORT"] == "MCO"


def test_rows_for_date(sample_store: DataStore) -> None:
    assert len(sample_store.rows_for_date("2015-01-01")) == 5
    assert len(sample_store.rows_for_date("2015-02-26")) == 1
    assert sample_store.rows_for_date("2015-03-01").empty


def test_qualifying_rows(sample_store: DataStore) -> None:
    # AA5 (E 22:23) and AA16 (E 21:36) fall outside the window; AA8, AA23, AA35, DL1639 qualify.
    flights = sorted(sample_store.qualifying_rows()["FLIGHT_NUMBER"])
    assert flights == ["1639", "23", "35", "8"]
    assert list(sample_store.qualifying_rows(airport="ATL")["FLIGHT_NUMBER"]) == ["1639"]
    assert len(sample_store.qualifying_rows(date="2015-01-01", airport="DFW")) == 3


def test_normalise_flight_no() -> None:
    assert normalise_flight_no("DL", "DL1639") == "1639"
    assert normalise_flight_no("DL", 1639) == "1639"
    assert normalise_flight_no("AA", "0008") == "8"
    with pytest.raises(ValueError):
        normalise_flight_no("DL", "AA12")


def test_distance_min_turn_is_gone_and_seed_helper() -> None:
    assert not hasattr(config, "min_turn")  # CAP-6: constraints.min_turnaround(airport) replaces it
    a = config.seeded_rng(config.seed_key("x")).random()
    assert a == config.seeded_rng(config.seed_key("x")).random()
    with pytest.raises(ValueError):
        config.seeded_rng("no-salt|x")


def test_earliest_landing_unwraps_wheels_on_after_midnight(make_row) -> None:
    # G1 fix: S 21:00, wheels-on 00:30 next day -> E = S - Ti + L + AL + SE, not 00:30.
    row = make_row(SCHEDULED_ARRIVAL="2100", WHEELS_ON="0030", ARRIVAL_TIME="0040",
                   ARRIVAL_DELAY="220", WEATHER_DELAY="200", LATE_AIRCRAFT_DELAY="0")
    assert earliest_landing(row) == 21 * 60 - 10
    assert qualifying(row)
