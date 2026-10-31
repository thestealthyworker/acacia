"""CSV loading, parsing helpers and the qualifying filter (HANDOVER 4.1, 4.2; PLAN 2.A.3)."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path

import pandas as pd

from acacia import config
from acacia.timeutil import parse_hhmm

log = logging.getLogger(__name__)

FLIGHT_COLUMNS: tuple[str, ...] = (
    "YEAR", "MONTH", "DAY", "DAY_OF_WEEK", "AIRLINE", "FLIGHT_NUMBER", "TAIL_NUMBER",
    "ORIGIN_AIRPORT", "DESTINATION_AIRPORT", "SCHEDULED_DEPARTURE", "DEPARTURE_TIME",
    "DEPARTURE_DELAY", "TAXI_OUT", "WHEELS_OFF", "SCHEDULED_TIME", "ELAPSED_TIME", "AIR_TIME",
    "DISTANCE", "WHEELS_ON", "TAXI_IN", "SCHEDULED_ARRIVAL", "ARRIVAL_TIME", "ARRIVAL_DELAY",
    "DIVERTED", "CANCELLED", "CANCELLATION_REASON", "AIR_SYSTEM_DELAY", "SECURITY_DELAY",
    "AIRLINE_DELAY", "LATE_AIRCRAFT_DELAY", "WEATHER_DELAY",
)
DATE_COL = "_DATE"  # derived "YYYY-MM-DD"
_IATA_RE = r"^[A-Z]{3}$"

Row = Mapping[str, object]


# --- scalar row helpers ------------------------------------------------------------------
def num(row: Row, col: str, default: int = 0) -> int:
    """Integer value of a CSV cell; blank or missing -> default (delay causes: blank means 0)."""
    raw = row.get(col)
    if raw is None:
        return default
    text = str(raw).strip()
    if text == "" or text.lower() == "nan":
        return default
    try:
        return int(round(float(text)))
    except ValueError as exc:
        raise ValueError(f"column {col}: not a number: {raw!r}") from exc


def row_date(row: Row) -> str:
    """"YYYY-MM-DD" from YEAR, MONTH, DAY."""
    return f"{num(row, 'YEAR'):04d}-{num(row, 'MONTH'):02d}-{num(row, 'DAY'):02d}"


def taxi_in(row: Row) -> int:
    ti = num(row, "TAXI_IN", default=-1)
    return config.DEFAULT_TAXI_IN if ti < 0 else ti


def earliest_landing(row: Row) -> int | None:
    """E = min(S - Ti + L + AL + SE, WO); A - Ti if WO is blank (HANDOVER 4.3). None if S blank."""
    sched = parse_hhmm(row.get("SCHEDULED_ARRIVAL"))
    if sched is None:
        return None
    ti = taxi_in(row)
    recoverable = sched - ti + num(row, "LATE_AIRCRAFT_DELAY") + num(row, "AIRLINE_DELAY") + num(
        row, "SECURITY_DELAY"
    )
    wheels_on = parse_hhmm(row.get("WHEELS_ON"))
    if wheels_on is None:
        actual = parse_hhmm(row.get("ARRIVAL_TIME"))
        if actual is None:
            actual = sched + num(row, "ARRIVAL_DELAY")
        wheels_on = actual - ti
    return min(recoverable, _unwrap_after(wheels_on, sched))


def _unwrap_after(t: int, sched: int) -> int:
    """Clock times are mod 1440: a wheels-on more than 12 h before S landed after midnight."""
    return t + config.MINUTES_PER_DAY if t < sched - config.MINUTES_PER_DAY // 2 else t


def qualifies_base(row: Row) -> bool:
    """HANDOVER 4.2: not cancelled, not diverted, and W > 15 or L > 20."""
    if num(row, "CANCELLED") != 0 or num(row, "DIVERTED") != 0:
        return False
    return (
        num(row, "WEATHER_DELAY") > config.WEATHER_THRESHOLD
        or num(row, "LATE_AIRCRAFT_DELAY") > config.LATE_AIRCRAFT_THRESHOLD
    )


def in_window(eta: int | None) -> bool:
    """PLAN 2.A.3: earliest landing within 01:00..21:30 inclusive."""
    return eta is not None and config.QUALIFY_WINDOW_START <= eta <= config.QUALIFY_WINDOW_END


def qualifying(row: Row) -> bool:
    """Full qualifying rule: HANDOVER 4.2 plus the PLAN 2.A.3 time window."""
    return qualifies_base(row) and in_window(earliest_landing(row))


def normalise_flight_no(airline: str, flight_no: object) -> str:
    """"DL1639" or "1639" or 1639 -> "1639"."""
    text = str(flight_no).strip().upper()
    if text.startswith(airline.upper()) and text[len(airline):].isdigit():
        text = text[len(airline):]
    if not text.isdigit():
        raise ValueError(f"invalid flight number {flight_no!r}")
    return str(int(text))


# --- data store ----------------------------------------------------------------------------
class DataStore:
    """Loads flights, airports and airlines. Falls back to sample files when the full ones are absent."""

    def __init__(
        self,
        data_dir: Path | str | None = None,
        flights_path: Path | str | None = None,
        airports_path: Path | str | None = None,
        use_sample: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else config.DATA_DIR
        self.flights_path = Path(flights_path) if flights_path else self._pick("flights", use_sample)
        self.airports_path = Path(airports_path) if airports_path else self._pick("airports", use_sample)
        self.flights = self._load_flights(self.flights_path)
        self.airports = self._read_csv(self.airports_path)
        airlines_path = self.data_dir / "airlines.csv"
        self.airlines = self._read_csv(airlines_path) if airlines_path.exists() else pd.DataFrame()
        self.date_range = self._date_range()
        log.info("flights %s: %d rows, dates %s to %s", self.flights_path.name, len(self.flights),
                 *self.date_range)

    def _pick(self, stem: str, use_sample: bool) -> Path:
        full = self.data_dir / f"{stem}.csv"
        sample = self.data_dir / f"sample_{stem}.csv"
        if not use_sample and full.exists():
            return full
        if sample.exists():
            if not use_sample:
                log.warning("%s not found; falling back to %s", full.name, sample.name)
            return sample
        raise FileNotFoundError(f"neither {full} nor {sample} exists")

    @staticmethod
    def _read_csv(path: Path) -> pd.DataFrame:
        try:
            return pd.read_csv(path, dtype=str, keep_default_na=False)
        except (OSError, pd.errors.ParserError) as exc:
            raise ValueError(f"cannot read {path}: {exc}") from exc

    def _load_flights(self, path: Path) -> pd.DataFrame:
        df = self._read_csv(path)
        missing = [c for c in FLIGHT_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"{path.name} is missing columns: {missing}")
        ok = df["ORIGIN_AIRPORT"].str.match(_IATA_RE) & df["DESTINATION_AIRPORT"].str.match(_IATA_RE)
        skipped = int((~ok).sum())
        self.skipped_airport_rows = skipped
        if skipped:
            log.warning("skipped %d rows with non-3-letter airport codes", skipped)
        df = df[ok].reset_index(drop=True)
        df[DATE_COL] = (
            df["YEAR"].str.zfill(4) + "-" + df["MONTH"].str.zfill(2) + "-" + df["DAY"].str.zfill(2)
        )
        return df

    def _date_range(self) -> tuple[str | None, str | None]:
        if self.flights.empty:
            return (None, None)
        return (str(self.flights[DATE_COL].min()), str(self.flights[DATE_COL].max()))

    # --- queries --------------------------------------------------------------------------
    def rows_for_date(self, date: str) -> pd.DataFrame:
        return self.flights[self.flights[DATE_COL] == date]

    def find_flight(
        self, airline: str, flight_no: object, date: str,
        origin: str | None = None, dest: str | None = None,
    ) -> dict[str, str]:
        """One flight row as a dict. Raises LookupError if absent, ValueError if ambiguous."""
        airline = airline.strip().upper()
        number = normalise_flight_no(airline, flight_no)
        df = self.rows_for_date(date)
        mask = (df["AIRLINE"] == airline) & (df["FLIGHT_NUMBER"].str.lstrip("0") == number)
        if origin:
            mask &= df["ORIGIN_AIRPORT"] == origin.upper()
        if dest:
            mask &= df["DESTINATION_AIRPORT"] == dest.upper()
        hits = df[mask]
        if hits.empty:
            raise LookupError(f"no flight {airline}{number} on {date}"
                              + (f" {origin or '*'}-{dest or '*'}" if origin or dest else ""))
        if len(hits) > 1:
            routes = ", ".join(hits["ORIGIN_AIRPORT"] + "-" + hits["DESTINATION_AIRPORT"])
            raise ValueError(f"{airline}{number} on {date} is ambiguous ({routes}); give origin/dest")
        return _row_dict(hits.iloc[0])

    def qualifying_rows(self, date: str | None = None, airport: str | None = None) -> pd.DataFrame:
        """Rows passing `qualifying`, optionally for one date and one destination airport."""
        df = self.flights if date is None else self.rows_for_date(date)
        if airport:
            df = df[df["DESTINATION_AIRPORT"] == airport.upper()]
        weather = pd.to_numeric(df["WEATHER_DELAY"], errors="coerce").fillna(0)
        late = pd.to_numeric(df["LATE_AIRCRAFT_DELAY"], errors="coerce").fillna(0)
        candidates = df[(weather > config.WEATHER_THRESHOLD) | (late > config.LATE_AIRCRAFT_THRESHOLD)]
        if candidates.empty:
            return candidates
        keep = candidates.apply(lambda r: qualifying(r.to_dict()), axis=1).astype(bool)
        return candidates[keep]

    def airport(self, code: str) -> dict[str, str] | None:
        hits = self.airports[self.airports["IATA_CODE"] == code.upper()]
        return None if hits.empty else _row_dict(hits.iloc[0])


def _row_dict(series: pd.Series) -> dict[str, str]:
    return {k: v for k, v in series.to_dict().items() if k != DATE_COL}


def is_iata(code: object) -> bool:
    return isinstance(code, str) and re.match(_IATA_RE, code) is not None
