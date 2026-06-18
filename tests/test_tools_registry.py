from __future__ import annotations

import typing

from jasper.tools import DEFAULT_TOOL_TIMEOUT_SEC, ToolRegistry, build_tool, tool
from jasper.tools import _annotation_to_schema


def test_registers_and_builds_schema_from_type_hints():
    @tool()
    def set_volume(level_db: float) -> dict:
        """Set speaker volume in dB."""
        return {"ok": True, "level_db": level_db}

    reg = ToolRegistry()
    reg.register(set_volume)

    decls = reg.function_declarations()
    assert len(decls) == 1
    decl = decls[0]
    assert decl["name"] == "set_volume"
    assert decl["description"] == "Set speaker volume in dB."
    assert decl["parameters"]["type"] == "object"
    assert decl["parameters"]["properties"] == {"level_db": {"type": "number"}}
    assert decl["parameters"]["required"] == ["level_db"]


def test_optional_arg_not_required():
    @tool()
    def spotify_play(query: str, kind: str = "track") -> dict:
        """Search Spotify and play."""
        return {}

    reg = ToolRegistry()
    reg.register(spotify_play)
    decl = reg.function_declarations()[0]
    assert decl["parameters"]["required"] == ["query"]
    assert "kind" in decl["parameters"]["properties"]


def test_get_returns_none_for_unknown():
    reg = ToolRegistry()
    assert reg.get("missing") is None


def test_custom_name_overrides_function_name():
    @tool(name="play_song")
    def _spotify_play_impl(query: str) -> dict:
        """."""
        return {}

    reg = ToolRegistry()
    reg.register(_spotify_play_impl)
    assert reg.get("play_song") is not None
    assert reg.get("_spotify_play_impl") is None


def test_no_arg_tool_has_empty_properties():
    @tool()
    def get_volume() -> dict:
        """Return current volume."""
        return {}

    reg = ToolRegistry()
    reg.register(get_volume)
    decl = reg.function_declarations()[0]
    assert decl["parameters"]["properties"] == {}
    assert "required" not in decl["parameters"]


# ---- Provider-aware serialization (multi-provider voice loop) -------------


def test_openai_tools_returns_flat_realtime_shape():
    """OpenAI Realtime expects a flat tool schema (not the nested
    Chat-Completions shape). Each entry: {type, name, description,
    parameters}."""
    @tool()
    def set_volume(percent: int) -> dict:
        """Set speaker volume."""
        return {}

    reg = ToolRegistry()
    reg.register(set_volume)

    tools = reg.openai_tools()
    assert len(tools) == 1
    t = tools[0]
    assert t["type"] == "function"
    assert t["name"] == "set_volume"
    assert t["description"] == "Set speaker volume."
    assert t["parameters"]["type"] == "object"
    assert t["parameters"]["properties"] == {"percent": {"type": "integer"}}
    assert "function" not in t  # explicitly NOT the Chat-Completions wrapper


def test_function_declarations_defaults_to_gemini_provider():
    """Existing call sites pass no provider; default must be 'gemini'
    so the Gemini adapter keeps working without code changes."""
    @tool()
    def x() -> dict:
        """."""
        return {}

    reg = ToolRegistry()
    reg.register(x)
    assert reg.function_declarations() == reg.function_declarations(provider="gemini")


def test_provider_allowlist_hides_tool_from_other_providers():
    """A tool tagged with `providers={'openai'}` is visible to OpenAI,
    invisible to Gemini and Grok. Hidden tools don't appear in the
    declaration lists at all (the model can't see what it can't call)."""
    @tool(providers={"openai"})
    def analyze_image(image_url: str) -> dict:
        """OpenAI-only — needs the image input modality."""
        return {}

    @tool()
    def get_volume() -> dict:
        """Universal."""
        return {}

    reg = ToolRegistry()
    reg.register(analyze_image)
    reg.register(get_volume)

    gemini_names = {d["name"] for d in reg.function_declarations(provider="gemini")}
    openai_names = {t["name"] for t in reg.openai_tools(provider="openai")}
    grok_names = {t["name"] for t in reg.openai_tools(provider="grok")}

    assert gemini_names == {"get_volume"}
    assert openai_names == {"get_volume", "analyze_image"}
    assert grok_names == {"get_volume"}  # also hidden from Grok


def test_register_kwarg_overrides_decorator_providers():
    """The wiring point may want to gate a generic tool to one backend
    without editing the tool's source. The `providers` kwarg on
    `register()` wins over the `@tool()` annotation."""
    @tool()  # no allowlist on the decorator
    def maybe_special() -> dict:
        """."""
        return {}

    reg = ToolRegistry()
    reg.register(maybe_special, providers={"openai"})

    assert reg.function_declarations(provider="gemini") == []
    assert {t["name"] for t in reg.openai_tools()} == {"maybe_special"}


