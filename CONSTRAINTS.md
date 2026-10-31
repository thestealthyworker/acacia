# CONSTRAINTS.md: challenge operational constraints (highest precedence for numbers)

Source of truth: `docs/challenge2_operational_constraints.xlsx` (tag **[CHALLENGE]**), exported to `data/challenge/*.csv`:
`airport_capacity.csv`, `wake_separation.csv`, `fuel_and_crew.csv`, `rules_of_play.csv`.

Precedence: this file wins over HANDOVER.md, FABLE_ADDENDUM.md and PLAN.md on every number and limit it covers. The rules of play state "any limit you change must be stated in your demo", so **every deviation is listed in the UI** (section 8).

Status tags used below and in the registry:
- **AS_GIVEN**: workbook value used unchanged.
- **ASSUMED**: the workbook is silent or ambiguous; our stated assumption.
- **CHANGED**: we use a different value than the workbook; reason given.
- **NOT_MODELLED**: workbook rule that does not apply to this build; reason given.

---

## 1. Loader: `acacia/constraints.py` (new, single access point)

Loads the four CSVs once (cached) from `config.DATA_DIR / "challenge"`, and exposes:

- `airport_limits(iata) -> AirportLimits` with `size_class, active_runways, gates, max_ops_per_hour, min_turnaround_min, spare_gates`. Unknown airport: use the "Non-hub" row values and mark it ASSUMED.
- `arrival_rate(iata, storm: "none"|"moderate"|"severe") -> float` arrivals per hour.
- `storm_level(weather_delay_min) -> "none"|"moderate"|"severe"`.
- `wake_class(distance_miles) -> "Heavy"|"Large"`, `wake_nm(leader, follower) -> int`, `wake_minutes(leader, follower) -> float`.
- `fuel_params()`, `crew_params()`: the values in section 4 and 5.
- `REGISTRY: list[ConstraintItem]` (`id, sheet, rule, workbook_value, used_value, status, note`), the list of every constraint and deviation in section 8. The UI and `cli constraints` render it.
- `CONSTRAINTS_VERSION`: sha256 of the four CSV files, first 12 chars. It goes into every `ConsensusRecord.stamp`.

No other module reads the CSVs or hard-codes these numbers.

---

## 2. Runway: capacity and wake separation (ATC agent, world, checker)

**Capacity [CHALLENGE airport_capacity].** ATL: 90 runway movements per hour.
- Arrival rate `R = max_ops_per_hour x 0.5`. ASSUMED: 50% of movements are arrivals. ATL nominal 45/hr.
- Weather: during a storm window, `R x 0.6` (moderate) or `R x 0.4` (severe). ASSUMED: severe when `WEATHER_DELAY > 45` min, else moderate. ATL: 27/hr moderate, 18/hr severe.
- This **replaces** the kit's "4 min grid, 8 min inside a runway closure". A WEATHER trigger's injected event keeps `type = RUNWAY_CLOSED` (no contract change) with `ref = "storm (moderate): arrival capacity x0.6"` or the severe wording, and the same start and end rule as HANDOVER 4.3. The world treats a `RUNWAY_CLOSED` interval as a storm window with that factor.

**Runway grid (world.py).** One merged arrival stream per airport and date. Grid points start at 00:00 and step `60 / R(t)` minutes, accumulated as a float. Each point is rounded to the nearest whole minute, and duplicates are dropped. Each point is background traffic with probability 0.55 (kit value, unchanged). Each traffic point gets a seeded wake class: Heavy 15%, Large 80%, Small 5%. ASSUMED: there is no Super (A380) traffic in 2015 US domestic data.

**Wake separation [CHALLENGE wake_separation].** Arrival separation in nm comes from the workbook table (leader, then follower; "any other pair" = 3 nm). Convert at an ASSUMED 140 kt approach speed: `minutes = nm x 60 / 140`, so 3 nm ≈ 1.3 min, 4 ≈ 1.7, 5 ≈ 2.1, 6 ≈ 2.6, 7 ≈ 3.0, 8 ≈ 3.4.
- **Own aircraft class.** ASSUMED: `Heavy` if `DISTANCE > 2000` miles, else `Large`, because flights.csv has no aircraft type.
- **A slot is separated** when `slot - prev >= wake_minutes(prev_class, own_class)` and `next - slot >= wake_minutes(own_class, next_class)`, where prev and next are the nearest traffic points or other events' held/committed slots in the ledger. Comparisons use float minutes against integer slot times.
- The wake rules apply across the merged stream, not per runway. This is conservative, because real wake separation applies on the same runway only. State it as a deviation.
- **This replaces the kit's flat `MIN_SEPARATION = 3` min.**
- ATC declares only separated free slots. `free_slots_after(t, n, view, own_class="Large")` returns only such slots. The checker's SEPARATION rule re-verifies with the same table, reading ground truth (the world's traffic plus the live ledger).
- `AtcPrivate` gains `own_class: str` and `traffic_classes: dict[int, str]` (ADDED; DECISIONS line). ATC's hard limit `A_H2` becomes `WAKE_SEPARATION` with value `{"table": "workbook", "own_class": ..., "min_minutes": {...}}`.
- The workbook's `min_time_s` column (same-runway **departures**) is NOT_MODELLED, because this build sequences arrivals only.

