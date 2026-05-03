from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass, field
from typing import Any, Callable


_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[..., Any]
    parameters: dict[str, Any]


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, fn: Callable[..., Any], *, name: str | None = None) -> Tool:
        tool = build_tool(fn, name=name)
        self.tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def function_declarations(self) -> list[dict[str, Any]]:
        """Gemini-shaped function declarations: {name, description, parameters}."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self.tools.values()
        ]


def tool(name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Tag a function for registration. Use with ToolRegistry.register()."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__jasper_tool_name__ = name or fn.__name__  # type: ignore[attr-defined]
        return fn

    return decorator


def build_tool(fn: Callable[..., Any], *, name: str | None = None) -> Tool:
    declared = name or getattr(fn, "__jasper_tool_name__", None) or fn.__name__
    desc = (inspect.getdoc(fn) or "").strip().split("\n\n")[0] or declared
    params = _params_schema(fn)
    return Tool(name=declared, description=desc, fn=fn, parameters=params)


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
