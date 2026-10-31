"""CLI (run, batch, verify-log, constraints, sign-off) and human sign-off."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acacia import cli, constraints
from acacia.agents.factory import make_agents
from acacia.coordinator import run_all
from acacia.log import LogChain, read_log, verify_chain
from acacia.models import ConsensusRecord, Stress
from acacia.signoff import SignOffError, last_record, sign_off
from acacia.triangulate import triangulate


@pytest.fixture(scope="module")
def dl1639(sample_store):
    return triangulate(sample_store.find_flight("DL", "1639", "2015-02-26"))


def _run_cli(tmp_path: Path, *extra: str) -> tuple[int, Path]:
    out = tmp_path / "run.jsonl"
    code = cli.main(["run", "--flight", "DL1639", "--date", "2015-02-26", "--agents", "rule",
                     "--out", str(out), *extra])
    return code, out


# --- cli ------------------------------------------------------------------------------------------
def test_run_prints_trace_and_record_and_writes_a_verified_log(tmp_path, capsys) -> None:
    code, out = _run_cli(tmp_path)
    text = capsys.readouterr().out
    assert code == 0
    assert "COORDINATOR" in text and "ConsensusRecord:" in text and "chain verified" in text
    record = json.loads(text.split("ConsensusRecord:", 1)[1])
    assert record["outcome"] == "CONSENSUS" and record["stamp"]["constraints"]
    assert verify_chain(out) == (True, None)


def test_run_with_stress_flags(tmp_path, capsys) -> None:
    code, out = _run_cli(tmp_path, "--low-fuel", "--quiet")
    assert code == 0
    rec = last_record(read_log(out))
    assert rec.outcome == "FALLBACK" and rec.package["divert"] is True


def test_run_from_payload_file(tmp_path, capsys) -> None:
    payload = tmp_path / "p.json"
    payload.write_text(json.dumps({"source": "row_ref", "flight": {
        "airline": "DL", "flight_number": "1639", "date": "2015-02-26"},
        "stress": {"corrupt_solver": True}}))
    out = tmp_path / "p.jsonl"
    assert cli.main(["run", "--payload", str(payload), "--out", str(out), "--quiet"]) == 0
    entries = read_log(out)
    assert any(e.type == "ResolutionPackage" and e.payload.get("corrupted") for e in entries)


def test_run_unknown_flight_is_a_clean_error(tmp_path, capsys) -> None:
    code = cli.main(["run", "--flight", "DL9999", "--date", "2015-02-26", "--out", str(tmp_path / "x.jsonl")])
    assert code == 2 and "error:" in capsys.readouterr().err


def test_verify_log_command_detects_tampering(tmp_path, capsys) -> None:
    _, out = _run_cli(tmp_path, "--quiet")
    assert cli.main(["verify-log", str(out)]) == 0
    lines = out.read_text().splitlines()
    row = json.loads(lines[3])
    row["payload"]["turn"] = 99
    lines[3] = json.dumps(row)
    out.write_text("\n".join(lines) + "\n")
    assert cli.main(["verify-log", str(out)]) == 1
    assert "BROKEN" in capsys.readouterr().out


def test_constraints_command_lists_every_registry_row(capsys) -> None:
    assert cli.main(["constraints"]) == 0
    text = capsys.readouterr().out
    assert constraints.constraints_version() in text
    for item in constraints.REGISTRY:
        assert item.id in text
    assert "ASSUMED" in text and "CHANGED" in text and "NOT_MODELLED" in text


def test_batch_runs_qualifying_rows(tmp_path, capsys) -> None:
    assert cli.main(["batch", "--date", "2015-01-01", "--limit", "2", "--out-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "outcome" in text and len(list(tmp_path.glob("*.jsonl"))) == 2


def test_sign_off_command_appends_and_keeps_the_chain(tmp_path, capsys) -> None:
    _, out = _run_cli(tmp_path, "--quiet")
    assert cli.main(["sign-off", str(out), "--approve", "--who", "duty manager"]) == 0
    entries = read_log(out)
    assert entries[-1].type == "SignOff" and verify_chain(out) == (True, None)
    assert last_record(entries).status == "APPROVED"


# --- sign-off -------------------------------------------------------------------------------------
def _fresh(event, stress: Stress = Stress()) -> tuple[LogChain, ConsensusRecord]:
    entries = run_all(event, stress, make_agents("rule"))
    return LogChain.resume(entries), ConsensusRecord.model_validate(entries[-1].payload)


def test_sign_off_approve_updates_status_and_chain(dl1639) -> None:
    chain, rec = _fresh(dl1639)
    entry, updated = sign_off(chain, rec, True, "Ashiq")
    assert updated.status == "APPROVED" and updated.signed_off_by == "Ashiq"
    assert entry.who == "human" and entry.type == "SignOff" and entry.payload["record"]["status"] == "APPROVED"
    assert chain.entries[-1] == entry and verify_chain(chain.entries) == (True, None)


def test_sign_off_reject_and_no_double_sign_off(dl1639) -> None:
    chain, rec = _fresh(dl1639)
    _, updated = sign_off(chain, rec, False, "Ashiq", note="prefer manual")
    assert updated.status == "REJECTED"
    with pytest.raises(SignOffError):
        sign_off(chain, updated, True, "Ashiq")


def test_sign_off_needs_a_name_and_refuses_checker_failures(dl1639) -> None:
    chain, rec = _fresh(dl1639)
    with pytest.raises(SignOffError):
        sign_off(chain, rec, True, "  ")
    failed = rec.model_copy(update={"checker_pass": False, "outcome": "FALLBACK"})
    with pytest.raises(SignOffError):
        sign_off(chain, failed, True, "Ashiq")
    _, rejected = sign_off(chain, failed, False, "Ashiq")
    assert rejected.status == "REJECTED"


def test_divert_can_be_approved(dl1639) -> None:
    chain, rec = _fresh(dl1639, Stress(low_fuel=True))
    assert rec.checker_pass is None and rec.package["divert"]
    _, updated = sign_off(chain, rec, True, "Ashiq")
    assert updated.status == "APPROVED"


def test_sign_off_refuses_another_events_log(dl1639, sample_store) -> None:
    other = triangulate(sample_store.qualifying_rows().iloc[0].to_dict())
    chain, _ = _fresh(other)
    _, rec = _fresh(dl1639)
    with pytest.raises(SignOffError):
        sign_off(chain, rec, True, "Ashiq")
