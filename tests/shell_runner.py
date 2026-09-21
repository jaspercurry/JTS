# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded shell-script tests, with process-tree cleanup on timeout."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def run_bash(
    args: list[str], *, timeout: float, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    # Darwin can shrink pipes below modern Bash's here-document threshold.
    # Its system Bash uses temporary files instead; callers must support 3.2.
    bash = "/bin/bash" if sys.platform == "darwin" else "bash"
    with subprocess.Popen(
        [bash, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True, env=env,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:  # noqa: BLE001 — interruptions must also reap children
            # The parent may already have exited while a child holds the pipes.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
