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
- deploy/bin/jasper-aec-reconcile — bash, can't import; carries its
  own copies of the constants with a comment pointing here as the
  canonical source. Keep both in sync when changing names/numids.

What this package is NOT:
- A runtime mic-detection layer (no VID/PID auto-discovery yet)
- A firmware-flash abstraction (mechanisms vary wildly across
  vendors; not worth abstracting until we have two concrete cases)
- A `MicProfile` base class (see above — premature)
"""
from __future__ import annotations

from . import xvf3800

# USB VID:PID → profile module. Stable identifier for the mic family.
PROFILES = {
    xvf3800.USB_VID_PID: xvf3800,
}
