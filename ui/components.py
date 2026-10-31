"""Left-panel cards (HANDOVER 13 items 2-5 and 7): event, protocol, agents, solver, outcome.

Every number shown comes from the log payloads (via ui.view) or from acacia.constraints for
workbook values; nothing here recomputes a plan.
"""

from __future__ import annotations

import streamlit as st

from acacia import config, constraints
from acacia.models import DisruptionEvent
from acacia.timeutil import to_hhmm, to_min
from ui.theme import STAGES, chip, colour, esc, heading, scenario_chip, sim_chip
from ui.view import View, hard

OUTCOME_ANCHOR = "ac-outcome"  # ui/verdict.py links here

CAUSES = (("weather", "Weather", "var(--accent)"), ("late_aircraft", "Late aircraft", "var(--ground)"),
          ("air_system", "Air system", "var(--pilot)"), ("airline", "Airline", "var(--solver)"),
          ("security", "Security", "var(--bad)"))
BREAKDOWN = (("throughput", "var(--atc)"), ("connections", "var(--pilot)"),
             ("fuel", "var(--bad)"), ("turnaround", "var(--ground)"))
LOCK = "🔒"


def _html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def _kv(rows: list[tuple[str, object]]) -> str:
    return '<dl class="kv">' + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows) + "</dl>"


# --- event card ----------------------------------------------------------------------------
def storm_of(event: DisruptionEvent) -> constraints.Storm:
    for inj in event.injected:
        if inj.type == "RUNWAY_CLOSED":
            return inj.severity or "moderate"  # CAP-8
    return "none"


def _cause_bar(event: DisruptionEvent) -> str:
    c = event.causes.model_dump()
    total = sum(c.values()) or 1
    spans = "".join(f'<span style="width:{100 * c[k] / total:.1f}%;background:{col}" title="{esc(lbl)} {c[k]} min"></span>'
                    for k, lbl, col in CAUSES if c[k])
    legend = "".join(f'<span><i style="background:{col}"></i>{esc(lbl)} {c[k]}</span>' for k, lbl, col in CAUSES if c[k])
    return f'<div class="causebar">{spans}</div><div class="legend">{legend}</div>'


def _limits_box(event: DisruptionEvent) -> str:
    dest = event.flight.dest
    lim = constraints.airport_limits(dest)
    storm = storm_of(event)
    nominal = constraints.arrival_rate(dest, "none")
    storm_txt = (f"{constraints.arrival_rate(dest, storm):g}/hr in storm ({storm} ×{constraints.STORM_FACTOR[storm]:g})"
                 if storm != "none" else "no storm window")
    wake = constraints.wake_class(event.distance)
    assumed = chip("Non-hub values, ASSUMED", "warn") if lim.assumed else ""
    rows = [("Wake class", f"{wake} ({event.distance} mi; WAKE-3)"),
            ("Arrivals/hr", f"{nominal:g} nominal ({lim.max_ops_per_hour} ops × {constraints.ARRIVAL_SHARE:g})"),
            ("Storm capacity", storm_txt),
            ("Min turnaround", f"{lim.min_turnaround_min} min ({lim.size_class})"),
            ("Spare gates", f"{lim.spare_gates} of {lim.gates}")]
    return f'<div class="wb-box"><b>Workbook limits at {esc(dest)}</b> {assumed}{_kv(rows)}</div>'


def _commit_text(event: DisruptionEvent) -> str:
    """TIME-1..3: decision + negotiation + sign-off budget, and the earliest slot after it."""
    return (f"{to_hhmm(constraints.commit_time_min(event))} (+{constraints.NEGOTIATION_BUDGET_MIN}+"
            f"{constraints.SIGNOFF_BUDGET_MIN} min); slots from {to_hhmm(constraints.earliest_slot_min(event))}")


def event_card(event: DisruptionEvent) -> None:
    f = event.flight
    with st.container(border=True):
        _html(heading("Event"))
        a, b = st.columns([1.2, 1])
        with a:
            _html(f'<div class="ev-big">{esc(f.code)}</div><div class="ac-sub">{esc(f.origin)} → {esc(f.dest)} · '
                  f'{esc(f.date)} · tail {esc(f.tail)} · {scenario_chip(event.scenario_type.value)}</div>'
                  + _kv([("Scheduled", event.sched_arrival), ("Actual (history)", event.hist_arrival),
                         ("Delay", f"{event.hist_delay_min} min"), ("Earliest landing E", event.eta_landing),
                         ("Taxi-in", f"{event.taxi_in} min"), ("Decision time", event.decision_time or "—"),
                         ("Commit (simulated)", _commit_text(event))])
                  + _cause_bar(event))
            inj = "".join(f"<div>{sim_chip()} <b>{esc(i.type.replace('_', ' ').title())}</b> {esc(i.start)}–{esc(i.end)}: "
                          f"{esc(i.ref)}</div>" for i in event.injected)
            _html(f'<div class="inject">{inj or "No injected event."}</div>')
        with b:
            _html(_limits_box(event))


