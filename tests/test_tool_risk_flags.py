# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Risk-category flags on tools — `untrusted_output` / `consequential`.

These are DECLARATIVE prompt-injection risk categories: metadata surfaced in
the manifest and catalog, not extra prompt text and not a permission
boundary (ADR-0338). They don't change runtime behavior today (the fencing,
taint window, and confirmation gate are wired explicitly inside the tools),
but they must stay truthful — so this pins the `@tool()` -> `build_tool()`
propagation and the safe default. The per-tool annotations are checked
where each tool's fakes already live (test_tools_gmail / test_tools_calendar
/ test_tools_home_assistant); the runtime enforcement is tested in
test_tools_fencing and the gate tests.
"""
from __future__ import annotations

import pytest

from jasper.tools import build_tool, tool


@pytest.mark.parametrize(
    ("untrusted_output", "consequential"),
    [
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    ],
)
def test_risk_flags_propagate_from_tool_to_built_tool(
    untrusted_output: bool, consequential: bool,
):
    @tool(untrusted_output=untrusted_output, consequential=consequential)
    async def flagged() -> dict:
        """A tool declaring this case's risk flags."""
        return {}

    built = build_tool(flagged)
    assert built.untrusted_output is untrusted_output
    assert built.consequential is consequential
