"""Slice 1 gate — the data-driven tool-pack walk must produce a registry
byte-identical to the legacy hardcoded `_build_registry` sequence.

The hard invariant: same tool NAMES, descriptions, parameters, providers,
timeouts, AND the same registration ORDER (models over-rely on tool
ordering). We prove it by building the registry two ways — via
`register_packs(reg, deps)` and via a hand-written reference sequence
that mirrors the old `_build_registry` body — and comparing the ordered
serialized manifest lists. The manifest entry is the single richest
serialization (name + model-facing description + schema + providers +
timeout), so one comparison covers every field at once.

All factories build closures and capture deps lazily — none invoke the
deps at build time — so passing sentinel/None deps builds tools whose
schemas are identical regardless of dep values.
"""
from __future__ import annotations

import asyncio
import types

from jasper.tools import (
    PythonExecutor,
    Tool,
    ToolDefinition,
    ToolRegistry,
    dispatch_tool,
)
from jasper.tools.audio import make_audio_tools
from jasper.tools.calendar import make_calendar_tools
from jasper.tools.catalog import build_catalog
from jasper.tools.diagnostic import make_diagnostic_tools
from jasper.tools.gmail import make_gmail_tools
from jasper.tools.home_assistant import make_home_assistant_tools
from jasper.tools.packs import (
    TOOL_PACKS,
    CapabilityPack,
    CatalogPack,
    PackOutcome,
    ToolDeps,
    ToolPack,
    outcomes_to_state,
    register_packs,
)
from jasper.tools.spotify import make_spotify_tools
from jasper.tools.time import make_time_tools
from jasper.tools.timer import make_timer_tools
from jasper.tools.transport import make_transport_tools
from jasper.tools.weather import make_weather_tools
from tests._tool_pack_contract import (
    EXPECTED_TOOL_NAMES,
    LEGACY_PACK_ORDER,
    assert_duplicate_pack_fails_without_partial_registration,
    assert_duplicate_second_pack_fails_without_rolling_back_first,
    full_tool_deps,
    minimal_tool_deps,
    transit_tool_stubs,
)


def _reference_registry(deps: ToolDeps) -> ToolRegistry:
    """Hand-written mirror of the LEGACY `_build_registry` body — the
    exact per-subsystem `for fn in make_X(...)` sequence with the same
    inline gates. This is the ground truth the data-driven walk must
    reproduce byte-for-byte."""
    reg = ToolRegistry()
    for fn in make_audio_tools(deps.volume_coordinator):
        _register_tool_or_callable(reg, fn)
    for fn in make_transport_tools(deps.renderer, deps.router):
        _register_tool_or_callable(reg, fn)
    for fn in make_spotify_tools(
        deps.router, deps.renderer, deps.spotify_device_name, deps.spotify_setup_url,
    ):
        _register_tool_or_callable(reg, fn)
    for fn in make_weather_tools(deps.weather):
        _register_tool_or_callable(reg, fn)
    for fn in deps.transit_tools:
        _register_tool_or_callable(reg, fn)
    for fn in make_home_assistant_tools(deps.ha):
        _register_tool_or_callable(reg, fn)
    for fn in make_time_tools():
        _register_tool_or_callable(reg, fn)
    if deps.timer_scheduler is not None:
        for fn in make_timer_tools(deps.timer_scheduler):
            _register_tool_or_callable(reg, fn)
    if deps.google_clients is not None and deps.google_clients.list_account_names():
        for fn in make_calendar_tools(deps.google_clients):
            _register_tool_or_callable(reg, fn)
        for fn in make_gmail_tools(deps.google_clients):
            _register_tool_or_callable(reg, fn)
    for fn in make_diagnostic_tools(deps.wake_event_store):
        _register_tool_or_callable(reg, fn)
    return reg


def _register_tool_or_callable(reg: ToolRegistry, item) -> None:
    if isinstance(item, Tool):
        reg.register_tool(item)
    else:
        reg.register(item)


