from __future__ import annotations

from jasper.tools import ToolRegistry, tool


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
