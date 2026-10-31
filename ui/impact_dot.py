"""Impact graph as Graphviz DOT for st.graphviz_chart (HANDOVER 13 multi-trigger additions).

The selected trigger's downstream subgraph from its ImpactAssessment: left to right, rotation
edges solid and labelled with slack and propagated delay, connection edges dashed and labelled
with passengers at risk, nodes filled by propagated delay, the trigger outlined. Pure string
building (deterministic layout; rendered client-side, so no `dot` binary is needed).
"""

from __future__ import annotations

from acacia.graph import ImpactGraph
from acacia.models import DownstreamImpact, ImpactAssessment
from acacia.timeutil import to_hhmm

# (upper bound of propagated delay in minutes, fill colour); first match wins
DELAY_FILLS: tuple[tuple[float, str, str], ...] = (
    (0, "#e4e9ed", "0 min"),
    (15, "#f6ead2", "1-15"),
    (30, "#f3c98b", "16-30"),
    (60, "#e8955a", "31-60"),
    (float("inf"), "#d9604a", "> 60"),
)
INK, ROTATION_EDGE, CONNECTION_EDGE, TRIGGER_OUTLINE = "#14202a", "#3a4652", "#4a4fc2", "#0d6d86"


def fill_for(delay_min: int) -> str:
    return next(colour for upper, colour, _ in DELAY_FILLS if delay_min <= upper)


def _q(text: object) -> str:
    """A DOT double-quoted string."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _label(g: ImpactGraph | None, node_id: str, tail: str) -> str:
    n = g.nodes.get(node_id) if g is not None else None
    if not n:
        return f"{node_id}\n{tail}"
    return (f"{n['airline']}{n['flight_no']}  {n['origin']}→{n['dest']}\n"
            f"dep {to_hhmm(int(n['sched_dep']))} · tail {n['tail'] or '—'}\n{tail}")


def _node(node_id: str, label: str, fill: str, *, trigger: bool = False, dashed: bool = False) -> str:
    style = "rounded,filled" + (",dashed" if dashed else "")
    outline = f' color={_q(TRIGGER_OUTLINE)} penwidth=3' if trigger else f' color={_q(ROTATION_EDGE)}'
    return f"  {_q(node_id)} [label={_q(label)} fillcolor={_q(fill)} style={_q(style)}{outline}];"


def _parents(trigger_id: str, downstream: list[DownstreamImpact]) -> dict[int, str]:
    """depth -> the rotation node at that depth (depth 0 = the trigger); the chain is linear."""
    out = {0: trigger_id}
    for d in downstream:
        if d.edge_type == "ROTATION":
            out[d.depth] = d.node_id
    return out


def impact_dot(a: ImpactAssessment, trigger_id: str | None, g: ImpactGraph | None, trigger_code: str = "") -> str:
    """DOT source for one trigger's downstream chain; a trigger missing from the graph is drawn alone."""
    tid = trigger_id or f"{a.event_id} (not in graph)"
    lines = ["digraph impact {", '  rankdir=LR; bgcolor="transparent"; nodesep=0.25; ranksep=0.45;',
             f'  node [shape=box fontname="Helvetica" fontsize=10 fontcolor={_q(INK)}];',
             f'  edge [fontname="Helvetica" fontsize=9 fontcolor={_q(INK)}];']
    head = f"TRIGGER {trigger_code}".strip() + f" · +{a.input_delay_min} min"
    lines.append(_node(tid, _label(g, tid, head), fill_for(a.input_delay_min), trigger=True))
    parents = _parents(tid, a.downstream)
    for d in a.downstream:
        src = parents.get(d.depth - 1, tid)
        if d.edge_type == "ROTATION":
            late = " · after 23:00" if d.late_night else ""
            lines.append(_node(d.node_id, _label(g, d.node_id, f"+{d.propagated_delay_min} min{late}"),
                               fill_for(d.propagated_delay_min)))
            lines.append(f"  {_q(src)} -> {_q(d.node_id)} [label={_q(f'slack {d.slack_min} · +{d.propagated_delay_min}')}"
                         f" color={_q(ROTATION_EDGE)} penwidth=1.6];")
        else:
            lines.append(_node(d.node_id, _label(g, d.node_id, f"connection broken · {d.pax_at_risk} pax"),
                               fill_for(0), dashed=True))
            lines.append(f"  {_q(src)} -> {_q(d.node_id)} [label={_q(f'{d.pax_at_risk} pax · gap {d.gap_min}')}"
                         f" style=dashed color={_q(CONNECTION_EDGE)}];")
    lines.append("}")
    return "\n".join(lines)
