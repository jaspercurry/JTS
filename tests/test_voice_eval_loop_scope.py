# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guard: the voice-eval session-loop pin must actually win.

pytest-asyncio's auto mode puts a bare ``asyncio`` marker on every
async test and resolves loop scope from the CLOSEST marker. The
voice_eval conftest pins ``loop_scope="session"`` so the whole suite
shares one event loop with the session-scoped LiveConnection — but an
appended marker silently loses to auto mode's, each test gets its own
function loop, and that loop's Runner teardown runs
``shutdown_asyncgens()``, finalizing google-genai's suspended
``connect()`` asyncgen and cleanly closing the live websocket between
tests. That shipped: every paid scenario after the first failed with
``ConnectionClosedOK`` (2026-06-11 investigation, PR #610).

This collects the real suite in-process (no paid sessions, no network)
and asserts the pin is the closest marker on every item.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_voice_eval_session_loop_pin_is_the_closest_marker():
    captured: dict[str, list[dict]] = {}

    class _Capture:
        @pytest.hookimpl(trylast=True)
        def pytest_collection_modifyitems(self, items):
            for item in items:
                captured[item.nodeid] = [
                    dict(m.kwargs) for m in item.iter_markers(name="asyncio")
                ]

    rc = pytest.main(
        [
            "--collect-only", "-q",
            "-p", "no:cacheprovider",
            str(ROOT / "tests" / "voice_eval" / "regression" / "test_volume.py"),
        ],
        plugins=[_Capture()],
    )
    assert rc == 0, f"nested collection failed with exit code {rc}"
    assert captured, "nested collection found no voice_eval items"
    for nodeid, marks in captured.items():
        assert marks, f"{nodeid}: no asyncio marker at all"
        assert marks[0].get("loop_scope") == "session", (
            f"{nodeid}: closest asyncio marker is {marks[0]} — the session "
            "loop pin lost precedence (add_marker must use append=False); "
            "every paid scenario after the first will fail with "
            "ConnectionClosedOK"
        )
