# Acacia build kit: Statement 2

Everything Claude Code and Fable need to build the multi-agent disruption negotiation system.

| Path | Purpose |
|---|---|
| `CLAUDE.md` | Start here. Reading order, rules, commands |
| `HANDOVER.md` | Full build spec (18 sections) |
| `FABLE_ADDENDUM.md` | Detailed spec for the triage, impact graph and ledger workstream |
| `DECISIONS.md` | Log of every deviation from the spec (starts with the known ones) |
| `docs/architecture.html` | Agreed architecture (design authority) |
| `docs/statement-2.png` | Problem statement |
| `docs/ui-reference.html` | Working browser prototype of the single-event console |
| `docs/trigger-payload.example.json` | Placeholder trigger payloads; add Ashiq's real sample as `docs/trigger-payload.sample.json` |
| `data/` | `airlines.csv` (complete) and small samples. Add the full `flights.csv` and `airports.csv` |
| `tests/fixtures/rotation_day.csv` | Hand-built rotation chain with expected values (FABLE_ADDENDUM section 10) |
| `.env.example`, `requirements.txt` | Setup |

## Before you start

1. Copy the full `flights.csv` and `airports.csv` into `data/`.
2. Add the real trigger payload as `docs/trigger-payload.sample.json`.
3. `cp .env.example .env` and fill in the API key and model.
4. Open Claude Code in this folder and tell it: "Read CLAUDE.md and follow it."
5. Give Fable `FABLE_ADDENDUM.md`, `HANDOVER.md` and `docs/architecture.html`.
