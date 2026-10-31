"""Test 24: the back-test runs on the synthetic fixture day and returns its metrics (FABLE 9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acacia.backtest import backtest, spearman, write_backtest
from acacia.cli import main
from acacia.data import DataStore

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "multi_trigger_day.csv"
DATE = "2015-03-12"


@pytest.fixture(scope="module")
def store() -> DataStore:
    return DataStore(ROOT / "data", flights_path=FIXTURE, airports_path=ROOT / "data" / "sample_airports.csv")


def test_spearman_plain_helper() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0
    assert spearman([1, 1, 2], [1, 2, 2]) == pytest.approx(0.5)  # average ranks on ties
    assert spearman([1, 1, 1], [1, 2, 3]) is None  # no variance
    assert spearman([1], [2]) is None
    with pytest.raises(ValueError):
        spearman([1, 2], [1])


def test_backtest_on_fixture_day(store: DataStore) -> None:
    result = backtest(store, DATE)
    # 5 ATL triggers plus DL211 (MCO) and DL214 (BOS), which also carry LATE_AIRCRAFT > 20; DL214 has no next leg
    assert result["date"] == DATE and result["n_triggers"] == 7 and result["n"] == 6
    rows = {r["flight"]: r for r in result["rows"]}
    assert set(rows) == {"DL201", "DL202", "DL203", "DL204", "DL205", "DL211"}
    assert rows["DL201"]["predicted_next"] == 25 and rows["DL201"]["actual_late"] == 25
    assert rows["DL201"]["actual_dep"] == 25
    assert rows["DL204"]["predicted_next"] == 65 and rows["DL204"]["actual_late"] == 30  # slack 5
    assert rows["DL205"]["predicted_next"] == 10 and rows["DL205"]["actual_late"] == 0  # slack 15
    assert rows["DL201"]["knock_on_pred"] == 40 and rows["DL201"]["knock_on_actual"] == 30  # 25+15 vs 25+5
    for key in ("late", "dep"):
        m = result["next_leg"][key]
        assert m["n"] == 6 and m["mae"] >= 0 and m["median_ae"] >= 0
        assert 0 <= m["within_15"] <= 1 and 0 <= m["sign_agreement"] <= 1
    assert result["next_leg"]["late"]["within_15"] == pytest.approx(4 / 6, abs=1e-3)  # DL204 (35), DL211 (20) miss
    assert result["ranking"]["spearman"] is not None and -1 <= result["ranking"]["spearman"] <= 1
    assert result["graph_version"] and result["policy_version"] == "impact-0.1"


def test_backtest_writes_json_and_table(store: DataStore, tmp_path: Path) -> None:
    path, lines = write_backtest(store, DATE, runs_dir=tmp_path)
    assert path == tmp_path / f"backtest-{DATE}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["n"] == 6
    assert any("spearman" in ln.lower() for ln in lines) and any("MAE" in ln for ln in lines)


def test_cli_backtest_with_fit_date(tmp_path: Path, capsys) -> None:
    code = main(["backtest", "--date", DATE, "--fit-date", DATE, "--flights", str(FIXTURE),
                 "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0 and (tmp_path / f"backtest-{DATE}.json").exists()
    assert "fit" in out.lower() and "spearman" in out.lower()


def test_cli_triage_scan_and_window(tmp_path: Path, capsys) -> None:
    code = main(["triage", "--scan", "--date", DATE, "--flights", str(FIXTURE)])
    out = capsys.readouterr().out
    assert code == 0 and "ATL" in out and "5" in out
    code = main(["triage", "--airport", "ATL", "--date", DATE, "--window", "180", "--flights", str(FIXTURE),
                 "--runs-dir", str(tmp_path), "--quiet"])
    out = capsys.readouterr().out
    assert code == 0 and "AWAITING_SIGNOFF" in out
    assert (tmp_path / f"triage-ATL-{DATE}" / "orchestrator.jsonl").exists()
