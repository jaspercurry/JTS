# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hold an env file's advisory lock the way its other writers do.

``jasper/atomic_io.py``'s ``locked_update_env_file`` (jasper-control's
``aec_endpoints._write_aec_leg``) takes ``<dir>/.<basename>.lock``, reads the
file, and writes the WHOLE file back. A bash writer that does not take the
same lock either loses its write to that write-back or appends a second line
for a key the holder just set — and every reader of these files takes the last
line. The pins that hold the bash writers to the lock all need the same
process: take the lock, announce it, hold it, optionally republish.
"""
from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def spawn_lock_holder(
    env_file: Path,
    *,
    hold_seconds: float,
    write_back: str = "",
    reap_timeout: float = 60.0,
) -> Iterator[None]:
    """Hold ``env_file``'s advisory lock for ``hold_seconds``.

    With ``write_back``, republish the file on release as the snapshot taken
    under the lock plus that text — the write-back an unlocked writer loses to.
    Without it the holder only occupies the lock, which is what a pin of the
    give-up path needs. The block runs once the lock is provably held.
    """
    lock = env_file.parent / f".{env_file.name}.lock"
    holding = env_file.parent / f".{env_file.name}.holding"
    script = [
        f'exec 9>>"{lock}"',
        "flock 9",
        f'snapshot="$(cat "{env_file}" 2>/dev/null || true)"',
        f': > "{holding}"',
        f"sleep {hold_seconds}",
    ]
    if write_back:
        script.append(
            'printf "%s\\n%s" "$snapshot" "$JTS_LOCK_HOLDER_APPEND" '
            f'> "{env_file}"'
        )
    holder = subprocess.Popen(
        ["bash", "-c", "\n".join(script) + "\n"],
        env={**os.environ, "JTS_LOCK_HOLDER_APPEND": write_back},
    )
    try:
        deadline = time.monotonic() + 10
        while not holding.exists():
            assert time.monotonic() < deadline, "holder never took the lock"
            time.sleep(0.01)
        yield
    finally:
        holder.wait(timeout=reap_timeout)
