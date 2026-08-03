# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only systemd probes shared by setup wizards."""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)


def systemctl(*args: str, timeout: int = 8) -> tuple[int, str]:
    """Run a bounded read-only systemctl query and fail soft."""

    try:
        proc = subprocess.run(
            ["systemctl", *args],
            check=False,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("systemctl %s failed: %s", " ".join(args), exc)
        return 1, str(exc)
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def unit_active(unit: str) -> bool:
    """Return whether systemd reports the unit active."""

    rc, _ = systemctl("is-active", "--quiet", unit, timeout=5)
    return rc == 0
