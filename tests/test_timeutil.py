"""HANDOVER 14 test 1 (parsing) and the HH:MM helpers."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from acacia.timeutil import is_hhmm, parse_hhmm, to_hhmm, to_min


@pytest.mark.parametrize(
    "raw, expected",
    [("0648", 408), ("648", 408), ("2400", 0), ("0000", 0), ("1200", 720), ("2359", 1439), ("0005", 5)],
)
def test_parse_hhmm_values(raw: str, expected: int) -> None:
    assert parse_hhmm(raw) == expected


@pytest.mark.parametrize("blank", ["", "  ", None, float("nan"), "nan"])
def test_parse_hhmm_blank_is_none(blank: object) -> None:
    assert parse_hhmm(blank) is None


def test_parse_hhmm_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_hhmm("12:00x")


def test_to_min_and_back() -> None:
    assert to_min("06:48") == 408
    assert to_min("00:00") == 0
    assert to_hhmm(731) == "12:11"
    assert to_hhmm(0) == "00:00"


@pytest.mark.parametrize("bad", ["24:00", "7:05", "12:60", "1200", "", "ab:cd"])
def test_to_min_rejects_bad(bad: str) -> None:
    with pytest.raises(ValueError):
        to_min(bad)


def test_to_hhmm_wraps_and_rejects_non_int() -> None:
    assert to_hhmm(1440) == "00:00"
    assert to_hhmm(-10) == "23:50"
    with pytest.raises(ValueError):
        to_hhmm(12.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        to_hhmm(True)  # type: ignore[arg-type]


@given(st.integers(min_value=0, max_value=1439))
def test_roundtrip(minutes: int) -> None:
    text = to_hhmm(minutes)
    assert is_hhmm(text)
    assert to_min(text) == minutes
