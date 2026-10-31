"""HANDOVER 14 test 13: every accepted payload shape gives a valid DisruptionEvent;
non-qualifying payloads are rejected unless they carry an explicit injected event."""

import json
from pathlib import Path

import pytest

from acacia.models import DisruptionEvent, Stress
from acacia.trigger import TriggerError, stress_for, to_event, to_events, to_triggers, window_minutes
from acacia.triangulate import triangulate

DOCS = Path(__file__).resolve().parent.parent / "docs"
EXAMPLE = json.loads((DOCS / "trigger-payload.example.json").read_text())
SAMPLE_PATH = DOCS / "trigger-payload.sample.json"
DL_REF = {"airline": "DL", "flight_number": "1639", "date": "2015-02-26", "origin": "DCA", "dest": "ATL"}


def test_row_ref(sample_store, dl1639_row) -> None:
    event = to_event(EXAMPLE["single_row_reference"], sample_store, Stress())
    assert isinstance(event, DisruptionEvent)
    assert event == triangulate(dl1639_row)
    assert event.overrides == []


def test_row_ref_flight_code_and_extra_keys(sample_store) -> None:
    payload = {"flight": "DL1639", "date": "2015-02-26", "unexpected": {"x": 1}, "_note": "ok"}
    assert to_event(payload, sample_store).eta_landing == "12:11"
    assert to_event({"flight": {"airline": "DL", "flight_no": "DL1639", "date": "2015-02-26"}},
                    sample_store).flight.code == "DL1639"


def test_raw_row_inline_and_nested(sample_store, dl1639_row) -> None:
    expected = triangulate(dl1639_row)
    assert to_event(dict(dl1639_row), sample_store) == expected
    assert to_event({"source": "raw", "row": dl1639_row}, sample_store) == expected


def test_raw_row_missing_columns(sample_store, dl1639_row) -> None:
    row = {k: v for k, v in dl1639_row.items() if k != "TAXI_IN"}
    with pytest.raises(TriggerError, match="TAXI_IN"):
        to_event({"row": row}, sample_store)


def test_explicit_event_overrides_recorded(sample_store) -> None:
    payload = {**EXAMPLE["explicit_event"], "declared_emergency": True,
               "overrides": {"eta_landing": "12:20", "taxi_in": 9}}
    event = to_event(payload, sample_store)
    assert event.injected[0].end == "12:50" and event.injected[0].start == "12:01"
    assert event.eta_landing == "12:20" and event.taxi_in == 9
    assert event.overrides == ["eta_landing", "taxi_in", "injected", "declared_emergency"]
    assert stress_for(payload).declared_emergency is True


def test_full_event_shape(sample_store, dl1639_row) -> None:
    event = triangulate(dl1639_row)
    assert to_event({"event": event.model_dump(mode="json")}, sample_store) == event


def test_bad_override_rejected(sample_store) -> None:
    for overrides in ({"event_id": "EVT-X"}, {"nope": 1}, {"eta_landing": "25:00"}):
        with pytest.raises(TriggerError):
            to_event({"flight": DL_REF, "overrides": overrides}, sample_store)


def test_non_qualifying_rejected(sample_store, make_row) -> None:
    quiet = make_row(WEATHER_DELAY="0", LATE_AIRCRAFT_DELAY="10")
    with pytest.raises(TriggerError, match="does not qualify"):
        to_event(quiet, sample_store)


def test_non_qualifying_with_injected_accepted(sample_store, make_row) -> None:
    quiet = make_row(WEATHER_DELAY="0", LATE_AIRCRAFT_DELAY="10")
    injected = [{"type": "EQUIPMENT_FAILURE", "ref": "jet bridge", "start": "11:40", "end": "14:00"}]
    event = to_event({"row": quiet, "injected": injected}, sample_store)
    assert event.injected[0].type == "EQUIPMENT_FAILURE" and "injected" in event.overrides


def test_cancelled_and_window_always_rejected(sample_store, make_row) -> None:
    injected = [{"type": "RUNWAY_CLOSED", "ref": "r", "start": "11:00", "end": "12:00"}]
    with pytest.raises(TriggerError, match="cancelled"):
        to_event({"row": make_row(CANCELLED="1"), "injected": injected}, sample_store)
    with pytest.raises(TriggerError, match="window"):
        to_event({"flight": "AA5", "date": "2015-01-01"}, sample_store)  # E = 22:23
    with pytest.raises(TriggerError, match="window"):
        to_event({"flight": DL_REF, "overrides": {"eta_landing": "00:30"}}, sample_store)


def test_unknown_flight_and_bad_shapes(sample_store) -> None:
    with pytest.raises(TriggerError, match="no flight"):
        to_event({"flight": {**DL_REF, "date": "2015-02-27"}}, sample_store)
    with pytest.raises(TriggerError):
        to_event({"hello": "world"}, sample_store)
    with pytest.raises(TriggerError):
        to_event(["not", "a", "dict"], sample_store)  # type: ignore[arg-type]
    with pytest.raises(TriggerError):
        to_event({"flight": {"airline": "DL"}}, sample_store)


