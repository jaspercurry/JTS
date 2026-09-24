# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The largest import cycle through the tuning code only shrinks.

grimp counts function-local imports, so deferring an import into a function
does not hide a cycle from this test; moving the shared fact down to the
module that owns it does. Removal condition: delete this test once
``LARGEST_TUNING_CYCLE`` reaches 0 and the "Tuning subtrees are acyclic"
import-linter contract lists ``jasper.active_speaker`` itself.
"""

from __future__ import annotations

import sys

import grimp

TUNING = ("jasper.active_speaker", "jasper.audio_measurement", "jasper.sound")

#: Lower this when a change shrinks the cycle; it must never rise.
LARGEST_TUNING_CYCLE = 0


def _cycles(graph: grimp.ImportGraph) -> list[set[str]]:
    """Strongly connected components of more than one module (Tarjan)."""
    order: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[set[str]] = []

    def visit(module: str) -> None:
        order[module] = low[module] = len(order)
        stack.append(module)
        on_stack.add(module)
        for imported in graph.find_modules_directly_imported_by(module):
            if imported not in order:
                visit(imported)
                low[module] = min(low[module], low[imported])
            elif imported in on_stack:
                low[module] = min(low[module], order[imported])
        if low[module] == order[module]:
            component = set()
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.add(member)
                if member == module:
                    break
            if len(component) > 1:
                found.append(component)

    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 10_000))
    try:
        for module in sorted(graph.modules):
            if module not in order:
                visit(module)
    finally:
        sys.setrecursionlimit(limit)
    return found


def test_the_largest_tuning_import_cycle_does_not_grow():
    graph = grimp.build_graph("jasper", exclude_type_checking_imports=True, cache_dir=None)
    tuning = [cycle for cycle in _cycles(graph) if any(module.startswith(TUNING) for module in cycle)]
    largest = max(tuning, key=len, default=set())

    assert len(largest) <= LARGEST_TUNING_CYCLE, sorted(largest)