# --- protocol -------------------------------------------------------------------------------
def protocol(view: View) -> None:
    tiles = []
    for i, (name, kind) in enumerate(STAGES):
        cls = "done" if i < view.stage or view.state == "AWAITING_SIGNOFF" else "active" if i == view.stage else ""
        tiles.append(f'<div class="stage {cls}"><b>{esc(name)}</b><small>{esc(kind)}</small></div>')
    segs = "".join(f'<span class="{"on" if i < view.turn else ""}"></span>' for i in range(config.MAX_TURNS))
    with st.container(border=True):
        _html(heading("Protocol") + f'<div class="stages">{"".join(tiles)}</div>'
              f'<div class="turnrow"><span>State <span class="state ac-mono">{esc(view.state)}</span></span>'
              f'<span>Turn {min(view.turn, config.MAX_TURNS)} of {config.MAX_TURNS} <span class="turns">{segs}</span></span></div>')


# --- agent cards ------------------------------------------------------------------------------
def _value(rule: str, v: object) -> str:
    if rule == "WAKE_SEPARATION" and isinstance(v, dict):
        return f"workbook table, own class {v.get('own_class', '?')}"
    if rule == "GATE_CHANGE_NOTICE" and isinstance(v, dict):
        return (f"≥ {v.get('min_notice_min')} min from commit {v.get('commit_time') or v.get('decision_time')}, "
                f"assigned {v.get('assigned_gate') or 'unknown'}")
    if isinstance(v, dict):
        return ", ".join(f"{k} {val if val is not None else 'unavailable'}" for k, val in v.items())
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def _atc_workbook(p: dict, event: DisruptionEvent) -> list[str]:
    h = hard(p, "WAKE_SEPARATION")
    if not h or not isinstance(h.get("value"), dict):
        return []
    mins = h["value"].get("min_minutes", {})
    pairs = ", ".join(f"{k} {v:g}" for k, v in sorted(mins.items()))
    return [f"Wake separation, own class {h['value'].get('own_class', '?')} (min): {pairs}",
            f"nm → min at {constraints.APPROACH_SPEED_KT:g} kt (WAKE-2, ASSUMED)"]


def _pilot_workbook(p: dict, event: DisruptionEvent) -> list[str]:
    out: list[str] = []
    h = hard(p, "LAND_BY")
    if h:
        cont = constraints.contingency_min(event.air_time, event.scheduled_time)
        expected = constraints.land_by(to_min(event.eta_landing), event.air_time, event.scheduled_time)
        line = (f"LAND_BY {h['value']} = E {event.eta_landing} + {constraints.SPARE_HOLDING_MIN} spare + {cont} "
                f"contingency (5% of air time); {constraints.FINAL_RESERVE_MIN} min final reserve kept")
        if to_min(str(h["value"])) != expected:
            line += f" — declared differs from workbook {to_hhmm(expected)} (stress or default)"
        out.append(line)
    duty = hard(p, "FLIGHT_CREW_DUTY_END")
    if duty:
        out.append(f"Flight crew duty end {duty['value']} (13 h from report, CREW-1)")
    burn = (p.get("facts_owned") or {}).get("burn_kg_per_h")
    if burn:
        out.append(f"Holding burn {burn:,} kg/h (FUEL-4)")
    return out


def _ground_workbook(p: dict, event: DisruptionEvent) -> list[str]:
    out: list[str] = []
    n = hard(p, "GATE_CHANGE_NOTICE")
    if n and isinstance(n.get("value"), dict):
        v = n["value"]
        out.append(f"Gate-change notice ≥ {v.get('min_notice_min')} min before gate arrival (HARD), from commit "
                   f"{v.get('commit_time') or v.get('decision_time')} (TIME-4), assigned {v.get('assigned_gate') or 'unknown'}")
    facts = p.get("facts_owned") or {}
    d = hard(p, "CREW_DUTY_LIMIT")
    if d:
        out.append(f"Crew duty limit {d['value']} (shift end + {constraints.GROUND_MAX_OVERTIME_MIN} max overtime, CREW-3)")
    if facts.get("shift_start"):
        out.append(f"Shift {facts['shift_start']} start, 8 h, overtime ×{constraints.OVERTIME_COST_FACTOR:g} (CREW-2)")
    if "min_turn_min" in facts:
        out.append(f"Min turnaround {facts['min_turn_min']} min; spare gates {facts.get('spare_gates', '—')}")
    return out


WORKBOOK = {"ATC": _atc_workbook, "PILOT": _pilot_workbook, "GROUND": _ground_workbook}