def _serialize(reg: ToolRegistry) -> list[dict]:
    """Ordered, field-complete serialization of the registry — name +
    model-facing description + schema + providers + timeout, in
    registration order. One comparison covers every invariant at once."""
    return [t.to_manifest_entry() for t in reg.tools.values()]


def _pack_named(name: str) -> CapabilityPack:
    return next(p for p in TOOL_PACKS if p.name == name)


class _ExplicitOnlyRegistry(ToolRegistry):
    def register(self, *_args, **_kwargs):  # pragma: no cover - failure path
        raise AssertionError("migrated pack must register explicit Tool objects")


def test_pack_order_matches_legacy_sequence():
    """A reorder of TOOL_PACKS must fail loudly — registration order is
    load-bearing (models over-rely on it)."""
    assert [p.name for p in TOOL_PACKS] == LEGACY_PACK_ORDER


def test_toolpack_is_compatibility_alias_for_capabilitypack():
    assert ToolPack is CapabilityPack


def test_data_driven_walk_equals_legacy_sequence():
    """The core Slice 1 gate: the data-driven walk produces a registry
    byte-identical to the hand-written legacy sequence."""
    deps = full_tool_deps()

    walk_reg = ToolRegistry()
    register_packs(walk_reg, deps, disabled=frozenset(), disabled_packs=frozenset())

    ref_reg = _reference_registry(deps)

    # Full registry must be the complete shipped set, in order — guards
    # against a stub silently dropping a pack on BOTH sides.
    assert list(walk_reg.tools.keys()) == EXPECTED_TOOL_NAMES
    assert len(walk_reg.tools) == 29

    # Byte-identical ordered serialization (names, descriptions,
    # parameters, providers, timeouts, AND order — all at once).
    assert _serialize(walk_reg) == _serialize(ref_reg)


def test_real_time_pack_uses_explicit_tool_boundary_end_to_end():
    """The shipped time pack is a copyable production example of explicit
    ToolDefinition + PythonExecutor authoring, not just a test fixture."""
    pack = _pack_named("time")
    reg = _ExplicitOnlyRegistry()

    outcomes = register_packs(
        reg,
        full_tool_deps(),
        disabled=frozenset(),
        disabled_packs=frozenset(),
        packs=(pack,),
    )

    assert outcomes == [PackOutcome("time", "registered", tool_count=1)]
    tool = reg.get("get_current_time")
    assert tool is not None
    assert isinstance(tool.definition, ToolDefinition)
    assert isinstance(tool.executor, PythonExecutor)
    assert reg.tool_packs == {"get_current_time": "time"}

    expected_parameters = {"type": "object", "properties": {}}
    assert tool.parameters == expected_parameters
    assert tool.labels == ("time", "utility")
    assert tool.providers is None
    assert tool.timeout == 12.0

    assert reg.function_declarations() == [{
        "name": "get_current_time",
        "description": tool.model_facing_description(),
        "parameters": expected_parameters,
    }]
    assert reg.openai_tools() == [{
        "type": "function",
        "name": "get_current_time",
        "description": tool.model_facing_description(),
        "parameters": expected_parameters,
    }]
    assert reg.to_manifest() == [tool.to_manifest_entry()]

    catalog = build_catalog(reg, frozenset(), packs=(pack,))
    row = catalog["tools"][0]
    assert row["name"] == "get_current_time"
    assert row["status"] == "active"
    assert row["pack"]["id"] == "time"
    assert row["labels"] == ["time", "utility"]
    assert row["parameters"] == expected_parameters

    out = asyncio.run(dispatch_tool(reg, "get_current_time", {}))
    assert set(out) == {"local_time", "timezone", "day_of_week"}
    assert len(out["local_time"]) == len("2026-05-21T15:47")
    assert "T" in out["local_time"]


