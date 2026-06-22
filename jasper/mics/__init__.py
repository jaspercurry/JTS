# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-mic-family capability and check registry.

Each supported microphone gets a profile module in this package
(e.g. `xvf3800.py`). The module holds the mic-family-specific
knowledge: USB identity, ALSA card name, firmware variants, mixer
invariants, and small helpers that callers (doctor, aec_bridge,
reconciler tooling) consult instead of inlining literals.

Pattern: add a new mic = drop a new file here + add to PROFILES.
**Do NOT generalize the interface yet.** There is exactly one mic
in this registry today; designing a `MicProfile` Protocol or ABC
from a single data point is the over-abstraction trap. When a
second mic actually lands, the common surface will be obvious from
diffing the two profiles, and we'll define an interface only for
what's genuinely shared.

Consumers today:
- jasper.cli.doctor — reads constants, runs the per-profile checks
- jasper.cli.aec_bridge — reads ALSA card name + channel indices
- jasper.cli.xvf_profile — import-cheap bridge that resolves profile
  facts for deploy/bin/jasper-aec-reconcile, so bash consumes generated
  env instead of owning XVF geometry/channel constants.

What this package is NOT:
- A general runtime mic-detection layer (it maps known USB IDs to the
  single XVF3800 family profile; it does not probe arbitrary mics)
- A firmware-flash abstraction (mechanisms vary wildly across
  vendors; not worth abstracting until we have two concrete cases)
- A `MicProfile` base class (see above — premature)
"""
from __future__ import annotations

from . import xvf3800

# USB VID:PID → profile module. Stable identifier for the mic family.
PROFILES = {
    vid_pid: xvf3800 for vid_pid in xvf3800.USB_VID_PIDS
}
