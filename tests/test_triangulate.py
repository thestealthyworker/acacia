"""HANDOVER 14 test 3: earliest landing E, classification, injected event, event_id."""

import hashlib

from acacia import config
from acacia.models import ScenarioType
from acacia.timeutil import to_min
from acacia.triangulate import make_event_id, triangulate


def test_dl1639(sample_store) -> None:
    event = triangulate(sample_store.find_flight("DL", "1639", "2015-02-26"))
    assert event.scenario_type == ScenarioType.COMBINED
    assert event.eta_landing == "12:11"  # min(720 - 10 + 21 + 0 + 0, 761)
    assert event.sched_arrival == "12:00" and event.hist_arrival == "12:51"
    assert event.hist_delay_min == 51 and event.taxi_in == 10 and event.distance == 547
    assert event.causes.weather == 30 and event.causes.late_aircraft == 21
    assert event.flight.code == "DL1639" and event.flight.tail == "UNKNOWN"
    assert event.flight.date == "2015-02-26"
    (closure,) = event.injected
    assert closure.type == "RUNWAY_CLOSED" and closure.ref == "storm (moderate): arrival capacity x0.6"
    assert closure.severity == "moderate"  # WEATHER_DELAY 30 <= 45 (CAP-3)
    assert closure.start == "12:01"
    assert 731 + 30 <= to_min(closure.end) <= 731 + 30 + 10
    assert event.turn == 1 and event.overrides == []


def test_event_id_formula() -> None:
    digest = hashlib.sha256(b"DL1639-2015-02-26-DCAATL").hexdigest()[:8].upper()
    assert make_event_id("DL", "1639", "2015-02-26", "DCA", "ATL") == f"EVT-{digest}"


def test_deterministic(dl1639_row) -> None:
    assert triangulate(dl1639_row) == triangulate(dict(dl1639_row))


def test_closure_end_uses_salt(dl1639_row, monkeypatch) -> None:
    ends = set()
    for salt in ("s1", "s2", "s3", "s4", "s5", "s6"):
        monkeypatch.setattr(config, "SEED_SALT", salt)
        ends.add(triangulate(dl1639_row).injected[0].end)
    assert len(ends) > 1  # the random part moves with the salt


def test_wheels_on_caps_eta(make_row) -> None:
    # S - Ti + L = 720 - 10 + 21 = 731, but wheels-on at 11:55 = 715 is earlier
    event = triangulate(make_row(WHEELS_ON="1155"))
    assert event.eta_landing == "11:55"


def test_blank_wheels_on_uses_arrival_minus_taxi(make_row) -> None:
    # A - Ti = 12:20 - 10 = 730 < 731
    event = triangulate(make_row(WHEELS_ON="", ARRIVAL_TIME="1220"))
    assert event.eta_landing == "12:10"


def test_blank_taxi_in_defaults_to_8(make_row) -> None:
    event = triangulate(make_row(TAXI_IN=""))
    assert event.taxi_in == 8
    assert event.eta_landing == "12:13"  # 720 - 8 + 21


def test_knock_on_equipment_failure(make_row) -> None:
    event = triangulate(make_row(WEATHER_DELAY="", AIRLINE_DELAY="5", SECURITY_DELAY="2"))
    assert event.scenario_type == ScenarioType.KNOCK_ON
    eta = 720 - 10 + 21 + 5 + 2
    assert to_min(event.eta_landing) == eta
    (failure,) = event.injected
    assert failure.type == "EQUIPMENT_FAILURE" and failure.ref == "jet bridge out at one gate"
    assert to_min(failure.start) == eta - 20 and to_min(failure.end) == eta + 180


def test_injected_end_clamped_to_day(make_row) -> None:
    # E = 21:20 -> E + 180 would pass midnight; clamped to 23:59
    event = triangulate(make_row(WEATHER_DELAY="0", SCHEDULED_ARRIVAL="2109", WHEELS_ON="2359"))
    assert event.eta_landing == "21:20"
    assert event.injected[0].end == "23:59"


def test_weather_only(make_row) -> None:
    event = triangulate(make_row(LATE_AIRCRAFT_DELAY="0"))
    assert event.scenario_type == ScenarioType.WEATHER
    assert event.eta_landing == "11:50"


def test_air_time_and_scheduled_time_filled_from_row(make_row) -> None:
    event = triangulate(make_row())
    assert event.air_time == 125 and event.scheduled_time == 120  # CONSTRAINTS.md 4 inputs
    blank = triangulate(make_row(AIR_TIME="", SCHEDULED_TIME="0"))
    assert blank.air_time is None and blank.scheduled_time is None


def test_severe_storm_when_weather_delay_over_45(make_row) -> None:
    (storm,) = triangulate(make_row(WEATHER_DELAY="46")).injected
    assert storm.severity == "severe" and storm.ref == "storm (severe): arrival capacity x0.4"
