# Changelog

All notable changes to JTS will be documented in this file.

This project follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format. A maintainer will tag `v0.1.0` at OSS launch; this branch does not create
release tags.

## [Unreleased]

### Added

- Multiroom dataplane foundations: grouping control endpoints, `/rooms` pairing
  UX, runtime health surfaces, and the grouping supervisor path for bonded
  speakers.
- Member-local assistant playback for grouped speakers through `jasper-outputd`,
  backed by the shared `jasper-tts-protocol` Rust crate.
- Transit provider registry rework: self-contained city packs, provider-owned
  environment parsing, and the `/transit/` city-pack wizard flow.
- Deploy identity and direction guards that pin a speaker peer ID and refuse
  accidental downgrades to already-updated Pis.
- Active-speaker setup improvements, including guarded crossover preview assets,
  driver auto-level flow, and output-topology fixes.
- Web wizard safety and maintainability guards for JSON islands, CSRF routing,
  shared escaping, event logging, and static ES module conventions.
- 2026-06-12 OSS due-diligence review snapshot documenting launch-gate tooling,
  release, security, and maintainer-readiness gaps.
