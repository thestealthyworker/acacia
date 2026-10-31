# Acacia demo script

App: https://atcbrain.streamlit.app/ · Total time about 6 minutes · Agent mode: **Replay** (recorded Claude runs, works with no network)

Before you start: open the app, wait for the first screen to load, and leave the stress toggles **off**.

---

## 0. Opening (30 s)

**Say:** "When a runway closes or the weather turns, recovery today runs on phone calls. Acacia lets three AI agents (ATC, the pilot and ground operations) negotiate the recovery themselves. The agents argue. Deterministic code decides. A human signs off."

**Show:** the first screen. It has already played the DL1639 negotiation.

---

## 1. Scenario A: a normal recovery (90 s)

The app opens on **DL1639 DCA→ATL, 26 Feb 2015**. The flight was 51 min late: 30 min weather plus 21 min late aircraft.

1. Point to the **verdict banner**: "CONSENSUS · checker PASS · land ATL 12:29 on R1, gate A2 · 2 of 3 turns".
   **Say:** "Agreement in two turns. The plan lands it 12 minutes earlier at the gate than it arrived in reality."
2. Point to the **event card**: the simulated moderate storm, which cuts Atlanta's capacity from 45 to 27 arrivals an hour.
   **Say:** "Every limit comes from the challenge workbook: capacity, wake separation, fuel reserve and turnaround."
3. Point to the **three agent cards**: ATC offers slots, the pilot must land by 13:02 on fuel, and ground offers gates.
   **Say:** "Each agent is a separate Claude call, with its own prompt and its own private data. They only exchange structured messages."
4. Point to the **solver table**.
   **Say:** "The agents never pick a time. Code builds every slot, runway and gate combination, throws out anything that breaks a declared limit, and scores the rest."
5. Point to the **agent channel** on the right and press **Replay**. It scrolls on its own.
   **Say:** "Every message is logged and hash-chained. 'Chain verified' means nothing was altered."
6. Press **Approve** in the outcome panel.
   **Say:** "Nothing executes until a human approves."

---

## 2. Scenario B: catching a bad plan (60 s)

1. Turn on **Corrupt solver**.
2. **Show:** at turn 2 the ATC agent rejects with **SEPARATION**. At turn 3 the agents agree on a safe plan.
   **Say:** "We deliberately broke the solver so it proposed an unsafe landing slot. The agent caught it, and the independent checker would have blocked it anyway. An unsafe plan can never be committed."
3. Turn **Corrupt solver** off.

---

## 3. Scenario C: low fuel (30 s)

1. Turn on **Low fuel**.
2. **Show:** **FALLBACK**, divert to **CSG**, with the distance.
   **Say:** "No slot is early enough for the fuel left, so the agents don't negotiate. The rule-based fallback diverts to the nearest airport and protects the 30-minute final reserve."
3. Turn **Low fuel** off.

---

## 4. Scenario D: tight crew (30 s)

1. Turn on **Tight crew**.
2. **Show:** at turn 2 Ground rejects with **CREW_DUTY** (25 min overtime is over its red line). Turn 3 ends in **FALLBACK** with the checker passing.
   **Say:** "Three turns maximum, then the deterministic fallback decides and logs why. No endless loops."
3. Turn **Tight crew** off.

---

## 5. Scenario E: several disruptions at once (90 s)

1. Set **View** to **Airport window**. Data: the synthetic ATL fixture day. Press **Run window**.
2. **Show:** the **trigger queue**: 5 delayed flights, ranked with impact bars.
   **Say:** "When five flights are disrupted together, an orchestrator ranks them on an impact graph built from real aircraft rotations and connections. Safety always goes first, then the most time-critical."
3. Click **Open** on a flight. **Show** its knock-on delay graph.
4. **Show** the **booking ledger** and the **orchestrator thread**.
   **Say:** "All 5 reach agreement. The ledger guarantees that no two flights get the same runway slot or gate."

**Optional, Scenario F:** tick **"Scenario F: low fuel on the lowest-ranked trigger after the first dispatch"** and press **Run window**.
**Say:** "A flight that becomes a fuel emergency jumps the queue. The negotiation in progress is parked and resumed later."

---

## 6. Close (30 s)

Open the **Constraints & deviations** panel.

**Say:** "Every workbook limit is listed, with what we used: as given, assumed or changed. That covers the brief: separate agents instead of one prompt; a hard limit of three turns; every safety margin re-checked by code; a traceable log; and a deterministic fallback."

---

## If asked

- **Is the AI live?** Replay plays recorded Claude runs. Switching **Agents** to **LLM** runs Claude live; that needs an API key.
- **What is simulated?** Each agent's private data: slots, passengers, gates and crew shifts. It is seeded and labelled "simulated". The delays come from the real 2015 US DOT data.
- **What are the known limits?** On the test day the plans add up to more total knock-on delay than doing nothing (242 vs 140 min). The scoring currently favours less congested later slots, and re-weighting it is the next step.

## Troubleshooting

- **The app is asleep:** press the wake button and wait about 30 s.
- **The screen looks stuck:** press **End**, then **Replay**.
- **To reset everything:** reload the page.