def test_real_weather_pack_uses_explicit_tool_boundary_end_to_end():
    """The shipped API-backed weather pack also crosses the explicit
    boundary while preserving the WeatherClient call shape."""
    class FakeWeather:
        def __init__(self):
            self.calls = []

        async def get_weather(self, location: str = "") -> dict:
            self.calls.append(location)
            return {"location": location, "ok": True}

    weather = FakeWeather()
    pack = _pack_named("weather")
    deps = full_tool_deps(weather=weather)
    reg = _ExplicitOnlyRegistry()

    outcomes = register_packs(
        reg,
        deps,
        disabled=frozenset(),
        disabled_packs=frozenset(),
        packs=(pack,),
    )

    assert outcomes == [PackOutcome("weather", "registered", tool_count=1)]
    tool = reg.get("get_weather")
    assert tool is not None
    assert isinstance(tool.definition, ToolDefinition)
    assert isinstance(tool.executor, PythonExecutor)
    assert reg.tool_packs == {"get_weather": "weather"}

    expected_parameters = {
        "type": "object",
        "properties": {"location": {"type": "string"}},
    }
    assert tool.parameters == expected_parameters
    assert tool.labels == ("weather", "utility")
    assert tool.providers is None
    assert tool.timeout == 12.0
    assert "Tampa, Florida" in tool.description
    assert "next_rain_window" in tool.description

    assert reg.function_declarations() == [{
        "name": "get_weather",
        "description": tool.model_facing_description(),
        "parameters": expected_parameters,
    }]
    assert reg.openai_tools() == [{
        "type": "function",
        "name": "get_weather",
        "description": tool.model_facing_description(),
        "parameters": expected_parameters,
    }]
    assert reg.to_manifest() == [tool.to_manifest_entry()]

    catalog = build_catalog(reg, frozenset(), packs=(pack,))
    row = catalog["tools"][0]
    assert row["name"] == "get_weather"
    assert row["status"] == "active"
    assert row["pack"]["id"] == "weather"
    assert row["labels"] == ["weather", "utility"]
    assert row["parameters"] == expected_parameters

    assert asyncio.run(
        dispatch_tool(reg, "get_weather", {"location": "Tampa, Florida"}),
    ) == {"location": "Tampa, Florida", "ok": True}
    assert weather.calls == ["Tampa, Florida"]


def test_custom_capability_pack_registers_explicit_tool_boundary():
    """A contributor/no-code-style pack can hand the registry a built
    ToolDefinition + ToolExecutor instead of a decorated Python function.

    This pins the source-neutral boundary: the pack is the copyable unit, and
    runtime still flows through ToolRegistry + dispatch_tool."""
    class RecordingExecutor:
        def __init__(self):
            self.calls = []

        async def execute(self, args):
            self.calls.append(dict(args))
            return {"echo": args["text"]}

    executor = RecordingExecutor()
    explicit = Tool(
        definition=ToolDefinition(
            name="contrib_echo",
            description="Echo contributor input.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            labels=("contrib", "example"),
        ),
        executor=executor,
    )
    pack = CapabilityPack(
        "contrib_echo",
        lambda _d: [explicit],
        category="Utilities",
        catalog_pack=CatalogPack(
            "contrib-echo",
            "Contributor Echo",
            "Example contributor capability.",
        ),
    )

    reg = ToolRegistry()
    outcomes = register_packs(
        reg,
        full_tool_deps(),
        disabled=frozenset(),
        disabled_packs=frozenset(),
        packs=(pack,),
    )

    assert outcomes == [PackOutcome("contrib_echo", "registered", tool_count=1)]
    assert reg.tool_packs == {"contrib_echo": "contrib_echo"}
    assert reg.to_manifest()[0]["labels"] == ["contrib", "example"]

    assert asyncio.run(dispatch_tool(reg, "contrib_echo", {"text": "hi"})) == {
        "echo": "hi",
    }
    assert executor.calls == [{"text": "hi"}]


def test_register_packs_records_internal_pack_for_catalog_metadata():
    """The live registry keeps a tool -> internal pack index for catalog-only
    display metadata. It is not a runtime dispatch surface, but it lets the
    catalog map tools back to their category/display pack without hardcoding
    per-tool names."""
    reg = ToolRegistry()
    register_packs(
        reg,
        full_tool_deps(),
        disabled=frozenset(),
        disabled_packs=frozenset(),
    )

    assert reg.tool_packs["spotify_play"] == "spotify"
    assert reg.tool_packs["get_volume"] == "audio"
    assert reg.tool_packs["calendar_today_summary"] == "calendar"
    assert reg.tool_packs["gmail_unread_summary"] == "gmail"
    assert set(reg.tool_packs) == set(reg.tools)


