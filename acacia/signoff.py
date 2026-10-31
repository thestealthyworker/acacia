"""Human sign-off (HANDOVER 11.1 AWAITING_SIGNOFF): nothing executes until a person approves.

`sign_off` appends a hash-chained SignOff entry whose payload carries the decision and the
updated ConsensusRecord (status APPROVED or REJECTED, signed_off_by), so the chain proves who
decided what. A plan the checker failed cannot be approved; it can only be rejected for
manual handling. The ledger is not touched here: the dispatcher commits or releases.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from acacia.log import JsonlLog, LogChain, read_log
from acacia.models import ConsensusRecord, LogEntry, SignOff

APPROVED_TEXT = "Plan approved by {who}. `signed_off_by` recorded; execution may proceed."
REJECTED_TEXT = "Plan rejected by {who}. Event returned for manual handling."


class SignOffError(ValueError):
    """The record cannot be signed off as asked."""


def _validate(record: ConsensusRecord, approve: bool, who: str) -> None:
    if not who or not who.strip():
        raise SignOffError("sign-off needs the name of the person deciding")
    if record.status != "AWAITING_SIGNOFF":
        raise SignOffError(f"{record.event_id} is already {record.status}")
    is_divert = bool(record.package.get("divert"))
    if approve and record.checker_pass is not True and not is_divert:
        raise SignOffError(f"{record.event_id}: the checker did not pass this plan; reject it for manual handling")


def _chain(log: LogChain | Sequence[LogEntry], event_id: str) -> LogChain:
    chain = log if isinstance(log, LogChain) else LogChain.resume(list(log))
    if chain.event_id not in (None, event_id):
        raise SignOffError(f"log is for {chain.event_id}, record is for {event_id}")
    return chain


def sign_off(log: LogChain | Sequence[LogEntry], record: ConsensusRecord, approve: bool, who: str,
             note: str | None = None) -> tuple[LogEntry, ConsensusRecord]:
    """Append the SignOff entry to `log` and return it with the updated record.

    A LogChain is extended in place; a list of entries is verified and resumed (append the
    returned entry to it yourself). Raises SignOffError on an invalid request and ValueError
    when the given entries do not verify.
    """
    _validate(record, approve, who)
    chain = _chain(log, record.event_id)
    status = "APPROVED" if approve else "REJECTED"
    updated = record.model_copy(update={"status": status, "signed_off_by": who.strip()})
    decision = SignOff(event_id=record.event_id, approved=approve, who=who.strip(), note=note,
                       ts=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    text = (APPROVED_TEXT if approve else REJECTED_TEXT).format(who=who.strip())
    payload = {"signoff": decision.model_dump(mode="json", exclude={"ts"}),
               "record": updated.model_dump(mode="json")}
    entry = chain.append("human", "SignOff", text, state=status, stage=5,
                         turn=record.turns_used, payload=payload)
    return entry, updated


def last_record(entries: Sequence[LogEntry]) -> ConsensusRecord:
    """The latest record in a log: a SignOff's updated record, else the ConsensusRecord."""
    for entry in reversed(entries):
        if entry.type == "SignOff" and "record" in entry.payload:
            return ConsensusRecord.model_validate(entry.payload["record"])
        if entry.type == "ConsensusRecord":
            return ConsensusRecord.model_validate(entry.payload)
    raise SignOffError("log has no ConsensusRecord to sign off")


def sign_off_file(path: Path | str, approve: bool, who: str,
                  note: str | None = None) -> tuple[LogEntry, ConsensusRecord]:
    """Sign off the run stored in a JSONL log and append the entry to that file."""
    entries = read_log(path)
    entry, updated = sign_off(entries, last_record(entries), approve, who, note)
    with JsonlLog(path, append=True) as out:
        out.write(entry)
    return entry, updated