def test_provider_allowlist_with_multiple_providers():
    """A tool tagged with multiple providers is visible to each of them
    and hidden from the rest."""
    @tool(providers={"gemini", "openai"})
    def shared() -> dict:
        """."""
        return {}

    reg = ToolRegistry()
    reg.register(shared)

    assert {d["name"] for d in reg.function_declarations(provider="gemini")} == {"shared"}
    assert {t["name"] for t in reg.openai_tools(provider="openai")} == {"shared"}
    assert reg.openai_tools(provider="grok") == []


def test_get_returns_tool_even_if_invisible_to_active_provider():
    """`get(name)` is the dispatch path — once a provider has issued a
    function call, we still need to invoke it. The visibility filter
    only governs what the model is told about up front."""
    @tool(providers={"openai"})
    def restricted() -> dict:
        """."""
        return {}

    reg = ToolRegistry()
    reg.register(restricted)
    assert reg.get("restricted") is not None


# ---- Per-tool dispatch timeout --------------------------------------------


def test_build_tool_defaults_timeout_to_named_constant():
    """A tool with no `timeout=` on its decorator carries the default
    dispatch budget. This is the budget the session adapters apply at
    their `asyncio.wait_for` seam for the common (fast) tool."""
    @tool()
    def get_volume() -> dict:
        """."""
        return {}

    built = build_tool(get_volume)
    assert built.timeout == DEFAULT_TOOL_TIMEOUT_SEC
    assert DEFAULT_TOOL_TIMEOUT_SEC == 12.0


def test_tool_decorator_timeout_kwarg_overrides_default():
    """A slow-backend tool raises its dispatch budget via
    `@tool(timeout=...)`; build_tool reads it onto Tool.timeout."""
    @tool(timeout=90.0)
    async def slow_thing() -> dict:
        """."""
        return {}

    built = build_tool(slow_thing)
    assert built.timeout == 90.0


def test_registered_tool_preserves_timeout():
    """The registry's get() — the dispatch lookup path — returns the
    Tool with its declared timeout intact, so the seam can read it."""
    @tool(timeout=42.0)
    async def slow_thing() -> dict:
        """."""
        return {}

    reg = ToolRegistry()
    reg.register(slow_thing)
    assert reg.get("slow_thing").timeout == 42.0


# ---- Enriched schema generation (Literal enums + list items) --------------


def test_literal_str_annotation_yields_enum():
    """A `Literal['a', 'b']` param emits a JSON-Schema string with an
    `enum` constraint so the model is bounded to the valid values —
    previously it silently collapsed to a bare `{"type": "string"}`."""
    assert _annotation_to_schema(typing.Literal["a", "b"]) == {
        "type": "string",
        "enum": ["a", "b"],
    }


def test_literal_mixed_types_falls_back_to_string():
    """A heterogeneous literal can't be expressed as a string enum the
    providers will validate, so it degrades to a bare string rather than
    emitting a mixed-type enum."""
    assert _annotation_to_schema(typing.Literal["a", 1]) == {"type": "string"}


def test_list_annotation_yields_array_with_string_items():
    """`list[str]` emits an array schema whose items carry the element
    type — previously collapsed to a bare string."""
    assert _annotation_to_schema(list[str]) == {
        "type": "array",
        "items": {"type": "string"},
    }


def test_list_of_int_items_carry_element_type():
    assert _annotation_to_schema(list[int]) == {
        "type": "array",
        "items": {"type": "integer"},
    }


def test_tuple_homogeneous_yields_array():
    assert _annotation_to_schema(tuple[str, ...]) == {
        "type": "array",
        "items": {"type": "string"},
    }


def test_dict_annotation_stays_string():
    """No current tool declares a structured dict param, so dict stays a
    bare string rather than generating a speculative object schema."""
    assert _annotation_to_schema(dict) == {"type": "string"}


def test_existing_scalar_annotations_unaffected():
    """Strictly additive: str/int/float/bool still serialize as before."""
    assert _annotation_to_schema(str) == {"type": "string"}
    assert _annotation_to_schema(int) == {"type": "integer"}
    assert _annotation_to_schema(float) == {"type": "number"}
    assert _annotation_to_schema(bool) == {"type": "boolean"}


def test_literal_enum_rides_through_both_serializers():
    """The enriched schema must pass through both Gemini and OpenAI
    serializers unchanged — `enum` is standard JSON Schema and rides
    along with `parameters`. Mirrors spotify_play's `kind` param shape."""
    @tool()
    def play(query: str, kind: typing.Literal["artist", "track", "album",
                                               "playlist", "auto"] = "auto") -> dict:
        """Search and play."""
        return {}

    reg = ToolRegistry()
    reg.register(play)

    expected_kind = {
        "type": "string",
        "enum": ["artist", "track", "album", "playlist", "auto"],
    }

    gemini = reg.function_declarations()[0]
    assert gemini["parameters"]["properties"]["kind"] == expected_kind

    openai = reg.openai_tools()[0]
    assert openai["parameters"]["properties"]["kind"] == expected_kind


