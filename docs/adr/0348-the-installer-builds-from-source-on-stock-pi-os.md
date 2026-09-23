# ADR-0348: The installer builds from source on stock Pi OS

- **Date:** 2026-09-23
- **Status:** Accepted. Supersedes
  [ADR-0164](0164-a-pi-image-is-a-cached-versioned-input-to-the-installer-not-a-second-installer.md).
- **Context:** ADR-0164 allowed a Pi image or a cached, versioned runtime
  bundle as an input to the installer. A prebuilt ARM64 bundle of
  `jasper-fanin`, `jasper-outputd` and the `jts_ring` ioplug followed: a
  manual `workflow_dispatch` build, a verifier, and an installer seam that
  used a bundle only when one was staged. No install ever staged one, and
  the path cost about 4,000 lines. On #5643 (R-243) the owner chose, for
  now, to point new users at the stock Pi OS flasher and let Claude load the
  rest of the software; that also settles backlog row R-243 on #4804
  (first tracked as #4707).
- **Decision:** JTS ships no prebuilt image and no runtime bundle. A speaker
  starts as stock Raspberry Pi OS written by the official Raspberry Pi
  Imager; the Claude-driven `scripts/onboard.sh` and `scripts/deploy-to-pi.sh`
  then install JTS, and `deploy/install.sh` builds the native daemons
  (`jasper-fanin`, `jasper-outputd`, the `jts_ring` ioplug) from source on the
  Pi on every install.
- **Consequences:** There is one installer path: the from-source build every
  install already ran is the only one. A first install takes the from-source
  build time on the Pi. ADR-0164's rules for an image lapse with it, so
  bringing back an image or a bundle takes a new ADR. `LICENSE-third-party.md`
  keeps its rows for the native binaries, now evidenced by `rust/Cargo.lock`
  and `c/jts-ring-ioplug/Makefile`. Rejected: keeping the bundle as an unused
  option, which carried about 4,000 lines that no install consumed.
