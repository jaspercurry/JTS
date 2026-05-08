from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable


_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


@dataclass
class Tool:
    """A registered voice tool.

    `providers` is None when the tool works across every voice provider
    (the common case — a tool that calls into our own subsystems). Set
    it to a frozenset of provider names (e.g. `frozenset({"openai"})`)
    when the tool depends on something only one provider can do (image
    input, MCP, model-specific built-ins). Tools with a non-None
    `providers` are filtered out of the per-provider tool list when the
    active provider isn't in the set, so the model literally cannot see
    or call them.
    """
    name: str
    description: str
    fn: Callable[..., Any]
    parameters: dict[str, Any]
    providers: frozenset[str] | None = None


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        providers: Iterable[str] | None = None,
    ) -> Tool:
        """Register `fn` as a tool. `providers` overrides any allowlist
        the `@tool(...)` decorator set on the function — useful when a
        wiring point needs to gate a generic tool to one backend without
        editing the tool itself."""
        tool = build_tool(fn, name=name)
        if providers is not None:
            tool = replace(tool, providers=frozenset(providers))
        self.tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def _visible_to(self, provider: str) -> list[Tool]:
        return [
            t for t in self.tools.values()
            if t.providers is None or provider in t.providers
        ]

    def function_declarations(
        self, *, provider: str = "gemini",
    ) -> list[dict[str, Any]]:
        """Gemini-shaped function declarations: {name, description, parameters}.

        Filtered to tools visible to `provider`. Default `"gemini"` keeps
        the existing call sites in `GeminiLiveConnection._build_config`
        working unchanged."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._visible_to(provider)
        ]

    def openai_tools(
        self, *, provider: str = "openai",
    ) -> list[dict[str, Any]]:
        """OpenAI Realtime tool schema (flat shape):
        ``{type: "function", name, description, parameters}``.

        Note this is the Realtime shape — different from Chat
        Completions, which nests under ``function: {...}``. Used by
        `OpenAIRealtimeConnection` and (via the same wire format) by
        the xAI Grok Voice Agent."""
        return [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._visible_to(provider)
        ]


def tool(
    name: str | None = None,
    *,
    providers: Iterable[str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Tag a function for registration.

    `providers` may be an iterable of provider names (`"gemini"`,
    `"openai"`, `"grok"`) — when set, the tool is hidden from any
    provider not in the set. None (default) means visible to every
    provider. Use with `ToolRegistry.register()`."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__jasper_tool_name__ = name or fn.__name__  # type: ignore[attr-defined]
        if providers is not None:
            fn.__jasper_tool_providers__ = frozenset(providers)  # type: ignore[attr-defined]
        return fn

    return decorator


def build_tool(fn: Callable[..., Any], *, name: str | None = None) -> Tool:
    declared = name or getattr(fn, "__jasper_tool_name__", None) or fn.__name__
    desc = (inspect.getdoc(fn) or "").strip().split("\n\n")[0] or declared
    params = _params_schema(fn)
    decl_providers = getattr(fn, "__jasper_tool_providers__", None)
    return Tool(
        name=declared,
        description=desc,
        fn=fn,
        parameters=params,
        providers=decl_providers,
    )


def _params_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if pname in {"self", "cls"}:
            continue
        annotation = hints.get(pname, str)
        properties[pname] = _annotation_to_schema(annotation)
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _annotation_to_schema(annotation: Any) -> dict[str, Any]:
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _annotation_to_schema(args[0])
    if annotation in _PY_TO_JSON:
        return {"type": _PY_TO_JSON[annotation]}
    if isinstance(annotation, type) and issubclass(annotation, str):
        return {"type": "string"}
    return {"type": "string"}