def test_real_spotify_play_kind_serializes_without_error():
    """Smoke check on the shipped spotify_play tool: its serialized
    schema must always include a `kind` enum, guarding the Literal
    enrichment on the production tool's signature."""
    from jasper.tools.spotify import make_spotify_tools

    reg = ToolRegistry()
    # Schema serialization only inspects signatures/annotations, so the
    # router/renderer collaborators are never invoked here — Nones suffice.
    for fn in make_spotify_tools(None, None, "JTS"):
        reg.register(fn)

    play = reg.get("spotify_play")
    assert play is not None
    assert "kind" in play.parameters["properties"]
    assert play.parameters["properties"]["kind"]["enum"] == [
        "auto", "artist", "track", "album", "playlist",
    ]


# ---------------------------------------------------------------------------
# Sync-fn registration warning
# ---------------------------------------------------------------------------
def test_build_tool_warns_once_for_non_coroutine_fn(caplog):
    """`dispatch_tool` runs a non-coroutine fn inline on the voice event
    loop, outside the per-tool `asyncio.wait_for` budget — a slow sync
    body stalls wake/audio with no timeout. Registration flags it once
    (event=tool.sync_fn) so the straggler is visible at daemon startup,
    not discovered as a mystery stall in production."""
    @tool()
    def blocking_lookup(q: str) -> dict:
        """Engineer wrote a sync tool by mistake."""
        return {"q": q}

    with caplog.at_level("WARNING", logger="jasper.tools"):
        built = build_tool(blocking_lookup)
    assert built.name == "blocking_lookup"
    warns = [
        r for r in caplog.records if "event=tool.sync_fn" in r.getMessage()
    ]
    assert len(warns) == 1
    assert "blocking_lookup" in warns[0].getMessage()


def test_build_tool_is_silent_for_coroutine_fn(caplog):
    @tool()
    async def fine_tool() -> dict:
        """The normal shape."""
        return {"ok": True}

    with caplog.at_level("WARNING", logger="jasper.tools"):
        build_tool(fine_tool)
    assert not [
        r for r in caplog.records if "event=tool.sync_fn" in r.getMessage()
    ]


# ---- llm_description seam (model-facing description override) --------------


def test_llm_description_defaults_to_docstring():
    """No `llm_description` on the decorator → the model sees the full
    docstring. Default (None) preserves today's behavior verbatim."""
    @tool()
    async def get_volume() -> dict:
        """Return the current speaker volume in dB."""
        return {}

    built = build_tool(get_volume)
    assert built.llm_description is None
    assert built.model_facing_description() == built.description
    assert built.model_facing_description() == "Return the current speaker volume in dB."


def test_llm_description_overrides_model_facing_only():
    """`@tool(llm_description=...)` changes what the MODEL sees in both
    serializers, but the docstring stays the engineer-facing
    `description` (the manifest's human/source-of-truth text)."""
    @tool(llm_description="Set volume.")
    async def set_volume(level_db: float) -> dict:
        """Set the speaker volume in dB. Long engineer-facing docstring
        with when-to-call rules, response shape, and voice-answer style
        that the model does not need verbatim."""
        return {}

    built = build_tool(set_volume)
    assert built.llm_description == "Set volume."
    # Engineer-facing + manifest description is the full docstring.
    assert built.description.startswith("Set the speaker volume in dB.")
    assert built.model_facing_description() == "Set volume."

    reg = ToolRegistry()
    reg.register(set_volume)
    assert reg.function_declarations()[0]["description"] == "Set volume."
    assert reg.openai_tools()[0]["description"] == "Set volume."
    # But the source Tool's description is unchanged.
    assert reg.get("set_volume").description.startswith("Set the speaker volume in dB.")


def test_user_prompt_override_updates_provider_serializers_only():
    """User-edited prompts are what providers see, but code defaults remain
    available for reset/metadata."""
    @tool(llm_description="Set volume.")
    async def set_volume(level_db: float) -> dict:
        """Set the speaker volume in dB. Long engineer-facing docstring
        with when-to-call rules, response shape, and voice-answer style."""
        return {}

    reg = ToolRegistry()
    reg.register(set_volume)
    reg.apply_prompt_overrides({
        "set_volume": "Use the user's custom volume prompt.",
        "missing_tool": "Ignored stale override.",
    })

    t = reg.get("set_volume")
    assert t is not None
    assert t.description.startswith("Set the speaker volume in dB.")
    assert t.default_model_facing_description() == "Set volume."
    assert t.model_facing_description() == "Use the user's custom volume prompt."
    assert t.prompt_customized() is True

    assert reg.function_declarations()[0]["description"] == (
        "Use the user's custom volume prompt."
    )
    assert reg.openai_tools()[0]["description"] == (
        "Use the user's custom volume prompt."
    )