def _vote_line(vote: dict | None) -> str:
    if not vote:
        return '<div class="vote ac-sub">No vote yet.</div>'
    ok = vote.get("verdict") == "ACCEPT"
    tag = chip(f"T{vote.get('turn')} ACCEPT", "good") if ok else chip(
        f"T{vote.get('turn')} REJECT {vote.get('reason_code')} · {vote.get('cites')}", "bad")
    return f'<div class="vote">{tag} {esc(vote.get("rationale", ""))}</div>'


def _agent_card(role: str, packet: dict | None, vote: dict | None, default: str | None,
                event: DisruptionEvent) -> str:
    who = role.lower()
    c = colour(who)
    head = f'<div class="who"><b>{esc(role)}</b><span>{sim_chip()}{chip("default", "bad") if default else ""}</span></div>'
    if packet is None:
        return f'<div class="agent" style="--c:{c}">{head}<div class="ac-sub">Waiting for its ConstraintPacket.</div></div>'
    items = [f'<li class="hard"><span class="tag">{LOCK} {esc(h["id"])}</span><span>{esc(h["rule"])}: '
             f'{esc(_value(h.get("rule", ""), h.get("value")))}</span></li>' for h in packet.get("hard", [])]
    items += [f'<li><span class="tag">{esc(s["id"])}</span><span>{esc(s["preference"])} '
              f'{chip(f"w {s["weight"]:g}", "info")}</span></li>' for s in packet.get("soft", [])]
    wb = "".join(f"<div>{esc(line)}</div>" for line in WORKBOOK[role](packet, event))
    wb_box = f'<div class="wb-box"><b>Workbook limits declared</b>{wb}</div>' if wb else ""
    return (f'<div class="agent" style="--c:{c}">{head}<ul>{"".join(items)}</ul>{wb_box}'
            f'{_vote_line(vote)}</div>')


def agent_cards(view: View, event: DisruptionEvent) -> None:
    with st.container(border=True):
        _html(heading("Declared constraints", " " + sim_chip()))
        cols = st.columns(3)
        for col, role in zip(cols, ("ATC", "PILOT", "GROUND")):
            with col:
                _html(_agent_card(role, view.packets.get(role), view.votes.get(role), view.defaults.get(role), event))
        for note in view.audits:
            st.caption(f"AUDIT: {note}")


# --- solver table -------------------------------------------------------------------------------
def _status(cand: dict, view: View, turn: int | None) -> tuple[str, str]:
    pkg, status = cand["package"], cand.get("status", "")
    sent = view.sent or {}
    same = all(sent.get(k) == pkg.get(k) for k in ("slot", "gate", "runway"))  # CAP-9: runway too
    if same and sent.get("turn") == turn:
        return "sent", "info"
    if status == "DISCARDED":
        return "discarded: " + ", ".join(cand.get("discard", [])), "bad"
    if status == "CUT":
        return f"cut by {cand.get('cut_by') or '?'}", "warn"
    return status.lower(), "good" if status == "FEASIBLE" else "muted" if status == "TRIED" else "bad"


def _bar(sb: dict) -> str:
    return '<div class="bar">' + "".join(
        f'<span style="width:{100 * max(0.0, sb.get(k, 0.0)):.1f}%;background:{c}" title="{k} {sb.get(k, 0):.2f}"></span>'
        for k, c in BREAKDOWN) + "</div>"


def _gate_arr(slot: str, taxi_in: int) -> str:
    return to_hhmm(to_min(slot) + taxi_in)


def solver_table(view: View, event: DisruptionEvent) -> None:
    with st.container(border=True):
        _html(heading("Solver packages", f' <span class="ac-sub">policy {esc(config.POLICY_VERSION)} · weights '
                      + " / ".join(f"{k} {v:g}" for k, v in config.POLICY.items()) + "</span>"))
        if not view.solver:
            _html('<div class="ac-sub">Appears once all three packets are in.</div>')
            return
        turn = (view.solver.get("package") or {}).get("turn") or view.turn
        rows = []
        order = {"sent": 0, "feasible": 1}
        cands = sorted(view.solver.get("candidates", []), key=lambda c: (
            order.get(_status(c, view, turn)[0], 2), -c["package"].get("score", 0)))
        for cand in cands:
            pkg, sb = cand["package"], cand["package"].get("score_breakdown", {})
            label, kind = _status(cand, view, turn)
            cls = "sent" if label == "sent" else "disc" if kind in ("bad", "muted") else ""
            kg = sb.get("holding_fuel_kg")
            rows.append(f'<tr class="{cls}"><td>{esc(pkg["package_id"])}</td><td>{esc(pkg["slot"])}</td>'
                        f'<td>{esc(pkg.get("runway") or "—")}</td><td>{esc(pkg["gate"])}</td><td>{esc(_gate_arr(pkg["slot"], event.taxi_in))}</td>'
                        f'<td>{pkg.get("score", 0):.2f}</td><td>{_bar(sb)}</td>'
                        f'<td>{"—" if kg is None else f"{kg:,.0f}"}</td><td>{sb.get("overtime_cost_min", 0):g}</td>'
                        f'<td>{chip(label, kind)}</td></tr>')
        legend = "".join(f'<span><i style="background:{c}"></i>{k}</span>' for k, c in BREAKDOWN)
        _html('<table class="pk"><thead><tr><th>ID</th><th>Slot</th><th>Runway</th><th>Gate</th><th>At gate</th><th>Score</th>'
              '<th>Breakdown</th><th>Hold fuel kg</th><th>Overtime cost min</th><th>Status</th></tr></thead>'
              f'<tbody>{"".join(rows)}</tbody></table><div class="legend" style="margin-top:6px">{legend}'
              f'<span>Turn {esc(turn)}{" · corrupted (safety test)" if view.solver.get("corrupted") else ""}</span></div>')


