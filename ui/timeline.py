"""Timeline (HANDOVER 13 item 6), Altair: runway lane, gate lanes, limit lines.

Built from declarations in the visible log only: ATC traffic and offered slots, Ground gate
free times, Pilot and Ground limits, the package under vote and the historical arrival.
"""

from __future__ import annotations

from dataclasses import dataclass

import altair as alt
import pandas as pd

from acacia import constraints
from acacia.models import DisruptionEvent
from acacia.timeutil import to_min
from ui.view import View, hard, soft

RUNWAY = "Runway"
MAX_GATE_LANES = 4
PAD_MIN = 20
LANE_STEP_PX = 34
LEGEND_COLUMNS = 3  # four columns truncated the longest label (QA fix 8)
LEGEND_LABEL_LIMIT_PX = 220
COLOURS = {
    "storm window": "#e5b157", "occupied (simulated)": "#9aa7b1", "unavailable": "#bf3b2b",
    "turnaround (under vote)": "#ad5f17", "traffic": "#5a6a77", "offered slot": "#0d7f8f",
    "under vote": "#4a4fc2",
}
LIMIT_COLOURS = {"fuel limit": "#bf3b2b", "connection deadline": "#4a4fc2", "crew shift end": "#ad5f17",
                 "historical arrival": "#5a6a77"}


@dataclass(frozen=True)
class TimelineData:
    bands: pd.DataFrame  # lane, start, end, kind
    marks: pd.DataFrame  # lane, t, kind
    limits: pd.DataFrame  # t, kind (limit label)
    lanes: list[str]


def _ts(date: str, minute: int) -> pd.Timestamp:
    return pd.Timestamp(date) + pd.Timedelta(minutes=int(minute))


def _hhmm_list(values: object) -> list[int]:
    out = []
    for v in values if isinstance(values, list) else []:
        try:
            out.append(to_min(str(v)))
        except (ValueError, TypeError):
            continue
    return out


def _limits(view: View, event: DisruptionEvent) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = [(to_min(event.hist_arrival), "historical arrival")]
    p, g = view.packets.get("PILOT"), view.packets.get("GROUND")
    for item, label in ((hard(p, "LAND_BY"), "fuel limit"), (soft(p, "gate_arrival_by"), "connection deadline"),
                        (soft(g, "turnaround_done_by"), "crew shift end")):
        raw = (item or {}).get("value" if label == "fuel limit" else "target")
        if isinstance(raw, str):
            try:
                found.append((to_min(raw), label))
            except ValueError:
                continue
    return found


def _gate_rows(view: View, event: DisruptionEvent, t0: int) -> tuple[list[str], list[dict]]:
    g = view.packets.get("GROUND")
    free = (hard(g, "GATE_FREE_FROM") or {}).get("value")
    if not isinstance(free, dict):
        return [], []
    lanes = [f"Gate {k}" for k in list(free)[:MAX_GATE_LANES]]
    rows = []
    for gate, when in list(free.items())[:MAX_GATE_LANES]:
        if when is None or when == "unavailable":
            rows.append({"lane": f"Gate {gate}", "start": t0, "end": t0 + 240, "kind": "unavailable"})
        elif isinstance(when, str) and to_min(when) > t0:
            rows.append({"lane": f"Gate {gate}", "start": t0, "end": to_min(when), "kind": "occupied (simulated)"})
    sent = view.sent
    if sent and f"Gate {sent.get('gate')}" in lanes:
        turn = int((g or {}).get("facts_owned", {}).get("min_turn_min")
                   or constraints.min_turnaround(event.flight.dest))
        arr = to_min(sent["slot"]) + event.taxi_in
        rows.append({"lane": f"Gate {sent['gate']}", "start": arr, "end": arr + turn,
                     "kind": "turnaround (under vote)"})
    return lanes, rows


def timeline_data(view: View, event: DisruptionEvent) -> TimelineData | None:
    atc = view.packets.get("ATC")
    if not atc:
        return None
    eta = to_min(event.eta_landing)
    t0 = (eta - PAD_MIN) // 10 * 10
    offered = _hhmm_list((hard(atc, "SLOT_IN_SEQUENCE") or {}).get("value"))
    traffic = [t for t in _hhmm_list((atc.get("facts_owned") or {}).get("traffic")) if t >= t0]
    limits = _limits(view, event)
    t1 = max([eta, *offered, *(t for t, _ in limits)]) + PAD_MIN
    marks = [{"lane": RUNWAY, "t": t, "kind": "traffic"} for t in traffic if t <= t1]
    marks += [{"lane": RUNWAY, "t": t, "kind": "offered slot"} for t in offered]
    if view.sent:
        marks.append({"lane": RUNWAY, "t": to_min(view.sent["slot"]), "kind": "under vote"})
    bands = [{"lane": RUNWAY, "start": max(t0, to_min(i.start)), "end": min(t1, to_min(i.end)), "kind": "storm window"}
             for i in event.injected if i.type == "RUNWAY_CLOSED"]
    lanes, gate_rows = _gate_rows(view, event, t0)
    bands += [{**r, "end": min(r["end"], t1)} for r in gate_rows]
    d = event.flight.date
    bands_df = pd.DataFrame([{**b, "start": _ts(d, b["start"]), "end": _ts(d, b["end"])} for b in bands],
                            columns=["lane", "start", "end", "kind"])
    marks_df = pd.DataFrame([{**m, "t": _ts(d, m["t"])} for m in marks], columns=["lane", "t", "kind"])
    limits_df = pd.DataFrame([{"t": _ts(d, t), "kind": lbl} for t, lbl in limits], columns=["t", "kind"])
    return TimelineData(bands_df, marks_df, limits_df, [RUNWAY, *lanes])


def timeline_chart(data: TimelineData) -> alt.LayerChart:
    palette = {**COLOURS, **LIMIT_COLOURS}
    y = alt.Y("lane:N", sort=data.lanes, title=None, scale=alt.Scale(domain=data.lanes))
    colour = alt.Color("kind:N", scale=alt.Scale(domain=list(palette), range=list(palette.values())),
                       legend=alt.Legend(title=None, orient="bottom", columns=LEGEND_COLUMNS,
                                         labelLimit=LEGEND_LABEL_LIMIT_PX))
    x_axis = alt.Axis(format="%H:%M", title=None, grid=True)
    bands = alt.Chart(data.bands).mark_bar(opacity=0.55, cornerRadius=2).encode(
        x=alt.X("start:T", axis=x_axis), x2="end:T", y=y, color=colour,
        tooltip=["lane", "kind", alt.Tooltip("start:T", format="%H:%M"), alt.Tooltip("end:T", format="%H:%M")])
    marks = alt.Chart(data.marks).mark_tick(thickness=3, size=LANE_STEP_PX - 8).encode(
        x="t:T", y=y, color=colour, tooltip=["kind", alt.Tooltip("t:T", format="%H:%M")])
    rules = alt.Chart(data.limits).mark_rule(strokeDash=[5, 3], strokeWidth=2).encode(
        x="t:T", color=colour, tooltip=["kind", alt.Tooltip("t:T", format="%H:%M")])
    return alt.layer(bands, marks, rules).properties(height=alt.Step(LANE_STEP_PX))
