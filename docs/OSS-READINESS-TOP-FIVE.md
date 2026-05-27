# OSS Readiness Top Five

Last reviewed: 2026-05-27

This is the living, ordered worklist for bringing JTS from "excellent
personal project" to "credible open-source appliance project." The
historical staff-engineering review lives in
[REVIEW-google-oss-readiness.md](REVIEW-google-oss-readiness.md); use
this file for current priorities.

Ordering principle: reduce real user/operator risk first, then make the
repo easier for outside contributors to change safely. Avoid broad
rewrites. Each item should land as reviewable slices with tests or
operational checks.

## 1. Security and Privacy Quick Wins

**Status.** First hardening slice shipped in PR #339. The current
private vulnerability reporting path is documented in `SECURITY.md` as
`jc@jasper.tech`. GitHub private vulnerability reporting remains an
optional repository setting to enable later.

**Why it matters.** JTS stores API keys, OAuth tokens, Wi-Fi recovery
PSKs, Home Assistant tokens, and short wake-event audio recordings.
Those are useful operationally and sensitive socially. The project needs
a clear reporting path and must not leak secrets through its own
diagnostic tooling.

**Risk of not doing it.** A well-meaning bug report or diagnostic bundle
can expose credentials or household audio. Security researchers have no
private reporting path, so they may either disappear or file details
publicly.

**Current definition of done.**
- `SECURITY.md` defines scope, current reporting, support, and current
  limits.
- Diagnostic scripts redact current and future env-style secret
  assignments in fetched logs/config snapshots before writing to disk.
- The `/wake/` page discloses local wake-event recording, retention
  location, and export/reset tools.
- README points to the policy and the current worklist.

**Cost and trade-off.** Low engineering cost, mostly documentation and
small script changes. The trade-off is that this does not add full
authentication, HTTPS, or rootless daemons; it prevents avoidable leaks
while the larger security posture is designed.

## 2. Management Surface Hardening

**Status.** First browser-boundary and input-size slice shipped in PR
#339. Full local authentication / HTTPS / pairing remains backlog.

**Why it matters.** `jasper-control` exposes useful LAN endpoints for
the dashboard, dial, scripts, and future accessories. Some endpoints
can restart audio, toggle wake/AEC behavior, mute the mic, reboot, or
power off the speaker.

**Risk of not doing it.** Any browser tab on the household network can
attempt cross-origin POSTs, and DNS rebinding can make a local service
appear under an attacker-controlled hostname. A large request body can
also waste memory in a root-running daemon.

**Current definition of done.**
- Reject unknown/public `Host` headers on `jasper-control` reads and
  writes.
- Reject mutating requests with cross-site, `null`, or mismatched
  `Origin` headers while preserving no-Origin clients like curl, the
  dial, Home Assistant, and local proxy code.
- Cap `jasper-control` POST bodies with an environment override.
- Emit `Cache-Control: no-store` on control JSON responses.
- Cover the behavior with pure helper tests and route-level tests.

**Cost and trade-off.** Low to medium. This is intentionally not auth:
trusted LAN clients still work without provisioning credentials. The
main compatibility risk is custom local hostnames; `JASPER_HOSTNAME` and
`JASPER_MANAGEMENT_ALLOWED_HOSTS` are the escape hatches.

## 3. Supply-Chain Pinning and Provenance

**Status.** First provenance slice shipped: direct deploy-time
release archives, model files, and source-build git inputs now have a
canonical manifest, checksum/commit verification where JTS controls the
fetch, and a local provenance check. Python determinism is partially
started: important direct dependencies are pinned or bounded in
`pyproject.toml`, `pycamilladsp` is pinned to a commit, and
CONTRIBUTING recommends `uv sync` for local development. There is not
yet a committed lock artifact that deploy or CI consume. Remaining
work: Python lock/hash install adoption, apt snapshots, and PlatformIO
transitive/toolchain lock depth.