# --- outcome -----------------------------------------------------------------------------------
def _rel(n: int | None, what: str) -> str:
    if n is None:
        return f"vs {what}: n/a"
    if n == 0:
        return f"same as {what}"
    return f"{abs(n)} min {'earlier' if n >= 0 else 'later'} than {what}"


def _check_chips(view: View) -> str:
    check = view.check if (view.record or {}).get("outcome") == "CONSENSUS" else (view.fallback or {}).get("check")
    if not check:
        return chip("checker n/a (divert)", "muted")
    return "".join(chip(f"{r['rule']} {'PASS' if r['passed'] else 'FAIL'}", "good" if r["passed"] else "bad")
                   for r in check.get("rules", []))


def _outcome_rows(rec: dict, view: View) -> list[tuple[str, object]]:
    pkg = rec.get("package", {})
    sb = pkg.get("score_breakdown") or {}
    rows: list[tuple[str, object]] = [("Turns used", f"{rec.get('turns_used')} of {config.MAX_TURNS}")]
    if pkg.get("divert"):
        rows.append(("Divert", f"{pkg.get('alternate')} ({pkg.get('alternate_distance_mi')} mi)"))
    else:
        if pkg.get("runway"):
            rows.append(("Runway", pkg["runway"]))  # CAP-9 arrival runway
        rows += [("Gate arrival", pkg.get("gate_arrival")),
                 ("Holding", f"{pkg.get('holding_min')} min, {pkg.get('holding_fuel_kg') or 0:,.0f} kg fuel at "
                             f"{pkg.get('burn_kg_per_h') or 0:,} kg/h"),
                 ("Overtime cost", f"{sb.get('overtime_cost_min', 0):g} min (×{constraints.OVERTIME_COST_FACTOR:g})"
                  if sb else "n/a (fallback plan is not scored)"),
                 ("vs history (illustrative)", _rel(rec.get("delta_vs_history_min"), "history")),
                 ("vs fallback plan", _rel(rec.get("delta_vs_fallback_min"), "fallback"))]
    return rows


def outcome_panel(view: View) -> None:
    rec = view.record
    with st.container(border=True):
        _html(f'<div id="{OUTCOME_ANCHOR}" class="ac-anchor"></div>' + heading("Outcome"))
        if not rec:
            _html('<div class="ac-sub">Appears when the coordinator reaches AWAITING_SIGNOFF.</div>')
            return
        is_consensus = rec.get("outcome") == "CONSENSUS"
        oc = "var(--good)" if is_consensus else "var(--warn)"
        pkg = rec.get("package", {})
        reason = pkg.get("fallback_reason") or view.fallback_reason or (view.fallback or {}).get("reason")
        why = "" if is_consensus else f'<div><b>Fallback reason:</b> {esc(reason or "not stated")}</div>'
        status = rec.get("status", "AWAITING_SIGNOFF")
        stamp = " · ".join(f"{k} {v}" for k, v in sorted((rec.get("stamp") or {}).items()) if not isinstance(v, dict))
        _html(f'<div class="outcome" style="--oc:{oc}">{chip(rec.get("outcome"), "good" if is_consensus else "warn")}'
              f'{chip("checker PASS" if rec.get("checker_pass") else "checker n/a" if rec.get("checker_pass") is None else "checker FAIL", "good" if rec.get("checker_pass") else "bad")}'
              f'{chip(status, "info" if status == "AWAITING_SIGNOFF" else "good" if status == "APPROVED" else "bad")}'
              f'<div class="plan">{esc(pkg.get("flight_plan", ""))}</div>{why}{_kv(_outcome_rows(rec, view))}'
              f'<div style="margin-top:4px">{_check_chips(view)}</div>'
              f'<div class="stamp">stamp: {esc(stamp)}</div></div>')
