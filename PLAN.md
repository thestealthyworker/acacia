# PLAN.md: Acacia Statement 2 build plan (corrected)

Status: approved 2026-10-31 (owner asked to build). Target: 2 h 15 min, run as a staged multi-agent workflow (section 7).

The kit is a strong spec. Its core idea is right and stays: agents argue, code decides. The contracts, the state machine, the checker and the fallback all stay. This plan keeps HANDOVER.md as the build spec. It changes six things, defines eight behaviours the spec leaves open, and reorders the build so the AI agents are demo-ready earlier.

---

## 1. Inputs still missing

| Item | Needed by | Without it |
|---|---|---|
| `data/flights.csv` (full, ~38.6 MB) | Phase 1 (real DL1639 row), Fable graph and backtest | Build runs on the 6-row sample; triage and backtest cannot be demoed |
| `docs/trigger-payload.sample.json` (the "problem payload") | `trigger.py` | Adapter built against the placeholder shapes; the real sample is added as a test when it arrives |
| `ANTHROPIC_API_KEY` in `.env` | Phase 4 (LLM agents) | Rule and replay modes still work |

---

## 2. Changes to the spec (each goes into DECISIONS.md once approved)

### A. Safety and correctness fixes

1. **Gate check covers the whole turnaround, not only the start.** As specified, `GATE_CONFLICT` passes when `slot + Ti >= free_from`. But `world.gate_free_from` only guarantees 30 free minutes, and `min_turn` can be 35 to 60. A package could therefore put an aircraft on a gate that a background booking needs 30 minutes later, and the checker would pass it. Fix:
   - `gate_free_from(gate, t, view, duration)` takes the required duration (`min_turn`).
   - The checker tests the full interval `[slot + Ti, slot + Ti + min_turn)` against world busy intervals, failures and the live ledger.
   - New checker test for this case.
2. **Build the airport world once per dispatch, not incrementally.** The addendum rebuilds the runway grid when more injected events arrive. A rebuild changes grid spacing inside closures, so a slot already HELD in the ledger can fall off the grid or become traffic. Fix: the dispatcher collects all triggers first, then builds the world once from all injected events. Pre-emption changes stress only (for example `low_fuel`), never closures.
3. **Midnight.** Times are minutes after local midnight, and the world grid stops at 24:00. A late-evening trigger (earliest landing plus 150 min) would run past the grid. Fix: in the MVP, triggers with earliest landing before 01:00 or after 21:30 do not qualify, and the count is logged. Delays always come from `ARRIVAL_DELAY`, never from `A - S`.

### B. Scope and sequence

4. **Move the LLM agents before the UI.** The brief is judged on AI agents negotiating. The spec builds them last (2:25 to 2:45), so a slip leaves a demo with no AI in it. New order: coordinator, then LLM and replay agents, then UI. Rule agents remain the test and fallback mode.
5. **Run Fable's workstream as parallel Fable-model subagents in the same working tree.** Each agent owns a fixed set of files, so no worktrees are needed (worktrees inside a Google Drive folder sync badly). Contracts are frozen in `models.py` before the split. Only gate validators commit.

### C. Hosting

6. **Vercel cannot run this UI.** Streamlit needs a long-running Python server with websockets, which Vercel's serverless functions do not provide. Options:
   - (a) Recommended: deploy the Streamlit app to Streamlit Community Cloud (free, deploys from the public GitHub repo). Leave the Vercel project unused, or delete it later.
   - (b) Run the Streamlit app locally for the demo only.
   - (c) Also publish a static replay viewer on Vercel (an HTML page like `ui-reference.html` that plays recorded JSONL runs). Stretch goal only, after freeze.

---

## 3. Behaviours the spec leaves open (defaults chosen)

1. **Turn counter.** Turn 1 is declare. Entering SOLVING after declare sets turn = 2. A reject at turn 2 sets turn = 3. A reject at turn 3 goes to FALLBACK. So at most two packages are voted on, matching the architecture's "Check T2, Commit T3".
2. **Cuts from a hard-limit reject.** The spec defines cuts only for soft-preference cites. If a REJECT cites a hard limit:
   - ATC `A_H1`: exclude every package with that slot.
   - Ground `G_H1`: exclude every package with that gate.
   - Pilot `P_H1`, or Ground `G_H2`: exclude every package at that slot or later.
3. **Pilot "connections fully lost".** Means connections utility = 0, that is gate arrival at or after `connect_by + 30`.
4. **Ground overtime.** `overtime = max(0, slot + Ti + min_turn - shift_end)`.
5. **Section 6 versus section 18.2.** Where they differ (runway grid, gate names, which gate an equipment failure takes), section 18.2 and the addendum win. Section 6 still defines queue, pilot state, crew shift and red lines.
6. **Solver isolation.** `solve()` does not take private state as a parameter at all. Test 7 checks the signature and also runs the garbage-state comparison.
7. **Streamlit playback.** Streamlit reruns the whole script on every interaction, so a live generator cannot survive across reruns. Rule and replay runs are collected into a list first, then played back by an index in `st.session_state` with a timed fragment. LLM runs execute in a background thread that appends entries to a session list, which the same playback reads.
8. **LLM model.** `ACACIA_MODEL=claude-haiku-5-5` by default (fast, cheap), switchable to `claude-sonnet-5-5`. Confirm the request parameters against the current API docs when building `llm.py`.

