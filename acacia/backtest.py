"""Back-test of the knock-on model against the data's own answer (FABLE_ADDENDUM 9, HANDOVER 18.7).

For each qualifying trigger on a date that is in the impact graph and has a next rotation leg:
predicted delay on that leg = max(0, hist_delay - slack); actual = the leg's LATE_AIRCRAFT_DELAY
(0 if blank) and, separately, its DEPARTURE_DELAY clipped at 0. Ranking quality is the Spearman
correlation between predicted `knock_on_min` and the actual sum of LATE_AIRCRAFT_DELAY over the
downstream rotation legs. Plain rank helper, no SciPy. Formulas and weights are never tuned
here; `--fit-date` only reports the same metrics for another date.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from acacia import config
from acacia.data import DataStore
from acacia.graph import ImpactGraph
from acacia.triage import breakdown_of, propagate, trigger_node
from acacia.triangulate import triangulate

WITHIN_MIN = 15


def _ranks(values: list[float]) -> list[float]:
    """Average ranks (1-based) with ties sharing the mean rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        mean = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = mean
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    """Spearman rank correlation (Pearson of average ranks); None with < 2 points or no variance."""
    if len(x) != len(y):
        raise ValueError(f"x and y must have the same length, got {len(x)} and {len(y)}")
    n = len(x)
    if n < 2:
        return None
    rx, ry = _ranks([float(v) for v in x]), _ranks([float(v) for v in y])
    mx, my = sum(rx) / n, sum(ry) / n
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    return round(sxy / (sxx * syy) ** 0.5, 4)


def _chain_actual(g: ImpactGraph, node: str, max_depth: int = config.PROPAGATION_MAX_DEPTH) -> int:
    """Sum of LATE_AIRCRAFT_DELAY over the downstream rotation legs (the whole chain)."""
    total, depth = 0, 0
    while depth < max_depth:
        nxt = g.rotation_next(node)
        if nxt is None:
            break
        node, _ = nxt
        total += int(g.nodes[node]["late_aircraft_delay"])
        depth += 1
    return total


def _rows(data: DataStore, g: ImpactGraph, date: str) -> tuple[list[dict], int]:
    rows: list[dict] = []
    triggers = 0
    for _, raw in data.qualifying_rows(date=date).iterrows():
        event = triangulate(raw.to_dict())
        triggers += 1
        node = trigger_node(event, g)
        nxt = g.rotation_next(node) if node is not None else None
        if node is None or nxt is None:
            continue
        next_id, slack = nxt
        leg = g.nodes[next_id]
        rows.append({
            "flight": event.flight.code, "event_id": event.event_id, "node": node, "next": next_id,
            "hist_delay": event.hist_delay_min, "slack": slack,
            "predicted_next": max(0, event.hist_delay_min - slack),
            "actual_late": int(leg["late_aircraft_delay"]), "actual_dep": max(0, int(leg["departure_delay"])),
            "knock_on_pred": breakdown_of(propagate(g, node, event.hist_delay_min))["knock_on_min"],
            "knock_on_actual": _chain_actual(g, node),
        })
    return rows, triggers


def _metrics(pred: list[int], actual: list[int]) -> dict:
    if not pred:
        return {"n": 0, "mae": None, "median_ae": None, "within_15": None, "sign_agreement": None}
    errors = [abs(p - a) for p, a in zip(pred, actual, strict=True)]
    return {
        "n": len(pred), "mae": round(statistics.fmean(errors), 2), "median_ae": statistics.median(errors),
        "within_15": round(sum(e <= WITHIN_MIN for e in errors) / len(errors), 3),
        "sign_agreement": round(sum((p > 0) == (a > 0) for p, a in zip(pred, actual, strict=True)) / len(pred), 3),
    }


def backtest(data: DataStore, date: str) -> dict:
    """Metrics for one date (see module docstring). Raises ValueError when the date has no rows."""
    day = data.rows_for_date(date)
    if day.empty:
        raise ValueError(f"no flights on {date} in {data.flights_path.name}")
    g = ImpactGraph.build(day, date)
    rows, triggers = _rows(data, g, date)
    pred = [r["predicted_next"] for r in rows]
    return {
        "date": date, "flights_file": data.flights_path.name, "graph_version": g.version,
        "policy_version": config.IMPACT_POLICY_VERSION, "n_triggers": triggers, "n": len(rows),
        "next_leg": {"late": _metrics(pred, [r["actual_late"] for r in rows]),
                     "dep": _metrics(pred, [r["actual_dep"] for r in rows])},
        "ranking": {"n": len(rows),
                    "spearman": spearman([r["knock_on_pred"] for r in rows], [r["knock_on_actual"] for r in rows])},
        "rows": rows,
    }


def _fmt(v: object) -> str:
    return "n/a" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v))


def table(result: dict, label: str = "report") -> list[str]:
    """Short text table for one back-test result."""
    lines = [f"Back-test {label}: {result['date']} ({result['flights_file']}, graph {result['graph_version']}, "
             f"{result['policy_version']})",
             f"  triggers {result['n_triggers']}, with a next rotation leg {result['n']}",
             f"  {'next-leg actual':<18}{'n':>4}{'MAE':>8}{'median':>8}{'<=15 min':>10}{'sign ok':>9}"]
    for key, name in (("late", "LATE_AIRCRAFT"), ("dep", "DEPARTURE>=0")):
        m = result["next_leg"][key]
        lines.append(f"  {name:<18}{_fmt(m['n']):>4}{_fmt(m['mae']):>8}{_fmt(m['median_ae']):>8}"
                     f"{_fmt(m['within_15']):>10}{_fmt(m['sign_agreement']):>9}")
    lines.append(f"  ranking: Spearman(knock_on_pred, actual downstream LATE_AIRCRAFT sum) = "
                 f"{_fmt(result['ranking']['spearman'])} over {result['ranking']['n']}")
    return lines


def write_backtest(data: DataStore, date: str, fit_date: str | None = None,
                   runs_dir: Path | None = None) -> tuple[Path, list[str]]:
    """Run, write runs/backtest-{date}.json and return (path, table lines)."""
    result = backtest(data, date)
    lines = table(result)
    if fit_date:
        fit = backtest(data, fit_date)
        result["fit"] = {k: v for k, v in fit.items() if k != "rows"}
        lines += [""] + table(fit, label="fit date (formulas unchanged)")
    out_dir = Path(runs_dir) if runs_dir is not None else config.RUNS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"backtest-{date}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, lines
