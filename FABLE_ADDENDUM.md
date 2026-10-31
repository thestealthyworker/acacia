# Fable Spec Addendum: Deterministic Rule Set and Impact Graph

Workstream brief for Fable. Claude Code builds the rest of the system in parallel. This file is self-contained for your scope. `HANDOVER.md` sections 5, 6 and 18 are the full background; where this file is more specific, this file wins for your modules.

---

## 1. Your scope

You own five modules and their tests. Nothing in them may call an LLM.

| Module | Responsibility |
|---|---|
| `acacia/world.py` | Shared airport world: runway grid and gate pool per airport and date |
| `acacia/graph.py` | Impact graph over all flights of a date: rotation and connection edges |
| `acacia/triage.py` | Knock-on propagation, impact score, safety tier, priority order, clusters |
| `acacia/ledger.py` | Booking ledger for runway slots and gates |
| `acacia/dispatcher.py` | Orchestrator: collect, triage, cluster, dispatch, pre-empt, summarise |
| `acacia/cli.py` (subcommands only) | `triage`, `triage --scan`, `backtest` |
| `tests/test_world.py`, `test_graph.py`, `test_triage.py`, `test_ledger.py`, `test_dispatcher.py`, `test_backtest.py` | Tests 15 to 24 in HANDOVER section 14 |

You do **not** own the agents, solver, checker, fallback, coordinator, UI or Pydantic models. You consume and produce the contracts in section 3 below. If you need a contract changed, write the change in `DECISIONS.md` and tell Claude Code before you rely on it.

**The rules you must never break:**

1. Deterministic: same inputs, salt and policy give byte-identical outputs. Every random value comes from a seeded `random.Random`, and every ordering has an explicit tie-break.
2. Safety is a gate, not a weight: tier 0 always outranks tier 1.
3. The ledger is the single source of truth for bookings. Only the dispatcher writes to it.
4. No double booking, ever.

---

## 2. Conventions shared with Claude Code