---

## 3. Turnaround and gates (Ground agent, graph, checker)

- **Minimum turnaround [CHALLENGE].** `min_turn = airport_limits(dest).min_turnaround_min`: ATL 45 (Large hub 45, others 35). This **replaces** the distance-based rule `60/45/35` everywhere: simulate, solver inputs (Ground declares `facts_owned["min_turn_min"]`), checker, fallback, ledger gate intervals, and **graph slack**. Graph slack uses the turnaround at the connecting station (`a.dest`).
  - Fixture update (FABLE_ADDENDUM 10): DCA 35, ATL 45, MCO 35, so the slacks are L1→L2 = 15, L2→L3 = 5 and L3→L4 = 10, and the propagated delays from 60 are L2 = 45, L3 = 40 and L4 = 30. That gives `knock_on_min = 115` and `flights_affected = 3`. Connection L2→C1 still breaks. With input 10, L2 = 0.
- **Gate options [CHALLENGE spare_gates_at_any_time].** Ground offers the assigned gate plus up to `spare_gates` other gates (ATL: 3 spare, so 4 options). They are picked from the world's gate pool by earliest `gate_free_from(..., duration=min_turn)`. The world keeps a modelled pool of 8 gates per airport. That is a subset of the workbook's `gates` (ATL 150): ASSUMED, stated as a deviation.
- **Gate-change notice (HARD) [CHALLENGE fuel_and_crew].** "At least 20 minutes before arrival." If the package's gate differs from the assigned gate, then `gate_arrival - decision_time >= 20`, where `gate_arrival = slot + taxi_in`.
  - `DisruptionEvent.decision_time` ("HH:MM", ADDED). The trigger payload may set it. Default: the start of the earliest injected event, or `E - 30` if there is none. ASSUMED: the negotiation runs when the disruption is first known.
  - Ground declares hard limit `G_H3 GATE_CHANGE_NOTICE` with value `{"min_notice_min": 20, "decision_time": "HH:MM", "assigned_gate": ...}`.
  - The solver discards a violating package with reason `GATE_CHANGE_NOTICE`.
  - Add `ReasonCode.GATE_CHANGE_NOTICE` (ADDED). Ground may REJECT with it, citing `G_H3`.
  - New checker rule `GATE_CHANGE_NOTICE`. A `REMOTE-*` stand counts as a gate change too, so it needs the same notice.

---

## 4. Fuel (Pilot agent) [CHALLENGE fuel_and_crew]

- **Final reserve:** 30 min of holding fuel. The plan never goes below it (AS_GIVEN).
- **Spare fuel on arrival:** 45 min of holding. ASSUMED interpretation: the 45 min of holding is **above** the 30 min final reserve.
- **Contingency:** 5% of trip fuel, expressed as minutes of flying. ASSUMED: `contingency_min = round(0.05 x AIR_TIME)`; if `AIR_TIME` is blank, use `SCHEDULED_TIME - 20`. Add `DisruptionEvent.air_time` (ADDED).
- **`land_by = E + 45 + contingency_min`.** This replaces the kit's `E + rand(45..74)`, so it is now deterministic per flight. The `low_fuel` stress is unchanged: `land_by = first - 3`.
- **Holding burn:** narrowbody 2,400 kg/h, widebody 6,500 kg/h. Widebody when the wake class is Heavy (ASSUMED, same distance rule). For every package compute `holding_min = max(0, slot - E)` and `holding_fuel_kg = holding_min x burn / 60`. Report it in `score_breakdown` (as information, not a utility), in the solver table, in the outcome, and in `ConsensusRecord`. This is the brief's "excessive fuel consumption" made visible.
- **Fuel utility:** `clamp((land_by - slot) / (land_by - E), 0, 1)`. This replaces the kit's `/ 45`.
- **Pilot hard limits:** `P_H1 LAND_BY` (unchanged meaning) and `P_H2 FLIGHT_CREW_DUTY_END`. Flight crew maximum duty is 13 h from report (AS_GIVEN, simplified FAA Part 117). ASSUMED: report time = `SCHEDULED_DEPARTURE - 60` of this leg, so the limit is `slot + taxi_in <= report + 780`. The solver discards with `CREW_DUTY`, and the checker's CREW_DUTY rule also verifies it. Note that it rarely binds.

---

## 5. Ground crew [CHALLENGE fuel_and_crew]

- **Shift:** 8 h (AS_GIVEN). `shift_end` follows HANDOVER 6 (seeded, plus the `tight_crew` variant), and `shift_start = shift_end - 480` is shown.
- **Overtime:** costs 1.5x (AS_GIVEN). In the solver, `crew = max(0, 1 - 1.5 x overtime / 60)`. Report `overtime_cost_min = 1.5 x overtime` in `score_breakdown` (information).
- **Hard duty limit** `duty_limit = shift_end + 45`: ASSUMED maximum overtime. The kit's private red line (reject when overtime > 20 min) stays ASSUMED.

