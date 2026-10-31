# Acacia: multi-agent disruption negotiation (Statement 2)

When a flight is disrupted (weather, or a late inbound aircraft), three agents negotiate a recovery plan: **ATC** (runway slots and wake separation), **Pilot** (fuel and flight crew duty) and **Ground** (gates, turnaround and ground crew). They argue in structured messages; deterministic code decides. A solver picks only from what the agents declared, an independent checker re-verifies the plan against ground truth, the run always ends within 3 turns (or a deterministic fallback decides and logs why), and nothing executes until a human signs off.

Above that, an **orchestrator** handles many triggers at one airport: it ranks them by safety tier and downstream impact (knock-on delay through aircraft rotations and broken passenger connections), clusters the ones that compete for the same runway window, negotiates them in rank order, and keeps a booking ledger so no two events ever hold the same slot or gate.

All numbers and limits come from the challenge workbook `challenge2_operational_constraints.xlsx`. Every value we did not use as given is listed in the UI ("Constraints & deviations") and in `python -m acacia.cli constraints`. See [Challenge constraints](#challenge-constraints).

## How it meets the brief

Mapping of `docs/architecture.html` section 7 to the code:

| Requirement or "avoid" item | Where it is handled in code |
|---|---|
| Three independent domains, not one prompt | `acacia/agents/` (`rule.py`, `llm.py`, `replay.py`), one prompt each in `acacia/agents/prompts/`; private state per role from `acacia/simulate.py`; agents only see the coordinator's bus |
| No endless loops | `acacia/coordinator.py` turn counter (`config.MAX_TURNS = 3`); turn 3 forces commit or fallback |
| No disregard of safety margins | Hard limits filtered in `acacia/solver.py`, re-checked by `acacia/checker.py` (imports nothing from the solver, reads the live ledger); an LLM never sets a time or commits |
| Structured messages and explicit constraint checking | Pydantic contracts in `acacia/models.py`, fixed reason codes (`ReasonCode`), checker rules per constraint |
| Traceable consensus log | Hash-chained append-only log `acacia/log.py`, `ConsensusRecord`, `cli verify-log` |
| Deterministic non-AI fallback | `acacia/fallback.py`: first-come-first-served slot, earliest feasible gate, divert if the fuel limit is breached; reason string in the log |
| Resolution in under 3 turns | Turn 1 declare, turns 2 and 3 vote; the happy path commits at turn 2 |
| Many triggers at once (our addition) | `acacia/graph.py`, `triage.py`, `world.py`, `ledger.py`, `dispatcher.py` |

## Architecture

```
 flights.csv row / trigger payload
            │
            ▼
 trigger.py ─► triangulate.py ─► DisruptionEvent (+ simulated injected event)
            │
            ├──── single flight ──────────────────────────────────────────────┐
            │                                                                  │
            ▼ airport window                                                   │
 dispatcher.py (orchestrator, own hash chain)                                  │
   COLLECTING ─► TRIAGING (graph.py impact graph, triage.py tier + impact)     │
   ─► CLUSTERING (shared runway window / rotation / connection)                │
   ─► DISPATCHING each event in rank order, fresh ledger view ──────────────┐  │
   ─► DRAINED (summary)            ▲ ledger.py: HELD / COMMITTED / RELEASED │  │
                                   │                                        ▼  ▼
                                   │      coordinator.py (state machine, ≤ 3 turns)
                                   │        turn 1: ATC, Pilot, Ground declare ConstraintPackets
                                   │                (simulate.py private state, world.py runway + gates)
                                   │        solver.py: ranks packages from declarations only
                                   │        turns 2-3: agents vote ACCEPT / REJECT (cite a hard limit)
                                   │        checker.py: re-verifies against ground truth + live ledger
                                   │        fallback.py: deterministic plan if no consensus
                                   └─────── ConsensusRecord (AWAITING_SIGNOFF) ─► human sign-off
                                                                                 (signoff.py)
 Every limit: acacia/constraints.py (workbook CSVs in data/challenge/, REGISTRY of deviations)
 UI: app.py + ui/ (Streamlit), CLI: acacia/cli.py + acacia/cli_triage.py
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then set ANTHROPIC_API_KEY and ACACIA_MODEL for live LLM runs
```

Data files:

- `data/airlines.csv`, `data/sample_flights.csv`, `data/sample_airports.csv` and the workbook export `data/challenge/*.csv` are included.
- Add the full `data/flights.csv` and `data/airports.csv` (2015 US DOT on-time data) for real multi-trigger days. Never commit `flights.csv` (it is in `.gitignore`). Without it, the loaders fall back to the sample files, and the UI's airport window uses the synthetic fixture day.
- Optional: `docs/trigger-payload.sample.json` (the real trigger payload sample; the adapter test runs once it is present).

## Commands

Every command below is from `CLAUDE.md`. "Offline" means it needs no API key.

| Command | What it shows | Offline |
|---|---|---|
| `pip install -r requirements.txt` | Installs pandas, pydantic, anthropic, streamlit, graphviz, pytest, hypothesis, python-dotenv | needs network |
| `pytest -q` | The full test suite (HANDOVER 14 tests 1 to 24, scenarios A to F, UI smoke tests) with rule agents | yes |
| `python -m acacia.cli run --flight DL1639 --date 2015-02-26 --agents rule` | One negotiation with the deterministic rule agents: every log entry, then the `ConsensusRecord`; writes `runs/<event_id>.jsonl` | yes |
| `python -m acacia.cli run --flight DL1639 --date 2015-02-26 --agents llm --record` | The same negotiation with live LLM agents; records every response under `recordings/<event_id>/` | no (API key) |
| `python -m acacia.cli run --flight DL1639 --date 2015-02-26 --agents replay` | Replays recorded LLM answers offline. Without recordings each agent counts as timed out (most restrictive default) and the fallback decides | yes |
| `python -m acacia.cli triage --scan --date 2015-02-26` | Airport windows ranked by number of qualifying triggers, for picking scenarios E and F | yes |
| `python -m acacia.cli triage --airport ATL --date 2015-02-26 --window 180` | Orchestrator run: triage, clusters, queue, every negotiation, ledger holds; writes `runs/triage-ATL-<date>/` | yes |
| `python -m acacia.cli backtest --date 2015-02-26` | Predicted knock-on delay vs the data's actual `LATE_AIRCRAFT_DELAY` (MAE, share within 15 min, Spearman); writes `runs/backtest-<date>.json` | yes |
| `python -m acacia.cli verify-log runs/<file>.jsonl` | Re-verifies a log's hash chain; any edited entry breaks it | yes |
| `streamlit run app.py` | The dashboard and agent channel (Single flight and Airport window modes) | yes (Rule/Replay) |

Extra commands: `python -m acacia.cli constraints` (the workbook values and every deviation), `python -m acacia.cli batch --date 2015-02-26 --limit 5`, `python -m acacia.cli sign-off runs/<event_id>.jsonl --approve --who "duty manager"`. To run the orchestrator on the synthetic multi-trigger day: `python -m acacia.cli triage --airport ATL --date 2015-03-12 --window 180 --flights tests/fixtures/multi_trigger_day.csv` (and the same `--flights` for `backtest --date 2015-03-12`).

## Demo script (scenarios A to F)

Start `streamlit run app.py`. Leave the agent mode on **Rule** (or **Replay** once recordings exist). Open "Constraints & deviations" first: it lists every workbook value and every deviation.

| Scenario | Steps | What to point out |
|---|---|---|
| A. Normal | Single flight, pick DL1639 (DCA→ATL, 26 Feb 2015), press End | Three declared constraint cards (hard limits locked, workbook limits boxed), solver packages with holding fuel kg, consensus at turn 2 or 3, checker PASS, gate arrival vs history (illustrative), Approve appends a SignOff to the hash chain |
| B. Safety test | Toggle **Corrupt solver**, rerun DL1639 | The corrupted package is rejected by an agent or failed by the checker and is never committed; a later package or the fallback is used |
| C. Low fuel | Toggle **Low fuel** | Feasible set empty after declare; FALLBACK diverts to the nearest alternate with its distance; the reason is in the log |
| D. Tight crew | Toggle **Tight crew** | Ground rejects `CREW_DUTY` citing `G_S1`; consensus at turn 3 or FALLBACK |
| E. Multi-trigger | Switch to **Airport window**, keep "Synthetic fixture day", ATL, 2015-03-12, 180 min, press **Run window** | Trigger queue ranked by tier then impact (bar split into knock-on, connection pax, late-night legs, flights affected), each row's ATL capacity and storm factor, two clusters, impact graph (rotation solid, connections dashed), booking ledger with no double booking, orchestrator thread; **Open** a row for its dashboard and thread |
| F. Pre-emption | Same as E with "Scenario F: low fuel on the lowest-ranked trigger after the first dispatch" ticked | The lowest-ranked trigger jumps to tier 0 (SAFETY) and rank 1; the in-progress negotiation finishes its turn, is logged `PREEMPTED` and re-queued; both attempts stay in its thread |

Window mode also takes a list of trigger payloads (JSON list, `{"triggers": [...]}` or a scan object), pasted or uploaded.

## Challenge constraints

Source of truth: `docs/challenge2_operational_constraints.xlsx` [CHALLENGE], exported to `data/challenge/*.csv` and read only through `acacia/constraints.py`. Full rules and the deviations registry: [CONSTRAINTS.md](CONSTRAINTS.md). The UI renders every registry row, grouped by sheet, with its status (AS_GIVEN, ASSUMED, CHANGED, NOT_MODELLED).

| Area | Workbook value | How we use it |
|---|---|---|
| Runway capacity | ATL 90 runway movements per hour (per airport: Large hub 90, Medium hub 60, Non-hub 20) | ASSUMED 50% arrivals, so ATL 45 arrivals/hr nominal; storm window ×0.6 moderate (27/hr) or ×0.4 severe (18/hr), severe when `WEATHER_DELAY > 45` (ASSUMED). Replaces the kit's 4 min / 8 min grid. Window mode shows each trigger's capacity and storm factor |
| Wake separation | Arrival nm by leader/follower pair (Heavy→Heavy 4, Heavy→Large 5, Heavy→Small 6, Large→Small 4, any other pair 3) | Converted at an ASSUMED 140 kt: 3 nm ≈ 1.3 min, 4 ≈ 1.7, 5 ≈ 2.1, 6 ≈ 2.6. Own class ASSUMED Heavy if distance > 2000 mi, else Large. Applied across the merged arrival stream (CHANGED, conservative). Replaces the flat 3 min |
| Turnaround | ATL 45 min (Large hub 45, others 35) | Used everywhere, including the impact graph's rotation slack |
| Spare gates | ATL 3 | Ground offers the assigned gate plus 3 spares, from a modelled pool of 8 gates per airport (CHANGED: workbook ATL 150) |
| Gate-change notice | At least 20 min before arrival | HARD: `gate_arrival - decision_time >= 20` for any gate change (remote stands included); solver discards, checker rule `GATE_CHANGE_NOTICE` |
| Fuel | Final reserve 30 min; spare on arrival 45 min; contingency 5% of trip fuel; holding burn 2,400 / 6,500 kg/h | `land_by = E + 45 + 5% of air time` (ASSUMED interpretation); holding fuel kg reported per package |
| Crew | Flight crew 13 h from report; ground shift 8 h, overtime ×1.5 | Report = scheduled departure − 60 (ASSUMED); ground hard limit shift end + 45 min (ASSUMED) |
| Rules of play | One qualifying row (W > 15 or L > 20); 3 turns; fallback logs why; output = revised time, gate or flight plan | As given, plus a 01:00 to 21:30 earliest-landing window (CHANGED, no midnight crossing in the MVP) |

## Honest limitations

- **Private state is simulated.** Fuel on board, background runway traffic, gate occupancy, crew shifts and connecting passenger counts are not in the data. They are drawn with a fixed seed (`ACACIA_SEED_SALT`), so every run is repeatable, and they are labelled "simulated" in the UI.
- **The multi-trigger fixture is synthetic until `flights.csv` is added.** `tests/fixtures/multi_trigger_day.csv` is hand-built (fictional tails, real airport codes); the UI labels it "synthetic". With only the sample flights, an airport window has one trigger.
- **DL1639 in `data/sample_flights.csv` is reconstructed** from the architecture's worked trace, not the real row.
- **LLM runs and recordings need an API key.** Rule mode is the deterministic stand-in and runs offline; Replay needs recordings made once with `--agents llm --record`.
- **Wake separation is applied across the merged arrival stream**, not per runway, and only arrivals are sequenced.
- **Knock-on "after plans" can exceed do-nothing** on the synthetic day, because storm-reduced capacity pushes simulated slots later than history. We report the number as it is.
- Window mode drains the whole queue at once (no timed playback); single-flight mode has playback.

## Hosting

The app runs on **Streamlit Community Cloud**: point it at this repository and `app.py`, Python 3.12, `requirements.txt`. Put `ANTHROPIC_API_KEY` and `ACACIA_MODEL` in the app's Secrets only if live LLM mode is wanted; without them, Rule and Replay work. `flights.csv` is too large for the repository, so the hosted app uses the sample data and the synthetic multi-trigger day.

## Repository map

| Path | Purpose |
|---|---|
| `CLAUDE.md`, `HANDOVER.md`, `FABLE_ADDENDUM.md`, `PLAN.md` | Build instructions and spec |
| `CONSTRAINTS.md` | Challenge workbook rules and the deviations registry (highest precedence on numbers) |
| `DECISIONS.md` | Every deviation from the spec, one line each |
| `acacia/` | Library: contracts, data, agents, solver, checker, coordinator, triage, dispatcher, CLI |
| `app.py`, `ui/` | Streamlit UI |
| `tests/`, `tests/fixtures/` | Offline tests and synthetic fixture days |
| `docs/` | Architecture, statement, UI reference prototype, workbook |
