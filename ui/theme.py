"""Ops-console look from docs/ui-reference.html: tokens, sender identities, HTML chip helpers.

Strip-board vocabulary (ATC flight progress strips), Barlow Condensed display type, JetBrains
Mono for times and codes. Colours are semantic per sender and per status.
"""

from __future__ import annotations

from html import escape

SENDERS: dict[str, tuple[str, str, str]] = {
    # who -> (label, avatar, css colour token)
    "coordinator": ("Coordinator", ":material/hub:", "coord"),
    "builder": ("Scenario builder", ":material/construction:", "builder"),
    "atc": ("ATC agent", ":material/cell_tower:", "atc"),
    "pilot": ("Pilot agent", ":material/flight:", "pilot"),
    "ground": ("Ground agent", ":material/luggage:", "ground"),
    "solver": ("Solver", ":material/calculate:", "solver"),
    "checker": ("Checker", ":material/verified_user:", "checker"),
    "human": ("Duty manager", ":material/person:", "human"),
    "orchestrator": ("Orchestrator", ":material/account_tree:", "orch"),
}
SINGLE_FLIGHT_SENDERS = ("coordinator", "builder", "atc", "pilot", "ground", "solver", "checker", "human")

STAGES: tuple[tuple[str, str], ...] = (
    ("Triangulate", "code"), ("Trigger", "code"), ("Declare", "AI · turn 1"),
    ("Solve", "code"), ("Check", "AI + code"), ("Commit", "code"),
)

