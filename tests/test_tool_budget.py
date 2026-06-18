"""Hardware-free guard: the sum of model-facing tool description tokens
across the FULL shipped registry must stay well under OpenAI Realtime's
16,384-token instructions+tools ceiling. Adding a verbose tool can't
silently blow that ceiling — this fails first, cheaply, in CI.

Token estimate is chars/4 (a cheap, dependency-free heuristic; no
tiktoken). It overestimates slightly for natural-language text, which is
the safe direction for a ceiling guard. We measure descriptions only
(the dominant term; the 29 descriptions are ~3.9k estimated tokens after
the Phase 1.6 pass, down from ~8.5k) — JSON schema overhead is small and
bounded.
"""
from __future__ import annotations

import types

from jasper.tools import ToolRegistry
from jasper.tools.bus import make_bus_tools
from jasper.tools.citibike import make_citibike_tools
from jasper.tools.packs import ToolDeps, register_packs
from jasper.tools.subway import make_subway_tools

# After the Phase 1.6 representative llm_description pass, the full 29-tool
# registry should stay around ~3.9k estimated description tokens. 6k leaves
# room for careful additions while catching a regression to the old ~8.5k
# footprint. chars/4 estimate, descriptions only.
MODEL_FACING_DESCRIPTION_TOKEN_BUDGET = 6_000

LLM_DESCRIPTION_TOOLS = {
    "get_current_time",
    "get_weather",
    "get_subway_arrivals",
    "get_bus_arrivals",
    "get_citibike_status",
    "spotify_play",
    "home_assistant",
    "flag_recent_issue",
}


def _full_registry() -> ToolRegistry:
    """Build the complete 29-tool registry hardware-free — every pack
    gate satisfied with lazy sentinel deps (factories capture deps in
    closures; none are invoked at build time)."""
    transit = []
    transit += list(make_subway_tools(object()))
    transit += list(make_bus_tools(types.SimpleNamespace(enabled=True)))
    transit += list(make_citibike_tools(types.SimpleNamespace(enabled=True)))
    deps = ToolDeps(
        volume_coordinator=None,
        renderer=None,
        router=None,
        weather=None,
        spotify_device_name="JTS",
        spotify_setup_url="",
        transit_tools=transit,
        ha=object(),
        timer_scheduler=object(),
        google_clients=types.SimpleNamespace(list_account_names=lambda: ["jasper"]),
        wake_event_store=object(),
    )
    reg = ToolRegistry()
    register_packs(reg, deps)
    return reg


def test_model_facing_descriptions_stay_under_budget():
    reg = _full_registry()
    assert len(reg.tools) == 29, "full registry should hold all 29 shipped tools"

    total_chars = sum(len(t.model_facing_description()) for t in reg.tools.values())
    est_tokens = total_chars // 4

    assert est_tokens < MODEL_FACING_DESCRIPTION_TOKEN_BUDGET, (
        f"model-facing tool descriptions estimate {est_tokens} tokens "
        f"(chars/4 over {len(reg.tools)} tools), at/over the "
        f"{MODEL_FACING_DESCRIPTION_TOKEN_BUDGET}-token budget. "
        "Trim a docstring or set a shorter @tool(llm_description=...)."
    )


def test_representative_tools_keep_rich_docs_and_short_model_text():
    reg = _full_registry()

    for name in LLM_DESCRIPTION_TOOLS:
        t = reg.get(name)
        assert t is not None, name
        assert t.llm_description, name
        assert t.description != t.model_facing_description(), name
        assert len(t.model_facing_description()) < len(t.description), name


def test_safety_and_routing_phrases_remain_model_facing():
    reg = _full_registry()

    ha = reg.get("home_assistant").model_facing_description()
    assert "Do NOT call for weather (get_weather)" in ha
    assert "Consequential actions" in ha
    assert "are NOT done in that call" in ha
    assert "home_assistant_confirm only after a clear yes" in ha

    weather = reg.get("get_weather").model_facing_description()
    assert "Call for weather, temperature, rain, sunrise, or sunset" in weather
    assert "Omit location for the speaker's default area" in weather
    assert "pass the full spoken place with qualifiers" in weather

    for name in (
        "get_subway_arrivals",
        "get_bus_arrivals",
        "get_citibike_status",
    ):
        desc = reg.get(name).model_facing_description()
        assert "Call fresh" in desc, name
