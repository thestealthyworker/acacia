"""Keep a fixed-height Streamlit container scrolled to its newest entry (owner feedback, QA fix 2).

`anchor(name)` drops a hidden marker inside the scrollable container; `follow(name, token)`
renders a tiny same-origin iframe (st.iframe; st.components.v1.html on older Streamlit) whose
script finds that marker in the parent page, walks up to its scrollable ancestor and sets scrollTop = scrollHeight. It scrolls only when
`token` changed since the last scroll (new entries arrived), so a reader can still scroll back
through the thread between ticks. If the parent page is unreachable the script does nothing.
"""

from __future__ import annotations

import json
import re

import streamlit as st

ANCHOR_PREFIX = "ac-scroll-"
RETRY_DELAYS_MS = (0, 120, 300, 700, 1400)  # the new entry may still be mounting when the script runs

_SCRIPT = """<script>
(function () {
  var name = %(name)s, token = %(token)s, delays = %(delays)s;
  var w, doc;
  try { w = window.parent; doc = w.document; if (!doc) return; } catch (e) { return; }
  var seen = w.__acaciaScroll = w.__acaciaScroll || {};
  if (seen[name] === token) return;
  seen[name] = token;
  function box() {
    var a = doc.getElementById(%(anchor)s);
    var el = a ? a.parentElement : null;
    while (el && el !== doc.body) {
      var oy = w.getComputedStyle(el).overflowY;
      if ((oy === "auto" || oy === "scroll") && el.getAttribute("data-testid") !== "stMain") return el;
      el = el.parentElement;
    }
    return null;
  }
  delays.forEach(function (ms) {
    setTimeout(function () {
      try { var b = box(); if (b && seen[name] === token) b.scrollTop = b.scrollHeight; } catch (e) { /* degrade */ }
    }, ms);
  });
})();
</script>"""


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", name)


def anchor_html(name: str) -> str:
    return f'<span id="{ANCHOR_PREFIX}{_safe(name)}" class="ac-scroll-anchor"></span>'


def anchor(name: str) -> None:
    """Call inside the scrollable container (its element container is hidden by theme CSS)."""
    st.markdown(anchor_html(name), unsafe_allow_html=True)


def script(name: str, token: str) -> str:
    safe = _safe(name)
    return _SCRIPT % {"name": json.dumps(safe), "token": json.dumps(str(token)),
                      "anchor": json.dumps(ANCHOR_PREFIX + safe), "delays": json.dumps(list(RETRY_DELAYS_MS))}


def follow(name: str, token: object) -> None:
    """Call after the container; scrolls it to the bottom whenever `token` changes."""
    markup = script(name, str(token))
    with st.container(key=f"ac-follow-{_safe(name)}"):  # taken out of the layout by theme CSS
        if hasattr(st, "iframe"):
            st.iframe(markup, height=1)
        else:  # Streamlit < 1.5x
            from streamlit.components.v1 import html as components_html

            components_html(markup, height=0)