def test_batch(sample_store) -> None:
    events = to_events(EXAMPLE["batch"], sample_store)
    assert [e.flight.code for e in events] == ["DL1639"]
    mixed = {"triggers": [{"flight": DL_REF}, {"flight": "AA5", "date": "2015-01-01"}]}
    with pytest.raises(TriggerError, match="trigger 1"):
        to_events(mixed, sample_store)
    assert len(to_events(mixed, sample_store, skip_invalid=True)) == 1
    assert len(to_events([{"flight": DL_REF}], sample_store)) == 1


def test_batch_stress_per_trigger(sample_store) -> None:
    payload = {"triggers": [{"flight": DL_REF, "stress": {"low_fuel": True}}]}
    ((event, stress),) = to_triggers(payload, sample_store, Stress(tight_crew=True))
    assert stress.low_fuel and stress.tight_crew and not stress.corrupt_solver
    assert event.flight.code == "DL1639"


def test_scan(sample_store) -> None:
    assert [e.flight.code for e in to_events(EXAMPLE["scan"], sample_store)] == ["DL1639"]
    dfw = {"source": "scan", "airport": "DFW", "date": "2015-01-01", "window_min": 24 * 60}
    codes = [e.flight.code for e in to_events(dfw, sample_store)]
    assert codes == ["AA8", "AA23", "AA35"]  # landing order 06:47, 16:42, 17:00
    early = {**dfw, "start": "06:00", "window_min": 180}
    assert [e.flight.code for e in to_events(early, sample_store)] == ["AA8"]
    with pytest.raises(TriggerError):
        to_events({"source": "scan", "date": "2015-01-01"}, sample_store)


def test_window_minutes() -> None:
    assert window_minutes(180) == 180
    assert window_minutes("3h") == 180
    assert window_minutes("90m") == 90
    with pytest.raises(TriggerError):
        window_minutes("soon")


def test_every_example_shape_is_valid(sample_store) -> None:
    for name, payload in EXAMPLE.items():
        if name.startswith("_"):
            continue
        events = to_events(payload, sample_store)
        assert events and all(isinstance(e, DisruptionEvent) for e in events), name


@pytest.mark.skipif(not SAMPLE_PATH.exists(), reason="real trigger payload sample not provided yet")
def test_provided_sample(sample_store) -> None:
    payload = json.loads(SAMPLE_PATH.read_text())
    events = to_events(payload, sample_store, skip_invalid=True)
    assert all(isinstance(e, DisruptionEvent) for e in events)


def test_full_event_without_injected_must_qualify(sample_store, dl1639_row) -> None:
    # G1 fix: an {"event": ...} payload with no injected event obeys the qualifying rule.
    event = triangulate(dl1639_row).model_dump(mode="json")
    event["injected"] = []
    event["causes"] = {"weather": 15, "late_aircraft": 20}
    with pytest.raises(TriggerError, match="does not qualify"):
        to_event({"event": event}, sample_store)
    event["causes"] = {"weather": 16, "late_aircraft": 0}
    assert to_event({"event": event}, sample_store).injected == []


# --- CONSTRAINTS.md 3: decision time for the gate-change notice ----------------------------------
def test_decision_time_default_follows_final_injected(sample_store) -> None:
    payload = {"flight": DL_REF, "injected": [
        {"type": "EQUIPMENT_FAILURE", "ref": "jet bridge", "start": "11:20", "end": "14:00"},
        {"type": "RUNWAY_CLOSED", "ref": "r", "start": "11:50", "end": "12:30"}]}
    event = to_event(payload, sample_store)
    assert event.decision_time == "11:20"  # earliest injected start, not the triangulated one
    assert "decision_time" not in event.overrides


def test_payload_sets_decision_time_and_records_it(sample_store) -> None:
    top = to_event({"flight": DL_REF, "decision_time": "11:30"}, sample_store)
    assert top.decision_time == "11:30" and top.overrides == ["decision_time"]
    nested = to_event({"flight": DL_REF, "overrides": {"decision_time": "11:45"}}, sample_store)
    assert nested.decision_time == "11:45" and nested.overrides == ["decision_time"]
    with pytest.raises(TriggerError):
        to_event({"flight": DL_REF, "decision_time": "25:00"}, sample_store)


def test_event_without_injected_defaults_decision_time_to_e_minus_30(sample_store, dl1639_row) -> None:
    event = triangulate(dl1639_row).model_dump(mode="json")
    event.update(injected=[], decision_time=None, causes={"weather": 16, "late_aircraft": 0})
    assert to_event({"event": event}, sample_store).decision_time == "11:41"