STATUS_CHIP = {"AS_GIVEN": "good", "ASSUMED": "warn", "CHANGED": "bad", "NOT_MODELLED": "muted"}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=Barlow:wght@400;600&family=JetBrains+Mono:wght@400;600&display=swap');
:root{
  --bg:#e9edf0; --panel:#ffffff; --panel2:#f4f6f8; --ink:#14202a; --muted:#5a6a77; --line:#d3dbe1; --line2:#e4e9ed;
  --accent:#0d6d86; --accent-soft:#d8eef3; --strip:#f7e7b9; --strip-ink:#4a3a0c;
  --atc:#0d7f8f; --pilot:#4a4fc2; --ground:#ad5f17; --coord:#3a4652; --solver:#6b5a8e; --checker:#1d7f49;
  --human:#8a3b6b; --builder:#5a6a77; --orch:#7a5c00;
  --good:#1d7f49; --good-soft:#dcf1e4; --bad:#bf3b2b; --bad-soft:#f8e0dc; --warn:#a86b0e; --warn-soft:#f6ead2;
  --display:"Barlow Condensed","Arial Narrow",Arial,sans-serif; --body:"Barlow","Segoe UI",system-ui,sans-serif;
  --mono:"JetBrains Mono",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0d1317; --panel:#141c22; --panel2:#18222a; --ink:#e3eaef; --muted:#8fa0ad; --line:#26333c; --line2:#1e2a32;
  --accent:#4cc0d9; --accent-soft:#12323b; --strip:#3a3216; --strip-ink:#f1dfa4;
  --atc:#4cc3cf; --pilot:#9a9ef2; --ground:#e3a15a; --coord:#a9b6c1; --solver:#b9a6df; --checker:#5fcf8e;
  --human:#e08ab9; --builder:#8fa0ad; --orch:#e5c157;
  --good:#5fcf8e; --good-soft:#123222; --bad:#f08a7a; --bad-soft:#3a1c18; --warn:#e5b157; --warn-soft:#352812;}}
:root[data-theme="dark"]{
  --bg:#0d1317; --panel:#141c22; --panel2:#18222a; --ink:#e3eaef; --muted:#8fa0ad; --line:#26333c; --line2:#1e2a32;
  --accent:#4cc0d9; --accent-soft:#12323b; --strip:#3a3216; --strip-ink:#f1dfa4;
  --atc:#4cc3cf; --pilot:#9a9ef2; --ground:#e3a15a; --coord:#a9b6c1; --solver:#b9a6df; --checker:#5fcf8e;
  --human:#e08ab9; --builder:#8fa0ad; --orch:#e5c157;
  --good:#5fcf8e; --good-soft:#123222; --bad:#f08a7a; --bad-soft:#3a1c18; --warn:#e5b157; --warn-soft:#352812;}
.ac h1,.ac h2,.ac-h{font-family:var(--display);letter-spacing:.06em;text-transform:uppercase}
.ac-h{font-weight:600;font-size:14px;color:var(--muted);margin:2px 0 8px;display:flex;gap:8px;align-items:center}
.ac-brand{font-family:var(--display);font-weight:700;font-size:26px;letter-spacing:.03em;text-transform:uppercase;margin:0}
.ac-sub{color:var(--muted);font-size:13px}
.ac-mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.chip{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:11px;font-weight:600;
  padding:1px 7px;border-radius:999px;border:1px solid currentColor;white-space:nowrap;margin:1px 2px}
.chip.weather{color:var(--accent)} .chip.knock{color:var(--ground)} .chip.combined{color:var(--pilot)}
.chip.sim{color:var(--warn);border-style:dashed}
.chip.good{color:var(--good);background:var(--good-soft);border-color:transparent}
.chip.bad{color:var(--bad);background:var(--bad-soft);border-color:transparent}
.chip.warn{color:var(--warn);background:var(--warn-soft);border-color:transparent}
.chip.muted{color:var(--muted);border-style:dotted}
.chip.info{color:var(--accent);background:var(--accent-soft);border-color:transparent}
.ev-big{font-family:var(--display);font-weight:700;font-size:30px;letter-spacing:.02em;line-height:1}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:13px;margin-top:6px}
.kv dt{color:var(--muted)} .kv dd{margin:0;font-family:var(--mono);font-variant-numeric:tabular-nums}
.causebar{display:flex;height:14px;border-radius:3px;overflow:hidden;background:var(--line2);margin:6px 0 4px}
.causebar span{display:block;height:100%}
.legend{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.inject{margin-top:8px;border:1px dashed var(--warn);color:var(--warn);background:var(--warn-soft);border-radius:6px;
  padding:6px 8px;font-size:12.5px}
.stages{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:6px}
.stage{border:1px solid var(--line);border-radius:6px;padding:6px 8px;background:var(--panel2);min-width:0}
.stage b{display:block;font-family:var(--display);font-size:13px;letter-spacing:.03em;text-transform:uppercase}
.stage small{color:var(--muted);font-size:11px}
.stage.done{border-color:var(--good);background:var(--good-soft)}
.stage.active{border-color:var(--accent);background:var(--accent-soft);box-shadow:inset 0 -3px 0 var(--accent)}
.turnrow{display:flex;justify-content:space-between;align-items:center;margin-top:8px;font-size:12.5px;color:var(--muted);gap:8px;flex-wrap:wrap}
.turns{display:flex;gap:4px} .turns span{width:28px;height:8px;border-radius:2px;background:var(--line)}
.turns span.on{background:var(--accent)}
.agent{border:1px solid var(--line);border-top:3px solid var(--c);border-radius:8px;padding:8px 10px;background:var(--panel2);min-width:0;height:100%}
.agent .who{display:flex;justify-content:space-between;align-items:center;gap:6px;margin-bottom:6px}
.agent .who b{font-family:var(--display);font-size:15px;letter-spacing:.05em;text-transform:uppercase;color:var(--c)}
.agent ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px;font-size:12.5px}
.agent li{display:grid;grid-template-columns:46px 1fr;gap:6px;align-items:start}
.agent li .tag{font-family:var(--mono);font-size:10.5px;font-weight:600;color:var(--muted);padding-top:1px}
.agent li.hard .tag{color:var(--bad)} .agent li.wb .tag{color:var(--accent)}
.agent .vote{margin-top:8px;font-size:12px;border-top:1px dashed var(--line);padding-top:6px}
.wb-box{margin-top:6px;border-left:3px solid var(--accent);padding:4px 8px;background:var(--accent-soft);border-radius:0 6px 6px 0;font-size:12px}
table.pk{width:100%;border-collapse:collapse;font-size:12.5px}
table.pk th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
  border-bottom:1px solid var(--line);padding:4px 6px}
