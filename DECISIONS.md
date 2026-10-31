# Decisions and deviations

One line each: date, who, what changed, why. Newest at the bottom.

- 2026-10-31, spec: `Decision.cites` added so a REJECT must cite a declared constraint (architecture review item 1).
- 2026-10-31, spec: `SoftPref.metric/target/stake` added so the solver can score from declarations only, without reading private state.
- 2026-10-31, spec: `DisruptionEvent.eta_landing/taxi_in/distance/overrides` added for the simulators and the trigger adapter.
- 2026-10-31, spec: `ConsensusRecord.delta_vs_fallback_min/status/stamp/impact/reservations` added for evaluation, governance and the ledger.
- 2026-10-31, spec: `ReasonCode.RESOURCE_CONFLICT` added; used by the checker only, for ledger double bookings.
- 2026-10-31, spec: runway grid and gate pool moved from per-event simulation to a shared AirportWorld so multiple triggers can compete for real.
- 2026-10-31, spec: rotations do not cross midnight in the MVP (data is dated by departure day).
- 2026-10-31, data: DL1639 in data/sample_flights.csv is reconstructed from the architecture's worked trace; tail UNKNOWN. Replace with the real row.
- 2026-10-31, plan: checker tests the full gate turnaround interval [slot+Ti, slot+Ti+min_turn) and world.gate_free_from takes a duration (PLAN 2.A.1).
- 2026-10-31, plan: airport world built once per dispatch from all collected triggers; pre-emption changes stress only (PLAN 2.A.2).
- 2026-10-31, plan: triggers with earliest landing before 01:00 or after 21:30 do not qualify in the MVP (PLAN 2.A.3).
- 2026-10-31, plan: LLM agents built before the UI (PLAN 2.B.4).
- 2026-10-31, plan: Streamlit is hosted on Streamlit Community Cloud, not Vercel (PLAN 2.C.6).
- 2026-10-31, plan: turn counter, hard-limit cuts, red-line and overtime definitions, solver signature and playback defined in PLAN section 3.
