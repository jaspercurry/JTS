# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Node-driven behaviour pins for the shared front-end modules.

``deploy/assets/shared/js/http.js`` is the CSRF-aware fetch helper shared by
every canonical (ES-module) wizard page. A stale, long-idle page's next
mutating POST used to 403 and render a bare synthesized "HTTP 403" —
``parseResponse()`` only ever tried ``r.json()``, so the server's own honest
"Session expired" copy (``jasper.web._common.reject_csrf``, HTML) was
silently discarded. This runs the Node harness
(``tests/js/http_stale_session_test.mjs``) that pins the fix: a mutating
POST's 403 with a non-JSON body now shows an honest "went stale, reloading"
copy and reloads, while a route's own JSON error payload (403 or otherwise)
still surfaces its own message untouched.

``tests/js/polling_test.mjs`` pins the other half of the module: the shared
``startPolling()`` every live page schedules through — a hidden tab slows to
the hidden cadence instead of hammering the Pi, becoming visible comes
current at once, a rejected ``fn`` is retried on the next tick, and ``stop()``
is final.

``tests/js/settings_status_test.mjs`` pins ``settings-status.js``, the
settings-surface module the landing page and the area hubs share: gating runs
off the install-baked capability map before any fetch and fails closed, and a
poll tick fills the ``status-*`` sublabels without re-driving layout.

Mirrors ``tests/test_dialog_helper.py`` / ``test_web_rooms_setup.py``'s
``test_dom_append_children_export_via_node`` — skip (not fail) when node
isn't on PATH; this repo has no browser-based JS runner.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_JS_DIR = Path(__file__).resolve().parent / "js"

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not on PATH")


@pytest.mark.parametrize(
    ("harness", "min_passed"),
    [
        ("http_stale_session_test.mjs", 16),
        ("polling_test.mjs", 14),
        ("settings_status_test.mjs", 23),
    ],
)
def test_shared_module_harness_via_node(harness: str, min_passed: int):
    proc = subprocess.run(
        [_NODE, str(_JS_DIR / harness)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (
        f"{harness} errored:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ok"] is True
    assert out["passed"] >= min_passed
