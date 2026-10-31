"""Contract validators: REJECT needs reason_code and cites; HH:MM format; shared protocol."""

import pytest
from pydantic import ValidationError

from acacia.models import (
    AgentRole, CheckResult, ConsensusRecord, Decision, EmptyLedgerView, Injected, LedgerView, LogEntry,
    ReasonCode, Reservation, ResolutionPackage, RuleResult, Stress, Verdict,
)


def _decision(**kw) -> Decision:
    base = dict(event_id="EVT-1", package_id="P1", agent=AgentRole.PILOT, turn=2,
                verdict=Verdict.ACCEPT, rationale="ok")
    return Decision(**{**base, **kw})


def test_accept_needs_nothing_else() -> None:
    assert _decision().verdict == Verdict.ACCEPT


def test_reject_needs_reason_code() -> None:
    with pytest.raises(ValidationError, match="reason_code"):
        _decision(verdict=Verdict.REJECT, cites="P_H1")


def test_reject_needs_cites() -> None:
    with pytest.raises(ValidationError, match="cite"):
        _decision(verdict=Verdict.REJECT, reason_code=ReasonCode.FUEL_LIMIT)
    with pytest.raises(ValidationError):
        _decision(verdict=Verdict.REJECT, reason_code=ReasonCode.FUEL_LIMIT, cites="")


def test_valid_reject() -> None:
    d = _decision(verdict="REJECT", reason_code="FUEL_LIMIT", cites="P_H1")
    assert d.reason_code == ReasonCode.FUEL_LIMIT


@pytest.mark.parametrize("bad", ["24:00", "7:05", "12:60", "1205", "", "12:5"])
def test_hhmm_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        Injected(type="RUNWAY_CLOSED", ref="r", start=bad, end="13:00")


def test_hhmm_accepted_on_package() -> None:
    pkg = ResolutionPackage(event_id="E", package_id="P1", turn=2, slot="00:00", gate="A7",
                            score=0.5, score_breakdown={"fuel": 1.0})
    assert pkg.satisfies_hard is True
    with pytest.raises(ValidationError):
        pkg.model_validate({**pkg.model_dump(), "slot": "23:60"})


def test_resource_conflict_code_and_stress_defaults() -> None:
    assert ReasonCode("RESOURCE_CONFLICT") is ReasonCode.RESOURCE_CONFLICT
    assert Stress().model_dump() == {"low_fuel": False, "tight_crew": False,
                                     "corrupt_solver": False, "declared_emergency": False}


def test_consensus_defaults() -> None:
    rec = ConsensusRecord(event_id="E", outcome="FALLBACK", package={}, turns_used=3,
                          decisions=[], checker_pass=None)
    assert rec.status == "AWAITING_SIGNOFF" and rec.impact is None and rec.reservations == []


def test_check_result_failed() -> None:
    res = CheckResult(event_id="E", package_id="P1", passed=False, rules=[
        RuleResult(rule=ReasonCode.SEPARATION, passed=True, detail="ok"),
        RuleResult(rule=ReasonCode.FUEL_LIMIT, passed=False, detail="late"),
    ])
    assert [r.rule for r in res.failed()] == [ReasonCode.FUEL_LIMIT]


def test_log_entry_who_and_stage() -> None:
    kw = dict(seq=0, ts="t", type="STATE", text="x", prev_hash="GENESIS", hash="h")
    assert LogEntry(who="orchestrator", **kw).event_id is None
    with pytest.raises(ValidationError):
        LogEntry(who="llm", **kw)
    with pytest.raises(ValidationError):
        LogEntry(who="coordinator", stage=6, **kw)


def test_reservation_times() -> None:
    with pytest.raises(ValidationError):
        Reservation(res_id="R1", resource_type="GATE", airport="ATL", resource_id="A7",
                    start="25:00", end="13:00", event_id="E", status="HELD")


def test_empty_ledger_view_is_a_ledger_view() -> None:
    view = EmptyLedgerView()
    assert isinstance(view, LedgerView)
    assert view.slot_taken("ATL", 720) is False
    assert view.gate_busy("ATL", "A7", 700, 760, exclude_event="E") is False
