# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Copyable starter for a trusted in-repo JTS capability pack.

This is documentation-by-example, not a shipped capability. It is not imported
by jasper.tools.packs.TOOL_PACKS, so it does not create a production
user-facing tool. Tests import it to keep the example aligned with the real
CapabilityPack -> ToolDefinition + ToolExecutor -> ToolRegistry path.
"""
from __future__ import annotations

from dataclasses import dataclass

from jasper.tools import PythonExecutor, Tool, ToolDefinition
from jasper.tools.packs import CapabilityPack, CatalogPack

STARTER_TOOL_NAME = "example_postcard"
STARTER_TOOL_TIMEOUT_SEC = 2.0
STARTER_TOOL_LABELS = ("example", "postcard")

STARTER_CATALOG_PACK = CatalogPack(
    id="example-postcard",
    title="Example Postcard",
    summary="Non-production starter pack showing the tool boundary.",
    setup_url=None,
)


@dataclass(frozen=True)
class StarterDeps:
    """Tiny deps bundle showing where runtime collaborators enter a pack."""

    sender_name: str = "JTS"
    enabled: bool = True


STARTER_TOOL_DEFINITION = ToolDefinition(
    name=STARTER_TOOL_NAME,
    description=(
        "Return a deterministic postcard-shaped example payload. This is a "
        "non-production starter for contributors copying the tool-pack shape."
    ),
    llm_description=(
        "Use only in tests or while copying the starter pack. Return a tiny "
        "postcard-shaped example payload; do not treat this as a real user "
        "capability."
    ),
    parameters={
        "type": "object",
        "properties": {
            "recipient": {"type": "string"},
        },
    },
    labels=STARTER_TOOL_LABELS,
    timeout=STARTER_TOOL_TIMEOUT_SEC,
    untrusted_output=False,
    consequential=False,
)


def build_starter_tools(deps: StarterDeps):
    """Build explicit Tool objects from deps.

    A richer pack can create clients, read setup state, or construct several
    definitions here. Keep dependency setup lazy and bounded: this example
    does no I/O and starts no background work.
    """

    async def render_postcard(recipient: str = "friend") -> dict:
        return {
            "recipient": recipient or "friend",
            "sender": deps.sender_name,
            "message": f"Wish you were here, {recipient or 'friend'}.",
        }

    return [
        Tool(
            definition=STARTER_TOOL_DEFINITION,
            executor=PythonExecutor(render_postcard),
        ),
    ]


STARTER_PACK = CapabilityPack(
    name="example_postcard",
    category="Examples",
    catalog_pack=STARTER_CATALOG_PACK,
    gate=lambda deps: deps.enabled,
    build=build_starter_tools,
)
