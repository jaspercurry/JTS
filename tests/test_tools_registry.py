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
