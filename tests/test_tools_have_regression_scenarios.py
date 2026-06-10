"""Guard: every LLM voice tool has a regression scenario file mention.

AGENTS.md ("Test discipline — required, not optional"): every tool the
LLM can call — anything registered via a `make_*_tools` factory in
jasper/tools/ — ships with a regression scenario under
tests/voice_eval/regression/. *No exceptions.* A tool with no scenario
can't be reasoned about across model swaps or provider switches.

This guard is purely static: it extracts the `@tool(...)`-decorated
function names from jasper/tools/*.py (the name is what the LLM sees —
build_tool() uses fn.__name__) and asserts each one is mentioned
somewhere in the regression scenario sources. It NEVER imports or runs
the voice_eval suite — those scenarios open paid realtime LLM sessions
and are excluded from CI by design.

"Mentioned in a scenario file" is intentionally weak (a docstring
mention would satisfy it); the strong form — the scenario actually
asserts the tool's trajectory — is a human-review concern. The weak
form still catches the real failure mode: a tool landing with no
scenario at all.

_KNOWN_UNCOVERED is the two-sided allowlist for pre-existing gaps: an
entry whose tool gains a scenario (or stops existing) fails, so the
list can only shrink.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "jasper" / "tools"
REGRESSION_DIR = ROOT / "tests" / "voice_eval" / "regression"

# `@tool(...)` (any args) immediately decorating an async def — the
# shape every tool module uses. `build_tool` names the tool after the
# function, so the def name is the LLM-visible tool name. (A
# `registry.register(fn, name=...)` rename at a wiring point would
# escape this extraction; none exists today.)
_TOOL_DEF = re.compile(r"@tool\([^)]*\)\s*\n\s*async\s+def\s+([a-z0-9_]+)\s*\(")

# Pre-existing coverage gaps surfaced when this guard first ran
# (2026-06-10). Each violates the "no exceptions" rule and needs a
# scenario written — adding one here is documenting debt, not paying it.
# Two-sided: the entry fails once a scenario mentions the tool, so it
# must be removed in the same PR that adds the scenario.
_KNOWN_UNCOVERED = {
    # TODO: needs a scenario in tests/voice_eval/regression/test_timer.py
    # ("what timers are running?" → list_timers trajectory).
    "list_timers",
    # TODO: needs a scenario in tests/voice_eval/regression/test_spotify.py
    # ("queue up X" → spotify_queue trajectory; playback-side-effecting,
    # gate on JASPER_VOICE_EVAL_SKIP_PLAYBACK like its siblings).
    "spotify_queue",
}


def _tool_names() -> set[str]:
    names: set[str] = set()
    for py in sorted(TOOLS_DIR.glob("*.py")):
        names.update(_TOOL_DEF.findall(py.read_text(encoding="utf-8")))
    return names


def _scenario_text() -> str:
    return "\n".join(
        py.read_text(encoding="utf-8")
        for py in sorted(REGRESSION_DIR.glob("test_*.py"))
    )


def test_extraction_still_sees_the_tool_surface():
    """If the decorator shape changes and extraction finds (almost)
    nothing, every other assertion would pass vacuously — pin a floor."""
    names = _tool_names()
    assert len(names) >= 20, (
        f"only {len(names)} @tool defs extracted from jasper/tools/ — the "
        "_TOOL_DEF regex no longer matches the decorator shape; fix the "
        "extraction, don't trust this guard until it sees the real surface."
    )


def test_every_tool_has_a_regression_scenario_mention():
    text = _scenario_text()
    missing = sorted(
        name
        for name in _tool_names()
        if name not in _KNOWN_UNCOVERED
        and not re.search(rf"\b{re.escape(name)}\b", text)
    )
    assert not missing, (
        f"voice tool(s) with no mention in any tests/voice_eval/regression/ "
        f"scenario: {missing}. AGENTS.md test discipline: every tool the LLM "
        "can call ships with a regression scenario — no exceptions. Write "
        "one (mirror the three-assertion shape in regression/test_time.py); "
        "do not extend _KNOWN_UNCOVERED for new tools."
    )


def test_known_uncovered_list_is_not_stale():
    names = _tool_names()
    text = _scenario_text()
    for tool in sorted(_KNOWN_UNCOVERED):
        assert tool in names, (
            f"_KNOWN_UNCOVERED entry {tool!r} is no longer a registered tool "
            "— remove the entry."
        )
        assert not re.search(rf"\b{re.escape(tool)}\b", text), (
            f"_KNOWN_UNCOVERED entry {tool!r} now appears in a regression "
            "scenario — the gap is closed; remove the entry."
        )
