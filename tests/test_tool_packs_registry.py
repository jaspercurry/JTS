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

import types

from jasper.tools import ToolRegistry
from jasper.tools.audio import make_audio_tools
from jasper.tools.bus import make_bus_tools
from jasper.tools.calendar import make_calendar_tools
from jasper.tools.citibike import make_citibike_tools
from jasper.tools.diagnostic import make_diagnostic_tools
from jasper.tools.gmail import make_gmail_tools
from jasper.tools.home_assistant import make_home_assistant_tools
from jasper.tools.packs import (
    TOOL_PACKS,
    PackOutcome,
    ToolDeps,
    ToolPack,
    outcomes_to_state,
    register_packs,
)
from jasper.tools.spotify import make_spotify_tools
from jasper.tools.subway import make_subway_tools
from jasper.tools.time import make_time_tools
from jasper.tools.timer import make_timer_tools
from jasper.tools.transport import make_transport_tools
from jasper.tools.weather import make_weather_tools

# The documented legacy registration order — pinned as a literal so a
# reorder of TOOL_PACKS fails loudly.
LEGACY_PACK_ORDER = [
    "audio",
    "transport",
    "spotify",
    "weather",
    "transit",
    "home_assistant",
    "time",
    "timer",
    "calendar",
    "gmail",
    "diagnostic",
]

# The full shipped tool set, in registration order. Pinned so a stub that
# silently drops a pack (e.g. a google stub that fails its gate) trips the
# count/name assertions instead of passing with under-coverage.
EXPECTED_TOOL_NAMES = [
    "get_volume", "set_volume", "adjust_volume", "mute", "unmute",
    "next_track", "previous_track", "pause", "resume", "get_now_playing",
    "spotify_play", "spotify_play_latest_by_artist", "spotify_queue",
    "get_weather",
    "get_subway_arrivals", "get_bus_arrivals", "get_citibike_status",
    "home_assistant", "home_assistant_confirm",
    "get_current_time",
    "set_timer", "list_timers", "cancel_timer", "update_timer",
    "calendar_today_summary", "calendar_upcoming",
    "gmail_unread_summary", "gmail_read_thread",
    "flag_recent_issue",
]


def _transit_tools():
    """The 3 shipped transit tools, built hardware-free with lazy stubs.

    subway self-gates on `is None`; bus/citibike on `not dep.enabled`.
    The tool closures only touch the dep at call time, so a minimal stub
    that satisfies the gate yields the real tool schemas."""
    tools = []
    tools += list(make_subway_tools(object()))
    tools += list(make_bus_tools(types.SimpleNamespace(enabled=True)))
    tools += list(make_citibike_tools(types.SimpleNamespace(enabled=True)))
    return tools


def _full_deps():
    """Deps that satisfy EVERY pack gate, so the full 29-tool registry
    builds. Each factory captures its dep lazily, so sentinels suffice:
    timer needs only a non-None scheduler; google needs ≥1 account name."""
    google = types.SimpleNamespace(list_account_names=lambda: ["jasper"])
    return ToolDeps(
        volume_coordinator=None,
        renderer=None,
        router=None,
        weather=None,
        spotify_device_name="JTS",
        spotify_setup_url="",
        transit_tools=_transit_tools(),
        ha=object(),
        timer_scheduler=object(),
        google_clients=google,
        wake_event_store=object(),
    )


def _reference_registry(deps: ToolDeps) -> ToolRegistry:
    """Hand-written mirror of the LEGACY `_build_registry` body — the
    exact per-subsystem `for fn in make_X(...)` sequence with the same
    inline gates. This is the ground truth the data-driven walk must
    reproduce byte-for-byte."""
    reg = ToolRegistry()
    for fn in make_audio_tools(deps.volume_coordinator):
        reg.register(fn)
    for fn in make_transport_tools(deps.renderer, deps.router):
        reg.register(fn)
    for fn in make_spotify_tools(
        deps.router, deps.renderer, deps.spotify_device_name, deps.spotify_setup_url,
    ):
        reg.register(fn)
    for fn in make_weather_tools(deps.weather):
        reg.register(fn)
    for fn in deps.transit_tools:
        reg.register(fn)
    for fn in make_home_assistant_tools(deps.ha):
        reg.register(fn)
    for fn in make_time_tools():
        reg.register(fn)
    if deps.timer_scheduler is not None:
        for fn in make_timer_tools(deps.timer_scheduler):
            reg.register(fn)
    if deps.google_clients is not None and deps.google_clients.list_account_names():
        for fn in make_calendar_tools(deps.google_clients):
            reg.register(fn)
        for fn in make_gmail_tools(deps.google_clients):
            reg.register(fn)
    for fn in make_diagnostic_tools(deps.wake_event_store):
        reg.register(fn)
    return reg


def _serialize(reg: ToolRegistry) -> list[dict]:
    """Ordered, field-complete serialization of the registry — name +
    model-facing description + schema + providers + timeout, in
    registration order. One comparison covers every invariant at once."""
    return [t.to_manifest_entry() for t in reg.tools.values()]


def test_pack_order_matches_legacy_sequence():
    """A reorder of TOOL_PACKS must fail loudly — registration order is
    load-bearing (models over-rely on it)."""
    assert [p.name for p in TOOL_PACKS] == LEGACY_PACK_ORDER


