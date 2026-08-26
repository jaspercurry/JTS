# Handoff: First-Party ARM64 Runtime Artifacts

This is the canonical operational reference for building, inspecting, and
installing JTS's narrow first-party ARM64 runtime bundle. The broader image
strategy lives in
[`HANDOFF-pi-image-delivery.md`](HANDOFF-pi-image-delivery.md); third-party
fetch provenance lives in
[`HANDOFF-supply-chain.md`](HANDOFF-supply-chain.md).

## Current State

JTS can produce one self-verifying Debian Trixie ARM64 bundle containing the
JTS-owned compiled runtime that otherwise costs meaningful time during a fresh
Pi install. It currently covers the two Rust audio daemons and the JTS ALSA
ring plugin.

The release lane is deliberately manual:

- [`.github/workflows/first-party-arm64-release.yml`](../.github/workflows/first-party-arm64-release.yml)
  builds on a native ARM64 runner in a digest-pinned Debian Trixie container;
- the workflow uploads a short-lived review artifact and never creates a
  GitHub Release;
- [`deploy/install.sh`](../deploy/install.sh) consumes an explicitly staged
  extracted bundle, or retains its existing source-build behavior when no
  bundle is configured;
- a configured but invalid bundle fails closed. It never silently compiles
  different bytes and continues.

This is release groundwork, not a public binary release or a complete Pi
image.

## One Source of Truth

[`release/first-party-arm64/artifacts.toml`](../release/first-party-arm64/artifacts.toml)
is the sole declaration of:

- build commands and source outputs;
- bundle paths and final install destinations;
- installed file modes;
- target OS, architecture, ELF class/machine, interpreter, and allowed dynamic
  libraries;
- required dynamic libraries and exported symbols;
- Cargo roots/lockfiles;
- the exact reviewed Cargo license-expression policy; and
- derived-code and system-library notice inputs.

The builder, verifier, installer plan, tests, and generated `BUILD-INFO.json`
consume that contract. Do not add a parallel artifact/path table to shell or
documentation. Extend the TOML contract and its parser instead.

[`release/first-party-arm64/BUILD-INFO.schema.json`](../release/first-party-arm64/BUILD-INFO.schema.json)
is the external metadata schema. The stdlib verifier mirrors it with stricter
semantic checks so a fresh Pi does not need the `jsonschema` package.

## Build

The supported release build is the manual GitHub Actions workflow. A local
build is also supported only on native Debian Trixie ARM64:

```sh
python3 scripts/build-first-party-arm64-release.py
```

Optional flags:

```sh
python3 scripts/build-first-party-arm64-release.py \
  --version 2026.07.27-rc1 \
  --output-dir dist/first-party-arm64
```

The builder requires:

- a clean Git checkout with an exact full commit SHA;
- native `aarch64`/`arm64`, not a hidden cross-build;
- Debian Trixie;
- the committed Cargo lockfiles (`cargo --locked`);
- a freshly deleted, release-owned `target/first-party-arm64-release/` output
  root (ordinary ignored Cargo targets and prior plugin outputs are never
  consumed);
- the declared ELF outputs; and
- every required license/notice input.

It always deletes and recreates the isolated output root, directs Cargo there
with `CARGO_TARGET_DIR`, directs the plugin Makefile there with
`PLUGIN_OUTPUT`, and assembles only the paths returned by that build. This
prevents ignored conventional targets—or Cargo's mtime-based false-`Fresh`
decision—from being labeled as the current commit. The builder then rechecks
that the source commit and clean-tree state did not change during compilation.
It emits an extracted bundle, a normalized `.tar.xz`, and a sidecar archive
SHA-256. Output defaults to `dist/first-party-arm64/`; there is intentionally
no “package whatever binaries are already here” release option.

## What Verification Proves

[`scripts/verify-first-party-arm64-release.py`](../scripts/verify-first-party-arm64-release.py)
checks all of these before producing an install plan:

1. `BUILD-INFO.json` has the exact supported schema and contract constants.
2. `SHA256SUMS` is sorted, complete, and matches every bundle file.
3. The bundle has no unlisted files or symlinks.
4. Artifact size, SHA-256, and mode match both metadata and the live file.
5. Every ELF is 64-bit AArch64, dynamically linked as declared, uses only the
   reviewed interpreter/library allowlist, and has no `RPATH`/`RUNPATH`.
6. Required ALSA linkage and plugin entry points exist.
7. The recorded live ELF evidence equals a fresh `readelf` inspection.
8. Cargo runtime/build graphs are exact, sorted, lockfile-bound, and exclude
   dev-only edges.
9. The full Debian build-root package inventory, compiler versions, stable
   build environment, source epoch, and Git identity are recorded.
10. Every bundled file is covered by the final checksum manifest.

Manual inspection:

```sh
python3 scripts/verify-first-party-arm64-release.py \
  dist/first-party-arm64/jts-first-party-runtime-<version>
```

An image/installer must additionally bind the bundle to the exact application
source it is installing:

```sh
python3 scripts/verify-first-party-arm64-release.py \
  --expected-source-sha <40-character-commit> \
  dist/first-party-arm64/jts-first-party-runtime-<version>
```

A clean bundle from a different commit is not interchangeable.

## Determinism Boundary

The packaging format is deterministic:

- file ordering, owner/group, modes, and mtimes are normalized;
- timestamps derive from `SOURCE_DATE_EPOCH` (the commit time by default);
- Rust debug paths are remapped and incremental compilation is disabled;
- JSON, Cargo graphs, checksum lines, and archive members are sorted; and
- the container image is pinned by both date tag and digest.

