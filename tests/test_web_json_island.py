"""The shared JSON data-island helper + its conventions guard.

`jasper.web._common.json_island` is the way a wizard page hands data to
its ES module: it owns the json.dumps + `<`/`>`/`&` JSON-unicode escaping
that keeps untrusted strings from closing the inline ``<script>`` element
early. The conventions test at the bottom keeps the next wizard from
hand-rolling an island without the guard.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from jasper.web._common import json_island

WEB_DIR = Path(__file__).resolve().parent.parent / "jasper" / "web"


def test_emits_typed_island_element():
    out = json_island("my-data", {"a": 1})
    assert out.startswith('<script type="application/json" id="my-data">')
    assert out.endswith("</script>")


def _island_body(out: str) -> str:
    m = re.fullmatch(r"<script[^>]*>(.*)</script>", out, flags=re.S)
    assert m, out
    return m.group(1)


@pytest.mark.parametrize("payload", [
    {"agent": "</script><img src=x onerror=alert(1)>"},
    {"label": "<!--<script>"},
    {"x": 'quotes " and \\ slashes'},
    {"nested": {"list": ["</script>", 3, None, True]}},
    "bare string with </script>",
])
def test_round_trips_and_never_contains_breakout(payload):
    out = json_island("d", payload)
    body = _island_body(out)
    # No literal angle bracket survives in the serialized body, so the
    # island cannot be closed early and cannot flip the parser into the
    # escaped-script-data state.
    assert "<" not in body and ">" not in body
    assert json.loads(body) == payload


def test_ampersand_is_escaped_too():
    body = _island_body(json_island("d", {"q": "a&b"}))
    assert "&" not in body
    assert json.loads(body) == {"q": "a&b"}


def test_element_id_is_attribute_escaped():
    out = json_island('x" onload="evil', {"a": 1})
    assert '"x&quot; onload=&quot;evil"' in out
    assert ' onload="evil"' not in out


def test_no_wizard_hand_rolls_a_json_island():
    """Every server-rendered application/json island must come from
    `json_island()`."""
    offenders = []
    for py in sorted(WEB_DIR.glob("*.py")):
        if py.name == "_common.py":
            continue
        text = py.read_text(encoding="utf-8")
        if 'type="application/json"' in text:
            offenders.append(py.name)
    assert not offenders, (
        f"hand-rolled application/json island(s) in {offenders}; "
        "use jasper.web._common.json_island()"
    )


def test_old_close_sequence_guard_idiom_is_gone():
    offenders = []
    for py in sorted(WEB_DIR.glob("*.py")):
        text = py.read_text(encoding="utf-8")
        if '.replace("</"' in text:
            offenders.append(py.name)
    assert not offenders, (
        f"manual </ escape in {offenders}; use json_island() instead"
    )
