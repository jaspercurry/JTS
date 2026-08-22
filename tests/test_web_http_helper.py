# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Issue #1926 regression coverage for the shared fetch helper.

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
_HARNESS = Path(__file__).resolve().parent / "js" / "http_stale_session_test.mjs"

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not on PATH")


def test_http_helper_stale_session_via_node():
    proc = subprocess.run(
        [_NODE, str(_HARNESS)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (
        f"http.js stale-session harness errored:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["ok"] is True
    assert out["passed"] >= 16