That does **not yet mean bit-reproducible compilation**. Apt consumes the
signed moving Trixie repositories, not a Debian snapshot. `BUILD-INFO.json`
therefore records every installed Debian package and tool version so a build
is auditable and meaningfully rebuildable, but the project must not claim
binary reproducibility until two clean native builds compare byte-for-byte.
The first public promotion should either pin an apt snapshot or record a
successful two-build comparison (and still describe the remaining boundary).

## License and Notice Boundary

The bundle includes:

- JTS `LICENSE` and `NOTICE`;
- the exact license files found in every resolved registry Cargo package used
  by the active target graph;
- the preserved MIT attribution for the PipeWire `spa_dll` math port; and
- the actual Debian rustc copyright inventory and Apache-2.0 text covering
  statically incorporated standard-library, panic-runtime, and
  compiler-builtins code; and
- the Debian-provided LGPL notice for dynamically linked `libasound`.

Missing Cargo license metadata/files and any license expression absent from
the manifest's reviewed allowlist fail the build. This makes a new dependency
license a conscious redistribution review, not something the notice generator
silently accepts. The notice index and license files are themselves
checksummed.

This is intentionally narrow. It does not assert redistribution clearance for
Raspberry Pi OS, apt packages, Python, firmware, models, CamillaDSP,
CamillaGUI, librespot/raspotify, shairport-sync, nqptp, enhanced AEC, or a
whole `.img.xz`. See
[`../LICENSE-third-party.md`](../LICENSE-third-party.md) before widening the
bundle or an image.

## Installer Consumption

Extract a verified bundle locally on the target and pass its directory plus
the exact source identity:

```sh
sudo \
  JASPER_DEPLOY_SHA_FULL=<40-character-commit> \
  JASPER_FIRST_PARTY_RUNTIME_BUNDLE=/path/to/extracted/bundle \
  bash deploy/install.sh
```

For normal laptop-driven deploys, `scripts/deploy-to-pi.sh` already supplies
`JASPER_DEPLOY_SHA_FULL`. A future image first-boot service has no `.git`
directory, so its pinned release manifest must explicitly supply the baked
full SHA. Dirty, shortened, uppercase, missing, or `unknown` identities are
rejected. A Pi-local clean Git checkout may derive its exact `HEAD`.

When `JASPER_FIRST_PARTY_RUNTIME_BUNDLE` is unset, the existing source builds
remain the fallback. Once it is set, verification or installation failure
blocks fallback for the rest of that install process.

If a prior install used a bundle and a later deploy selects source builds, the
installer atomically moves the old provenance to
`/opt/jasper/share/first-party-runtime-superseded/`, removes its `INSTALLED`
claim, and labels its BUILD-INFO historical before compiling. The global
verified build manifest remains the success marker for the source-built
install; stale bundle metadata is never left claiming to describe active
bytes.

The installer:

1. recovers any journaled interrupted prior attempt;
2. copies the mutable input once into a root-private snapshot;
3. verifies and installs only from that snapshot;
4. stages every destination and records backups in a durable transaction
   journal before the first replacement;
5. activates the complete artifact set;
6. atomically publishes the exact verified snapshot, notices included, under
   `/opt/jasper/share/first-party-runtime/`; and
7. transitions the rollback journal to an idempotent committed-cleanup record
   only after the `INSTALLED` success marker and complete provenance tree are
   durable, then removes backups and that record.

A crash before commit is rolled back on the next installer invocation; a crash
after commit keeps the new files and finishes cleanup. The installer never
verifies one tree and copies artifacts from another.

## Changing the Artifact Set

Keep changes in this order:

1. Update `artifacts.toml`; do not start in an installer helper.
   `build_system` and `build_output_path` declare how the fresh output root is
   populated; no builder output path belongs in Python or shell.
2. Update the BUILD-INFO schema/parser only if metadata shape changes.
3. Ensure the exact active target dependency graph has distributable license
   metadata and real notice files.
4. Add/adjust fail-closed tests in
   [`tests/test_first_party_arm64_release.py`](../tests/test_first_party_arm64_release.py).
5. Run the hardware-free checks below.
6. Run the manual native workflow.
7. Inspect its BUILD-INFO, notice index, package inventory, hashes, and ELF
   evidence.
8. Promote only after a Pi 5 install and runtime health pass.

Hardware-free checks:

```sh
pytest -q tests/test_first_party_arm64_release.py
python3 scripts/docs-impact.py --validate-only
python3 scripts/docs-linkcheck.py \
  --changed-file docs/HANDOFF-first-party-arm64-artifacts.md
```

## Known Gaps

- No public artifact URL, signature, transparency log, or automatic release.
- No Debian snapshot or proven two-build bit identity.
- No Pi hardware boot/runtime evidence from CI.
- No whole-image SBOM or redistribution clearance.
- No retention policy beyond the workflow's short review-artifact window.
- The native GitHub ARM64 runner label is still a public-preview dependency.

These are explicit promotion gates, not reasons to put compilation back on
the first boot path.

Last verified: 2026-08-26 (kept whole — every claim re-checked and still true:
the workflow is `workflow_dispatch`-only on `ubuntu-24.04-arm` in a
digest-pinned `debian:trixie-*-slim` container, uploads a 14-day review
artifact and creates no release; `artifacts.toml` still declares the target
triple/ELF class/interpreter/`allowed_needed`/`required_symbols`/build
commands/install paths/modes and the Cargo license allowlist; the builder and
verifier flags, `BUILD-INFO.schema.json`,
`tests/test_first_party_arm64_release.py`, and the
`JASPER_FIRST_PARTY_RUNTIME_BUNDLE` seam in
`deploy/lib/install/first-party-runtime.sh` — including the
`first-party-runtime-superseded/` retirement path — all match)
