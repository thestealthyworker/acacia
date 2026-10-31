"""CLI subcommands for the triage workstream (FABLE_ADDENDUM 1, 9, 11):

    python -m acacia.cli triage --scan --date 2015-02-26 [--window 180]
    python -m acacia.cli triage --airport ATL --date 2015-02-26 --window 180 [--agents rule|llm|replay]
    python -m acacia.cli backtest --date 2015-02-26 [--fit-date 2015-02-25]

`register(subparsers)` is called by acacia.cli; each parser sets `func(args) -> int`.
`--flights PATH` points the DataStore at another flights file (a fixture); `--runs-dir` moves
the output (default runs/). A window run writes runs/triage-{AP}-{D}/orchestrator.jsonl, one
{event_id}.jsonl per negotiation (attempt files for pre-empted runs) and the ledger for the
date; the ledger file is removed first unless --keep-ledger, so a demo starts from no holds.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

from acacia import config
from acacia.agents.factory import make_agents
from acacia.backtest import write_backtest
from acacia.data import DataStore, earliest_landing
from acacia.dispatcher import Dispatcher, DispatcherError
from acacia.ledger import Ledger
from acacia.log import JsonlLog, verify_chain
from acacia.models import LogEntry, Stress
from acacia.timeutil import to_hhmm
from acacia.trigger import TriggerError

DEFAULT_WINDOW = 180


def _out(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def _format(entry: LogEntry) -> str:
    from acacia.cli import format_entry  # lazy: acacia.cli imports this module at load time

    tag = f" ({entry.event_id})" if entry.event_id and entry.who == "orchestrator" else ""
    return format_entry(entry).replace("\n", tag + "\n", 1)


def _store(args: argparse.Namespace) -> DataStore:
    if getattr(args, "flights", None):
        return DataStore(config.DATA_DIR, flights_path=args.flights)
    return DataStore()


# --- triage --scan ----------------------------------------------------------------------------------
def scan_windows(data: DataStore, date: str, window: int) -> list[dict]:
    """Per destination airport: the densest `window`-minute span of qualifying earliest landings."""
    rows = data.qualifying_rows(date=date)
    out: list[dict] = []
    if rows.empty:
        return out
    for airport, group in rows.groupby("DESTINATION_AIRPORT"):
        etas = sorted(e for e in (earliest_landing(r.to_dict()) for _, r in group.iterrows()) if e is not None)
        best, start = 0, etas[0]
        for i, t in enumerate(etas):
            count = sum(1 for u in etas[i:] if u < t + window)
            if count > best:
                best, start = count, t
        out.append({"airport": str(airport), "triggers": best, "total": len(etas),
                    "start": to_hhmm(start), "end": to_hhmm(min(start + window, config.DAY_END))})
    return sorted(out, key=lambda w: (-w["triggers"], -w["total"], w["airport"]))


def cmd_scan(args: argparse.Namespace, data: DataStore) -> int:
    windows = scan_windows(data, args.date, args.window)
    if not windows:
        _out(f"No qualifying triggers on {args.date} in {data.flights_path.name}.")
        return 0
    _out(f"Qualifying triggers on {args.date} ({data.flights_path.name}), best {args.window}-min window per airport:")
    _out(f"{'airport':<8}{'in window':>10}{'on day':>8}  window")
    for w in windows[: args.limit or len(windows)]:
        _out(f"{w['airport']:<8}{w['triggers']:>10}{w['total']:>8}  {w['start']}-{w['end']}")
    return 0


# --- triage --airport -----------------------------------------------------------------------------------
def _run_dir(args: argparse.Namespace) -> Path:
    base = Path(args.runs_dir) if args.runs_dir else config.RUNS_DIR
    return base / f"triage-{args.airport.upper()}-{args.date}"


def _write_event_logs(d: Dispatcher, run_dir: Path) -> None:
    for eid in d.events:
        segments = d.log_segments(eid)
        for i, seg in enumerate(segments):
            name = f"{eid}.jsonl" if i == len(segments) - 1 else f"{eid}-attempt{i + 1}.jsonl"
            with JsonlLog(run_dir / name) as log:
                log.write_all(seg)


def _queue_table(d: Dispatcher) -> Iterable[str]:
    yield f"{'rank':<5}{'flight':<8}{'tier':<5}{'impact':>8}  {'deadline':<9}{'cluster':<12}{'status':<17}outcome"
    for it in d.queue().items:
        rec = d.records.get(it.event_id)
        plan = rec.package.get("flight_plan", "") if rec else ""
        yield (f"{it.rank:<5}{it.flight_no:<8}{it.tier:<5}{it.impact_score:>8.1f}  {it.decision_deadline:<9}"
               f"{it.cluster_id:<12}{it.status:<17}{d.outcomes.get(it.event_id, '')} {plan}")


def cmd_window(args: argparse.Namespace, data: DataStore) -> int:
    run_dir = _run_dir(args)
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = run_dir / f"ledger-{args.date}.jsonl"
    if ledger_path.exists() and not args.keep_ledger:
        ledger_path.unlink()
    d = Dispatcher(data, make_agents(args.agents, record=getattr(args, "record", False), stress=Stress()), Ledger(args.date, runs_dir=run_dir), config.IMPACT_POLICY)
    events = d.collect(scan=(args.airport.upper(), args.date, args.window))
    if not events:
        _out(f"No qualifying triggers land at {args.airport.upper()} on {args.date} within {args.window} min.")
        return 0
    with JsonlLog(run_dir / "orchestrator.jsonl") as log:
        for entry in d.run():
            if entry.who == "orchestrator":
                log.write(entry)
            if not args.quiet or entry.who == "orchestrator":
                _out(_format(entry))
    ok, bad = verify_chain([e for e in d.entries])
    _write_event_logs(d, run_dir)
    _out()
    for line in _queue_table(d):
        _out(line)
    _out(f"Orchestrator log: {run_dir / 'orchestrator.jsonl'} ({len(d.entries)} entries, chain "
         f"{'verified' if ok else f'BROKEN at seq {bad}'}); ledger {ledger_path.name}, "
         f"{len(d.ledger.all_reservations())} reservation(s). Sign off with: acacia sign-off {run_dir}/<event>.jsonl")
    return 0 if ok else 1


def cmd_triage(args: argparse.Namespace) -> int:
    data = _store(args)
    if args.scan:
        return cmd_scan(args, data)
    if not args.airport:
        raise TriggerError("triage needs --scan or --airport AP")
    try:
        return cmd_window(args, data)
    except DispatcherError as exc:
        raise TriggerError(str(exc)) from exc


# --- backtest --------------------------------------------------------------------------------------------
def cmd_backtest(args: argparse.Namespace) -> int:
    data = _store(args)
    try:
        path, lines = write_backtest(data, args.date, args.fit_date, Path(args.runs_dir) if args.runs_dir else None)
    except ValueError as exc:
        raise TriggerError(str(exc)) from exc
    for line in lines:
        _out(line)
    _out(f"Written: {path}")
    return 0


# --- parser ---------------------------------------------------------------------------------------------
def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--flights", help="flights CSV to use instead of data/flights.csv (e.g. a fixture)")
    p.add_argument("--runs-dir", help="output directory (default runs/)")


def register(subparsers) -> None:
    t = subparsers.add_parser("triage", help="rank, cluster and negotiate every trigger in an airport window")
    t.add_argument("--scan", action="store_true", help="list airport windows by qualifying trigger count")
    t.add_argument("--airport", help="destination airport for a window run")
    t.add_argument("--window", type=int, default=DEFAULT_WINDOW, help="window length in minutes (default 180)")
    t.add_argument("--agents", choices=("rule", "llm", "replay"), default="rule")
    t.add_argument("--record", action="store_true", help="record LLM responses (llm mode)")
    t.add_argument("--limit", type=int, default=0, help="--scan: show at most N airports")
    t.add_argument("--quiet", action="store_true", help="print orchestrator entries and the queue only")
    t.add_argument("--keep-ledger", action="store_true", help="replay an existing ledger file for the run")
    _common(t)
    t.set_defaults(func=cmd_triage)
    b = subparsers.add_parser("backtest", help="compare predicted knock-on with the data's actual delays")
    b.add_argument("--fit-date", help="also report the same metrics for this date (no tuning)")
    _common(b)
    b.set_defaults(func=cmd_backtest)
