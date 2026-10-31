"""Shared fixtures: a DataStore on the sample data, the rotation fixture and a row factory."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from acacia.data import FLIGHT_COLUMNS, DataStore

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# DL1639 DCA-ATL 2015-02-26 (reconstructed sample row 6)
DL1639_VALUES = (
    "2015,2,26,4,DL,1639,UNKNOWN,DCA,ATL,1000,1021,21,15,1036,120,150,125,547,1241,10,1200,1251,51,"
    "0,0,,0,0,0,21,30"
).split(",")
DL1639_ROW: dict[str, str] = dict(zip(FLIGHT_COLUMNS, DL1639_VALUES, strict=True))


@pytest.fixture(scope="session")
def sample_store() -> DataStore:
    """DataStore on sample_flights.csv / sample_airports.csv, even if the full files exist."""
    return DataStore(DATA_DIR, use_sample=True)


@pytest.fixture(scope="session")
def rotation_fixture_path() -> Path:
    return FIXTURES / "rotation_day.csv"


@pytest.fixture(scope="session")
def rotation_store(rotation_fixture_path: Path) -> DataStore:
    """DataStore whose flights are the hand-built rotation day (FABLE_ADDENDUM 10)."""
    return DataStore(DATA_DIR, flights_path=rotation_fixture_path,
                     airports_path=DATA_DIR / "sample_airports.csv")


@pytest.fixture
def dl1639_row() -> dict[str, str]:
    return dict(DL1639_ROW)


@pytest.fixture
def make_row() -> Callable[..., dict[str, str]]:
    """Factory: the DL1639 row with column overrides, e.g. make_row(WEATHER_DELAY="16")."""

    def _make(**overrides: object) -> dict[str, str]:
        unknown = set(overrides) - set(FLIGHT_COLUMNS)
        if unknown:
            raise KeyError(f"unknown columns {sorted(unknown)}")
        return {**DL1639_ROW, **{k: str(v) for k, v in overrides.items()}}

    return _make
