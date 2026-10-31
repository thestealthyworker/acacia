# Acacia: judge demo script

App: https://atcbrain.streamlit.app/ · Length: about 4 min 45 s · Agent mode: **Replay** (recorded Claude runs, works offline)

**The one idea to land:** *the AI agents argue, deterministic code decides.* Every moment below proves that the AI is useful but can never break a safety limit.

---

## Before you go on stage (5 min earlier)

1. Open the app so it wakes up. Check the banner reads **CONSENSUS · checker PASS**.
2. **Tab 1:** single flight (DL1639), all stress toggles **off**, browser zoom about 110%.
3. **Tab 2:** same app, **View → Airport window**, press **Run window** now so the queue is ready.
4. **Terminal**, in the repo folder, with this already run once:
   ```bash
   python -m acacia.cli run --flight DL1639 --date 2015-02-26 --agents replay --quiet --out /tmp/acacia-demo.jsonl
   ```
5. Backup if the Wi-Fi fails: `streamlit run app.py` locally. Replay needs no network.

---

## Run of show

### 0:00–0:30 · The problem
**Say:** "When a runway closes or a storm hits, recovery today runs on phone calls between ATC, airlines and ground crews. Acacia replaces that with three AI agents from three organisations that negotiate the recovery, within the safety limits in the challenge workbook."

### 0:30–1:30 · Scenario A: how it works (Tab 1, already loaded)
| Click or point | Say |
|---|---|
| **Verdict banner**: CONSENSUS, land 12:29 on R1, gate A2, 2 of 3 turns | "A real delayed flight, DL1639 into Atlanta. The agents agreed in two turns." |
| **Event card**: moderate storm, 45 down to 27 arrivals per hour | "Every limit comes from the workbook: capacity, wake separation, fuel reserve, turnaround." |
| **Three agent cards** | "Three separate Claude calls, each with its own prompt and private data. They only exchange structured messages." |
| **Solver table** | "The agents never choose a time. Code builds every slot, runway and gate option, drops anything that breaks a declared limit, and scores the rest." |
| **Agent channel → Replay** (it scrolls by itself) | "Every message is logged in a hash chain." |
| **Approve** | "Nothing executes until a human signs off." |

### 1:30–2:15 · Scenario B: safety (slow down here)
1. Turn on **Corrupt solver**.
2. Point at turn 2: **ATC REJECT · SEPARATION**. Then turn 3: a safe plan.

**Say:** "Here we sabotaged the solver so it proposes an unsafe landing slot. The ATC agent rejected it, and if it hadn't, the independent checker re-verifies every hard limit and blocks the commit. An unsafe plan can never go through, whatever the AI says."

3. Turn **Corrupt solver** off.

### 2:15–2:45 · Scenario D: no endless loops
1. Turn on **Tight crew**.
2. Point at turn 2: **Ground REJECT · CREW_DUTY (G_S1)**, then turn 3: **FALLBACK**, checker PASS.

**Say:** "Three turns maximum. If the agents don't agree, a deterministic fallback decides and logs why. No loops, no hanging."

3. Turn **Tight crew** off.

### 2:45–3:45 · Scenario E: many disruptions at once (Tab 2)
| Click or point | Say |
|---|---|
| **Trigger queue**: 5 flights with impact bars | "Five disruptions at once. An orchestrator ranks them on an impact graph built from real aircraft rotations and connections. Safety tier first, then the most time-critical." |
| **Open** on a flight to show its knock-on graph | "Each delay is followed down the aircraft's next legs." |
| **Booking ledger** | "All five agree, and the ledger guarantees no two flights share a runway slot or gate." |
| *(if time)* tick **Scenario F** and press **Run window** | "A fuel emergency jumps the queue; the negotiation in progress is parked and resumed." |

### 3:45–4:15 · Proof it can't be faked (terminal)
```bash
python -m acacia.cli verify-log /tmp/acacia-demo.jsonl
sed -i '' -E 's/"slot": ?"12:29"/"slot":"12:19"/' /tmp/acacia-demo.jsonl
python -m acacia.cli verify-log /tmp/acacia-demo.jsonl
```
**Say:** "That's the consensus log. It verifies. Now I change one landing time by ten minutes … BROKEN at entry 8. The record of what was agreed can't be quietly edited."

### 4:15–4:45 · Close (Tab 1, open **Constraints & deviations**)
**Say:** "Every workbook limit is listed with what we used: as given, assumed or changed. That's the brief: separate agents instead of one prompt, a hard three-turn limit, every safety margin re-checked by code, a traceable log, and a deterministic fallback. 517 automated tests, all passing offline."

---

## Optional live moment (only if the API key is set in Streamlit Secrets)
Set **Agents** to **LLM** and re-run DL1639.
**Say:** "That's Claude live, right now." If it phrases things differently from the recording: "The wording can vary; the code still decides."

## Judge questions

- **"Is it one model playing three roles?"**
  No. Three independent calls with separate prompts and separate private state. The channel shows each agent sees only its own data.
- **"What stops the AI inventing numbers?"**
  It can't set a time or a margin. Code computes them, a declaration audit flags any mismatch, and the checker re-verifies.
- **"What's real and what's simulated?"**
  The delays are real 2015 US DOT data. Agents' private data (slots, passengers, crew shifts) is simulated, seeded and labelled.
- **"How does it scale?"**
  Impact graph, a safety tier, deadline-ordered dispatch with a shared clock, and a ledger that rejects double booking.
- **"What would you build next?"**
  Real crew rosters, live data feeds, optimising a whole hub at once, and signing every message.

## Troubleshooting
- **App asleep:** press the wake button and wait about 30 s.
- **Playback looks stuck:** press **End**, then **Replay**.
- **Anything odd:** reload the page; it reopens on the finished demo.
