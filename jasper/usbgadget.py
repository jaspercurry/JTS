# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Import-light composition intent and observed state for the USB gadget.

The gadget owner binds ConfigFS to a Linux UDC; the kernel then exposes the
host-side connection state below ``/sys/class/udc``.  Management surfaces use
these helpers instead of depending on a second daemon to copy that kernel truth
into a JSON file, and instead of re-implementing the shell truth table that
decides which functions the gadget composes.
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


def network_wanted() -> bool:
    """Return whether the USB management network is wanted.

    Unless ``JASPER_USB_NETWORK`` is the exact literal ``disabled``
    (case-insensitive) the network is wanted — the same convention as
    ``JASPER_SHAIRPORT_SUPERVISOR`` / ``JASPER_SYSTEM_SUPERVISOR``. The value is
    NOT stripped, matching the raw comparison in the shell truth table
    (``deploy/usbsink/jasper-usbgadget-compose.sh``): a whitespace-decorated
    ``" disabled"`` stays enabled on both sides, because a stray space must
    never silently drop the fallback network. Read from ``os.environ`` on every
    call — ``jasper.env_load`` unions ``/etc/jasper/jasper.env`` into it at
    startup, and a long-lived daemon is not restarted when the switch flips.
    """

    return os.environ.get("JASPER_USB_NETWORK", "enabled").lower() != "disabled"


__all__ = ["DEFAULT_UDC_CLASS_DIR", "network_wanted", "udc_host_connected"]
