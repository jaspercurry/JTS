"""Resolve-path + cross-copy contract coverage for the <dialog> confirm/alert
helper.

The dialog resolves its Promise on the native <dialog> `close` event, which
headless Chrome does not fire — so this drives a Node DOM shim instead
(tests/js/dialog_harness.mjs), giving the resolve path (click Confirm → resolve
true → action proceeds) real automated coverage rather than manual-only.

Running the same harness against BOTH the canonical ES module and the legacy
inline twin, and asserting the same behavioural contract, also guards the two
copies against silent drift. Skips when node isn't on PATH (e.g. a CI image
without it); runs anywhere node is present.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from jasper.web._common import dialog_helpers_js

_NODE = shutil.which("node")
_HARNESS = Path("tests/js/dialog_harness.mjs")
_CANONICAL = Path("deploy/assets/shared/js/dialog.js")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not on PATH")


def _drive(source_path: str) -> dict:
    proc = subprocess.run(
        [_NODE, str(_HARNESS), source_path],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"dialog harness errored:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _assert_shared_contract(out: dict) -> None:
    # Cancel-left / Confirm-right; destructive dialogs autofocus the safe button.
    assert out["confirmButtonValues"] == ["cancel", "confirm"]
    assert out["dangerAutofocusCancel"] is True
    assert out["nonDangerAutofocusConfirm"] is True
    # The resolve mapping: only the Confirm button yields true; ESC and Cancel
    # both yield false. This is the path headless can't exercise.
    assert out["resolveTrueOnConfirm"] is True
    assert out["resolveFalseOnEsc"] is True
    assert out["resolveFalseOnCancel"] is True
    # Alert: one OK button, resolves on acknowledge.
    assert out["alertButtonValues"] == ["ok"]
    assert out["alertResolves"] is True
    # No DOM leak once the dialog closes.
    assert out["removedAfterClose"] is True


def test_canonical_dialog_module_resolves_and_meets_contract():
    out = _drive(str(_CANONICAL))
    _assert_shared_contract(out)
    # The ES-module pages render their own forms, so no jtsConfirmSubmit there.
    assert out["hasConfirmSubmit"] is False


def test_legacy_dialog_twin_resolves_and_meets_contract():
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(dialog_helpers_js())
        path = f.name
    try:
        out = _drive(path)
    finally:
        os.unlink(path)
    _assert_shared_contract(out)
    # The legacy twin adds jtsConfirmSubmit for onsubmit forms: it cancels the
    # native (synchronous) submit and re-submits only once the user confirms.
    assert out["hasConfirmSubmit"] is True
    assert out["confirmSubmitReturnsFalse"] is True
    assert out["confirmSubmitSubmitsOnConfirm"] is True
