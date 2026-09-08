# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behaviour pin for tests/js/_loader.mjs's buildFunction(strictImports).

Skips when node isn't on PATH (e.g. a CI image without it); runs anywhere
node is present.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")
_HARNESS = Path("tests/js/loader_strict_imports_test.mjs")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not on PATH")


def test_strict_imports_rejects_an_import_outside_sources():
    proc = subprocess.run(
        [_NODE, str(_HARNESS)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"loader strict-imports test errored:\n{proc.stderr}"
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {"ok": True}
