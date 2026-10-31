"""'Constraints & deviations' panel (CONSTRAINTS.md 8): every REGISTRY row, grouped by sheet.

The challenge's rules of play require every changed limit to be stated in the demo, so this
panel is expanded by default and lists AS_GIVEN, ASSUMED, CHANGED and NOT_MODELLED rows.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

import streamlit as st

from acacia import constraints
from acacia.constraints import ConstraintItem
from ui.theme import STATUS_CHIP, chip, esc

SHEET_TITLES = {
    "airport_capacity": "Airport capacity",
    "wake_separation": "Wake separation",
    "fuel_and_crew": "Fuel and crew",
    "rules_of_play": "Rules of play",
    "kit": "Kit (not in workbook)",
}


def group_by_sheet(items: Iterable[ConstraintItem]) -> dict[str, list[ConstraintItem]]:
    """Rows grouped by sheet, in workbook order, preserving registry order within a sheet."""
    out: dict[str, list[ConstraintItem]] = {k: [] for k in SHEET_TITLES}
    for item in items:
        out.setdefault(item.sheet, []).append(item)
    return {k: v for k, v in out.items() if v}


def status_counts(items: Iterable[ConstraintItem]) -> dict[str, int]:
    counts = Counter(i.status for i in items)
    return {s: counts.get(s, 0) for s in STATUS_CHIP}


def _row(item: ConstraintItem) -> str:
    note = f' <span class="wb">({esc(item.note)})</span>' if item.note else ""
    return (f'<tr><td class="id">{esc(item.id)}</td><td>{esc(item.rule)}<br>'
            f'<span class="wb">workbook: {esc(item.workbook_value)}</span><br>used: {esc(item.used_value)}{note}</td>'
            f'<td>{chip(item.status.replace("_", " "), STATUS_CHIP.get(item.status, ""))}</td></tr>')


def registry_html(items: Iterable[ConstraintItem]) -> str:
    parts = []
    for sheet, rows in group_by_sheet(items).items():
        parts.append(f'<div class="ac-h" style="margin-top:8px">{esc(SHEET_TITLES.get(sheet, sheet))}</div>'
                     f'<table class="reg"><tbody>{"".join(_row(r) for r in rows)}</tbody></table>')
    return "".join(parts)


def _version() -> str:
    try:
        return constraints.constraints_version()
    except OSError as exc:
        return f"unavailable ({exc})"


def constraints_panel() -> None:
    items = list(constraints.REGISTRY)
    counts = status_counts(items)
    deviations = sum(n for s, n in counts.items() if s != "AS_GIVEN")
    with st.expander(f"Constraints & deviations · {deviations} deviations of {len(items)} rows", expanded=True):
        summary = "".join(chip(f"{s.replace('_', ' ')} {n}", STATUS_CHIP[s]) for s, n in counts.items())
        st.markdown(f'<div>{summary} <span class="ac-sub ac-mono">constraints {esc(_version())}</span></div>'
                    '<div class="ac-sub">Source: challenge2_operational_constraints.xlsx [CHALLENGE]. '
                    'Every value not used as given is listed here.</div>', unsafe_allow_html=True)
        with st.container(height=360, border=False):
            st.markdown(registry_html(items), unsafe_allow_html=True)