- **Times:** integer minutes after local midnight internally; `"HH:MM"` strings in contracts. Use the shared helpers `to_min("HH:MM")` and `to_hhmm(int)` from `acacia/timeutil.py` (Claude Code creates it; if it doesn't exist yet, create it with exactly these two functions).
- **Parsing:** `hhmm` CSV values parse as `h = int(s)//100 % 24`, `m = int(s) % 100`.
- **Seeds:** `random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:16], 16))`, where `key` always starts with `config.SEED_SALT`. Key formats are given per module below. Draw values in the order written.
- **Policy versions:** `impact-0.1`, defined in `config.py` as a dict. Never hard-code weights.
- **Logging:** the dispatcher yields `LogEntry` objects (HANDOVER 11.4) with `who="orchestrator"`. It does not print.

---

## 3. Contracts you produce or consume

Defined in `acacia/models.py` (HANDOVER 5 and 18.3). Summary:

- **Consume:** `DisruptionEvent`, `Injected`, `ConsensusRecord`, `LogEntry`, `Stress`.
- **Produce:** `DownstreamImpact`, `ImpactAssessment`, `QueueItem`, `TriggerQueue`, `Cluster`, `Reservation`, and the `LedgerView` protocol.

Interfaces Claude Code will call (keep these signatures):

```python
# world.py
def get_world(airport: str, date: str, injected: list[Injected]) -> "AirportWorld": ...   # cached per (airport, date)
class AirportWorld:
    def free_slots_after(self, t: int, n: int, view: "LedgerView") -> list[int]: ...
    def is_traffic(self, t: int) -> bool: ...
    def traffic_times(self, start: int, end: int) -> list[int]: ...
    def gates(self) -> list[str]: ...
    def gate_free_from(self, gate: str, t: int, view: "LedgerView") -> int | None: ...  # None = out of service
    def closure_at(self, t: int) -> bool: ...

# ledger.py
class LedgerView(Protocol):
    def slot_taken(self, airport: str, t: int, exclude_event: str | None = None) -> bool: ...
    def gate_busy(self, airport: str, gate: str, start: int, end: int, exclude_event: str | None = None) -> bool: ...
class Ledger:
    def view(self) -> LedgerView: ...          # read-only snapshot
    def live(self) -> LedgerView: ...          # read-only, always current (used by the checker)
    def hold(self, event_id: str, airport: str, slot: int, gate: str, gate_start: int, gate_end: int) -> list[Reservation]: ...
    def commit(self, event_id: str) -> None: ...
    def release(self, event_id: str) -> None: ...
    def reservations_for(self, event_id: str) -> list[Reservation]: ...

# graph.py
class ImpactGraph:
    @classmethod
    def build(cls, day_rows: "pd.DataFrame", date: str) -> "ImpactGraph": ...
    def find(self, airline: str, flight_no: str, origin: str, dest: str, sched_dep: int | None = None) -> str | None: ...
    def rotation_next(self, node_id: str) -> tuple[str, int] | None: ...     # (next node, slack)
    def connections(self, node_id: str) -> list[tuple[str, int, int]]: ...   # (node, gap, pax)
    def node(self, node_id: str) -> dict: ...
    version: str

# triage.py
def propagate(g: ImpactGraph, node_id: str, input_delay: int, max_depth: int = 8) -> list[DownstreamImpact]: ...
def safety_tier(event: DisruptionEvent, land_by: int, first_slot: int, stress: Stress) -> tuple[int, str]: ...
def assess(event: DisruptionEvent, g: ImpactGraph, land_by: int, first_slot: int, stress: Stress, policy: dict) -> ImpactAssessment: ...
def rank(assessments: list[ImpactAssessment]) -> list[ImpactAssessment]: ...
def cluster(events: list[DisruptionEvent], assessments: list[ImpactAssessment], g: ImpactGraph) -> list[Cluster]: ...

# dispatcher.py
class Dispatcher:
    def __init__(self, data, agents, ledger: Ledger, policy: dict, stress: dict[str, Stress] | None = None): ...
    def collect(self, payloads: list[dict] | None = None, scan: tuple[str, str, int] | None = None) -> list[DisruptionEvent]: ...
    def run(self) -> Iterator[LogEntry]: ...   # yields orchestrator AND per-event entries, each tagged with event_id
    def preempt(self, event_id: str, stress: Stress) -> None: ...
    def sign_off(self, event_id: str, approve: bool, who: str) -> LogEntry: ...
```

Claude Code provides `coordinator.run(event, stress, agents, ledger_view) -> Iterator[LogEntry]` and the final `ConsensusRecord` as the last entry's payload.

---

## 4. `world.py`: rules

Seed key: `f"{SALT}|world|{airport}|{date}"`. Build lazily, cache per `(airport, date)`.

1. **Closures:** the union of `[start, end)` of every `RUNWAY_CLOSED` injected event for this airport and date. Passing more events later extends the closures; rebuild the grid from scratch in a deterministic order (events sorted by event_id).
2. **Runway grid:** start at 00:00 and step 4 min, or 8 min while inside a closure, up to 24:00 (keep the grid within the day). For each grid point draw `u = rng.random()`; it is traffic if `u < 0.55`. Draw in grid order.
3. **free_slots_after(t, n, view):** grid points `>= t`, in order, that are not traffic and not `view.slot_taken(airport, p)`. Skip `rand(0..1)` eligible points between returned slots, using a second rng seeded `f"{SALT}|slots|{airport}|{date}|{t}"`. Return up to n.
4. **Gate pool:** 8 gates. Letter: "A" for ATL, else the first letter of the airport code. Numbers: 8 distinct values from 1..30 (rng sample, then sort). For each gate, draw 2 to 4 busy intervals: start uniform over 05:00 to 22:00, length 30 to 70 min. Merge overlaps.
5. **Equipment failures:** for each `EQUIPMENT_FAILURE` event, take one gate out of service for its interval (rng choice seeded `f"{SALT}|fail|{event_id}"`).
6. **gate_free_from(gate, t, view):** the earliest time `>= t` at which the gate is free of background busy intervals, failure intervals and ledger bookings for at least 30 min. Return None if it is out of service at t. Never return a time inside a booked interval.

---

## 5. `graph.py`: rules

Build from **all** rows of the date (`YEAR, MONTH, DAY`), including non-qualifying ones.

**Skip rows** that are cancelled, have blank scheduled times, or have non-3-letter airport codes. Log the counts.

**Node id:** `f"{AL}{FN}-{date}-{O}{D}-{sched_dep:04d}"`, where `sched_dep` is the original hhmm integer. If two rows collide, add `-2`, `-3` and so on, in file order.

**Rotation edges:**
1. Group by tail. Exclude blank tails and `UNKNOWN`.
2. Within a tail, sort by `sched_dep` (local origin time), then file order.
3. For consecutive legs a then b, add an edge only if `a.dest == b.origin` and `b.sched_dep >= a.sched_arr - 30`. The 30-minute tolerance covers data noise; anything earlier means a broken chain, so skip it.
4. `slack = max(0, b.sched_dep - a.sched_arr - min_turn(a.distance))`, with `min_turn = 60 if d > 2000 else 45 if d > 1000 else 35`.
5. Each node has at most one rotation successor.

**Connection edges:**
1. For each inbound a (dest X), candidates are outbound b with the same airline, `b.origin == X`, b not on a's tail, and `35 <= b.sched_dep - a.sched_arr <= 120`.
2. Keep the 5 smallest gaps (tie-break by node id).
3. `pax = rand(2..15)`, seeded `f"{SALT}|pax|{a_id}|{b_id}"`.

**Version:** `sha256(f"{date}|{rows_used}|impact-0.1")[:12]`.

**Performance target:** one date of the full data set builds in under 5 s. Use pandas groupby, not nested Python loops over all rows, for the connection join (merge on airline and airport, then filter on the gap).

---

## 6. `triage.py`: rules

**Input delay:** the event's `hist_delay_min` (the do-nothing case, real data). If the trigger is not in the graph (for example a synthetic payload), propagate nothing, set `downstream = []`, and say so in `tier_reason` or the log.

**propagate** (breadth-first along rotation edges only):

```
d = input_delay; node = trigger; depth = 0
while depth < max_depth:
    nxt = rotation_next(node); if nxt is None: break
    node, slack = nxt; depth += 1
    d = max(0, d - slack)
    record DownstreamImpact(ROTATION, depth, slack, propagated_delay_min=d,
                            late_night = (sched_dep(node) + d) > 23*60)
    if d == 0: break
```

Then, for the trigger and for every rotation node with `d > 0`, check each connection `(b, gap, pax)`: it breaks if `d > gap - 35`. Record a `DownstreamImpact(CONNECTION, depth+1, gap_min=gap, propagated_delay_min=d, pax_at_risk=pax)`. Connections never propagate further.

If a downstream leg is cancelled or diverted in the data, it is not in the graph, and the chain ends there.

**Score** (policy `impact-0.1`):

| Component | Definition | Weight |
|---|---|---|
| `knock_on_min` | Sum of `propagated_delay_min` over ROTATION entries (not the trigger itself) | 1.0 |
| `pax_connections_at_risk` | Sum of `pax_at_risk` over CONNECTION entries | 3.0 |
| `late_night_legs` | Count of ROTATION entries with `late_night` | 60.0 |
| `flights_affected` | Count of ROTATION entries with `d > 0` | 10.0 |

`impact_score = round(sum(weight x component), 2)`. `breakdown` holds the four raw components.

**safety_tier:**
- tier 0, reason `"FUEL_MARGIN <= 10 min to first slot"`, if `land_by - first_slot <= 10`;
- tier 0, reason `"LOW_FUEL stress"`, if `stress.low_fuel`;
- tier 0, reason `"DECLARED_EMERGENCY"`, if the payload declares one;
- else tier 1, reason `"normal"`.

`land_by` and `first_slot` come from Claude Code's `simulate.py`. Call it, don't reimplement it.

**rank:** sort by `(tier, -impact_score, to_min(decision_deadline), event_id)`.

**cluster** (union-find over the events, in event_id order). Union two events if any of these hold:
1. Same destination and `abs(eta_a - eta_b) <= 60` min. Reason: `"shared runway window {AP} {HH:MM}-{HH:MM}"`.
2. One trigger's node appears in the other's ROTATION downstream. Reason: `"rotation {tail}"`.
3. They share a CONNECTION edge in either direction. Reason: `"connection {a}->{b}"`.

`cluster_id = "CL-" + sha256("|".join(sorted(ids)))[:8]`. Within a cluster, `event_ids` are in rank order. Order clusters by the rank of their best member.

---

## 7. `ledger.py`: rules

- Storage: an in-memory list plus append-only `runs/ledger-{date}.jsonl`. Every change appends a line `{ts, op, reservation}`. On startup, replay the file if it exists.
- `res_id = f"R-{event_id}-{resource_type}-{seq}"`.
- `hold` refuses (raises `DoubleBookingError`) if the slot is taken or the gate interval overlaps any HELD or COMMITTED reservation of another event. **Never silently overwrite.**
- Gate intervals are half-open `[start, end)`, so back-to-back bookings are allowed.
- `view()` returns a frozen copy; `live()` reads the current state. The checker uses `live()`.
- Status changes: HELD -> COMMITTED (approve), HELD -> RELEASED (reject or pre-empt). COMMITTED -> RELEASED only through an explicit `cancel` (not used in the MVP). Any other transition raises.

---

## 8. `dispatcher.py`: rules

States, each emitting one orchestrator `STATE` entry with reasoning text: `COLLECTING -> TRIAGING -> CLUSTERING -> DISPATCHING -> DRAINED`.

1. **collect:** from payloads (via Claude Code's `trigger.to_event`) or from a `scan(airport, date, window_min)` that takes every qualifying row whose earliest landing at that airport falls in the window. Deduplicate by event_id.
2. **triage:** build or reuse the `ImpactGraph` for the date. For each event, get `land_by` and `first_slot` from the simulator using the current ledger view. Call `assess`. Emit one `ImpactAssessment` entry per event, with a reasoning text such as "DL1639: tier 1, impact 412.0 (knock-on 236 min over 3 legs, 38 passengers at risk, 0 late-night legs)".
3. **cluster and queue:** emit one `CLUSTER` entry per cluster (members and reasons) and one `TriggerQueue` entry.
4. **dispatch:** for each cluster in order, and each event in rank order: emit `DISPATCH`, run `coordinator.run(event, stress, agents, ledger.view())`, and yield its entries tagged with the event_id. When the run ends in COMMITTED or a non-divert FALLBACK, call `ledger.hold` and emit a `LEDGER` entry. If `hold` raises `DoubleBookingError` (for example the world changed mid-run), log it, release, and re-run that event once with a fresh view; if it fails again, mark it for manual handling.
5. **pre-emption:** `preempt(event_id, stress)` recomputes that event's tier. If it becomes tier 0 while another event is negotiating, the current coordinator run finishes its current turn: check a flag between turns, which Claude Code exposes as `coordinator.run(..., should_yield=callable)`. Then mark that event `PREEMPTED`, release its holds, re-queue it, and dispatch the tier 0 event next. Emit a `PREEMPT` entry naming both events.
6. **sign_off:** commit on approve, release on reject. Emit a `LEDGER` entry plus Claude Code's `SignOff` entry.
7. **drained:** emit a `SUMMARY` entry: counts by outcome, total knock-on minutes in the do-nothing case vs after the plans (re-run `propagate` with each event's planned delay), consensus rate, fallbacks and diverts.

The orchestrator log is its own hash chain (HANDOVER 11.4). Every `DISPATCH` entry's hash is written into the started event's first entry as `dispatched_by`.

---

## 9. Back-test (`cli backtest --date D [--fit-date F]`)

For each qualifying trigger on date D that is found in the graph:

1. Predicted delay on the next rotation leg = `max(0, hist_delay - slack)`. Actual = that leg's `LATE_AIRCRAFT_DELAY` (0 if blank), and separately its `DEPARTURE_DELAY` clipped at 0.
2. Report n, MAE, median absolute error, the share within 15 min, and the share where predicted and actual are both 0 or both above 0.
3. Ranking: Spearman correlation between the predicted `knock_on_min` and the actual sum of `LATE_AIRCRAFT_DELAY` over the downstream rotation legs. Write a plain rank-correlation helper; no SciPy needed.
4. `--fit-date F` only prints the same metrics for F. Do not change formulas or weights in code based on results from D. Record any tuning in `DECISIONS.md` together with the date it was fitted on.

Write the results to `runs/backtest-{D}.json` and print a short table.

---

## 10. Fixture and expected values (`tests/fixtures/rotation_day.csv`)

The kit includes a hand-built fixture (fictional tail `N900ZZ`, airline DL, date 2015-03-10). Its rows:

| Leg | Route | Sched dep | Sched arr | Distance |
|---|---|---|---|---|
| L1 | ATL to DCA | 08:00 | 10:00 | 547 |
| L2 | DCA to ATL | 10:50 | 12:50 | 547 |
| L3 | ATL to MCO | 13:40 | 15:00 | 404 |
| L4 | MCO to ATL | 15:45 | 17:10 | 404 |
| C1 | DL2000 ATL to BOS, other tail | 13:40 | 16:05 | 946 |

L1 is the trigger, with an arrival delay of 60 min.

**Expected results:**

- Slacks: L1 to L2 = 15, L2 to L3 = 15, L3 to L4 = 10.
- Propagated delay: L2 = 45, L3 = 30, L4 = 20. So `knock_on_min = 95` and `flights_affected = 3`.
- Connection L2 to C1: gap 50, breaks because 45 > 15. L3 also connects to nothing in the fixture.
- `late_night_legs = 0`.
- `impact_score = 95 + 3 x pax(L2, C1) + 0 + 30`. The pax value comes from the seed; assert the formula, not a hard-coded pax.
- With input delay 10: L2 = 0. The chain stops after one entry; knock-on 0; no connection breaks.

Add your own fixtures for midnight, blank tails, duplicate node ids and cancelled legs.

---

## 11. Definition of done (your workstream)

- [ ] Tests 15 to 24 pass offline in under 10 s.
- [ ] `cli triage --scan --date D` lists airport windows by trigger count.
- [ ] `cli triage --airport AP --date D --window 180` prints the queue and runs every event to AWAITING_SIGNOFF, with no double booking.
- [ ] `cli backtest --date D` writes its JSON and table.
- [ ] Scenario E and F behaviour from HANDOVER section 12 is reproducible with a fixed salt.
- [ ] Every deviation is recorded in `DECISIONS.md`.
