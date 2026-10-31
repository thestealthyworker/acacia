"""Compact verdict banner near the top of the dashboard (QA fix 3).

Shown once a run reaches its final entry: outcome, checker, the revised plan (or the divert),
turns used, holding fuel and sign-off status, with a link down to the full Outcome panel.
Reads only the ConsensusRecord payload in the visible log (via ui.view), like every other card.
"""

from __future__ import annotations

from collections.abc import Sequence

from acacia import config
from acacia.models import LogEntry
from ui.components import OUTCOME_ANCHOR
from ui.theme import chip, esc
from ui.view import build_view

STATUS_KIND = {"AWAITING_SIGNOFF": "info", "APPROVED": "good", "REJECTED": "bad"}


def _checker(rec: dict) -> str:
    passed = rec.get("checker_pass")
    if passed is None:
        return chip("checker n/a", "muted")
    return chip("checker PASS", "good") if passed else chip("checker FAIL", "bad")


def _facts(rec: dict) -> str:
    pkg = rec.get("package") or {}
    facts = [f"turns {rec.get('turns_used', '?')} of {config.MAX_TURNS}"]
    if pkg.get("divert"):
        facts.append(f"divert to {pkg.get('alternate', '?')}")
    else:
        facts.append(f"holding {pkg.get('holding_min', 0)} min · {pkg.get('holding_fuel_kg') or 0:,.0f} kg fuel")
    return " · ".join(esc(f) for f in facts)


def verdict_html(entries: Sequence[LogEntry]) -> str | None:
    """Banner markup for a run that has reached its ConsensusRecord, else None."""
    rec = build_view(entries).record
    if not rec:
        return None
    consensus = rec.get("outcome") == "CONSENSUS"
    status = rec.get("status", "AWAITING_SIGNOFF")
    plan = (rec.get("package") or {}).get("flight_plan", "")
    oc = "var(--good)" if consensus else "var(--warn)"
    return (f'<div class="verdict" style="--oc:{oc}">'
            f'<div class="verdict-chips">{chip(rec.get("outcome"), "good" if consensus else "warn")}{_checker(rec)}'
            f'{chip(status, STATUS_KIND.get(status, "muted"))}</div>'
            f'<div class="verdict-plan">{esc(plan)}</div>'
            f'<div class="verdict-facts ac-mono">{_facts(rec)} · '
            f'<a href="#{OUTCOME_ANCHOR}">full outcome ↓</a></div></div>')