def test_data_driven_walk_equals_legacy_sequence():
    """The core Slice 1 gate: the data-driven walk produces a registry
    byte-identical to the hand-written legacy sequence."""
    deps = _full_deps()

    walk_reg = ToolRegistry()
    register_packs(walk_reg, deps)

    ref_reg = _reference_registry(deps)

    # Full registry must be the complete shipped set, in order — guards
    # against a stub silently dropping a pack on BOTH sides.
    assert list(walk_reg.tools.keys()) == EXPECTED_TOOL_NAMES
    assert len(walk_reg.tools) == 29

    # Byte-identical ordered serialization (names, descriptions,
    # parameters, providers, timeouts, AND order — all at once).
    assert _serialize(walk_reg) == _serialize(ref_reg)


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
        _transit_tools(),         # transit_tools
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
    minimal = ToolDeps(
        volume_coordinator=None,
        renderer=None,
        router=None,
        weather=None,
        spotify_device_name="JTS",
        spotify_setup_url="",
        transit_tools=[],  # no transit configured
        ha=None,  # home_assistant self-gates -> []
        timer_scheduler=None,  # gate False -> no timer tools
        google_clients=types.SimpleNamespace(list_account_names=lambda: []),
        wake_event_store=None,  # diagnostic self-gates -> []
    )

    walk_reg = ToolRegistry()
    register_packs(walk_reg, minimal)
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
    deps = _full_deps()
    reg = ToolRegistry()
    register_packs(reg, deps, disabled=frozenset({"get_weather", "spotify_play"}))

    names = list(reg.tools.keys())
    assert "get_weather" not in names
    assert "spotify_play" not in names
    assert len(names) == len(EXPECTED_TOOL_NAMES) - 2
    # Survivors keep their relative order from the full shipped sequence.
    assert names == [n for n in EXPECTED_TOOL_NAMES if n not in {"get_weather", "spotify_play"}]


def test_explicit_disabled_set_never_reads_the_ssot_file(monkeypatch):
    """Passing an explicit `disabled` set must NOT touch the SSOT file —
    tests stay filesystem-independent. Monkeypatch the reader to blow up;
    the walk with an explicit set still registers the full 28."""
    import jasper.tool_state as tool_state

    def _boom(*_a, **_k):
        raise AssertionError("read_disabled_tools must not be called with explicit disabled=")

    monkeypatch.setattr(tool_state, "read_disabled_tools", _boom)
    reg = ToolRegistry()
    register_packs(reg, _full_deps(), disabled=frozenset())
    assert len(reg.tools) == len(EXPECTED_TOOL_NAMES)


def test_default_disabled_is_fail_safe(monkeypatch):
    """With no `disabled=` passed, the walk reads the SSOT fail-safe. A
    reader that returns the empty set (the missing-file case) registers
    all 28 — the no-disabled path stays identical to today."""
    import jasper.tools.packs as packs_mod

    monkeypatch.setattr(packs_mod, "read_disabled_tools", lambda: frozenset(), raising=False)
    # read_disabled_tools is imported lazily inside register_packs from
    # jasper.tool_state; patch there to be safe.
    import jasper.tool_state as tool_state

    monkeypatch.setattr(tool_state, "read_disabled_tools", lambda *_a, **_k: frozenset())
    reg = ToolRegistry()
    register_packs(reg, _full_deps())
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

    deps = _full_deps()
    reg = ToolRegistry()
    # Patch the module-level tuple the walk iterates.
    import jasper.tools.packs as packs_mod

    original = packs_mod.TOOL_PACKS
    try:
        packs_mod.TOOL_PACKS = packs
        register_packs(reg, deps)  # must not raise
    finally:
        packs_mod.TOOL_PACKS = original

    names = set(reg.tools.keys())
    assert "get_current_time" in names  # good_a registered
    assert "get_weather" in names       # good_b registered after the break


# --------------------------------------------------------------- outcomes
# These pin the observability record register_packs returns — the data
# that surfaces a silently-missing tool family via /state.voice.tool_packs
# and jasper-doctor's check_tool_packs, instead of journal-only.


def test_register_packs_returns_outcome_per_pack_in_order():
    """One PackOutcome per pack, in TOOL_PACKS order. With gate-satisfying
    deps every pack registers (none skipped/failed) and the tool_count sum
    equals the full shipped set."""
    deps = _full_deps()
    reg = ToolRegistry()
    # Explicit empty disabled set keeps the test hermetic (no SSOT file read).
    outcomes = register_packs(reg, deps, disabled=frozenset())

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
    minimal = ToolDeps(
        volume_coordinator=None, renderer=None, router=None, weather=None,
        spotify_device_name="JTS", spotify_setup_url="",
        transit_tools=[], ha=None,
        timer_scheduler=None,  # gate False -> skipped
        google_clients=types.SimpleNamespace(list_account_names=lambda: []),
        wake_event_store=None,
    )
    outcomes = {
        o.name: o
        for o in register_packs(ToolRegistry(), minimal, disabled=frozenset())
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
                ToolRegistry(), _full_deps(), disabled=frozenset(),
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
    deps = _full_deps()
    reg = ToolRegistry()
    # get_weather is the sole tool in the "weather" pack.
    outcomes = {
        o.name: o
        for o in register_packs(
            reg, deps, disabled=frozenset({"get_weather"}),
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