table.pk td{border-bottom:1px solid var(--line2);padding:4px 6px;font-family:var(--mono);font-variant-numeric:tabular-nums;vertical-align:middle}
table.pk tr.sent td{background:var(--accent-soft)} table.pk tr.disc td{color:var(--muted)}
.bar{display:flex;height:10px;width:120px;border-radius:2px;overflow:hidden;background:var(--line2)}
.bar span{display:block;height:100%}
.outcome{border:1px solid var(--line);border-left:5px solid var(--oc);border-radius:8px;padding:10px 12px;background:var(--panel2)}
.outcome .plan{font-family:var(--mono);font-size:15px;font-weight:600;margin:6px 0}
.stamp{font-family:var(--mono);font-size:11px;color:var(--muted);word-break:break-all;margin-top:6px}
.reg{width:100%;border-collapse:collapse;font-size:12px}
.reg td{border-bottom:1px solid var(--line2);padding:3px 4px;vertical-align:top}
.reg td.id{font-family:var(--mono);font-weight:600;white-space:nowrap}
.reg .wb{color:var(--muted)}
.msg-head{font-size:12px;color:var(--muted);display:flex;gap:6px;flex-wrap:wrap;align-items:baseline}
.msg-head b{font-family:var(--display);letter-spacing:.05em;text-transform:uppercase;font-size:13.5px}
.msg-foot{font-family:var(--mono);font-size:10.5px;color:var(--muted)}
.composing{color:var(--muted);font-style:italic;font-size:13px;padding:6px 2px}
[class*="st-key-msg-coordinator"] [data-testid="stChatMessage"]{border:1px dashed var(--coord);background:var(--panel2)}
[class*="st-key-msg-checker"] [data-testid="stChatMessage"]{border-left:3px solid var(--checker)}
[class*="st-key-msg-human"] [data-testid="stChatMessage"]{border-left:3px solid var(--human)}
[class*="st-key-strip-"] button{justify-content:flex-start;border-left:6px solid var(--strip)}
[class*="st-key-strip-"] button > div{justify-content:flex-start;width:100%}
[class*="st-key-strip-"] button p{text-align:left;font-family:var(--mono);font-size:12.5px;white-space:pre}
[class*="st-key-strip-sel"] button{border-color:var(--accent);box-shadow:inset 3px 0 0 var(--accent)}
[class*="st-key-msg-orchestrator"] [data-testid="stChatMessage"]{border-left:3px solid var(--orch);background:var(--panel2)}
.qrow{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:13px}
.qrank{font-family:var(--display);font-weight:700;font-size:20px;min-width:22px;color:var(--muted)}
.qbar{width:140px;display:inline-flex;vertical-align:middle}
.qcap{font-size:11.5px;margin:2px 0 0 28px}
[class*="st-key-qrow-"]{border-left:6px solid var(--strip);padding-left:6px;border-radius:4px}
[class*="st-key-qrow-sel"]{border-left-color:var(--accent);background:var(--accent-soft)}
[data-testid="stMainBlockContainer"]{padding-top:4.5rem}
.verdict{border:1px solid var(--line);border-left:6px solid var(--oc);border-radius:8px;padding:8px 12px;background:var(--panel)}
.verdict-chips{display:flex;flex-wrap:wrap;gap:2px}
.verdict-plan{font-family:var(--mono);font-size:16px;font-weight:600;margin:5px 0 2px;color:var(--ink)}
.verdict-facts{font-size:12px;color:var(--muted)}
.verdict-facts a{color:var(--accent);font-weight:600;text-decoration:none;white-space:nowrap}
.verdict-facts a:hover,.verdict-facts a:focus-visible{text-decoration:underline}
ol.ac-how{margin:6px 0 0;padding-left:20px;color:var(--muted)} ol.ac-how li{margin:0;padding:0;font-size:13px!important;line-height:1.45}
.ac-progress{border:1px dashed var(--accent);border-radius:8px;padding:6px 10px;font-size:13px;color:var(--accent)}
.ac-cons-sum{display:flex;flex-wrap:wrap;align-items:center;gap:2px}
.ac-anchor{scroll-margin-top:4.5rem}
.stElementContainer:has(.ac-scroll-anchor){display:none}
[data-testid="stLayoutWrapper"]:has(> [class*="st-key-ac-follow"]){position:absolute!important;width:0;height:0;overflow:hidden}
[class*="st-key-sender_filter"] [data-testid="stButtonGroup"] > div,[class*="st-key-win_sender_filter"] [data-testid="stButtonGroup"] > div{flex-wrap:wrap}
@media (max-width:640px){
  [data-testid="stMainBlockContainer"]{padding-left:1rem;padding-right:1rem}
  .st-key-ac-playback [data-testid="stHorizontalBlock"],.st-key-ac-stress [data-testid="stHorizontalBlock"]{flex-wrap:nowrap;gap:4px}
  .st-key-ac-playback [data-testid="stColumn"],.st-key-ac-stress [data-testid="stColumn"]{min-width:0!important;width:auto!important;flex:1 1 0!important}
  .st-key-ac-playback button{padding:4px 2px;min-height:34px;gap:2px}
  .st-key-ac-playback button p{font-size:12.5px}
  .st-key-ac-stress label p{font-size:12.5px;line-height:1.2}
  .st-key-ac-body > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"]{flex-direction:column;flex-wrap:nowrap}
  .st-key-ac-body > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
  .st-key-ac-body > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] > [data-testid="stVerticalBlock"]{display:contents}
  [data-testid="stLayoutWrapper"]:has(> .st-key-ac-l-top){order:1}
  [data-testid="stLayoutWrapper"]:has(> .st-key-ac-r-chan){order:2}
  [data-testid="stLayoutWrapper"]:has(> .st-key-ac-l-dash){order:3}
  [data-testid="stLayoutWrapper"]:has(> .st-key-ac-l-cons){order:4}
  table.pk{display:block;overflow-x:auto}
  [class*="st-key-strip-"] button p{white-space:pre-wrap}
}
[data-testid="stBaseButton-primary"]{background:var(--accent)!important;border-color:var(--accent)!important;color:var(--panel)!important}
[data-testid="stBaseButton-primary"]:hover{filter:brightness(1.1)}
button[data-variant="pills"][aria-pressed="true"]{background:var(--accent-soft)!important;border-color:var(--accent)!important;color:var(--accent)!important}
</style>
"""


def esc(value: object) -> str:
    return escape("" if value is None else str(value))


def chip(text: object, kind: str = "") -> str:
    return f'<span class="chip {esc(kind)}">{esc(text)}</span>'


def sim_chip() -> str:
    return chip("simulated", "sim")


def colour(who: str) -> str:
    return f"var(--{SENDERS.get(who, SENDERS['coordinator'])[2]})"


def sender_label(who: str) -> str:
    return SENDERS.get(who, (who.title(), "", "coord"))[0]


def sender_avatar(who: str) -> str:
    return SENDERS.get(who, ("", ":material/chat:", ""))[1]


def scenario_chip(scenario: str) -> str:
    kind = {"WEATHER": "weather", "KNOCK_ON": "knock", "COMBINED": "combined"}.get(scenario, "")
    return chip(scenario.replace("_", "-"), kind)


def heading(text: str, extra: str = "") -> str:
    return f'<div class="ac-h">{esc(text)}{extra}</div>'
