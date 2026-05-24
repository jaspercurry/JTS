"""Single source of truth for OOMScoreAdjust values per critical
daemon, shared between Python (jasper.cli.doctor) and bash
(deploy/install.sh's migrate_memory_resilience step).

EDITING HERE CHANGES:
  - jasper-doctor's drift check (uses EXPECTED)
  - install.sh's live-write step (uses INSTALL_LIVE_WRITE)
  - The .service unit files MUST be updated separately to match —
    these constants don't write the unit files. systemctl reads
    OOMScoreAdjust= from the unit file at process start.

Don't add operator-tuning knobs here. These values are weighted
priorities calibrated for JTS's specific daemon set. Forks of the
project should rename the units and re-weight; they shouldn't tune
JTS's numbers per-deployment.

See docs/HANDOFF-resilience.md "Memory-pressure resilience (Stage 1)"
for the rationale on each value.
"""
from __future__ import annotations


# All daemons whose OOMScoreAdjust we verify in jasper-doctor.
# Includes ssh (Debian's openssh-server default = -1000) so a future
# packaging change that drops the default surfaces in drift detection.
EXPECTED: dict[str, int] = {
    "jasper-camilla": -900,     # silence = worst UX
    "jasper-aec-bridge": -700,  # real-time mic processing
    "jasper-control": -600,     # recovery surface (HTTP dashboard)
    "jasper-voice": -500,       # largest blast radius (LLM session)
    "jasper-mux": -300,         # transient-graceful (latest-source-wins)
    "jasper-input": -300,       # direct USB still works without bridge
    "ssh": -1000,               # recovery path; NEVER killable
}


# Subset of EXPECTED that install.sh's migrate_memory_resilience
# step actively live-writes to /proc/PID/oom_score_adj during deploy.
# Excludes ssh because JTS doesn't own the openssh-server unit file —
# Debian sets that. We verify drift on ssh but never overwrite it.
INSTALL_LIVE_WRITE: dict[str, int] = {
    k: v for k, v in EXPECTED.items() if k != "ssh"
}
