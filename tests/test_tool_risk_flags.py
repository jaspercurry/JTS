# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Risk-category flags on tools — `untrusted_output` / `consequential`.

These are DECLARATIVE prompt-injection risk categories for the planned tool
store's policy/permission layer. They don't change runtime behavior today
(the fencing, taint window, and confirmation gate are wired explicitly inside
the tools), but they must stay truthful — so this pins the `@tool()` →
`build_tool()` propagation and the safe default. The per-tool annotations are
checked where each tool's fakes already live (test_tools_gmail /
test_tools_calendar / test_tools_home_assistant); the runtime enforcement is
tested in test_tools_fencing and the gate tests.
"""
from __future__ import annotations

from jasper.tools import build_tool, tool


def test_flags_default_to_false():
    @tool()
    async def plain() -> dict:
        """A tool that neither returns third-party text nor takes an action."""
        return {}

    built = build_tool(plain)
    assert built.untrusted_output is False
    assert built.consequential is False


def test_untrusted_output_propagates():
    @tool(untrusted_output=True)
    async def reads_email() -> dict:
        """Returns attacker-controllable third-party text (a SOURCE)."""
        return {}

    built = build_tool(reads_email)
    assert built.untrusted_output is True
    assert built.consequential is False


def test_consequential_propagates():
    @tool(consequential=True)
    async def unlocks() -> dict:
        """Takes a real-world action (a SINK)."""
        return {}

    built = build_tool(unlocks)
    assert built.consequential is True
    assert built.untrusted_output is False


def test_tool_can_be_both_source_and_sink():
    @tool(untrusted_output=True, consequential=True)
    async def fetch_and_post() -> dict:
        """Reads the web AND posts it — the scariest combination, and the
        case the tool store most needs to flag."""
        return {}

    built = build_tool(fetch_and_post)
    assert built.untrusted_output is True
    assert built.consequential is True