---

## 4. Repository layout change

Move the kit's contents to the repo root (`git mv`) so `CLAUDE.md` loads automatically and the `acacia/` package sits at the root. Merge `.gitignore` (add `.venv/`, `.vercel/`, `.DS_Store`, `.env.*` except `.env.example`). Recordings are committed, because replay mode is the demo insurance. `runs/` and the full `flights.csv` are not committed.

---

## 5. Build order (3 hours, two streams)

Each phase ends at a gate. Do not start the next phase until its gate is green.

| Time | Main stream (Claude Code) | Fable stream (subagent, `ws/triage`) | Gate |
|---|---|---|---|
| 0:00 to 0:15 | Restructure repo, venv, `timeutil.py`, `config.py`, `models.py` with every contract (HANDOVER 5 and 18.3, plus the changes above); commit | Starts after the commit: stub every interface | Contracts frozen; `pytest -q` collects |
| 0:15 to 0:45 | `data.py`, `triangulate.py`, `trigger.py`; tests 1 to 3, 13 | `world.py` (with change 1), `ledger.py`; tests 21, 22 | Real events from rows |
| 0:45 to 1:20 | `simulate.py`, rule agents, `solver.py`, `checker.py` (full gate interval and `RESOURCE_CONFLICT`), `fallback.py`; tests 4 to 7, 11 | `graph.py`, `triage.py`; tests 15 to 20 | Deterministic core |
| 1:20 to 1:45 | `coordinator.py`, `log.py`, `cli run` and `verify-log`; tests 8 to 10, 12, 14 A to D; merge `ws/triage` | `dispatcher.py`, `cli triage` and `backtest`; tests 23, 24; scenarios E, F | **MVP floor**: single and multi-trigger runs, no AI, all tests green. Code review pass |
| 1:45 to 2:10 | `agents/llm.py`, `agents/replay.py`, prompts; record scenarios A, B, E | Backtest numbers; `triage --scan` to pick the demo airport and date | Real agents and recorded runs |
| 2:10 to 2:45 | `app.py`: single-flight mode first, then window mode; deploy | Integration fixes | Demo-able UI online |
| 2:45 to 3:00 | Freeze, README, rehearse | Freeze | Ship |

**Cut line at 1:45 (unchanged):** drop connection edges, drop pre-emption (scenario F), demo LLM from recordings. Never cut the ledger or `RESOURCE_CONFLICT`.

**Second cut line at 2:10 (new):** window mode in the UI shows the queue and the ledger as plain tables, with no graph drawing.

---

## 6. Engineering rules for the build

- Contracts first. Any change to `models.py` after 0:15 needs a DECISIONS.md line and a note to the other stream.
- Test first per module, using the numbered tests in HANDOVER 14. Run `pytest -q` after every module. Tests run offline.
- One commit per module, conventional messages (`feat:`, `test:`, `fix:`). Main stream commits to `main`; the Fable stream merges by pull request at its two checkpoints.
- Code review subagent at the MVP floor and before freeze. Fix CRITICAL and HIGH findings.
- No secrets in the repo. `.env` stays local. The repo is public.
- Everything seeded. Simulated values are labelled "simulated" in the UI.

---

## 7. Execution: staged workflow (target 2 h 15 min)

Builders own disjoint files and never commit. After each stage, a validator gate runs the full test suite, reviews the stage against HANDOVER, FABLE_ADDENDUM and this plan, fixes what it can, then commits and pushes to `main`. A red gate gets one fix agent and one re-check before the workflow continues.

| Stage | Builders (parallel) | Gate |
|---|---|---|
| 1 Foundation | timeutil, config, models (all contracts), data, triangulate, trigger; tests 1 to 3, 13 | G1: contracts frozen, push |
| 2 Core A | Main: rule agents, solver, fallback; tests 6, 7, 11. Fable: world, ledger; tests 21, 22 | none (stage 3 follows) |
| 3 Core B | Main: simulate, checker; tests 4, 5. Fable: graph, triage; tests 15 to 20 | G2: deterministic core, push |
| 4 Negotiation | Main: coordinator, log, cli run and verify-log; tests 8 to 10, 12, 14 A to D. LLM: llm.py, replay.py, prompts; mocked tests | G3: single-event MVP, push |
| 5 Orchestration and UI | Fable: dispatcher, cli triage and backtest; tests 23, 24, scenarios E and F. UI: app.py single-flight mode | G4: multi-trigger MVP floor, push |
| 6 Integration | UI window mode, README, end-to-end CLI checks | Final: independent code review, fixes, push |

Environment: Python 3.12 venv at `~/.venvs/acacia` (kept outside Google Drive). Full `airports.csv` added from the owner's Downloads. Still missing: full `flights.csv`, the trigger payload sample, and the API key (LLM recordings wait for it).
