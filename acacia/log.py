"""Hash-chained negotiation log (HANDOVER 11.4).

Each entry's hash is sha256(prev_hash + canonical_json({seq, who, type, payload})), starting
from "GENESIS". Timestamps, text and state are not hashed (so two identical runs give the
same chain); everything that matters for audit goes in the payload. One JSONL file per event
in runs/; `verify_chain` re-computes the chain and names the first bad entry.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from acacia import config
from acacia.models import LogEntry

GENESIS = "GENESIS"


def canonical_json(obj: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, UTF-8 kept, non-JSON values via str()."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def entry_hash(prev_hash: str, seq: int, who: str, type_: str, payload: dict) -> str:
    body = canonical_json({"seq": seq, "who": who, "type": type_, "payload": payload})
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _jsonable(payload: dict | None) -> dict:
    """Round-trip through canonical JSON so the stored payload is exactly what was hashed."""
    return json.loads(canonical_json(payload or {}))


class LogChain:
    """Builds LogEntry objects with consecutive seq numbers and linked hashes (append only)."""

    def __init__(self, event_id: str | None = None, entries: Sequence[LogEntry] = ()) -> None:
        self.event_id = event_id
        self._entries: list[LogEntry] = list(entries)

    @classmethod
    def resume(cls, entries: Sequence[LogEntry]) -> "LogChain":
        """Continue an existing chain; raises ValueError when it does not verify."""
        ok, bad = verify_chain(list(entries))
        if not ok:
            raise ValueError(f"cannot extend a broken log chain (first bad seq {bad})")
        event_id = next((e.event_id for e in entries if e.event_id), None)
        return cls(event_id=event_id, entries=entries)

    @property
    def entries(self) -> list[LogEntry]:
        return list(self._entries)

    @property
    def head(self) -> str:
        return self._entries[-1].hash if self._entries else GENESIS

    def append(self, who: str, type_: str, text: str, *, state: str | None = None,
               stage: int | None = None, turn: int | None = None, payload: dict | None = None,
               dispatched_by: dict | None = None) -> LogEntry:
        seq = len(self._entries) + 1
        body = _jsonable(payload)
        prev = self.head
        entry = LogEntry(
            seq=seq, ts=now_ts(), who=who, type=type_, state=state, stage=stage, turn=turn,
            text=text, payload=body, prev_hash=prev, hash=entry_hash(prev, seq, who, type_, body),
            event_id=self.event_id, dispatched_by=dispatched_by,
        )
        self._entries.append(entry)
        return entry


# --- verification ------------------------------------------------------------------------------
def _bad_entry(entry: LogEntry, expected_seq: int, prev: str) -> bool:
    if entry.seq != expected_seq or entry.prev_hash != prev:
        return True
    return entry.hash != entry_hash(prev, entry.seq, entry.who, entry.type, entry.payload)


def verify_chain(path_or_entries: str | Path | Iterable[LogEntry | dict]) -> tuple[bool, int | None]:
    """(True, None) when every link verifies, else (False, seq of the first bad entry).

    An unparseable line or entry reports its 1-based position as the bad seq.
    """
    if isinstance(path_or_entries, (str, Path)):
        try:
            raw: list[Any] = read_raw(path_or_entries)
        except ValueError as exc:
            pos = getattr(exc, "position", 1)
            return False, pos
    else:
        raw = list(path_or_entries)
    prev = GENESIS
    for i, item in enumerate(raw, start=1):
        try:
            entry = item if isinstance(item, LogEntry) else LogEntry.model_validate(item)
        except ValidationError:
            return False, i
        if _bad_entry(entry, i, prev):
            return False, entry.seq
        prev = entry.hash
    return True, None


# --- JSONL files -------------------------------------------------------------------------------
class LogParseError(ValueError):
    def __init__(self, message: str, position: int) -> None:
        super().__init__(message)
        self.position = position


def read_raw(path: str | Path) -> list[Any]:
    """Every non-blank line of a JSONL file, parsed. Raises LogParseError on a bad line."""
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LogParseError(f"cannot read {path}: {exc}", 1) from exc
    out: list[Any] = []
    for i, line in enumerate((ln for ln in lines if ln.strip()), start=1):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise LogParseError(f"{path} line {i} is not JSON: {exc}", i) from exc
    return out


def read_log(path: str | Path) -> list[LogEntry]:
    """Load a JSONL log as LogEntry objects (no chain check; use verify_chain)."""
    try:
        return [LogEntry.model_validate(item) for item in read_raw(path)]
    except ValidationError as exc:
        raise ValueError(f"{path} holds an invalid log entry: {exc}") from exc


def run_path(event_id: str, runs_dir: Path | str | None = None) -> Path:
    return Path(runs_dir or config.RUNS_DIR) / f"{event_id}.jsonl"


class JsonlLog:
    """Writes log entries, one JSON object per line. Truncates the file on open unless append."""

    def __init__(self, path: Path | str, append: bool = False) -> None:
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a" if append else "w", encoding="utf-8")
        except OSError as exc:
            raise OSError(f"cannot open log file {self.path}: {exc}") from exc

    @classmethod
    def for_event(cls, event_id: str, runs_dir: Path | str | None = None) -> "JsonlLog":
        return cls(run_path(event_id, runs_dir))

    def write(self, entry: LogEntry) -> None:
        self._fh.write(entry.model_dump_json() + "\n")
        self._fh.flush()

    def write_all(self, entries: Iterable[LogEntry]) -> None:
        for entry in entries:
            self.write(entry)

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "JsonlLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
