"""Voice-tool registry + per-provider schema serializers.

Tool factories under ``jasper.tools.*`` register callables via
``@tool(...)`` and ``ToolRegistry.register(fn)``; the registry then
serializes them to the provider-specific shape:

- ``function_declarations()`` — Gemini's ``Tool(function_declarations=[...])``
- ``openai_tools()`` — OpenAI Realtime's flat
  ``{type: "function", name, description, parameters}``. Grok's
  voice agent inherits this shape unchanged.

The LLM-facing description for each tool is the function's full
cleaned docstring (``build_tool`` sends ``inspect.getdoc(fn).strip()``
verbatim). Per-tool conditional rules — when to call, when NOT to
call, response shape, voice-answer style — live in the docstring
and are sent to the model with the tool. Engineer-only notes
(implementation details, TODOs) belong in ``#`` comments or this
module docstring, NOT in tool function docstrings.

When adding or editing a tool, read ``docs/HANDOFF-prompting.md``
first — it covers tool description style, where conditional rules
should live (here, not in ``SYSTEM_INSTRUCTION``), and the
cross-provider principles that hold for any prompt edit.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time as _time
import typing
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


# Default wall-clock budget for a single tool dispatch (the
# `asyncio.wait_for` cap the session adapters apply around each tool
# coroutine). 12s gives async tool calls (httpx HTTP + parsing)
# headroom on a busy Pi event loop where ONNX wake-word + audio
# resampling + the realtime WebSocket compete for CPU; anything slower
# usually means the upstream API is genuinely failing and we'd rather
# report the timeout than hang the session further. A tool whose
# backend is legitimately slow (e.g. an LLM-backed Home Assistant agent
# taking 30-60s) overrides this via the `timeout=` kwarg on `@tool()`.
# This is the ONLY place the 12s literal lives — the dispatch seams read
# `tool.timeout`.
DEFAULT_TOOL_TIMEOUT_SEC = 12.0


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
    # Per-tool dispatch budget (seconds) applied at the session adapters'
    # `asyncio.wait_for` seam. Defaults to `DEFAULT_TOOL_TIMEOUT_SEC`;
    # raise it for a tool whose backend is legitimately slow.
    timeout: float = DEFAULT_TOOL_TIMEOUT_SEC
    # Whether INFO-level tool dispatch logs may include a repr preview
    # of the returned payload. Content-bearing tools opt out so
    # journald keeps timing/shape diagnostics without message bodies.
    log_payload: bool = True


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
    timeout: float | None = None,
    log_payload: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Tag a function for registration.

    `providers` may be an iterable of provider names (`"gemini"`,
    `"openai"`, `"grok"`) — when set, the tool is hidden from any
    provider not in the set. None (default) means visible to every
    provider.

    `timeout` is the per-tool dispatch budget in seconds applied at the
    session adapters' `asyncio.wait_for` seam. None (default) keeps
    `DEFAULT_TOOL_TIMEOUT_SEC`; raise it for a tool whose backend is
    legitimately slow (e.g. an LLM-backed Home Assistant agent). Use
    `log_payload=False` keeps the INFO dispatch line redacted for
    content-bearing tool results.

    Use with `ToolRegistry.register()`."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__jasper_tool_name__ = name or fn.__name__  # type: ignore[attr-defined]
        if providers is not None:
            fn.__jasper_tool_providers__ = frozenset(providers)  # type: ignore[attr-defined]
        if timeout is not None:
            fn.__jasper_tool_timeout__ = timeout  # type: ignore[attr-defined]
        fn.__jasper_tool_log_payload__ = log_payload  # type: ignore[attr-defined]
        return fn

    return decorator


def build_tool(fn: Callable[..., Any], *, name: str | None = None) -> Tool:
    """Build a `Tool` from a decorated function. The full cleaned
    docstring becomes the LLM-facing description — when-to-call
    guidance, response shape, voice-answer style, and conditional
    output rules all live in the docstring and are sent to the
    model verbatim (see docs/HANDOFF-prompting.md for the
    rationale). Engineer-only notes (dev TODOs, implementation
    details) belong in `#` comments or the module docstring, not
    in the tool's function docstring.

    This does NOT validate or coerce the tool's return shape. The
    JTS upstream-failure contract (a tool returns
    ``{error: <speakable string>}`` on a hard failure and never an
    empty success payload) is a documented convention enforced by
    each tool's docstring, not by a base class here — see
    docs/HANDOFF-prompting.md "The upstream-failure contract"."""
    declared = name or getattr(fn, "__jasper_tool_name__", None) or fn.__name__
    desc = (inspect.getdoc(fn) or "").strip() or declared
    params = _params_schema(fn)
    decl_providers = getattr(fn, "__jasper_tool_providers__", None)
    decl_timeout = getattr(fn, "__jasper_tool_timeout__", DEFAULT_TOOL_TIMEOUT_SEC)
    decl_log_payload = getattr(fn, "__jasper_tool_log_payload__", True)
    if not asyncio.iscoroutinefunction(fn):
        # One line per registration (daemon startup), not per dispatch.
        # `dispatch_tool` runs a non-coroutine fn INLINE on the voice
        # event loop and its `asyncio.wait_for` budget only covers
        # awaitables — a slow sync body stalls wake detection and audio
        # playout with no timeout. Every shipped tool is `async def`
        # (blocking backends go through asyncio.to_thread inside the
        # tool); this flags the stragglers before they ship.
        logger.warning(
            "event=tool.sync_fn tool=%s — fn is not a coroutine function; "
            "it runs inline on the event loop with no %.0fs dispatch "
            "timeout. Make it `async def` and wrap blocking work in "
            "asyncio.to_thread.",
            declared, DEFAULT_TOOL_TIMEOUT_SEC,
        )
    return Tool(
        name=declared,
        description=desc,
        fn=fn,
        parameters=params,
        providers=decl_providers,
        timeout=decl_timeout,
        log_payload=decl_log_payload,
    )


def _payload_preview(tool: Tool, payload: dict[str, Any]) -> str:
    preview = repr(payload)
    if not tool.log_payload:
        return f"<redacted len={len(preview)}>"
    if len(preview) > 240:
        preview = preview[:237] + "..."
    return preview


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
    if origin is typing.Literal:
        members = typing.get_args(annotation)
        # Enum is only meaningful for a homogeneous string literal — that's
        # the only kind any current tool declares. Mixed-type literals fall
        # back to a bare string schema rather than emitting a heterogeneous
        # enum the providers can't validate.
        if members and all(isinstance(m, str) for m in members):
            return {"type": "string", "enum": list(members)}
        return {"type": "string"}
    if origin in (list, tuple):
        item_args = typing.get_args(annotation)
        if item_args:
            return {"type": "array", "items": _annotation_to_schema(item_args[0])}
        return {"type": "array"}
    if annotation in _PY_TO_JSON:
        return {"type": _PY_TO_JSON[annotation]}
    if isinstance(annotation, type) and issubclass(annotation, str):
        return {"type": "string"}
    # dict and any other unrecognized annotation stay a bare string — no
    # current tool declares a structured dict param, so object-schema
    # generation would be speculative.
    return {"type": "string"}


async def dispatch_tool(
    registry: ToolRegistry, name: str, args: dict[str, Any],
) -> dict[str, Any]:
    """Run one model-issued tool call; return the JSON-able payload dict
    the model should see back.

    This is the single, cross-provider home for the tool-dispatch
    contract. Every session adapter — Gemini, OpenAI, and Grok via the
    OpenAI subclass — routes through it, so the behaviour the model
    observes when a tool runs cannot drift between providers. Each
    adapter keeps only its genuinely provider-specific parts: parsing the
    call's arguments (Gemini hands us a dict, OpenAI a JSON string) and
    packaging the returned `payload` onto the wire
    (`types.FunctionResponse` vs a `conversation.item.create` event).

    The contract owned here:
      * unknown tool   -> ``{"error": "unknown tool <name>"}``
      * per-tool timeout -> awaited with ``tool.timeout`` (default
                          ``DEFAULT_TOOL_TIMEOUT_SEC``); on expiry returns
                          ``{"error": "<name> timed out"}`` rather than
                          hanging the session
      * any other error  -> ``{"error": str(exc)}``
      * dict result    -> passed straight through
      * scalar result  -> wrapped as ``{"value": <result>}`` so the model
                          never sees a bare scalar
    plus the structured timing logs (``tool <name> start`` / ``fn done``
    / ``TIMED OUT`` / ``RAISED``) journalctl shows for every call —
    identical across providers.

    A future provider adapter gets timeout, logging, and error-shaping
    for free by calling this — see docs/HANDOFF-voice-providers.md
    "Adding a fourth provider".
    """
    tool = registry.get(name)
    if tool is None:
        logger.warning("tool %s start args=%s → unknown tool", name, args)
        return {"error": f"unknown tool {name}"}

    logger.info("tool %s start args=%s", name, args)
    t_fn = _time.monotonic()
    try:
        out = tool.fn(**args)
        if asyncio.iscoroutine(out):
            # Anything slower than the tool's budget probably means the
            # upstream API is genuinely failing — report the timeout
            # rather than hang the session further.
            out = await asyncio.wait_for(out, timeout=tool.timeout)
        # Pass dict outputs straight through; only wrap scalars so the
        # model doesn't see {"result": {"ok": true}}.
        payload = out if isinstance(out, dict) else {"value": out}
        fn_ms = (_time.monotonic() - t_fn) * 1000
        # Truncate the payload preview — weather/subway responses can be
        # 4-8 KB and flood the journal. Content-bearing tools redact the
        # preview entirely but keep length/timing diagnostics.
        preview = _payload_preview(tool, payload)
        logger.info("tool %s fn done in %.0fms ok payload=%s", name, fn_ms, preview)
        return payload
    except asyncio.TimeoutError:
        fn_ms = (_time.monotonic() - t_fn) * 1000
        logger.warning("tool %s fn TIMED OUT after %.0fms", name, fn_ms)
        return {"error": f"{name} timed out"}
    except Exception as e:  # noqa: BLE001
        fn_ms = (_time.monotonic() - t_fn) * 1000
        logger.warning("tool %s fn RAISED after %.0fms: %s", name, fn_ms, e)
        return {"error": str(e)}