def test_disabled_tools_are_removed_from_pack_index_too():
    reg = ToolRegistry()
    register_packs(
        reg,
        full_tool_deps(),
        disabled=frozenset({"spotify_play"}),
        disabled_packs=frozenset(),
    )

    assert "spotify_play" not in reg.tools
    assert "spotify_play" not in reg.tool_packs


def test_disabled_catalog_pack_removes_all_child_tools():
    reg = ToolRegistry()
    register_packs(
        reg,
        full_tool_deps(),
        disabled=frozenset(),
        disabled_packs=frozenset({"google"}),
    )

    assert "calendar_today_summary" not in reg.tools
    assert "calendar_upcoming" not in reg.tools
    assert "gmail_unread_summary" not in reg.tools
    assert "gmail_read_thread" not in reg.tools


def test_real_build_registry_wrapper_produces_full_set():
    """Pin the PRODUCTION entry point, not just the pack walk.

    The equality test above builds `ToolDeps` directly, so it cannot
    catch a future field-swap in `_build_registry`'s 14-param ->
    ToolDeps mapping (e.g. `weather=renderer`, or dropping
    `transit_tools` from the bundle) — the walk would still pass.
    Call the real wrapper with gate-satisfying sentinels and assert the
    full ordered shipped set. `spotify_router` is a truthy sentinel so
    `_build_router(cfg)` is never reached; `cfg` only needs the two
    spotify string fields the bundle reads."""
    from jasper.voice.daemon_main import _build_registry

    cfg = types.SimpleNamespace(spotify_device_name="JTS", spotify_setup_url="")
    reg = _build_registry(
        cfg,
        None,                     # camilla (accepted-but-unused)
        None,                     # renderer
        None,                     # weather
        transit_tool_stubs(),     # transit_tools
        None,                     # volume_coordinator
        spotify_router=object(),  # truthy -> skip _build_router(cfg)
        timer_scheduler=object(),
        google_clients=types.SimpleNamespace(list_account_names=lambda: ["jasper"]),
        ha=object(),
        wake_event_store=object(),
    )

    assert list(reg.tools.keys()) == EXPECTED_TOOL_NAMES
    assert len(reg.tools) == 29


def test_load_bearing_gates_drop_their_tools_when_unsatisfied():
    """The two lifted inline gates are load-bearing:

    - timer's factory does NOT self-gate on None, so the pack gate must
      drop it (else tools register against a None scheduler).
    - calendar/gmail need ≥1 linked account (stricter than the factory's
      own `clients is None`), else the model sees dead tools.

    With a minimal deps bundle (no scheduler, no accounts) the walk and
    the reference sequence must both register ZERO timer/calendar/gmail
    tools — and stay identical."""
    minimal = minimal_tool_deps()

    walk_reg = ToolRegistry()
    register_packs(
        walk_reg,
        minimal,
        disabled=frozenset(),
        disabled_packs=frozenset(),
    )
    ref_reg = _reference_registry(minimal)

    gated = {
        "set_timer", "list_timers", "cancel_timer", "update_timer",
        "calendar_today_summary", "calendar_upcoming",
        "gmail_unread_summary", "gmail_read_thread",
        "home_assistant", "flag_recent_issue",
    }
    assert gated.isdisjoint(walk_reg.tools.keys())
    # Only the un-gated, always-present tools survive (audio + transport +
    # spotify + weather + time).
    assert set(walk_reg.tools.keys()) == {
        "get_volume", "set_volume", "adjust_volume", "mute", "unmute",
        "next_track", "previous_track", "pause", "resume", "get_now_playing",
        "spotify_play", "spotify_play_latest_by_artist", "spotify_queue",
        "get_weather", "get_current_time",
    }
    assert _serialize(walk_reg) == _serialize(ref_reg)


