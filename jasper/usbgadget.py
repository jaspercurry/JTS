# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Import-light observed state for the composite USB gadget.

The gadget owner binds ConfigFS to a Linux UDC; the kernel then exposes the
host-side connection state below ``/sys/class/udc``.  Management surfaces use
this helper instead of depending on a second daemon to copy that kernel truth
into a JSON file.
"""
from __future__ import annotations

import os
from pathlib import Path


DEFAULT_UDC_CLASS_DIR = "/sys/class/udc"


def udc_host_connected(
    udc_class_dir: str | os.PathLike[str] = DEFAULT_UDC_CLASS_DIR,
) -> bool:
    """Return whether any UDC reports the USB host as ``configured``.

    A Pi normally exposes one UDC, but iterating all entries avoids coupling the
    control plane to a controller name.  Missing/unreadable sysfs fails soft to
    ``False``: absence of evidence must never be reported as a connected host.
    """

    root = Path(udc_class_dir)
    try:
        controllers = tuple(root.iterdir())
    except OSError:
        return False
    for controller in controllers:
        try:
            if (controller / "state").read_text().strip().lower() == "configured":
                return True
        except OSError:
            continue
    return False


__all__ = ["DEFAULT_UDC_CLASS_DIR", "udc_host_connected"]
