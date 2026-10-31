"""Test 12 (HANDOVER 14): the log hash chain verifies, and tampering with one entry breaks it."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from acacia.log import (
    GENESIS, JsonlLog, LogChain, canonical_json, entry_hash, read_log, run_path, verify_chain,
)
from acacia.models import LogEntry


def _chain(n: int = 4) -> LogChain:
    chain = LogChain(event_id="EVT-TEST")
    for i in range(n):
        chain.append("coordinator", "STATE", f"step {i}", state="SOLVING", stage=3, turn=1,
                     payload={"i": i, "nested": {"b": 2, "a": [1, 2]}})
    return chain


def test_canonical_json_is_sorted_and_compact() -> None:
    assert canonical_json({"b": 1, "a": {"d": 2, "c": "é"}}) == '{"a":{"c":"é","d":2},"b":1}'


def test_entry_hash_matches_the_spec_formula() -> None:
    payload = {"x": 1}
    body = json.dumps({"seq": 1, "who": "atc", "type": "Decision", "payload": payload},
                      sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256((GENESIS + body).encode()).hexdigest()
    assert entry_hash(GENESIS, 1, "atc", "Decision", payload) == expected


def test_chain_starts_at_genesis_and_links() -> None:
    entries = _chain().entries
    assert entries[0].prev_hash == GENESIS
    assert [e.seq for e in entries] == [1, 2, 3, 4]
    for prev, cur in zip(entries, entries[1:]):
        assert cur.prev_hash == prev.hash
    assert all(e.event_id == "EVT-TEST" for e in entries)
    assert verify_chain(entries) == (True, None)


def test_empty_chain_verifies() -> None:
    assert verify_chain([]) == (True, None)


def test_tampered_payload_breaks_verification_at_that_entry() -> None:
    entries = _chain().entries
    bad = entries[2].model_copy(update={"payload": {"i": 99}})
    assert verify_chain([entries[0], entries[1], bad, entries[3]]) == (False, 3)


def test_tampered_hash_breaks_the_next_link() -> None:
    entries = _chain().entries
    forged = entries[1].model_copy(update={"hash": "0" * 64})
    ok, bad = verify_chain([entries[0], forged, *entries[2:]])
    assert not ok and bad == 2


def test_deleted_or_reordered_entries_break_verification() -> None:
    entries = _chain().entries
    assert verify_chain([entries[0], *entries[2:]]) == (False, 3)
    assert verify_chain([entries[1], entries[0], *entries[2:]])[0] is False


def test_text_and_timestamp_are_not_hashed() -> None:
    entries = _chain().entries
    edited = entries[1].model_copy(update={"text": "reworded", "ts": "2000-01-01T00:00:00Z"})
    assert verify_chain([entries[0], edited, *entries[2:]]) == (True, None)


def test_jsonl_round_trip_and_file_tampering(tmp_path: Path) -> None:
    entries = _chain().entries
    path = tmp_path / "EVT-TEST.jsonl"
    with JsonlLog(path) as out:
        out.write_all(entries)
    assert verify_chain(path) == (True, None)
    assert [e.hash for e in read_log(path)] == [e.hash for e in entries]

    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["payload"]["i"] = 42
    lines[1] = json.dumps(row)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify_chain(path) == (False, 2)


def test_unparseable_line_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "broken.jsonl"
    entries = _chain(2).entries
    path.write_text(entries[0].model_dump_json() + "\n{not json\n", encoding="utf-8")
    assert verify_chain(path) == (False, 2)


def test_missing_file_does_not_verify(tmp_path: Path) -> None:
    assert verify_chain(tmp_path / "absent.jsonl")[0] is False


def test_resume_refuses_a_broken_chain() -> None:
    entries = _chain().entries
    chain = LogChain.resume(entries)
    nxt = chain.append("human", "SignOff", "ok", payload={"approved": True})
    assert nxt.seq == 5 and nxt.prev_hash == entries[-1].hash
    with pytest.raises(ValueError):
        LogChain.resume([entries[0], entries[2]])


def test_run_path_names_the_event(tmp_path: Path) -> None:
    assert run_path("EVT-1", tmp_path) == tmp_path / "EVT-1.jsonl"


def test_entries_validate_as_log_entries() -> None:
    for e in _chain().entries:
        assert isinstance(LogEntry.model_validate(e.model_dump()), LogEntry)