def test_disabled_set_drops_named_tools_only():
    """`disabled=` filters by registered Tool.name at the single
    registration chokepoint: named tools vanish, all others survive in
    order, and the count drops by exactly the number disabled."""
    deps = full_tool_deps()
    reg = ToolRegistry()
    register_packs(
        reg,
        deps,
        disabled=frozenset({"get_weather", "spotify_play"}),
        disabled_packs=frozenset(),
    )

    names = list(reg.tools.keys())
    assert "get_weather" not in names
    assert "spotify_play" not in names
    assert len(names) == len(EXPECTED_TOOL_NAMES) - 2
    # Survivors keep their relative order from the full shipped sequence.
    assert names == [n for n in EXPECTED_TOOL_NAMES if n not in {"get_weather", "spotify_play"}]


def test_explicit_disabled_set_never_reads_the_ssot_file(monkeypatch):
    """Passing an explicit `disabled` set must NOT touch the SSOT file —
    tests stay filesystem-independent. Monkeypatch the reader to blow up;
    the walk with an explicit set still registers the full set."""
    import jasper.tool_state as tool_state

    def _boom(*_a, **_k):
        raise AssertionError("read_tool_state must not be called with explicit state")

    monkeypatch.setattr(tool_state, "read_tool_state", _boom)
    reg = ToolRegistry()
    register_packs(
        reg,
        full_tool_deps(),
        disabled=frozenset(),
        disabled_packs=frozenset(),
    )
    assert len(reg.tools) == len(EXPECTED_TOOL_NAMES)


def test_default_disabled_is_fail_safe(monkeypatch):
    """With no `disabled=` passed, the walk reads the SSOT fail-safe. A
    reader that returns the empty set (the missing-file case) registers
    the full set — the no-disabled path stays identical to today."""
    import jasper.tool_state as tool_state

    monkeypatch.setattr(tool_state, "read_tool_state", lambda *_a, **_k: tool_state.ToolState())
    reg = ToolRegistry()
    register_packs(reg, full_tool_deps())
    assert len(reg.tools) == len(EXPECTED_TOOL_NAMES)
    assert list(reg.tools.keys()) == EXPECTED_TOOL_NAMES


def test_broken_pack_is_isolated_other_packs_still_register():
    """Fault isolation: a pack whose `build` raises is skipped and
    logged; every other pack still registers. One broken tool module
    must not crash the daemon — mirrors transit.active_transit's
    per-provider guard."""
    def _boom(_d):
        raise RuntimeError("simulated import/factory failure")

    packs = (
        ToolPack("good_a", lambda _d: make_time_tools()),
        ToolPack("broken", _boom),
        ToolPack("good_b", lambda _d: make_weather_tools(None)),
    )

    deps = full_tool_deps()
    reg = ToolRegistry()
    # Patch the module-level tuple the walk iterates.
    import jasper.tools.packs as packs_mod

    original = packs_mod.TOOL_PACKS
    try:
        packs_mod.TOOL_PACKS = packs
        register_packs(
            reg,
            deps,
            disabled=frozenset(),
            disabled_packs=frozenset(),
        )  # must not raise
    finally:
        packs_mod.TOOL_PACKS = original

    names = set(reg.tools.keys())
    assert "get_current_time" in names  # good_a registered
    assert "get_weather" in names       # good_b registered after the break


