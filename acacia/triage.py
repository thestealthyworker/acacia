"""Triage (FABLE_ADDENDUM 6, HANDOVER 18.4): knock-on propagation, impact score, safety tier,
priority order and clusters. Pure functions over the ImpactGraph; no LLM, no simulation.

`land_by` and `first_slot` are inputs (the dispatcher gets them from the simulator).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from acacia import config
from acacia.graph import ImpactGraph
from acacia.models import Cluster, DisruptionEvent, DownstreamImpact, ImpactAssessment, Stress
from acacia.timeutil import to_min

BREAKDOWN_KEYS = ("knock_on_min", "pax_connections_at_risk", "late_night_legs", "flights_affected")
NOT_IN_GRAPH = "trigger not in graph, no propagation"


# --- propagation -------------------------------------------------------------------------------------
def propagate(
    g: ImpactGraph, node_id: str, input_delay: int, max_depth: int = config.PROPAGATION_MAX_DEPTH
) -> list[DownstreamImpact]:
    """Breadth-first along rotation edges, then broken connections off every delayed node."""
    if node_id not in g.nodes:
        return []
    rotation = _rotation_chain(g, node_id, input_delay, max_depth)
    delayed = [(node_id, 0, input_delay)] + [
        (d.node_id, d.depth, d.propagated_delay_min) for d in rotation if d.propagated_delay_min > 0
    ]
    connections = [c for node, depth, d in delayed for c in _broken_connections(g, node, depth, d)]
    return rotation + connections


def _rotation_chain(g: ImpactGraph, node_id: str, input_delay: int, max_depth: int) -> list[DownstreamImpact]:
    out: list[DownstreamImpact] = []
    d, node, depth = input_delay, node_id, 0
    while depth < max_depth:
        nxt = g.rotation_next(node)
        if nxt is None:
            break
        node, slack = nxt
        depth += 1
        d = max(0, d - slack)
        sched_dep = int(g.nodes[node]["sched_dep"])
        out.append(DownstreamImpact(node_id=node, edge_type="ROTATION", depth=depth, slack_min=slack,
                                    propagated_delay_min=d, late_night=(sched_dep + d) > config.LATE_NIGHT_FROM))
        if d == 0:
            break
    return out


def _broken_connections(g: ImpactGraph, node: str, depth: int, d: int) -> list[DownstreamImpact]:
    return [
        DownstreamImpact(node_id=b, edge_type="CONNECTION", depth=depth + 1, gap_min=gap,
                         propagated_delay_min=d, pax_at_risk=pax)
        for b, gap, pax in g.connections(node)
        if d > gap - config.MCT
    ]


# --- score and tier -----------------------------------------------------------------------------------
def breakdown_of(downstream: Iterable[DownstreamImpact]) -> dict[str, float]:
    items = list(downstream)
    rot = [d for d in items if d.edge_type == "ROTATION"]
    con = [d for d in items if d.edge_type == "CONNECTION"]
    return {
        "knock_on_min": float(sum(d.propagated_delay_min for d in rot)),
        "pax_connections_at_risk": float(sum(d.pax_at_risk for d in con)),
        "late_night_legs": float(sum(1 for d in rot if d.late_night)),
        "flights_affected": float(sum(1 for d in rot if d.propagated_delay_min > 0)),
    }


def score_of(breakdown: dict[str, float], policy: dict) -> float:
    missing = [k for k in BREAKDOWN_KEYS if k not in policy]
    if missing:
        raise ValueError(f"impact policy is missing weights {missing}")
    return round(sum(float(policy[k]) * breakdown[k] for k in BREAKDOWN_KEYS), 2)


def safety_tier(event: DisruptionEvent, land_by: int, first_slot: int, stress: Stress) -> tuple[int, str]:
    """Tier 0 is a gate: fuel margin, low-fuel stress or a declared emergency. Else tier 1."""
    if land_by - first_slot <= config.TIER0_FUEL_MARGIN:
        return 0, f"FUEL_MARGIN <= {config.TIER0_FUEL_MARGIN} min to first slot"
    if stress.low_fuel:
        return 0, "LOW_FUEL stress"
    if stress.declared_emergency or "declared_emergency" in event.overrides:
        return 0, "DECLARED_EMERGENCY"
    return 1, "normal"


def single_cluster_id(event_id: str) -> str:
    return cluster_id_for([event_id])


def cluster_id_for(event_ids: Iterable[str]) -> str:
    return "CL-" + hashlib.sha256("|".join(sorted(event_ids)).encode()).hexdigest()[:8]


def trigger_node(event: DisruptionEvent, g: ImpactGraph) -> str | None:
    f = event.flight
    return g.find(f.airline, f.flight_no, f.origin, f.dest)


def assess(
    event: DisruptionEvent, g: ImpactGraph, land_by: int, first_slot: int, stress: Stress, policy: dict
) -> ImpactAssessment:
    """Impact assessment for one trigger. cluster_id is the singleton id until `cluster` runs."""
    node = trigger_node(event, g)
    downstream = propagate(g, node, event.hist_delay_min) if node is not None else []
    tier, reason = safety_tier(event, land_by, first_slot, stress)
    if node is None:
        reason = f"{reason}; {NOT_IN_GRAPH}"
    breakdown = breakdown_of(downstream)
    return ImpactAssessment(
        event_id=event.event_id, tier=tier, tier_reason=reason, impact_score=score_of(breakdown, policy),
        breakdown=breakdown, downstream=downstream, input_delay_min=event.hist_delay_min,
        decision_deadline=event.eta_landing, cluster_id=single_cluster_id(event.event_id),
        graph_version=g.version, policy_version=config.IMPACT_POLICY_VERSION,
    )


def rank(assessments: list[ImpactAssessment]) -> list[ImpactAssessment]:
    """(tier asc, impact desc, decision deadline asc, event_id asc). Safety is a gate, not a weight."""
    return sorted(assessments, key=lambda a: (a.tier, -a.impact_score, to_min(a.decision_deadline), a.event_id))


# --- clusters ---------------------------------------------------------------------------------------
class _UnionFind:
    def __init__(self, ids: Iterable[str]) -> None:
        self.parent = {i: i for i in ids}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _link_reasons(a: DisruptionEvent, b: DisruptionEvent, by_id: dict[str, ImpactAssessment],
                  g: ImpactGraph) -> list[str]:
    reasons: list[str] = []
    eta_a, eta_b = to_min(a.eta_landing), to_min(b.eta_landing)
    if a.flight.dest == b.flight.dest and abs(eta_a - eta_b) <= config.CLUSTER_WINDOW_MIN:
        lo, hi = sorted([a.eta_landing, b.eta_landing])
        reasons.append(f"shared runway window {a.flight.dest} {lo}-{hi}")
    na, nb = trigger_node(a, g), trigger_node(b, g)
    if na is None or nb is None:
        return reasons
    for up, down_node, down_id in ((a, nb, b.event_id), (b, na, a.event_id)):
        rot = [d.node_id for d in by_id[up.event_id].downstream if d.edge_type == "ROTATION"]
        if down_node in rot and down_id != up.event_id:
            reasons.append(f"rotation {g.nodes[down_node]['tail']}")
    for x, y in ((na, nb), (nb, na)):
        if any(n == y for n, _, _ in g.connections(x)):
            reasons.append(f"connection {x}->{y}")
    return reasons


def cluster(events: list[DisruptionEvent], assessments: list[ImpactAssessment], g: ImpactGraph) -> list[Cluster]:
    """Union-find over the events in event_id order; members in rank order; clusters by best member."""
    by_id = {a.event_id: a for a in assessments}
    ordered = sorted(events, key=lambda e: e.event_id)
    missing = [e.event_id for e in ordered if e.event_id not in by_id]
    if missing:
        raise ValueError(f"no assessment for events {missing}")
    uf = _UnionFind(e.event_id for e in ordered)
    reasons: dict[str, list[str]] = {e.event_id: [] for e in ordered}
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            found = _link_reasons(a, b, by_id, g)
            if found:
                uf.union(a.event_id, b.event_id)
                reasons[a.event_id].extend(found)
    rank_pos = {a.event_id: i for i, a in enumerate(rank(assessments))}
    groups: dict[str, list[str]] = {}
    for e in ordered:
        groups.setdefault(uf.find(e.event_id), []).append(e.event_id)
    clusters = []
    for members in groups.values():
        members = sorted(members, key=rank_pos.__getitem__)
        merged = _dedupe(r for m in sorted(members) for r in reasons[m])
        clusters.append(Cluster(cluster_id=cluster_id_for(members), event_ids=members, reasons=merged))
    return sorted(clusters, key=lambda c: rank_pos[c.event_ids[0]])


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
