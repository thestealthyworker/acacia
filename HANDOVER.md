# Acacia Statement 2: Build Handover for Claude Code

Multi-agent disruption negotiation for air traffic management. ATC, Pilot and Ground AI agents negotiate a recovery for a real delayed flight. When several disruptions arrive together, a deterministic orchestrator ranks them on an impact graph and negotiates them in priority order against a shared booking ledger. Deterministic code makes every safety decision. Hackathon build, 3 hours.

**Workstreams.** Fable builds the deterministic rule set and the impact graph (section 18: `graph.py`, `triage.py`, `ledger.py`, `world.py`, `dispatcher.py` and their tests). Claude Code builds everything else. The contracts in sections 5 and 18.3 are the boundary between the two; neither side changes them without writing it in `DECISIONS.md`.

Read this file end to end before writing code. Then read the files listed in section 1.

---

## 0. Mission and definition of done

Build a Python app that:

1. Takes a trigger (one delayed flight from the 2015 US DOT data, or a trigger payload).
2. Runs the 6-stage, max-3-turn negotiation between three independent LLM agents (separate calls, separate private state).
3. Uses deterministic code for solving, safety checking, fallback and turn control.
4. Shows it in a two-panel Streamlit UI: dashboard on the left, the full agent conversation on the right.

**Done means all of these are true:**

- [ ] `pytest -q` passes with no network access (the tests use rule-based agents).
- [ ] `python -m acacia.cli run --flight DL1639 --date 2015-02-26 --agents rule` prints a full trace and a `ConsensusRecord`.
- [ ] `streamlit run app.py` shows the two-panel UI. Any qualifying flight can be picked and played.
- [ ] The four demo scenarios in section 12 behave as described, with `--agents rule` and with `--agents llm`.
- [ ] `--agents replay` runs a recorded LLM session with no API key (demo insurance).
- [ ] No plan is ever committed unless the independent checker passes it. A test proves this.
- [ ] `python -m acacia.cli triage --airport ATL --date 2015-02-26 --window 3h` (or any airport and date with 3+ triggers) prints a ranked trigger queue with impact breakdowns and clusters, then negotiates them in order.
- [ ] Two triggers at the same airport never hold the same runway slot or gate. A test proves this.
- [ ] `python -m acacia.cli backtest --date <date>` reports how well the knock-on delay prediction matches the real late-aircraft delay in the data.

**The design principle the whole build serves:** the AI agents argue, deterministic code decides. Agents only declare limits and vote. They never compute times, separation or fuel, and they never commit.

---

## 1. Inputs and source of truth

