# ADR-0315: Hardware tier and direct updates

- **Date:** 2026-09-14
- **Status:** Accepted
- **Context:** The retired Workstream D design note mixed adopted installer
  behavior with proposals. A constrained Pi can run the full product profile;
  profile alone cannot describe build capacity. An old installation can also
  need cold builds when dependency or provenance inputs have changed.
- **Decision:** Keep hardware capacity independent of product profile. The
  installer reports detected RAM, CPU, architecture and tier; build resource
  policy remains owned by [ADR-0163](0163-installer-builds-run-the-inverse-of-the-audio-daemon-memory-policy.md)
  and its implementation. Update directly to the selected target revision,
  applying that revision's convergent migrations. Do not introduce intermediate
  revision builds or a checkpointed updater to compensate for version skew.
- **Consequences:** Cold-build cost belongs to build containment and install
  recovery, not an additional update algorithm. Tier detection and architecture
  preflight are implemented in `deploy/install.sh`; the synthetic hardware
  matrix is in `tests/test_install_hardware_tier.py`. The proposed combined
  skew/tier warning and extra CI/canary matrix are not adopted by this record.
  This retires `docs/install-hardware-tier-and-staleness.md`; its source and
  investigation history remain in git. Existing AEC decisions and deploy
  guards are unchanged. Retirement authorized by #4202 and Astra lane B #5061.
