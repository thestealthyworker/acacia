"""Coordinator time budget (TIME-1..7): the deadline guard and the live wall-clock budget.

The simulated clock is deterministic: commit_time = decision_time + negotiation + sign-off
budget (acacia.constraints). The real wall-clock duration of a run is measured here and logged
only in the non-hashed `LogEntry.meta`, so the hash chain and replays stay identical across
runs. Only live LLM agents (`enforces_wall_budget = True`) can overshoot the budget; rule and
replay runs are never cut by the wall clock.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

from acacia import constraints
from acacia.config import MINUTES_PER_DAY
from acacia.models import DisruptionEvent
from acacia.timeutil import to_hhmm, to_min

SECONDS_PER_MIN = 60
WALL_BUDGET_S = constraints.NEGOTIATION_BUDGET_MIN * SECONDS_PER_MIN  # TIME-7
Clock = Callable[[], float]


def deadline_reason(event: DisruptionEvent) -> str | None:
    """Why there is no time to negotiate (TIME-5), or None when the budget fits before E."""
    if constraints.has_time_to_negotiate(event):
        return None
    decided, eta = constraints.decision_time_min(event), to_min(event.eta_landing)
    return (f"no time to negotiate: earliest landing {event.eta_landing} is {eta - decided} min after the "
            f"decision time {to_hhmm(decided)}, under the {constraints.time_budget_min()} min budget "
            f"({constraints.NEGOTIATION_BUDGET_MIN} min negotiation + {constraints.SIGNOFF_BUDGET_MIN} min "
            "sign-off)")


def budget_text(event: DisruptionEvent) -> str:
    """One sentence for the outcome entry: the simulated clock behind the plan (TIME-1..4)."""
    return (f" Clock (simulated): decided {to_hhmm(constraints.decision_time_min(event))}, commit "
            f"{to_hhmm(constraints.commit_time_min(event))} ({constraints.NEGOTIATION_BUDGET_MIN} min negotiation + "
            f"{constraints.SIGNOFF_BUDGET_MIN} min sign-off); slots from {to_hhmm(constraints.earliest_slot_min(event))}, "
            "gate-change notice counted from commit.")


def enforces_wall_budget(agents: Mapping[object, object]) -> bool:
    """True when any agent is a live LLM agent (the only kind whose wall time can overshoot)."""
    return any(getattr(a, "enforces_wall_budget", False) for a in agents.values())


class WallClock:
    """Real elapsed seconds since the run started; never part of a hashed payload."""

    def __init__(self, enforced: bool, clock: Clock | None = None) -> None:
        self.enforced = enforced
        self._clock = clock or time.monotonic
        self._start = self._clock()

    def elapsed(self) -> float:
        return max(0.0, self._clock() - self._start)

    def overshoot_reason(self) -> str | None:
        """The TIME-7 fallback reason when an enforced run is over budget, else None."""
        if not self.enforced:
            return None
        spent = self.elapsed()
        if spent <= WALL_BUDGET_S:
            return None
        return (f"wall-clock negotiation time {spent:.0f} s is over the {WALL_BUDGET_S} s budget "
                f"({constraints.NEGOTIATION_BUDGET_MIN} min, TIME-7); the slots offered may already be stale")

    def meta(self) -> dict:
        return {"wall_clock_s": round(self.elapsed(), 3), "wall_budget_s": WALL_BUDGET_S,
                "wall_budget_enforced": self.enforced}


def shared_clock(event: DisruptionEvent, previous_commit: int | None) -> tuple[DisruptionEvent, dict]:
    """TIME-6: the event as dispatched on the shared simulated clock, plus the clock payload.

    Its decision time is at least the previous dispatched event's commit time (cumulative across
    triggers, capped at 23:59 so it never wraps to the next day)."""
    own = constraints.decision_time_min(event)
    decided = own if previous_commit is None else max(own, previous_commit)
    decided = min(decided, MINUTES_PER_DAY - 1)
    timed = event.model_copy(update={"decision_time": to_hhmm(decided)})
    clock = {"own_decision_time": to_hhmm(own), "decision_time": to_hhmm(decided),
             "commit_time": to_hhmm(constraints.commit_time_for(decided)),
             "previous_commit_time": None if previous_commit is None else to_hhmm(previous_commit)}
    return timed, clock


def clock_text(clock: dict) -> str:
    prev = clock["previous_commit_time"]
    after = f", previous commit {prev}" if prev else ", first on the clock"
    return (f" Shared clock (simulated): decision {clock['decision_time']} (own {clock['own_decision_time']}"
            f"{after}), commit {clock['commit_time']}.")