| File (path in the kit) | What it is | Precedence |
|---|---|---|
| `docs/statement-2.png` (plus any full statement text Ashiq adds to `docs/`) | The brief: what to build and what to avoid | Highest: requirements |
| `docs/architecture.html` | The agreed architecture: stages, state machine, contracts, information ownership | Wins on design questions |
| `docs/trigger-payload.sample.json` (Ashiq's sample; `docs/trigger-payload.example.json` is only a placeholder) | The input shape the system must accept | Wins on input format |
| `HANDOVER.md` (this file) | Build spec: formulas, algorithms, file layout, tests | Wins on implementation detail |
| `FABLE_ADDENDUM.md` | Detailed spec for the triage and graph workstream | Wins on detail for section 18 modules |
| `docs/ui-reference.html` (the prototype) | Working browser prototype of the single-event logic and UI. Open it in a browser | Reference for UI layout and behaviour only |
| `data/flights.csv`, `data/airports.csv`, `data/airlines.csv` | 2015 US DOT on-time data. The kit ships `airlines.csv` plus small samples; Ashiq adds the full files | Data |

If two sources conflict, follow the precedence above and write the decision in `DECISIONS.md`.

The prototype implements most of this spec in JavaScript (functions `triangulate`, `simulate`, `declare`, `decide`, `solve`, `check`, `fallback`, `run`). Port its behaviour, not its code style. One known flaw in the prototype must NOT be copied: its solver reads the agents' private state to score packages. In the real build the solver may only use what agents declared (see section 7).

---

## 2. Stack and setup

- Python 3.11+
- `pandas`, `pydantic>=2`, `anthropic`, `streamlit`, `pytest`, `python-dotenv`
- Optional: `hypothesis` for property tests
- No agent framework. Plain Python plus the Anthropic SDK.

Environment variables (`.env`, never committed):

```
ANTHROPIC_API_KEY=...
ACACIA_MODEL=<a current fast Claude model id; confirm with Ashiq>
ACACIA_AGENT_MODE=rule        # rule | llm | replay
ACACIA_DATA_DIR=./data
ACACIA_SEED_SALT=acacia-2026  # changes every simulated value if altered
```

---

## 3. Repository layout

```
acacia/
  __init__.py
  config.py          # env, policy weights, thresholds (versioned)
  models.py          # Pydantic contracts (section 5)
  data.py            # CSV loading, parsing, qualifying filter (section 4)
  trigger.py         # trigger payload adapter -> DisruptionEvent (section 4.4)
  triangulate.py     # stage 0 (section 4.3)
  simulate.py        # seeded private state per agent (section 6)
  agents/
    base.py          # Agent interface
    rule.py          # deterministic stand-ins (tests, fallback mode)
    llm.py           # Anthropic-backed agents (section 8)
    replay.py        # serves recorded LLM responses
    prompts/         # atc.md, pilot.md, ground.md
  solver.py          # stage 3 (section 7)
  checker.py         # independent safety checks (section 9)
  fallback.py        # rule-based plan (section 10)
  coordinator.py     # state machine, turn counter, generator of log entries (section 11)
  log.py             # hash-chained JSONL audit log (section 11.4)
  world.py           # shared airport world: runway grid and gate pool (section 18.2)   [Fable]
  graph.py           # impact graph: rotation, connection, resource edges (18.1)       [Fable]
  triage.py          # knock-on propagation, impact score, priority, clusters (18.4)  [Fable]
  ledger.py          # booking ledger for slots and gates (18.5)                       [Fable]
  dispatcher.py      # orchestrator above the coordinator (18.6)                       [Fable]
  cli.py             # run, batch, triage, backtest, verify-log
app.py               # Streamlit UI (section 13)
tests/               # section 14
data/                # flights.csv, airports.csv, airlines.csv (not committed if large)
recordings/          # recorded LLM responses for replay mode
runs/                # JSONL logs, one per event
DECISIONS.md         # every deviation from this spec, one line each
```

---

## 4. Data

### 4.1 Files and columns

`flights.csv` has 31 columns: YEAR, MONTH, DAY, DAY_OF_WEEK, AIRLINE, FLIGHT_NUMBER, TAIL_NUMBER, ORIGIN_AIRPORT, DESTINATION_AIRPORT, SCHEDULED_DEPARTURE, DEPARTURE_TIME, DEPARTURE_DELAY, TAXI_OUT, WHEELS_OFF, SCHEDULED_TIME, ELAPSED_TIME, AIR_TIME, DISTANCE, WHEELS_ON, TAXI_IN, SCHEDULED_ARRIVAL, ARRIVAL_TIME, ARRIVAL_DELAY, DIVERTED, CANCELLED, CANCELLATION_REASON, AIR_SYSTEM_DELAY, SECURITY_DELAY, AIRLINE_DELAY, LATE_AIRCRAFT_DELAY, WEATHER_DELAY.

`airports.csv`: IATA_CODE, AIRPORT, CITY, STATE, COUNTRY, LATITUDE, LONGITUDE. Three rows (ECP, PBG, UST) have blank coordinates; skip them for distance maths.

`airlines.csv`: IATA_CODE, AIRLINE (14 carriers).

The provided flights.csv is about 38.6 MB, a subset of the full 2015 set. Confirm its date range on first load and log it.

### 4.2 Parsing rules

- Read time columns as strings (`dtype=str`) so leading zeros survive (`"0648"`).
- `hhmm` to minutes after midnight: `h = int(s)//100 % 24`, `m = int(s) % 100`. `"2400"` becomes 0.
- All times are local to their airport. Every calculation in this build happens at the destination airport, so no time-zone conversion is needed. Do not compare origin and destination clock times.
- Delay-cause columns are blank unless the arrival delay is 15 minutes or more. Blank means 0 for our formulas.
- In the full 2015 set, October rows use numeric airport IDs instead of IATA codes. If present, skip rows whose airport codes are not 3 letters and log the count.
- Cancelled or diverted rows are never triggers.

**Qualifying rule (state machine guard `IDLE -> TRIANGULATED`):** `CANCELLED == 0 and DIVERTED == 0 and (WEATHER_DELAY > 15 or LATE_AIRCRAFT_DELAY > 20)`.

### 4.3 Stage 0: triangulate

Let `S` = scheduled arrival, `A` = actual arrival, `WO` = wheels-on, `Ti` = taxi-in (default 8 if blank), and `W, L, AL, SE, AS` = weather, late-aircraft, airline, security and air-system delay.

- **Classification:** `COMBINED` if W > 15 and L > 20; else `WEATHER` if W > 15; else `KNOCK_ON`.
- **Earliest landing:** `E = min(S - Ti + L + AL + SE, WO)` (use `A - Ti` if WO is blank). Late-aircraft, airline and security delay happen before departure, so negotiation cannot recover them. Weather and air-system delay are what negotiation works on.
- **Injected event:**
  - If W > 15: `RUNWAY_CLOSED`, ref "one of two arrival runways", start `E - 10`, end `E + W + rand(0..10)`.
  - Else: `EQUIPMENT_FAILURE`, ref "jet bridge out at one gate", start `E - 20`, end `E + 180`.
- **event_id:** `EVT-` + first 8 hex characters of sha256 of `"{airline}{flight_no}-{date}-{origin}{dest}"`, upper case.

### 4.4 Trigger payload adapter

Ashiq will provide a sample trigger payload. Treat its shape as authoritative for input. Write `trigger.py` with one function:

```python
def to_event(payload: dict, data: DataStore, stress: Stress) -> DisruptionEvent: ...
```

It must accept at least:

1. A row reference: airline + flight number + date (+ origin/dest if ambiguous). Look the row up in flights.csv, then triangulate.
2. A raw row: the 31 CSV fields inline. Triangulate directly.
3. Whatever the provided sample contains (for example an explicit injected event, overrides or stress flags). Merge its fields over the triangulated event and record each override in the event's `overrides` list.

Reject a payload that fails the qualifying rule with a clear error, unless it carries an explicit injected event. Validate everything through Pydantic.

---

## 5. Message contracts (`models.py`)

These five contracts come from the architecture and are fixed. The extra fields marked `ADDED` are needed by this build; note each one in `DECISIONS.md`.

```python
class ScenarioType(str, Enum): WEATHER="WEATHER"; KNOCK_ON="KNOCK_ON"; COMBINED="COMBINED"
class AgentRole(str, Enum): ATC="ATC"; PILOT="PILOT"; GROUND="GROUND"
class Verdict(str, Enum): ACCEPT="ACCEPT"; REJECT="REJECT"
class ReasonCode(str, Enum):
    FUEL_LIMIT="FUEL_LIMIT"; GATE_CONFLICT="GATE_CONFLICT"; SEPARATION="SEPARATION"
    CREW_DUTY="CREW_DUTY"; CONNECTION_RISK="CONNECTION_RISK"

class Injected(BaseModel):
    type: Literal["RUNWAY_CLOSED","EQUIPMENT_FAILURE"]; ref: str; start: str; end: str  # "HH:MM"

class DisruptionEvent(BaseModel):
    event_id: str; scenario_type: ScenarioType
    flight: FlightRef            # airline, flight_no, tail, origin, dest, date
    sched_arrival: str; hist_arrival: str; hist_delay_min: int
    eta_landing: str             # ADDED: E from 4.3
    taxi_in: int                 # ADDED
    distance: int                # ADDED (drives turnaround)
    causes: Causes               # weather, late_aircraft, air_system, airline, security
    injected: list[Injected]; turn: int = 1
    overrides: list[str] = []    # ADDED: fields set by the trigger payload

class HardLimit(BaseModel): id: str; rule: str; value: Any
class SoftPref(BaseModel):
    id: str; preference: str; weight: float
    metric: Literal["slot_demand","gate_arrival_by","fuel_margin","turnaround_done_by","keep_gate"]  # ADDED
    target: Any = None           # ADDED: e.g. "12:45", gate id
    stake: int | None = None     # ADDED: e.g. connecting passengers
class Option(BaseModel): type: Literal["SLOT","GATE","ROUTE"]; value: str; demand: int | None = None

class ConstraintPacket(BaseModel):
    event_id: str; agent: AgentRole; turn: int
    hard: list[HardLimit]; soft: list[SoftPref]; options: list[Option]
    facts_owned: dict

class ResolutionPackage(BaseModel):
    event_id: str; package_id: str; turn: int
    slot: str; gate: str; route_change: str | None = None
    score: float; score_breakdown: dict[str, float]  # fuel, connections, throughput, turnaround
    satisfies_hard: bool = True

class Decision(BaseModel):
    event_id: str; package_id: str; agent: AgentRole; turn: int
    verdict: Verdict; reason_code: ReasonCode | None = None
    cites: str | None = None     # ADDED: id of the declared limit or preference behind a REJECT
    rationale: str               # one line, for the log

class ConsensusRecord(BaseModel):
    event_id: str; outcome: Literal["CONSENSUS","FALLBACK"]
    package: dict; turns_used: int; decisions: list[Decision]; checker_pass: bool | None
    delta_vs_history_min: int | None
    delta_vs_fallback_min: int | None   # ADDED
    status: Literal["AWAITING_SIGNOFF","APPROVED","REJECTED"] = "AWAITING_SIGNOFF"  # ADDED
    signed_off_by: str | None = None
    stamp: dict                          # ADDED: policy, solver, checker, model, prompt hashes
```

Validators:

- A REJECT must have a `reason_code` and a `cites`.
- Times are `"HH:MM"` strings on the wire and integer minutes inside the code. Put the conversion in one helper and test it.

---

## 6. Simulated private state (`simulate.py`)

**Multi-trigger change.** The runway grid (ATC) and the gate pool (Ground) are no longer simulated per event. They come from the shared `AirportWorld` for the destination airport and date (section 18.2), filtered through the booking ledger. That is what lets two events at the same airport actually compete for the same slots and gates. The formulas below still define the per-event pieces (queue, pilot state, crew shift, red lines). For a single event with an empty ledger the behaviour is the same as before.

Everything private is simulated, seeded, and labelled "simulated" in the UI. Seed: `random.Random(int(sha256(f"{SALT}|{event_id}|private").hexdigest()[:16], 16))`. Draw values in the order listed so runs are repeatable. The numbers will not match the JavaScript prototype exactly; that is fine.

`rand(a..b)` means an inclusive integer.

**ATC**
- `spacing(t) = 8 if inside the runway closure else 4` (minutes).
- `queue = round(W * 0.6) if W > 15 else rand(3..8)`; `first = E + queue`.
- Walk a runway grid from `E - 24` to `E + 150` by `spacing(t)`. Mark grid points at or after `first` as free slots until you have 3 (closure) or 4 (no closure), skipping `rand(0..1)` points between free slots. All other points are other traffic.
- `demand[i] = clamp(4 - i + rand(0..1), 1, 4)` for free slot i (earlier slots are more contested).
- `min_separation = 3` minutes.

**Pilot**
- `land_by = first - 3` if stress `low_fuel`, else `E + rand(45..74)`.
- Hub destinations (ATL, DFW, ORD, DEN, IAH, CLT, JFK, SFO, LAX, PHX, MSP, DTW, EWR, SEA): `pax = rand(24..60)`, else `rand(6..30)`.
- `connect_by = S + rand(28..55)` (the gate-arrival deadline to protect connections).
- Private red line (not declared): reject if connections are fully lost and `pax >= 25`.

**Ground**
- Gate letter: "A" for ATL, else the first letter of the destination code. Numbers: `n1 = rand(2..14)`, `n2 = n1 + rand(2..5)`, `n3 = n2 + rand(1..4)`. The assigned gate is the first.
- `free_from`: gate1 `E + Ti + rand(8..38)`, gate2 `E - 30`, gate3 `E + rand(5..30)`.
- If the event is `EQUIPMENT_FAILURE`, gate2 is out of service.
- `min_turn = 60 if distance > 2000 else 45 if distance > 1000 else 35`.
- `shift_end = first + Ti + min_turn - 6` if stress `tight_crew`, else `E + Ti + min_turn + rand(25..85)`.
- `duty_limit = shift_end + 45` (hard). `next_departure = S + min_turn + rand(20..60)`.
- Private red line (not declared): reject if crew overtime is more than 20 minutes.

Stress flags (`Stress` model): `low_fuel`, `tight_crew`, `corrupt_solver`.

---

## 7. Solver (`solver.py`), stage 3

**Input:** the three declared ConstraintPackets, the cuts so far, and the packages already tried. **The solver must not read private state.** Every number it uses comes from declared hard limits, soft preferences (`metric`, `target`, `stake`) and options.

1. Enumerate packages: every declared slot x every declared gate (route change unused in the MVP). Number them P1, P2, and so on.
2. Hard filter from the declarations. Discard a package with the reason code if:
   - slot is later than Pilot `LAND_BY` -> `FUEL_LIMIT`
   - gate is unavailable, or `slot + taxi_in` is before its free time -> `GATE_CONFLICT`
   - `slot + taxi_in + min_turn` is after `CREW_DUTY_LIMIT` -> `CREW_DUTY`
3. Score each survivor with utilities in 0..1:
   - `throughput = 1 - (demand - 1) / 3`
   - `connections = 1 if gate_arr <= target else max(0, 1 - (gate_arr - target) / 30)`
   - `fuel = clamp((land_by - slot) / 45, 0, 1)`
   - `turnaround = 0.7 * crew + 0.3 * gate`, where `crew = 1 if no overtime else max(0, 1 - overtime / 60)` and `gate = 1 if the assigned gate else 0.6`
4. `score = sum(POLICY[k] * utility[k])`, with `POLICY = {throughput: 0.35, connections: 0.30, fuel: 0.15, turnaround: 0.20}` and version `policy-0.1`, kept in `config.py`. Return `score_breakdown` per component.
5. **Cuts:** when a REJECT cites a soft preference id, every later package must score strictly higher on that preference's utility than the rejected package did.
6. Exclude packages already tried. Rank by score. Return the top package and the full list (with discard reasons) for the UI.

An empty feasible set sends the coordinator to `FALLBACK`.

`corrupt_solver` stress (safety demo only): on the first solve, return a package that breaks a hard limit while claiming `satisfies_hard=True`. If no infeasible package exists, move the top package's slot onto an occupied traffic time (a separation breach). The checker must catch it.

---

## 8. Agents (`agents/`)

### 8.1 Interface

```python
class Agent(Protocol):
    role: AgentRole
    def declare(self, event: DisruptionEvent, private: dict) -> ConstraintPacket: ...
    def decide(self, event: DisruptionEvent, package: ResolutionPackage,
               private: dict, own_packet: ConstraintPacket) -> Decision: ...
```

Each agent receives only the shared event plus its own private state. Never pass one agent's private state or packet to another agent.

### 8.2 Rule-based agents (`rule.py`)

These are deterministic stand-ins used by tests and by `--agents rule`. Port the prototype's `declare` and `decide`:

- **ATC** declares: `A_H1 SLOT_IN_SEQUENCE` (its free slots), `A_H2 MIN_SEPARATION_MIN = 3`, soft `A_S1` (slot_demand, weight 0.7), and slot options with demand. It decides REJECT `SEPARATION` citing `A_H1` if the slot is not one of its free slots, otherwise ACCEPT.
- **Pilot** declares: `P_H1 LAND_BY`, soft `P_S1` (gate_arrival_by, target connect_by, stake pax, weight 0.7) and `P_S2` (fuel_margin, weight 0.3). It decides REJECT `FUEL_LIMIT` citing `P_H1` if the slot is past land_by; REJECT `CONNECTION_RISK` citing `P_S1` on the red line; otherwise ACCEPT.
- **Ground** declares: `G_H1 GATE_FREE_FROM` (map of gate to time, or "unavailable"), `G_H2 CREW_DUTY_LIMIT`, soft `G_S1` (turnaround_done_by, target shift_end, weight 0.6) and `G_S2` (keep_gate, target the assigned gate, weight 0.4), and gate options. It decides REJECT `GATE_CONFLICT` citing `G_H1`, REJECT `CREW_DUTY` citing `G_H2` on a duty breach, REJECT `CREW_DUTY` citing `G_S1` on the overtime red line, otherwise ACCEPT.

### 8.3 LLM agents (`llm.py`)

- One Anthropic call per agent per step, in separate requests with separate system prompts. Temperature 0.
- Get structured output with tool use. Define one tool per step (`submit_constraint_packet`, `submit_decision`) whose `input_schema` is the Pydantic model's `model_json_schema()`. Force it with `tool_choice`.
- **Prompt contents:** the role and what it owns (from the architecture's information ownership table), the shared event JSON, its own private state JSON, and for `decide`, the package plus its own packet. Rules in the prompt:
  - declare only facts you own;
  - never invent numbers;
  - a REJECT must cite an id from your own packet;
  - keep the rationale to one line.
- Validate the output with Pydantic. On failure, retry once with the validation error appended. A second failure counts as a timeout (section 11.3).
- After a declaration, run a **declaration audit**: compare the declared hard limits against the private state and log any mismatch as an `AUDIT` note. Do not correct it, because the checker uses the ground truth anyway. This is part of the safety story.
- Per-call timeout 20 s (configurable).
- **Recording:** save every request/response pair to `recordings/{event_id}/{role}-{step}-{turn}.json`, keyed by a hash of the prompt. `replay.py` serves those files, so the demo can run with no network.

---

## 9. Checker (`checker.py`)

The checker is independent of the agents and the solver. It reads **ground-truth private state**, not declarations. It returns the overall result plus one result per rule, each with a detail string:

| Rule | Pass condition |
|---|---|
| SEPARATION | slot is one of ATC's free slots and at least `min_separation` minutes from every traffic time |
| FUEL_LIMIT | slot <= land_by |
| GATE_CONFLICT | gate not failed and `slot + Ti >= free_from[gate]`; any gate named `REMOTE-*` always passes |
| CREW_DUTY | `slot + Ti + min_turn <= duty_limit` |
| RESOURCE_CONFLICT | the slot is not held or committed by another event in the ledger, and the gate interval `[slot + Ti, slot + Ti + min_turn)` does not overlap another event's gate booking. The checker reads the ledger directly, never a copy the agents saw |

Add `RESOURCE_CONFLICT` to the `ReasonCode` enum (note it in `DECISIONS.md`). Agents never use it; only the checker does.

Write it as a separate module with no imports from `solver.py`. That separation is a selling point; keep it visible in the code.

---

## 10. Fallback (`fallback.py`)

Deterministic, with no AI involved:

1. Slot: the earliest ATC free slot (first-come-first-served).
2. If that slot is past `land_by`: **DIVERT** to the nearest airport in airports.csv (haversine distance from the destination, excluding the destination and origin). Return the alternate and its distance.
3. Otherwise, gate: the gate with the earliest free time that is free by the gate arrival time, excluding failed gates. If none is free, use `REMOTE-1`.
4. Run the checker on the fallback plan (unless it is a divert) and record the result.

Always compute the fallback plan, even when the agents reach consensus, so `delta_vs_fallback_min` can be reported.

---

## 11. Coordinator (`coordinator.py`)

### 11.1 State machine (from the architecture)

| State | On event | Next | Guard |
|---|---|---|---|
| IDLE | row selected | TRIANGULATED | qualifying rule (4.2) |
| TRIANGULATED | event published | DECLARING | turn = 1 |
| DECLARING | 3 valid packets | SOLVING | schema valid; timeout -> most restrictive default |
| SOLVING | package found | CHECKING | feasible set not empty, else FALLBACK |
| CHECKING | all ACCEPT + checker pass | COMMITTED | |
| CHECKING | any REJECT or checker fail | SOLVING | turn < 3; turn += 1; reason codes become cuts |
| CHECKING | any REJECT or checker fail | FALLBACK | turn = 3 |
| COMMITTED / FALLBACK | logged | AWAITING_SIGNOFF | a human approves before execution |

**Turn semantics.** Turn 1 is declare. Turns 2 and 3 are package votes. So at most two packages get voted on.

**Multi-trigger change.** The coordinator still negotiates exactly one event. It now takes a read-only `LedgerView` and returns its result to the dispatcher (section 18.6). It never writes to the ledger itself. `run(event, stress, agents, ledger_view)` yields log entries as before. When the run ends in `COMMITTED` or a non-divert `FALLBACK`, the dispatcher places a `HELD` booking.

### 11.2 Rules added in the architecture review (implement them)

1. **A reject must cite its own constraint.** A REJECT whose `cites` is not an id in that agent's own packet is converted to ACCEPT, with `objection` logged.
2. **Checker failure counts as a reject.** A package the checker fails can never be committed, even if all three agents accept.
3. **Fuel path.** An empty feasible set (no slot before land_by) goes straight to FALLBACK, which diverts.

### 11.3 Timeout defaults (a missing agent is "most restrictive")

- **ATC missing:** go straight to FALLBACK.
- **Pilot missing:** `LAND_BY = earliest offered slot`, connections ignored.
- **Ground missing:** all gates treated as unavailable except `REMOTE-1`, and `CREW_DUTY_LIMIT = E + Ti + min_turn`.

Log every default applied.

### 11.4 Log (`log.py`)

- `coordinator.run(event, stress, agents)` is a **generator** that yields `LogEntry` objects as they happen. The UI and CLI consume the same generator.
- `LogEntry` fields: `seq, ts, who (coordinator|builder|atc|pilot|ground|solver|checker|human), type (STATE, DisruptionEvent, BROADCAST, ConstraintPacket, AUDIT, ResolutionPackage, SEND, Decision, CheckResult, FallbackPlan, ConsensusRecord, SignOff), state, stage (0..5), turn, text, payload, prev_hash, hash`.
- `text` is a one-or-two-line human summary. For coordinator entries it explains the reasoning: which guard passed and why the state changes. This is what makes the right-hand panel readable. Use the prototype's wording as the model.
- Hash: `sha256(prev_hash + canonical_json({seq, who, type, payload}))`, starting from `GENESIS`. Write one JSONL file per event in `runs/`. `cli verify-log runs/<file>` checks the chain.

---

## 12. Demo scenarios and acceptance behaviour

Run each with `--agents rule` in tests and confirm by eye with `--agents llm`. The exact times depend on the seed; the behaviour must hold.

| Scenario | Stress | Expected behaviour |
|---|---|---|
| A. Normal | none | Consensus in turn 2 or 3; checker passes; gate arrival at or before history for weather cases |
| B. Safety test | corrupt_solver | At least one agent rejects or the checker fails on turn 2; that package is never committed; a later package or the fallback is used |
| C. Low fuel | low_fuel | Feasible set empty after declare; FALLBACK diverts to the nearest alternate with its distance |
| D. Tight crew | tight_crew | Ground rejects `CREW_DUTY` citing `G_S1` at least once; ends in consensus at turn 3 or in FALLBACK |
| E. Multi-trigger | 3+ triggers at one airport within 3 h | Queue ranked by tier, then impact; triggers that share a runway window are clustered; negotiated in rank order; no two hold the same slot or gate; each ImpactAssessment shows its downstream chain |
| F. Pre-emption | E, plus `low_fuel` on the lowest-ranked trigger | That trigger jumps to tier 0 and the front of the queue; the in-progress negotiation finishes its current turn, is logged `PREEMPTED`, and is re-queued |

To pick the demo airport and date for E and F, run `cli triage --scan` (section 18.6). It lists the airport-date windows with the most qualifying triggers. Choose one with 3 to 5.

**Data note.** DL1639 (DCA to ATL, 26 Feb 2015) in the architecture and the prototype was reconstructed from the architecture's worked trace. Use the real row from flights.csv. If the real numbers differ, the demo uses the real ones.

---

## 13. UI (`app.py`, Streamlit)

Copy the layout of `ui-reference.html`. Use `st.set_page_config(layout="wide")` and `left, right = st.columns([1.45, 1])`.

**Header:** title plus stress toggles (Low fuel, Tight crew, Corrupt solver) and an agent mode selector (Rule, LLM, Replay).

**Left (dashboard), top to bottom:**

1. **Disrupted flights:** search box (flight, airport, date) and a list of qualifying rows showing flight, route, date, scheduled and actual times, delay and type chip. Clicking one starts a run.
2. **Event card:** flight, route, tail, scheduled, actual, earliest landing, delay-cause stacked bar, classification chip, and the injected event in a "simulated" callout.
3. **Protocol:** six stage tiles (marked AI or code), current coordinator state, and a turn counter (3 segments).
4. **Declared constraints:** three agent cards with hard limits (lock icon), soft preferences with weights, the "simulated" chip, and the latest vote.
5. **Solver packages:** a table with ID, slot, gate, gate arrival, score, a stacked breakdown bar (throughput, connections, fuel, turnaround) and status (sent, feasible, discarded with reason, cut, tried).
6. **Timeline** (Plotly or Altair): runway lane with other traffic, offered slots, the closure band and the package under vote; three gate lanes showing occupancy and the turnaround block; vertical lines for fuel limit, connection deadline, crew shift end and historical arrival.
7. **Outcome:** CONSENSUS or FALLBACK chip, checker chip, plan, turns used, gate arrival vs history ("x min earlier/later", labelled illustrative), vs the fallback plan, the version stamp, and Approve/Reject buttons that append a `SignOff` entry to the log.

**Right (agent channel):**

- Header showing event_id and a "chain verified" badge.
- Sender filter chips: Coordinator, Scenario builder, ATC, Pilot, Ground, Solver, Checker, Duty manager.
- One `st.chat_message` per log entry, with a per-sender avatar and colour. The header line shows sender, message type, state and turn; the body is the `text`; an expander holds the JSON payload; a footer shows seq and short hash.
- Coordinator reasoning entries look visually distinct (dashed border, or "note" styling).
- Playback: Play/Pause, Step, Skip to end, Replay, speed. Drive it from the generator. In LLM mode, show "<agent> is composing..." while a call is in flight.

Keep all state in `st.session_state`, keyed by event_id. Both panels render from the same log entries, so they can never disagree.

**Multi-trigger additions:**

- **Mode switch** at the top of the left panel: "Single flight" (as above) or "Airport window" (pick an airport, a date and a window length, or load a list of trigger payloads).
- **Trigger queue** (left, above the event card in window mode): one row per trigger with rank, flight, tier chip (`SAFETY` or `NORMAL`), an impact score bar split by component (knock-on minutes, connections at risk, late-night legs, flights affected), cluster tag, decision deadline, and status (queued, negotiating, awaiting sign-off, approved, preempted). Clicking a row opens that event's dashboard and channel.
- **Impact graph** (left, below the queue): the selected trigger's downstream subgraph, drawn with `st.graphviz_chart` (deterministic layout, left to right). Rotation edges are solid and labelled with slack and propagated delay. Connection edges are dashed and labelled with passengers at risk. Nodes are coloured by propagated delay, and the trigger node is outlined.
- **Booking ledger** (left, collapsible): a table of held and committed slot and gate bookings by event.
- **Orchestrator thread** (right): in window mode the channel opens on the dispatcher's own messages (TRIAGE, ImpactAssessment, TriggerQueue, CLUSTER, DISPATCH, LEDGER, PREEMPT) under the sender "Orchestrator", with its own colour and filter chip. Each DISPATCH message links into that event's negotiation thread.

---

## 14. Tests (`tests/`), all offline with rule agents

1. **Parsing:** `"0648"` -> 408, `"2400"` -> 0, blank causes -> 0.
2. **Qualifying filter and classification** (WEATHER, KNOCK_ON, COMBINED, and edge values 15/16 and 20/21).
3. **Earliest landing E** for a hand-made row.
4. **Determinism:** same event and seed give identical private state and an identical log hash chain.
5. **Checker:** one passing and one failing case per rule, including the boundaries (exactly 3 min separation passes; slot exactly at land_by passes).
6. **Solver** never returns a package that breaks a declared hard limit (property test over many seeds).
7. **Solver isolation:** solver results are identical when the private state passed alongside is replaced with garbage (proves it only reads declarations).
8. **Safety invariant:** across all sample events and all stress combinations, nothing is committed unless `checker_pass` is true.
9. **Termination:** every run ends in AWAITING_SIGNOFF within 3 turns, including with agents that always reject, return malformed output, or time out.
10. **Reject without a valid cite** is converted to ACCEPT with an objection.
11. **Fallback:** always returns a plan; diverts when the earliest slot is past land_by; never picks a failed gate.
12. **Log chain:** verifies; tampering with one entry breaks verification.
13. **Trigger adapter:** each accepted payload shape (including the provided sample) produces a valid DisruptionEvent; a non-qualifying payload is rejected.
14. **Scenarios A to F** from section 12.

**Triage and graph tests (Fable's workstream):**

15. **Rotation edges** are created only when the tail matches, the date matches and this flight's destination equals the next flight's origin. Tails that are blank or `UNKNOWN` get no rotation edges.
16. **Slack and propagation** on a hand-built 4-leg chain match the formula in 18.4, including negative slack clamped to 0 and stopping at zero delay.
17. **Connection edges** respect the 35 to 120 min window and the fan-out cap.
18. **Impact is deterministic:** the same data, triggers and policy give an identical score, breakdown and queue order.
19. **Priority ordering:** a tier 0 trigger always outranks any tier 1 trigger regardless of impact; ties are broken by decision deadline, then event_id.
20. **Clustering:** triggers with overlapping runway windows at the same airport, or linked by a rotation or connection path, share a cluster; unrelated triggers do not.
21. **No double booking (property test):** across random multi-trigger sets, no slot or overlapping gate interval is ever held by two events, and the checker fails any package that would create one.
22. **Ledger lifecycle:** HELD on commit or fallback, COMMITTED on approve, RELEASED on reject, and a released slot becomes available to the next event.
23. **Pre-emption:** scenario F re-orders the queue and re-queues the interrupted event without losing its log.
24. **Back-test** runs on a fixture day and returns its metrics without error.

Optional if time allows: mutation testing on `checker.py` with mutmut.

---

## 15. Build order and timebox (3 hours)

The two workstreams run in parallel. Agree sections 5 and 18.3 in the first 15 minutes, then build against them.

| Time | Claude Code | Fable | Shared output |
|---|---|---|---|
| 0:00 to 0:15 | models.py and config.py, including the 18.3 contracts | Review the contracts; stub every interface | Frozen contracts |
| 0:15 to 0:45 | data.py, triangulate.py, trigger.py with tests 1 to 3 and 13 | world.py and ledger.py with tests 21 and 22 | Events from real rows; shared world |
| 0:45 to 1:20 | simulate.py (reading world and ledger), rule agents, solver, checker incl. RESOURCE_CONFLICT, fallback; tests 4 to 7 and 11 | graph.py and triage.py with tests 15 to 20 | Deterministic core and impact graph |
| 1:20 to 1:45 | coordinator and log, cli run; tests 8 to 10, 12, 14 (A to D) | dispatcher.py, cli triage and backtest; tests 23 and 24; scenarios E and F | **MVP floor: single and multi-trigger runs with no AI** |
| 1:45 to 2:25 | app.py: both modes, queue, graph view, ledger, orchestrator thread | Back-test numbers for the pitch; scan for the demo airport-date | Demo-able UI |
| 2:25 to 2:45 | llm.py and replay.py; record scenarios A, B and E | Support integration; fix bugs | Real agents, recorded runs |
| 2:45 to 3:00 | Freeze, README, rehearse | Freeze, README, rehearse | Ship |

**Cut line if behind at 1:45:** drop connection edges (keep rotation edges, which are real data), drop pre-emption (scenario F), and demo the LLM agents only from a recording. Never cut the ledger or the checker's `RESOURCE_CONFLICT` rule: without them, multi-trigger runs can double-book.

## 16. Out of scope

Joint negotiation of a whole cluster as one multi-flight event (clusters are negotiated one event at a time against the ledger instead), hub-wide optimisation, multi-day rotations, real crew rosters, a live map, an event bus, message signing, a database server, authentication, React. Mention these in the pitch as next steps; do not build them.

## 17. Open decisions (defaults to use if nobody answers)

| Question | Default |
|---|---|
| Model for agents | `ACACIA_MODEL` env; a fast current Claude model |
| Scoring weights | policy-0.1 above |
| Rules of thumb (45 min US domestic fuel reserve, 35 to 60 min narrow-body turnaround) | As given, labelled "to verify" in the UI |
| Divert alternates | Nearest airport by distance, no runway-length check |
| Time zones | Destination-local only (section 4.2) |
| Impact weights | `impact-0.1` in 18.4 |
| Cluster runway window | 60 min either side of earliest landing |
| Dispatch order across clusters | Sequential by best rank in each cluster (parallel is a stretch goal) |
| Connection passenger counts | Simulated per edge, 2 to 15, labelled "simulated" |

---

## 18. Multi-trigger triage and impact graph [Fable]

This section lets the system take several disruptions at once, decide which matters most, and negotiate them without double-booking. Everything here is deterministic: no LLM is involved in triage, ranking, clustering or booking.

### 18.1 Impact graph (`graph.py`)

Build one directed graph per date from **all** rows of that date (not only qualifying rows), so knock-on effects reach flights that were not themselves disrupted. Use plain Python dicts, or `networkx` if available.

**Node:** one per flight row. Id `"{AL}{FN}-{date}-{O}{D}-{sched_dep}"`. Attributes: airline, flight_no, tail, origin, dest, sched_dep, sched_arr, distance, and the actual delays (arrival delay, departure delay, late-aircraft delay) for back-testing.

**Edges:**

| Type | Rule | Attributes |
|---|---|---|
| `ROTATION` | Same tail (not blank, not `UNKNOWN`), same date, `a.dest == b.origin`, and b is the next departure of that tail after a's scheduled arrival | `slack = max(0, b.sched_dep - a.sched_arr - min_turn(a.distance))` (both times are local to the same airport) |
| `CONNECTION` | Same airline, `a.dest == b.origin`, `35 <= b.sched_dep - a.sched_arr <= 120`. Keep at most the 5 outbound flights with the smallest gap per inbound flight | `gap`, `mct = 35`, `pax = rand(2..15)` (seeded by both node ids; simulated) |
| `RESOURCE` | Not stored as edges. Computed on demand between triggers in 18.4 (shared runway window or gate pool) | |

`min_turn(distance)` is the same function as section 6. Rotations do not cross midnight in the MVP (the data is dated by departure day); note this limit in `DECISIONS.md`.

Expose a `graph_version` string: a hash of the date, row count and `impact-0.1`.

### 18.2 Shared airport world (`world.py`)

`AirportWorld(airport, date)` is seeded by `f"{SALT}|world|{airport}|{date}"` and built once per airport and date:

- **Runway grid:** grid points across the day, spaced 4 min, or 8 min inside any runway closure. Closures are the union of every `RUNWAY_CLOSED` injected event at that airport that day. Each grid point is background traffic with probability 0.55, else free capacity.
- **Gate pool:** 8 gates, named with the letter rule from section 6 and numbers 1 to 30 drawn without replacement. Each has a background `busy` list of intervals (2 to 4 per gate, 30 to 70 min each, seeded).
- **Equipment failures:** an `EQUIPMENT_FAILURE` event takes one gate (seeded choice) out for its interval.

Per-event private state (section 6) then reads from the world:

- **ATC slots:** the first 3 free grid points (4 without a closure) at or after `first` that are not `HELD` or `COMMITTED` in the ledger. Demand follows section 6.
- **Ground gates:** 3 gates from the pool, picked as the assigned gate (seeded) plus the two with the earliest free time at `E + Ti`. Each gate's `free_from` accounts for both background busy intervals and ledger bookings.

### 18.3 Contracts (add to `models.py`)

```python
class DownstreamImpact(BaseModel):
    node_id: str; edge_type: Literal["ROTATION","CONNECTION"]
    depth: int; slack_min: int | None; gap_min: int | None
    propagated_delay_min: int; pax_at_risk: int = 0; late_night: bool = False

class ImpactAssessment(BaseModel):
    event_id: str; tier: Literal[0, 1]; tier_reason: str
    impact_score: float
    breakdown: dict[str, float]      # knock_on_min, pax_connections_at_risk, late_night_legs, flights_affected
    downstream: list[DownstreamImpact]
    input_delay_min: int             # the do-nothing delay used for propagation
    decision_deadline: str           # "HH:MM", the earliest landing
    cluster_id: str; graph_version: str; policy_version: str

class QueueItem(BaseModel):
    rank: int; event_id: str; flight_no: str; tier: int; impact_score: float
    cluster_id: str; decision_deadline: str
    status: Literal["QUEUED","NEGOTIATING","AWAITING_SIGNOFF","APPROVED","REJECTED","PREEMPTED"]

class TriggerQueue(BaseModel):
    queue_id: str; airport: str | None; date: str; window: str
    items: list[QueueItem]; graph_version: str; policy_version: str

class Cluster(BaseModel):
    cluster_id: str; event_ids: list[str]    # in priority order
    reasons: list[str]                       # e.g. "shared runway window ATL 12:00-13:30", "rotation N123DL"

class Reservation(BaseModel):
    res_id: str; resource_type: Literal["RUNWAY_SLOT","GATE"]; airport: str
    resource_id: str                         # "RWY" or a gate id
    start: str; end: str                     # slot: start == end
    event_id: str; status: Literal["HELD","COMMITTED","RELEASED"]
```

`ConsensusRecord` gains an optional `impact: ImpactAssessment | None` and `reservations: list[str]` (res_ids). Record both in `DECISIONS.md`.

### 18.4 Triage rules (`triage.py`)

**Input delay** (do-nothing case) = the trigger's historical arrival delay from the data. It is real, and it represents what happened without this system.

**Knock-on propagation** along `ROTATION` edges, breadth-first from the trigger node:

```
d(trigger) = input_delay
d(next)    = max(0, d(current) - slack(current -> next))
stop when d = 0 or depth > 8
```

**Connections:** for every `CONNECTION` edge leaving a node with propagated delay `d`, the connection breaks if `d > gap - mct`. Add its `pax` to passengers at risk. Connections do not propagate further.

**Late-night legs** (proxy for crew duty and curfew risk): downstream flights whose propagated scheduled departure (`sched_dep + d`) is later than 23:00 local.

**Impact score** (policy `impact-0.1`, in `config.py`):

```
impact = 1.0  * knock_on_min            (sum of propagated delay over downstream flights, excluding the trigger)
       + 3.0  * pax_connections_at_risk
       + 60.0 * late_night_legs
       + 10.0 * flights_affected        (downstream flights with d > 0)
```

**Safety tier:**

- **Tier 0** if the simulated `land_by - first <= 10` min (the fuel margin to the first available slot), or the trigger carries `low_fuel`, or the payload declares an emergency.
- **Tier 1** otherwise.

`tier_reason` states which rule fired.

**Priority order** (lexicographic, fully deterministic): `(tier asc, impact_score desc, decision_deadline asc, event_id asc)`. A tier 0 trigger is never ranked below a tier 1 trigger, whatever the scores. Safety is a gate, not a weight.

**Clusters:** union-find over triggers. Join two triggers if any of these hold:

- same destination, and their earliest landings are within 60 min of each other (shared runway window and gate pool);
- one is downstream of the other in the rotation graph;
- they share a connection edge.

Cluster id = `CL-` + a hash of the sorted event_ids.

### 18.5 Booking ledger (`ledger.py`)

- In-memory, with append-only JSONL persistence to `runs/ledger-{date}.jsonl`. Only the dispatcher writes to it.
- `hold(event_id, slot, gate, airport, min_turn)` creates two reservations: `RUNWAY_SLOT` at the slot time, and `GATE` for `[slot + Ti, slot + Ti + min_turn)`. `commit(event_id)` and `release(event_id)` change status.
- Lifecycle: `HELD` when an event ends `COMMITTED` or in a non-divert `FALLBACK`; `COMMITTED` on human approve; `RELEASED` on reject or pre-emption.
- `LedgerView` is a read-only snapshot with `slot_taken(airport, t)` and `gate_busy(airport, gate, start, end)`. The world, the simulators and the checker query it. The checker queries the live ledger at check time, not the snapshot the agents saw, so a booking made in between is still caught.

### 18.6 Dispatcher (`dispatcher.py`), the orchestrator above the coordinator

States: `COLLECTING -> TRIAGING -> CLUSTERING -> DISPATCHING -> (per event: coordinator run) -> DISPATCHING ... -> DRAINED`.

1. **COLLECTING:** accept triggers from a list of payloads, or from `scan(airport, date, window)`, which turns every qualifying row in that window into a trigger. In live mode, collect for a short debounce window (default 60 s) before triaging.
2. **TRIAGING:** build or refresh the graph for the date, triangulate each trigger, compute an `ImpactAssessment` for each, and emit them.
3. **CLUSTERING:** form clusters and emit the `TriggerQueue`.
4. **DISPATCHING:** take clusters in order of their best-ranked member. Inside a cluster, run the coordinator for each event in rank order, each time with a fresh `LedgerView`. After each run, hold its bookings and emit a `LEDGER` entry. Clusters run one after another in the MVP.
5. **Pre-emption:** if a new or updated trigger becomes tier 0 while another event is negotiating, let the current turn finish, mark that event `PREEMPTED`, release any holds it has, put it back in the queue, and dispatch the tier 0 event next.
6. **DRAINED:** every trigger is awaiting sign-off, approved, rejected or diverted. Emit a summary: total knock-on minutes avoided against the do-nothing case, consensus rate, and how many fallbacks and diverts there were.

The dispatcher writes its own hash-chained log (`who = "orchestrator"`; types `TRIAGE`, `ImpactAssessment`, `TriggerQueue`, `CLUSTER`, `DISPATCH`, `LEDGER`, `PREEMPT`, `SUMMARY`). Each event's log records the dispatcher entry that started it (`dispatched_by` = dispatcher seq and hash), so the two chains link up. Every dispatcher `text` explains its reasoning in one or two lines, for example: "Ranked DL1639 first: tier 1, impact 412 (knock-on 236 min across 3 legs, 38 passengers at risk). Clustered with AA22: both land at ATL within 14 min."

`cli triage --scan --date <d>` lists airport-date windows by number of qualifying triggers, for picking demo scenarios E and F.

### 18.7 Back-test (`cli backtest`)

The data already contains the answer for knock-on delay, which gives the pitch real evidence:

- For every qualifying trigger on the date, compare the predicted propagated delay on the **next rotation leg** with that leg's actual `LATE_AIRCRAFT_DELAY` (and with its departure delay). Report the mean absolute error, the median, and the share predicted within 15 minutes.
- **Ranking quality:** Spearman rank correlation between each trigger's predicted `knock_on_min` and the actual sum of `LATE_AIRCRAFT_DELAY` over its downstream rotation legs.
- Write the results to `runs/backtest-{date}.json` and show them on the pitch slide. Report whatever the numbers are; do not tune the formula against the same date you report on (fit on one date, report on another).
