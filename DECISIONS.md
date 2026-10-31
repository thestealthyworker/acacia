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
- 2026-10-31, Foundation builder: Ground declares its min_turn in ConstraintPacket.facts_owned["min_turn_min"] so the solver can apply CREW_DUTY and gate-interval checks from declarations only (no HardLimit for it in HANDOVER 8.2).
- 2026-10-31, Foundation builder: Stress gains declared_emergency (default False) so a payload-declared emergency reaches triage tier 0 (HANDOVER 18.4); trigger.stress_for(payload, base) derives it.
- 2026-10-31, Foundation builder: FlightRef.flight_no holds the number only ("1639"); FlightRef.code gives "DL1639". Keeps the event_id key "{airline}{flight_no}-..." as specified.
- 2026-10-31, Foundation builder: LogEntry gains optional event_id and dispatched_by ({seq, hash}); LogEntry.type is a Literal of the HANDOVER 11.4 and 18.6 types plus OBJECTION, DEFAULT, ERROR.
- 2026-10-31, Foundation builder: models.py also holds the LedgerView protocol, EmptyLedgerView, the private-state models (AtcPrivate, PilotPrivate, GroundPrivate, PrivateState; int minutes), RuleResult/CheckResult (field 'passed', since 'pass' is a keyword), FallbackPlan (alternate_distance_mi, statute miles) and SignOff (approved, who).
- 2026-10-31, Foundation builder: POLICY and IMPACT_POLICY are flat weight dicts; their versions are separate constants POLICY_VERSION=policy-0.1 and IMPACT_POLICY_VERSION=impact-0.1, so sum(POLICY[k]*u[k]) works as written.
- 2026-10-31, Foundation builder: injected event start/end are clamped to 00:00..23:59 (e.g. EQUIPMENT_FAILURE end E+180 for E near 21:30), because HH:MM cannot express times past midnight (PLAN 2.A.3).
- 2026-10-31, Foundation builder: triangulate seeds the closure-end rand(0..10) with key "{SALT}|{event_id}|event" (the prototype's '|event' suffix).
- 2026-10-31, Foundation builder: trigger adapter: an explicit injected event waives only the delay-cause test; cancelled/diverted rows and the 01:00-21:30 window (on the final eta_landing, after overrides) are always enforced.
- 2026-10-31, Foundation builder: trigger scan payload without 'start' opens its window at the earliest qualifying landing at that airport; batch payloads fail as a whole unless skip_invalid=True.
- 2026-10-31, Foundation builder: hist_arrival falls back to S + ARRIVAL_DELAY when ARRIVAL_TIME is blank; E uses the same fallback when both WHEELS_ON and ARRIVAL_TIME are blank.
- 2026-10-31, G1 validator: earliest_landing unwraps WHEELS_ON/ARRIVAL_TIME more than 12 h before S as next-day (+1440), so a late flight landing after midnight gets E from S - Ti + L + AL + SE instead of a wrong small clock time.
- 2026-10-31, G1 validator: a full {"event": ...} trigger payload with an empty injected list must pass the delay-cause test on its causes (weather > 15 or late_aircraft > 20), same as row payloads.