def test_registration_failure_rolls_back_partial_pack_and_continues():
    """Fault isolation covers both build and registration.

    A contributor pack can return explicit Tool objects now. If one item
    registers and a later item is malformed, the pack must not leave a
    half-registered tool behind or erase a same-named tool from an earlier
    pack.
    """
    class StaticExecutor:
        def __init__(self, value: str):
            self.value = value

        async def execute(self, _args):
            return {"value": self.value}

    def explicit_tool(name: str, value: str) -> Tool:
        return Tool(
            definition=ToolDefinition(
                name=name,
                description=f"{value} explicit test tool.",
                parameters={"type": "object", "properties": {}},
            ),
            executor=StaticExecutor(value),
        )

    packs = (
        ToolPack("seed", lambda _d: [explicit_tool("shared_tool", "seed")]),
        ToolPack(
            "broken",
            lambda _d: [
                explicit_tool("broken_tool", "broken"),
                object(),  # not callable and not a Tool: registration fails
            ],
        ),
        ToolPack("tail", lambda _d: make_time_tools()),
    )

    reg = ToolRegistry()
    outcomes = {
        o.name: o
        for o in register_packs(
            reg,
            full_tool_deps(),
            disabled=frozenset(),
            disabled_packs=frozenset(),
            packs=packs,
        )
    }

    assert outcomes["seed"].status == "registered"
    assert outcomes["broken"].status == "failed"
    assert outcomes["tail"].status == "registered"
    assert "get_current_time" in reg.tools
    assert "broken_tool" not in reg.tools
    assert reg.tool_packs["shared_tool"] == "seed"

    import asyncio

    assert asyncio.run(dispatch_tool(reg, "shared_tool", {})) == {
        "value": "seed",
    }


def test_duplicate_tool_name_fails_pack_and_rolls_back_partial_registration():
    """Duplicate names are a pack-contract failure, never last-writer-wins."""
    duplicate = CapabilityPack(
        "duplicate",
        lambda _d: [*make_time_tools(), *make_time_tools()],
    )

    assert_duplicate_pack_fails_without_partial_registration(
        pack=duplicate,
        deps=object(),
        expected_rolled_back_names={"get_current_time"},
    )


def test_duplicate_tool_name_in_later_pack_preserves_prior_pack():
    """A later pack collision must fail only that pack and keep earlier tools."""
    first = CapabilityPack("first_time", lambda _d: make_time_tools())
    duplicate = CapabilityPack(
        "duplicate_weather_then_time",
        lambda _d: [*make_weather_tools(None), *make_time_tools()],
    )

    assert_duplicate_second_pack_fails_without_rolling_back_first(
        first_pack=first,
        duplicate_pack=duplicate,
        deps=object(),
        expected_remaining_names={"get_current_time"},
        expected_rolled_back_names={"get_weather"},
    )


# --------------------------------------------------------------- outcomes
# These pin the observability record register_packs returns — the data
# that surfaces a silently-missing tool family via /state.voice.tool_packs
# and jasper-doctor's check_tool_packs, instead of journal-only.


def test_register_packs_returns_outcome_per_pack_in_order():
    """One PackOutcome per pack, in TOOL_PACKS order. With gate-satisfying
    deps every pack registers (none skipped/failed) and the tool_count sum
    equals the full shipped set."""
    deps = full_tool_deps()
    reg = ToolRegistry()
    # Explicit empty disabled set keeps the test hermetic (no SSOT file read).
    outcomes = register_packs(
        reg,
        deps,
        disabled=frozenset(),
        disabled_packs=frozenset(),
    )

    assert [o.name for o in outcomes] == [p.name for p in TOOL_PACKS]
    assert all(o.status == "registered" for o in outcomes)
    assert all(o.error is None for o in outcomes)
    # tool_count is per-pack (post-disable); the total is the full registry.
    # Pin to the canonical EXPECTED_TOOL_NAMES so a tool added/removed on
    # main updates one place, not a literal here.
    assert (
        sum(o.tool_count for o in outcomes)
        == len(reg.tools)
        == len(EXPECTED_TOOL_NAMES)
    )


def test_register_packs_marks_gated_off_packs_skipped():
    """A pack whose gate predicate returns False is recorded "skipped"
    (expected, not a fault) with zero tools — distinct from a build
    failure."""
    minimal = minimal_tool_deps()
    outcomes = {
        o.name: o
        for o in register_packs(
            ToolRegistry(),
            minimal,
            disabled=frozenset(),
            disabled_packs=frozenset(),
        )
    }
    for name in ("timer", "calendar", "gmail"):
        assert outcomes[name].status == "skipped"
        assert outcomes[name].tool_count == 0
    # A self-gating factory that returns [] still "registered" (it built
    # without raising) — only the explicit gate produces "skipped".
    assert outcomes["home_assistant"].status == "registered"
    assert outcomes["home_assistant"].tool_count == 0


