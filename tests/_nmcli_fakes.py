# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared subprocess results for Wi-Fi wizard tests."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable


def mock_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["nmcli"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def scripted_nmcli(steps: Iterable[subprocess.CompletedProcess]):
    """Return scripted nmcli results, then harmless empty successes."""

    steps_iter = iter(steps)

    def side_effect(cmd, *args, **kwargs):
        try:
            return next(steps_iter)
        except StopIteration:
            return mock_proc()

    return side_effect
