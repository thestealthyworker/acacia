"""Command line: run one negotiation, batch a date, verify a log, list the challenge constraints.

    python -m acacia.cli run --flight DL1639 --date 2015-02-26 --agents rule
    python -m acacia.cli batch --date 2015-02-26 --limit 5
    python -m acacia.cli verify-log runs/<event_id>.jsonl
    python -m acacia.cli constraints
    python -m acacia.cli sign-off runs/<event_id>.jsonl --approve --who "duty manager"

If acacia.cli_triage exists, its `register(subparsers)` adds the triage and backtest commands;
each registered parser must set `func`, a callable taking the parsed args and returning an
exit code.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
from collections.abc import Iterable
from pathlib import Path

from acacia import constraints
from acacia.agents.factory import AgentModeUnavailable, make_agents
from acacia.coordinator import run
from acacia.data import DataStore
from acacia.log import JsonlLog, run_path, verify_chain
from acacia.models import ConsensusRecord, DisruptionEvent, LogEntry, Stress
from acacia.signoff import SignOffError, sign_off_file
from acacia.trigger import TriggerError, stress_for, to_event


try:  # Fable's triage/backtest commands, when that module is built
    from acacia.cli_triage import register as _triage_register  # type: ignore[import-not-found]
except ModuleNotFoundError as exc:
    if exc.name != "acacia.cli_triage":
        raise
    _triage_register = None

WRAP = 110


# --- output -------------------------------------------------------------------------------
def _out(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def format_entry(e: LogEntry) -> str:
    head = f"#{e.seq:<3} T{e.turn or 0} {e.who.upper():<11} {e.type}" + (f" [{e.state}]" if e.state else "")
    body = textwrap.fill(e.text, WRAP, initial_indent="      ", subsequent_indent="      ")
    return f"{head}\n{body}"


def stream(entries: Iterable[LogEntry], log: JsonlLog | None, quiet: bool) -> list[LogEntry]:
    """Print each entry as it arrives (and write it to the JSONL log)."""
    out: list[LogEntry] = []
    for e in entries:
        out.append(e)
        if log is not None:
            log.write(e)
        if not quiet:
            _out(format_entry(e))
    return out


def stress_from(args: argparse.Namespace) -> Stress:
    return Stress(low_fuel=args.low_fuel, tight_crew=args.tight_crew, corrupt_solver=args.corrupt_solver)


# --- run ----------------------------------------------------------------------------------------
def _event_for(args: argparse.Namespace, data: DataStore) -> tuple[DisruptionEvent, Stress]:
    base = stress_from(args)
    if args.payload:
        try:
            payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TriggerError(f"cannot read payload {args.payload}: {exc}") from exc
    else:
        if not args.flight or not args.date:
            raise TriggerError("run needs --flight and --date, or --payload FILE")
        payload = {"source": "row_ref", "flight": args.flight, "date": args.date}
        if args.origin or args.dest:
            payload.update({k: v for k, v in (("origin", args.origin), ("dest", args.dest)) if v})
    stress = stress_for(payload, base)
    return to_event(payload, data, stress), stress


def _run_one(event: DisruptionEvent, stress: Stress, agents: dict, data: DataStore,
             out_path: Path, quiet: bool) -> tuple[ConsensusRecord | None, list[LogEntry]]:
    with JsonlLog(out_path) as log:
        entries = stream(run(event, stress, agents, data=data), log, quiet)
    last = entries[-1] if entries else None
    if last is None or last.type != "ConsensusRecord":
        return None, entries
    return ConsensusRecord.model_validate(last.payload), entries


def cmd_run(args: argparse.Namespace) -> int:
    data = DataStore()
    event, stress = _event_for(args, data)
    agents = make_agents(args.agents, record=args.record, stress=stress)
    out_path = Path(args.out) if args.out else run_path(event.event_id)
    rec, entries = _run_one(event, stress, agents, data, out_path, args.quiet)
    ok, bad = verify_chain(entries)
    _out()
    _out(f"Log: {out_path} ({len(entries)} entries, chain {'verified' if ok else f'BROKEN at seq {bad}'})")
    if rec is None:
        _out("Run ended without a ConsensusRecord (pre-empted).")
        return 1
    _out("ConsensusRecord:")
    _out(json.dumps(rec.model_dump(mode="json"), indent=2))
    return 0 if ok else 1


# --- batch --------------------------------------------------------------------------------------
def cmd_batch(args: argparse.Namespace) -> int:
    data = DataStore()
    rows = data.qualifying_rows(date=args.date, airport=args.airport)
    if args.limit:
        rows = rows.head(args.limit)
    if rows.empty:
        _out(f"No qualifying flights on {args.date}.")
        return 0
    stress = stress_from(args)
    agents = make_agents(args.agents, stress=stress)
    out_dir = Path(args.out_dir) if args.out_dir else None
    _out(f"{'flight':<10} {'route':<8} {'outcome':<9} {'turns':<5} {'checker':<7} plan")
    for _, row in rows.iterrows():
        payload = {"source": "row_ref", "row": row.to_dict()}
        try:
            event = to_event(payload, data, stress)
        except TriggerError as exc:
            _out(f"skipped row: {exc}")
            continue
        rec, _ = _run_one(event, stress, agents, data, run_path(event.event_id, out_dir), quiet=True)
        route = f"{event.flight.origin}-{event.flight.dest}"
        if rec is None:
            _out(f"{event.flight.code:<10} {route:<8} PREEMPTED")
            continue
        _out(f"{event.flight.code:<10} {route:<8} {rec.outcome:<9} {rec.turns_used:<5} "
             f"{str(rec.checker_pass):<7} {rec.package.get('flight_plan', '')}")
    return 0


# --- verify-log, constraints, sign-off -----------------------------------------------------------
def cmd_verify(args: argparse.Namespace) -> int:
    ok, bad = verify_chain(args.file)
    _out(f"{args.file}: " + ("OK, hash chain verifies" if ok else f"BROKEN, first bad entry seq {bad}"))
    return 0 if ok else 1


TABLE_COLS = (("id", 7), ("status", 12), ("rule", 28), ("workbook_value", 26), ("used_value", 40),
              ("note", 30))  # (field, max column width); longer cells wrap


def _table_row(cells: list[str], width: list[int]) -> list[str]:
    wrapped = [textwrap.wrap(c, w) or [""] for c, w in zip(cells, width)]
    height = max(len(w) for w in wrapped)
    return ["  ".join((w[i] if i < len(w) else "").ljust(width[j]) for j, w in enumerate(wrapped)).rstrip()
            for i in range(height)]


def constraints_table() -> list[str]:
    """constraints.REGISTRY as a fixed-width text table, long cells wrapped."""
    rows = [[str(getattr(c, k)) for k, _ in TABLE_COLS] for c in constraints.REGISTRY]
    width = [min(cap, max(len(k), *(len(r[i]) for r in rows))) for i, (k, cap) in enumerate(TABLE_COLS)]
    out = _table_row([k.upper() for k, _ in TABLE_COLS], width) + ["  ".join("-" * w for w in width)]
    for r in rows:
        out += _table_row(r, width)
    return out


def cmd_constraints(args: argparse.Namespace) -> int:
    _out(f"Challenge constraints (CONSTRAINTS.md), version {constraints.constraints_version()}")
    for row in constraints_table():
        _out(row)
    deviations = sum(c.status != "AS_GIVEN" for c in constraints.REGISTRY)
    _out(f"{len(constraints.REGISTRY)} constraints, {deviations} deviations (ASSUMED, CHANGED, NOT_MODELLED).")
    return 0


def cmd_signoff(args: argparse.Namespace) -> int:
    entry, rec = sign_off_file(args.file, args.approve, args.who, args.note)
    _out(format_entry(entry))
    _out(f"{rec.event_id}: {rec.status} by {rec.signed_off_by}")
    return 0


# --- parser ---------------------------------------------------------------------------------------
def _add_stress(p: argparse.ArgumentParser) -> None:
    p.add_argument("--agents", choices=("rule", "llm", "replay"), default="rule")
    p.add_argument("--low-fuel", action="store_true")
    p.add_argument("--tight-crew", action="store_true")
    p.add_argument("--corrupt-solver", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acacia", description="Acacia multi-agent disruption negotiation")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="negotiate one flight")
    p.add_argument("--flight", help="flight code, e.g. DL1639")
    p.add_argument("--date", help="YYYY-MM-DD")
    p.add_argument("--origin")
    p.add_argument("--dest")
    p.add_argument("--payload", help="trigger payload JSON file (wins over --flight/--date)")
    p.add_argument("--record", action="store_true", help="record LLM responses (llm mode)")
    p.add_argument("--out", help="JSONL log path (default runs/{event_id}.jsonl)")
    p.add_argument("--quiet", action="store_true", help="print only the ConsensusRecord")
    _add_stress(p)
    p.set_defaults(func=cmd_run)
    b = sub.add_parser("batch", help="negotiate every qualifying flight on a date")
    b.add_argument("--date", required=True)
    b.add_argument("--limit", type=int, default=0)
    b.add_argument("--airport", help="destination airport filter")
    b.add_argument("--out-dir", help="directory for the JSONL logs (default runs/)")
    _add_stress(b)
    b.set_defaults(func=cmd_batch)
    v = sub.add_parser("verify-log", help="verify a JSONL log's hash chain")
    v.add_argument("file")
    v.set_defaults(func=cmd_verify)
    c = sub.add_parser("constraints", help="list the challenge constraints and every deviation")
    c.set_defaults(func=cmd_constraints)
    s = sub.add_parser("sign-off", help="approve or reject the plan in a JSONL log")
    s.add_argument("file")
    s.add_argument("--who", required=True)
    s.add_argument("--note")
    choice = s.add_mutually_exclusive_group(required=True)
    choice.add_argument("--approve", action="store_true")
    choice.add_argument("--reject", action="store_true")
    s.set_defaults(func=cmd_signoff)
    if _triage_register is not None:
        _triage_register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (TriggerError, LookupError, SignOffError, AgentModeUnavailable, FileNotFoundError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