**Why it matters.** Fresh installs fetch Python packages, models,
firmware tools, `.deb` artifacts, and source repos. OSS users need to
know what they are running and maintainers need repeatable installs.

**Risk of not doing it.** A mutable upstream tag, changed binary, or
silent model replacement can break installs or introduce unreviewed
code. Debugging becomes guesswork because two "same" installs may not
actually contain the same bits.

**Current definition of done.**
- Inventory every direct network fetch in `deploy/install.sh`, firmware
  build paths, wake/DTLN model registries, and Python direct URL deps.
- Pin immutable versions or SHAs where practical.
- Record checksums for binary/model artifacts JTS downloads directly.
- Document the provenance and update procedure in one canonical doc.
- Add a lightweight check that fails when a new direct fetch lacks
  provenance.

**Cost and trade-off.** Medium. Some upstream ecosystems are awkward
about immutable artifacts, and checksum updates add maintainer work.
The benefit is high repeatability and easier security review.

**Recently completed.** `rust/jasper-fanin/Cargo.lock` is committed and
checked by `scripts/check-provenance.py`, closing the Rust fan-in crate
gap without changing Pi runtime behavior. openWakeWord stock ONNX
package-resource assets are now explicit, hash-checked provenance
artifacts instead of hidden downloads behind
`openwakeword.utils.download_models()`.

**Deferred deliberately.** Python lock adoption is still valuable, but
it should wait until active `main` dependency churn calms down enough to
avoid creating a fragmented dependency-management story. When resumed,
choose one shared artifact (`uv.lock` or generated hash requirements)
and make deploy/CI consume it deliberately.

## 4. Tooling Enforcement

**Status.** Deferred while `main` is moving quickly. Pytest and the
supply-chain provenance check already run in GitHub Actions. Ruff is a
dev dependency and documented locally, but CI lint is intentionally not
enabled yet because `.github/workflows/tests.yml` records existing
lint noise that would require a cleanup pass.

**Why it matters.** The codebase already has strong conventions:
hardware-free tests, CSRF helpers, env-file atomics, and documentation
discipline. Contributors should get fast automated feedback before
review rather than learning these rules by accident.

**Risk of not doing it.** Style and test discipline become oral
tradition. Small regressions slip in, and reviewers spend attention on
mechanical issues instead of behavior and design.

**Definition of done.**
- Preserve the current PR pytest and provenance checks.
- Add lint/format enforcement only after the active feature branches can
  absorb the change without review churn.
- Scope ruff or equivalent rules to low-noise, codebase-compatible
  checks before making them merge-blocking.
- Existing doc freshness and attribution checks are easy to run locally
  and, when ready, in CI.
- CONTRIBUTING documents the exact local commands.

**Cost and trade-off.** Medium. The main cost is tuning rules so they
protect the project without creating noisy churn across active branches.

## 5. Refactor High-Complexity Hotspots

**Status.** Pending.

**Why it matters.** The architecture is strong, but a few files carry
too much operational surface area. The goal is not aesthetic cleanup;
the goal is making risky changes smaller, more testable, and easier for
new contributors to reason about.

**Risk of not doing it.** Future features pile onto the biggest files,
making bugs harder to localize and reviews harder to trust. The project
keeps depending on one maintainer's ability to hold large modules in
their head.

**Definition of done.**
- Identify the top hotspots by churn, size, and incident history.
- Split only along existing ownership boundaries: provider adapters,
  wake-loop lifecycle, web wizard shared mechanics, or control route
  groups.
- Preserve behavior with regression tests before moving code.
- Stop each slice when the next change becomes easier; avoid framework
  rewrites.

**Cost and trade-off.** Medium to high. Refactors create merge conflicts
in an actively developed repo, so they should be sequenced after the
lower-risk safety and tooling work unless a feature is already touching
the same area.

## Backlog

These are real but not top-five yet: full local authentication, HTTPS or
pairing for setup pages, rootless daemon privilege separation, DCO/CLA
policy, Dependabot/update automation, release artifacts, OTA update
design, metrics export, and broader third-party attribution depth.
