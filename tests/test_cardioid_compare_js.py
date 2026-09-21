# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_cardioid_compare_js():
    result = subprocess.run(
        ["node", "tests/js/cardioid_compare_test.mjs"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
