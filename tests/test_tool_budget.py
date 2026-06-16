"""Hardware-free guard: the sum of model-facing tool description tokens
across the FULL shipped registry must stay well under OpenAI Realtime's
16,384-token instructions+tools ceiling. Adding a verbose tool can't
silently blow that ceiling — this fails first, cheaply, in CI.

Token estimate is chars/4 (a cheap, dependency-free heuristic; no
tiktoken). It overestimates slightly for natural-language text, which is
the safe direction for a ceiling guard. We measure descriptions only
(the dominant term; the 29 descriptions total ~8.2k tokens today) — JSON
schema overhead is small and bounded.
"""
from __future__ import annotations

import types

from jasper.tools import ToolRegistry
from jasper.tools.bus import make_bus_tools
from jasper.tools.citibike import make_citibike_tools
from jasper.tools.packs import ToolDeps, register_packs
from jasper.tools.subway import make_subway_tools

# ~8.2k tokens today; 13k leaves clear headroom under OpenAI Realtime's
# 16,384 instructions+tools ceiling while still catching a runaway
# addition. chars/4 estimate, descriptions only.
MODEL_FACING_DESCRIPTION_TOKEN_BUDGET = 13_000


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
