"""Impact graph over all flights of one date (FABLE_ADDENDUM 5, HANDOVER 18.1).

Nodes are flight rows; ROTATION edges follow a tail through the day, CONNECTION edges link an
inbound flight to same-airline departures 35..120 min later. Plain dicts, no networkx. Nothing
here calls an LLM; every random value (connection pax) is seeded through `config.seeded_rng`.

Times are int minutes after local midnight; node ids keep the original hhmm integer.

Rotation slack uses the challenge workbook's minimum turnaround at the connecting station
(`constraints.min_turnaround(a.dest)`, CONSTRAINTS.md 3 / CAP-6), not the distance rule.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

import pandas as pd

from acacia import config, constraints
from acacia.data import is_iata, normalise_flight_no, num, row_date
from acacia.timeutil import parse_hhmm

log = logging.getLogger(__name__)

ROTATION_TOLERANCE = 30  # b may depart up to 30 min before a's scheduled arrival (data noise)
NO_TAIL = ("", "UNKNOWN")
SKIP_KEYS = ("cancelled", "diverted", "blank_time", "bad_airport", "other_date")
NODE_FIELDS = (
    "airline", "flight_no", "tail", "origin", "dest", "sched_dep", "sched_arr", "sched_dep_hhmm",
    "distance", "arrival_delay", "departure_delay", "late_aircraft_delay", "file_order",
)


def node_id_for(airline: str, flight_no: str, date: str, origin: str, dest: str, sched_dep_hhmm: int) -> str:
    """Base node id "{AL}{FN}-{date}-{O}{D}-{hhmm:04d}" (collision suffixes are added by build)."""
    return f"{airline}{normalise_flight_no(airline, flight_no)}-{date}-{origin}{dest}-{sched_dep_hhmm:04d}"


def _skip_reason(row: dict[str, object], date: str) -> str | None:
    if row_date(row) != date:
        return "other_date"
    if num(row, "CANCELLED") != 0:
        return "cancelled"
    if num(row, "DIVERTED") != 0:
        return "diverted"
    if parse_hhmm(row.get("SCHEDULED_DEPARTURE")) is None or parse_hhmm(row.get("SCHEDULED_ARRIVAL")) is None:
        return "blank_time"
    if not (is_iata(row.get("ORIGIN_AIRPORT")) and is_iata(row.get("DESTINATION_AIRPORT"))):
        return "bad_airport"
    return None


def _node_from_row(row: dict[str, object], order: int) -> dict[str, object]:
    airline = str(row["AIRLINE"]).strip().upper()
    dep_raw = int(float(str(row["SCHEDULED_DEPARTURE"]).strip()))
    return {
        "airline": airline,
        "flight_no": normalise_flight_no(airline, row["FLIGHT_NUMBER"]),
        "tail": str(row.get("TAIL_NUMBER") or "").strip().upper(),
        "origin": str(row["ORIGIN_AIRPORT"]).strip().upper(),
        "dest": str(row["DESTINATION_AIRPORT"]).strip().upper(),
        "sched_dep": parse_hhmm(row["SCHEDULED_DEPARTURE"]),
        "sched_arr": parse_hhmm(row["SCHEDULED_ARRIVAL"]),
        "sched_dep_hhmm": dep_raw,
        "distance": num(row, "DISTANCE"),
        "arrival_delay": num(row, "ARRIVAL_DELAY"),
        "departure_delay": num(row, "DEPARTURE_DELAY"),
        "late_aircraft_delay": num(row, "LATE_AIRCRAFT_DELAY"),
        "file_order": order,
    }


def _slack(a: dict[str, object], b: dict[str, object]) -> int:
    """max(0, b.dep - a.arr - min_turn), with min_turn the workbook turnaround at a.dest (CAP-6)."""
    turn = constraints.min_turnaround(str(a["dest"]))
    return max(0, int(b["sched_dep"]) - int(a["sched_arr"]) - turn)


def _code_frame(nodes: Iterable[dict[str, object]]) -> pd.DataFrame:
    """Integer-coded node table for the merge: airline, origin, dest, tail codes and times."""
    rows = list(nodes)
    airlines = pd.Categorical([str(n["airline"]) for n in rows]).codes
    airports = pd.Categorical([str(n["origin"]) for n in rows] + [str(n["dest"]) for n in rows]).codes
    tails = pd.Categorical([str(n["tail"]) for n in rows])
    tail_codes = tails.codes.astype("int64")
    tail_codes[pd.Series(tails.astype(str)).isin(NO_TAIL).to_numpy()] = -1
    return pd.DataFrame({
        "idx": range(len(rows)), "airline": airlines, "origin": airports[: len(rows)],
        "dest": airports[len(rows):], "tail": tail_codes,
        "sched_dep": [int(n["sched_dep"]) for n in rows], "sched_arr": [int(n["sched_arr"]) for n in rows],
    })


def _bucketed_merge(frame: pd.DataFrame) -> pd.DataFrame:
    """Inbound x outbound pairs with the same airline and airport whose departure hour bucket is
    0..2 hours after the arrival bucket (covers every gap in 0..120 min); gap is dep - arr."""
    hi = config.CONNECTION_WINDOW[1]
    offsets = range(hi // 60 + 1)
    inbound = frame[["idx", "airline", "dest", "sched_arr", "tail"]].rename(
        columns={"idx": "a", "dest": "airport", "tail": "a_tail"})
    inbound = pd.concat(
        [inbound.assign(bucket=inbound["sched_arr"] // 60 + k) for k in offsets], ignore_index=True)
    outbound = frame[["idx", "airline", "origin", "sched_dep", "tail"]].rename(
        columns={"idx": "b", "origin": "airport", "tail": "b_tail"})
    outbound = outbound.assign(bucket=outbound["sched_dep"] // 60)
    joined = inbound.merge(outbound, on=["airline", "airport", "bucket"])
    return joined.assign(gap=joined["sched_dep"] - joined["sched_arr"])


@dataclass
class ImpactGraph:
    date: str
    nodes: dict[str, dict[str, object]] = field(default_factory=dict)
    rotation: dict[str, tuple[str, int]] = field(default_factory=dict)
    conns: dict[str, list[tuple[str, int, int]]] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SKIP_KEYS, 0))
    version: str = ""
    _index: dict[tuple[str, str, str, str], list[str]] = field(default_factory=dict, repr=False)

    # --- build ---------------------------------------------------------------------------------
    @classmethod
    def build(cls, day_rows: pd.DataFrame, date: str) -> "ImpactGraph":
        """Build from every row of `date` (cancelled, diverted, blank-time and bad-airport rows skipped)."""
        g = cls(date=date)
        g._add_nodes(day_rows.to_dict("records"))
        g._add_rotation_edges()
        g._add_connection_edges()
        g.version = hashlib.sha256(
            f"{date}|{len(g.nodes)}|{config.IMPACT_POLICY_VERSION}".encode()
        ).hexdigest()[:12]
        skipped = {k: v for k, v in g.skipped.items() if v}
        log.info("impact graph %s: %d nodes, %d rotation edges, %d connection edges, skipped %s",
                 date, len(g.nodes), len(g.rotation), sum(len(v) for v in g.conns.values()), skipped)
        return g

    def _add_nodes(self, rows: Iterable[dict[str, object]]) -> None:
        for order, row in enumerate(rows):
            reason = _skip_reason(row, self.date)
            if reason is not None:
                self.skipped[reason] += 1
                continue
            node = _node_from_row(row, order)
            node_id = self._unique_id(node)
            self.nodes[node_id] = node
            key = (node["airline"], node["flight_no"], node["origin"], node["dest"])
            self._index.setdefault(key, []).append(node_id)

    def _unique_id(self, node: dict[str, object]) -> str:
        base = node_id_for(str(node["airline"]), str(node["flight_no"]), self.date,
                           str(node["origin"]), str(node["dest"]), int(node["sched_dep_hhmm"]))
        node_id, suffix = base, 1
        while node_id in self.nodes:
            suffix += 1
            node_id = f"{base}-{suffix}"
        return node_id

    def _add_rotation_edges(self) -> None:
        by_tail: dict[str, list[str]] = {}
        for node_id, node in self.nodes.items():
            tail = str(node["tail"])
            if tail in NO_TAIL:
                continue
            by_tail.setdefault(tail, []).append(node_id)
        for tail in sorted(by_tail):
            legs = sorted(by_tail[tail], key=lambda n: (self.nodes[n]["sched_dep"], self.nodes[n]["file_order"]))
            for a_id, b_id in zip(legs, legs[1:], strict=False):
                a, b = self.nodes[a_id], self.nodes[b_id]
                if a["dest"] != b["origin"] or int(b["sched_dep"]) < int(a["sched_arr"]) - ROTATION_TOLERANCE:
                    continue
                self.rotation[a_id] = (b_id, _slack(a, b))

    def _add_connection_edges(self) -> None:
        """Pandas merge on (airline, airport, hour bucket) with integer codes, then the gap filter."""
        if not self.nodes:
            return
        ids = list(self.nodes)
        frame = _code_frame(self.nodes.values())
        joined = _bucketed_merge(frame)
        lo, hi = config.CONNECTION_WINDOW
        same_tail = (joined["a_tail"] == joined["b_tail"]) & (joined["a_tail"] >= 0)
        joined = joined[(joined["gap"] >= lo) & (joined["gap"] <= hi) & ~same_tail & (joined["a"] != joined["b"])]
        joined = joined.assign(b_id=[ids[i] for i in joined["b"]])
        joined = joined.sort_values(["a", "gap", "b_id"]).groupby("a").head(config.CONNECTION_FANOUT)
        for a, b_id, gap in zip(joined["a"], joined["b_id"], joined["gap"], strict=True):
            a_id = ids[int(a)]
            pax = config.seeded_rng(config.seed_key("pax", a_id, b_id)).randint(2, 15)
            self.conns.setdefault(a_id, []).append((str(b_id), int(gap), pax))

    # --- queries -------------------------------------------------------------------------------
    def find(self, airline: str, flight_no: str, origin: str, dest: str, sched_dep: int | None = None) -> str | None:
        """Node id for a flight, or None. Several matches: filter by sched_dep (minutes), else first in file order."""
        airline = airline.strip().upper()
        try:
            number = normalise_flight_no(airline, flight_no)
        except ValueError:
            return None
        hits = self._index.get((airline, number, origin.strip().upper(), dest.strip().upper()), [])
        if sched_dep is not None:
            hits = [n for n in hits if self.nodes[n]["sched_dep"] == sched_dep]
        return hits[0] if hits else None

    def rotation_next(self, node_id: str) -> tuple[str, int] | None:
        """(next node on the same tail, slack minutes) or None."""
        return self.rotation.get(node_id)

    def connections(self, node_id: str) -> list[tuple[str, int, int]]:
        """[(outbound node, gap minutes, simulated pax)], smallest gap first."""
        return list(self.conns.get(node_id, []))

    def node(self, node_id: str) -> dict[str, object]:
        """Copy of the node attributes. Raises KeyError if absent."""
        if node_id not in self.nodes:
            raise KeyError(f"node {node_id!r} is not in the impact graph for {self.date}")
        return dict(self.nodes[node_id])