---

## 6. Rules of play [CHALLENGE rules_of_play]

- **Input:** one flights.csv row with `WEATHER_DELAY > 15` or `LATE_AIRCRAFT_DELAY > 20` (AS_GIVEN, matches HANDOVER 4.2). Our extra midnight window (PLAN 2.A.3) is a deviation: list it.
- **Turn:** one turn = each of the three agents sends one structured message, and consensus must come within 3 turns. Matches PLAN 3.1: turn 1 = three ConstraintPackets, turns 2 and 3 = three Decisions each.
- **No consensus after 3 turns:** the deterministic fallback decides, and the log states **why**. The FallbackPlan and the coordinator's FALLBACK entry carry a `reason` string, for example "turn 3: Ground REJECT CREW_DUTY citing G_S1", "feasible set empty: no separated slot before LAND_BY 13:05", or "ATC timeout".
- **Output:** a revised arrival time, gate, or flight plan (a divert counts as a flight plan), plus a consensus log showing each turn. ConsensusRecord and the UI outcome show all three explicitly.

---

## 7. What does not change

Contracts other than the ADDED fields above; the state machine; turn limits; checker independence (no solver import); MCT 35 min for connections (kit value; the workbook has none, so ASSUMED); impact policy `impact-0.1`; scoring policy weights `policy-0.1`.

---

## 8. Deviations registry (render in the UI and in `cli constraints`)

`acacia/constraints.py` holds `REGISTRY`, at minimum these items:

| id | Rule | Workbook | Used | Status |
|---|---|---|---|---|
| CAP-1 | Runway movements/hr | per airport (ATL 90) | as given | AS_GIVEN |
| CAP-2 | Arrival share | n/a | 50% | ASSUMED |
| CAP-3 | Storm factor | n/a | x0.6 moderate / x0.4 severe; severe if WEATHER_DELAY > 45 | ASSUMED |
| CAP-4 | Gates modelled | ATL 150 | pool of 8 per airport | CHANGED (tractable model) |
| CAP-5 | Spare gates | per airport (ATL 3) | as given, Ground options = assigned + spare | AS_GIVEN |
| CAP-6 | Min turnaround | per airport (ATL 45) | as given, everywhere incl. graph slack | AS_GIVEN |
| CAP-7 | Background runway load | n/a | 55% of capacity points occupied | ASSUMED |
| WAKE-1 | Wake separation table | nm by pair | as given | AS_GIVEN |
| WAKE-2 | nm to minutes | n/a | 140 kt (3 nm ≈ 1.3 min) | ASSUMED |
| WAKE-3 | Aircraft class | n/a | Heavy if distance > 2000 mi else Large; traffic 15/80/5 Heavy/Large/Small | ASSUMED |
| WAKE-4 | Same-runway scope | per runway | applied across the merged arrival stream | CHANGED (conservative) |
| WAKE-5 | Super (A380) rows | 6–8 nm | no A380 in data | NOT_MODELLED |
| WAKE-6 | Departure min_time_s | 60–180 s | arrivals only | NOT_MODELLED |
| FUEL-1 | Final reserve | 30 min | never planned below | AS_GIVEN |
| FUEL-2 | Spare on arrival | 45 min | 45 min above final reserve | ASSUMED (interpretation) |
| FUEL-3 | Contingency | 5% trip fuel | 5% of AIR_TIME as minutes | ASSUMED |
| FUEL-4 | Holding burn | 2,400 / 6,500 kg/h | as given; widebody = Heavy class | AS_GIVEN |
| CREW-1 | Flight crew max duty | 13 h from report | report = sched dep − 60 of this leg | ASSUMED |
| CREW-2 | Ground shift | 8 h, overtime 1.5x | as given | AS_GIVEN |
| CREW-3 | Ground max overtime | n/a | 45 min hard; reject over 20 min (red line) | ASSUMED |
| GATE-1 | Gate-change notice | ≥ 20 min before arrival | HARD; decision time = disruption start or E − 30 | AS_GIVEN + ASSUMED decision time |
| PLAY-1 | Qualifying input | W > 15 or L > 20 | as given, plus earliest landing 01:00–21:30 | CHANGED (no midnight crossing in MVP) |
| PLAY-2 | Turn and 3-turn limit | as given | turn 1 declare, turns 2–3 vote | AS_GIVEN |
| PLAY-3 | Fallback logs why | as given | reason string in FallbackPlan and log | AS_GIVEN |
| KIT-1 | Private state | n/a | slots, pax, gates and crew shift simulated with a fixed seed | ASSUMED (labelled simulated) |
| KIT-2 | MCT | n/a | 35 min | ASSUMED |

Add a row for any further deviation introduced while building.