def test_register_packs_marks_failed_pack_with_error():
    """A pack whose build raises is recorded "failed" with the exception
    repr — the alarm condition check_tool_packs fails on. Sibling packs
    still register (fault isolation)."""
    def _boom(_d):
        raise RuntimeError("simulated import/factory failure")

    packs = (
        ToolPack("good_a", lambda _d: make_time_tools()),
        ToolPack("broken", _boom),
        ToolPack("good_b", lambda _d: make_weather_tools(None)),
    )
    import jasper.tools.packs as packs_mod
    original = packs_mod.TOOL_PACKS
    try:
        packs_mod.TOOL_PACKS = packs
        outcomes = {
            o.name: o
            for o in register_packs(
                ToolRegistry(),
                full_tool_deps(),
                disabled=frozenset(),
                disabled_packs=frozenset(),
            )
        }
    finally:
        packs_mod.TOOL_PACKS = original

    assert outcomes["good_a"].status == "registered"
    assert outcomes["good_b"].status == "registered"
    assert outcomes["broken"].status == "failed"
    assert "simulated import/factory failure" in (outcomes["broken"].error or "")


def test_outcome_tool_count_reflects_user_disabled_removals():
    """A user-disabled tool is removed from the registry, so its pack's
    tool_count drops accordingly and the pack still reports "registered"
    (a user choice is not a build failure). Pins the docstring invariant
    sum(tool_count) == len(registry.tools) under the disable feature."""
    deps = full_tool_deps()
    reg = ToolRegistry()
    # get_weather is the sole tool in the "weather" pack.
    outcomes = {
        o.name: o
        for o in register_packs(
            reg,
            deps,
            disabled=frozenset({"get_weather"}),
            disabled_packs=frozenset(),
        )
    }
    assert outcomes["weather"].status == "registered"
    assert outcomes["weather"].tool_count == 0  # its only tool was disabled
    assert "get_weather" not in reg.tools
    assert (
        sum(o.tool_count for o in outcomes.values())
        == len(reg.tools)
        == len(EXPECTED_TOOL_NAMES) - 1
    )


def test_outcomes_to_state_is_json_shaped():
    """The serializer is the single home for the wire shape consumed by
    /state.voice.tool_packs and the doctor."""
    state = outcomes_to_state([
        PackOutcome("audio", "registered", tool_count=5),
        PackOutcome("timer", "skipped"),
        PackOutcome("broken", "failed", error="RuntimeError('x')"),
    ])
    assert state == [
        {"name": "audio", "status": "registered", "tool_count": 5, "error": None},
        {"name": "timer", "status": "skipped", "tool_count": 0, "error": None},
        {"name": "broken", "status": "failed", "tool_count": 0,
         "error": "RuntimeError('x')"},
    ]


def test_content_bearing_tools_pin_log_redaction():
    """Privacy invariant the byte-identical manifest gate does NOT cover:
    `to_manifest_entry()` omits `log_payload`/`log_args`, so a regression that
    started logging an email body or a close-to-verbatim "unlock the front
    door" utterance at INFO would slip through the equality comparison. Pin the
    redaction set explicitly instead. `log_payload=False` redacts the tool
    RESULT preview; `log_args=False` redacts the call ARGS — the home-control
    tools redact both because their args carry the user's request near-verbatim.
    Asserting the exact set catches drift in BOTH directions: a content tool
    silently losing redaction, or an unrelated tool unexpectedly gaining it."""
    reg = ToolRegistry()
    register_packs(
        reg, full_tool_deps(), disabled=frozenset(), disabled_packs=frozenset(),
    )
    redact_payload = {n for n, t in reg.tools.items() if not t.log_payload}
    redact_args = {n for n, t in reg.tools.items() if not t.log_args}
    assert redact_payload == {
        "calendar_today_summary", "calendar_upcoming",
        "gmail_unread_summary", "gmail_read_thread",
        "home_assistant", "home_assistant_confirm",
    }
    assert redact_args == {"home_assistant", "home_assistant_confirm"}
