# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Print the USB ids of every registered measurement microphone.

The shell bridge for ``deploy/bin/jasper-aec-reconcile``, which must never
select a calibrated measurement microphone (a UMIK-2 and friends) as the
voice/wake input: those mics carry no wake or AEC contract, so selecting one
would silently replace the household's room microphone with an instrument.

The vocabulary is owned by
``jasper.audio_measurement.mic_identity.SUPPORTED_MODELS`` (the numpy-free
registry leaf; ``calibration`` re-exports it) and reaches bash through this
command rather than a second copy of the ids in shell — the same bridge shape
``jasper.cli.xvf_profile`` uses for the mic profile. Import the leaf, never
``calibration``: the reconciler spawns this on every managed-XVF hotplug pass
(the XVF is a USB card), and ``calibration`` costs the full numpy import.

Output contract:

* **exit 0** — the registry was read; stdout holds one lower-cased
  ``vid:pid`` per line, and may legitimately be EMPTY if no registered model
  declares an id.
* **non-zero exit** — the registry could not be read (module unimportable,
  killed). The caller must treat this as "I could not classify", never as
  "no measurement mics exist"; the reconciler's own gate fails open to the
  closed candidate allowlist in that case.
"""
from __future__ import annotations

import sys

from jasper.audio_measurement.mic_identity import measurement_mic_usb_ids


def main(argv: list[str] | None = None) -> int:
    del argv
    for usb_id in measurement_mic_usb_ids():
        sys.stdout.write(f"{usb_id}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
