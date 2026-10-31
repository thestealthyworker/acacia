# CLAUDE.md: start here

You are building the Acacia Statement 2 hackathon project: multi-agent disruption negotiation for air traffic management. Hard time limit: 3 hours.

## Read in this order before writing any code

0. `CONSTRAINTS.md`: the challenge's operational constraints workbook. It wins over every other file on numbers and limits, and every deviation must be listed in the UI.
1. `docs/statement-2.png` (and any other statement text in `docs/`): the brief and the "what to avoid" list.
2. `docs/architecture.html`: the agreed architecture. It wins on design questions.
3. `HANDOVER.md`: the full build spec. It wins on implementation detail. Read all 18 sections.
4. `FABLE_ADDENDUM.md`: the spec for the triage and graph workstream (built by Fable in parallel). Read sections 2, 3 and 11 even if you are not building it. They define the interfaces you call.
5. `docs/trigger-payload.sample.json` if present: Ashiq's real trigger payload. It wins on input format. `docs/trigger-payload.example.json` is a placeholder only.
6. `docs/ui-reference.html`: open it in a browser. It is a working prototype of the single-event logic and the two-panel UI. Match its layout and behaviour; do not copy its solver, which wrongly reads private state (HANDOVER section 1).

Then write a short plan (modules, order, tests) in `PLAN.md` and start with HANDOVER section 15, row 1.

## Who builds what

- **Claude Code (you):** models, config, timeutil, data, trigger, triangulate, simulate, agents (rule, llm, replay), solver, checker, fallback, coordinator, log, cli run/batch/verify-log, app.py, and tests 1 to 14.
- **Fable:** world, graph, triage, ledger, dispatcher, cli triage/backtest, and tests 15 to 24.
- **The contracts in HANDOVER 5 and 18.3 are the boundary.** Create `models.py` with all of them first, so both sides build against the same code. If you are the only builder, build both workstreams in the order of HANDOVER section 15.

## Non-negotiable rules

- Agents argue, code decides. No LLM output ever sets a time, computes separation or fuel, ranks triggers, writes the ledger or commits a plan.
- The solver reads only declared ConstraintPackets, never private state. The checker reads ground truth and the live ledger, and imports nothing from the solver.
- Every run terminates in at most 3 turns. Timeouts never count as ACCEPT.
- Nothing is committed unless the checker passes it, and nothing executes without human sign-off.
- Everything deterministic is seeded and reproducible. Simulated values are labelled "simulated" in the UI.
- Never commit `.env` or the full `flights.csv`.
- Record every deviation from the spec as one line in `DECISIONS.md`.

## Commands (create these)

```bash
pip install -r requirements.txt
pytest -q                                    # must pass offline (rule agents)
python -m acacia.cli run --flight DL1639 --date 2015-02-26 --agents rule
python -m acacia.cli run --flight DL1639 --date 2015-02-26 --agents llm --record
python -m acacia.cli run --flight DL1639 --date 2015-02-26 --agents replay
python -m acacia.cli triage --scan --date 2015-02-26
python -m acacia.cli triage --airport ATL --date 2015-02-26 --window 180
python -m acacia.cli backtest --date 2015-02-26
python -m acacia.cli verify-log runs/<file>.jsonl
streamlit run app.py
```

## Working style

- Build the deterministic core first. Reach the "MVP floor" (HANDOVER section 15: full single-event and multi-trigger runs with rule agents, all tests green) before any LLM or UI work.
- Run `pytest -q` after every module.
- Keep LLM calls behind `agents/llm.py`. Record every response. Default the UI to replay mode for the demo.
- Use the sample data in `data/` until the full files arrive. `data/sample_flights.csv` row 6 (DL1639) is reconstructed, not real; replace it with the real row once `flights.csv` is present.
- If you are behind at 1:45, apply the cut line in HANDOVER section 15.
- Ask Ashiq before changing a contract, the state machine or the scoring weights.
