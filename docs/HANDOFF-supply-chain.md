# Handoff: Supply Chain Provenance

Current state first. This doc is the canonical reference for install and
build-time third-party inputs: what JTS fetches, how those inputs are
pinned, and what remains intentionally unresolved.

## Current Policy

JTS treats deploy-time network inputs as part of the appliance's trusted
computing base. If a script downloads code, a binary, or a model that
later runs on the speaker, that input needs an entry in
[`deploy/provenance.toml`](../deploy/provenance.toml).

The manifest is deliberately small and operational:

- Release archives, source archives, `.deb` files, and model files
  record a SHA-256.
- Source-build provenance records immutable commit archive URLs where
  practical. The immutable commit stays recorded even when the
  operator-friendly version remains a tag name.
- Install-time source builds consume byte-exact JTS release-asset
  mirrors for upstream GitHub/GitLab auto-generated source archives.
  The upstream commit archive URLs stay in provenance as
  `upstream_url` / `upstream_resolved_url`.
- Known gaps are represented as `[[surface]]` entries instead of being
  hidden in prose.

Run the local check before changing install/build fetches:

```sh
python3 scripts/check-provenance.py
```

To preview the install-time blast radius without mutating a host, run:

```sh
bash deploy/install.sh --dry-run
# or: JASPER_INSTALL_DRY_RUN=1 bash deploy/install.sh
```

Dry-run mode exits before the root check and prints the planned apt
package groups, direct downloads, source builds, runtime file writes,
env migrations, boot/config writes, systemd actions, restarts, and
post-install checks. It is a contributor planning aid, not a substitute
for hardware validation: the real installer remains the source of truth
for exact host-specific no-op decisions.

The provenance check validates manifest shape and verifies the known
fetch-bearing surfaces still have provenance entries:

- `deploy/install.sh`
- `pyproject.toml` direct URL dependencies
- `jasper_aec3/pyproject.toml` build requirements and direct URL dependencies
- `jasper/wake_models.py`
- `jasper/aec_engines/dtln_models.py`

Model downloads that install.sh performs through JTS Python use
`jasper.model_downloads.download_model_file`: each fetch has an
explicit socket timeout, retry count, maximum byte count, temp-file
staging, and SHA-256 verification before replacement.

## Pinned And Verified Today

`deploy/install.sh` verifies these downloaded artifacts with
`sha256sum -c` before installing or staging them:

- CamillaDSP `v4.1.3` aarch64 release archive.
- Raspotify `0.48.1` arm64 `.deb` carrying librespot `0.8.0-ea81314`.
- CamillaGUI `4.1.0` Linux bundles for `aarch64`, `x86_64`, and
  `armv7l`.
- Curated external wake model `jarvis_v2.onnx`.
- openWakeWord ONNX package-resource assets from the upstream `v0.5.1`
  release. The shared runtime assets are required fail-fast:
  `embedding_model.onnx`, `melspectrogram.onnx`, and
  `silero_vad.onnx`. The compiled fallback stock model
  `hey_jarvis_v0.1.onnx`, plus any active stock wake model, is also
  required. Inactive stock wake models (`alexa`, `hey_mycroft`,
  `hey_rhasspy`, `timer`, `weather`, etc.) are best-effort; if their
  bounded download fails, install continues and `/wake/` disables those
  rows until the next successful deploy/install.
- DTLN-aec ONNX model stages listed in `jasper/aec_engines/dtln_models.py`.

`jasper-doctor` re-checks presence and hashes at runtime for the opaque
model files that JTS stages directly and later loads through
ONNX/openWakeWord: required openWakeWord package assets, the active wake
model (hash-checked when the registry has a SHA-256 for it), and the
configured DTLN-aec ONNX stages when `JASPER_AEC_DTLN_ENABLED=1`. It
intentionally does **not** hash every installed package or source-built
binary; those surfaces are verified at install time and doctor checks
their behavior/version/service state instead.

`deploy/install.sh` also builds these source inputs from JTS release-asset
mirrors and verifies each archive with `sha256sum -c` before unpacking.
The mirrored bytes were downloaded from the upstream pinned commit archive
URLs and SHA-256 verified against `deploy/provenance.toml` before upload:

- `nqptp-c925f27c1fd1.tar.gz`
- `shairport-sync-0b1c4391ffd3.tar.gz`
- `webrtc-audio-processing-846fe90a289f.tar.gz`

Each mirror's upstream commit-archive URL and SHA-256 live in
`deploy/provenance.toml` as `upstream_url` / `upstream_resolved_url` /
`sha256`; `check-provenance.py` fails if `install.sh`'s copy of a hash drifts
from the manifest, so the manifest is the single place to read or change them.

CamillaDSP `v4.1.3`, Raspotify `0.48.1`, and CamillaGUI `4.1.0`
already consume upstream release assets rather than auto-generated commit
archives, so they do not need JTS mirrors in this slice.

The Python dependency for `pycamilladsp` uses a direct commit archive
URL in `pyproject.toml` with a `#sha256=` fragment. This keeps the
base Pi install from needing `git` just so pip can fetch that package.
It is tracked under the Python dependency accepted gap because mirroring it
requires a `pyproject.toml` dependency URL change, not an install.sh
source-build URL change.

Python dependency determinism is split by runtime target:

- **Laptop/local development** uses the committed `uv.lock` with
  [CONTRIBUTING.md](../CONTRIBUTING.md)'s `uv sync` flow. This pins the
  developer environment without pretending it is a Raspberry Pi wheel
  lock.
- **GitHub Actions** also consumes the committed `uv.lock`, on Ubuntu
  x86_64 / Python 3.13, with
  `uv sync --locked --extra full --extra dev --group openwakeword-onnx`.
  That makes the hardware-free CI slice test the reviewed dependency
  resolve instead of whatever PyPI happens to satisfy on a given run.
  The `openwakeword-onnx` dependency group keeps openWakeWord's non-tflite
  helper packages (`requests`, `tqdm`, `scikit-learn`) lock-covered. The
  only post-sync Python install is the exact ONNX-only
  `openwakeword==0.6.0 --no-deps` workaround; it remains outside `uv.lock`
  because the package's declared `tflite-runtime` dependency is
  unsatisfiable on Python 3.13 and is not imported by JTS.
- **Pi deploys** use the committed, Pi-generated
  `deploy/constraints-pi.txt` when present. `install.sh` passes it to
  pip via `-c` for open-range runtime installs, replaying a reviewed
  on-device resolve while still letting exact-pinned bootstrap installs
  (`pip`, `wheel`, `openwakeword --no-deps`) stay simple.
  `scripts/generate-pi-constraints.sh` drops the known Debian-only
  `flatbuffers==20181003210633` freeze value because PyPI cannot replay
  it; `onnxruntime` then resolves a published flatbuffers wheel during
  deploy. `tests/test_constraints_pi_resolvable.py` keeps the overlay in
  lockstep with `uv.lock`, dry-runs the actual versioned pyproject
  requirements, and separately resolves Linux aarch64 / Python 3.13 so
  x86-only availability cannot make the deploy guard falsely green.

This is one dependency-management story with platform-specific resolution
artifacts where they matter: `uv.lock` for laptop and GitHub Actions
x86_64 environments, and `deploy/constraints-pi.txt` for the arm64 Pi
runtime. The remaining Python gap is hash-level verification for PyPI
artifacts and `jasper_aec3` build-isolation dependencies.

Rust audio daemons commit lockfiles for their binary crates:
`rust/jasper-fanin/Cargo.lock` and `rust/jasper-outputd/Cargo.lock`.
`install.sh` builds both crates with `cargo --locked`, so lock drift
fails deploy instead of resolving live. The provenance checker fails if
either lockfile disappears or no longer covers the crate's direct
dependencies.

## First-Party ARM64 Release Bundle

The manual first-party ARM64 lane turns the locked Rust daemons and JTS ALSA
ring plugin into a narrow, self-verifying release bundle. Its BUILD-INFO
records the exact clean Git commit, active Cargo graphs, lockfile hashes,
compiler versions, complete Debian build-root package inventory, ELF dynamic
dependencies, and stable build environment. Its checksum manifest covers the
complete bundle, including exact Cargo license files and derived/system
notices.

The install seam additionally requires the bundle's commit to equal the exact
source revision being installed. An invalid, dirty, unknown, or mismatched
identity fails closed instead of falling back to a different source build.

Exact mechanics and the important non-hermetic apt boundary live in
[`HANDOFF-first-party-arm64-artifacts.md`](HANDOFF-first-party-arm64-artifacts.md).
This narrow bundle is not a whole-image SBOM or redistribution clearance.

## Accepted Gaps

These are real and intentionally left for later slices:

- **Apt packages.** `install_deps` owns the full-profile package set,
  `install_streambox_deps` owns the streambox-specific set, and their shared
  `_install_renderer_native_deps` helper owns the renderer package set used by
  both profiles. All three resolve package names from the current Raspberry Pi
  OS / Debian repositories. Apt signatures protect transport and repository
  integrity, but installs are not snapshot-pinned.
- **Python hash-level verification.** Local development and GitHub
  Actions have `uv.lock`, and Pi deploys have `deploy/constraints-pi.txt`,
  but pip/uv still download PyPI wheels/sdists without an enforced
  artifact mirror, CI's exact `openwakeword --no-deps` workaround remains
  a narrow exception, and `jasper_aec3` build isolation still resolves
  `jasper_aec3/pyproject.toml` requirements. The next supply-chain slice
  should add a hash-checked artifact or mirror path without collapsing the
  local-development/CI and Pi-runtime lock stories into one misleading file.
- **Python direct archive hosting.** `pycamilladsp` is pinned by commit
  and SHA-256 in `pyproject.toml`, but pip still downloads an upstream
  GitHub commit archive directly. Mirroring it should happen with the
  broader Python dependency determinism work so the project has one
  dependency-management story.

## Update Workflow

When adding or changing a network fetch:

1. Add or update the entry in `deploy/provenance.toml`.
2. Prefer immutable URLs and commits. If the upstream only exposes a
   mutable tag or branch, resolve it to a commit and prefer a commit
   archive URL with a recorded SHA-256 over a Pi-side checkout.
3. For binary/model/archive artifacts, compute SHA-256 from the exact
   file the install path downloads:

   ```sh
   sha256sum path/to/artifact
   ```

4. Wire the runtime/install path to verify the hash before unpacking,
   installing, or replacing an existing model.
5. Run `python3 scripts/check-provenance.py`.
6. If the fetch is a known gap that cannot be pinned yet, add a
   `[[surface]]` entry with `status = "accepted-gap"` and explain why.

## Out of Scope

This policy is deliberately not a full SBOM, a Nix-style hermetic build, or a
distro snapshot. What it buys is narrower and concrete: the artifacts JTS
downloads directly are visible, mostly immutable, and checked before use.

Nor does it fingerprint or force-reinstall renderer binaries that are *already*
on a box — the fleet is two operator-owned development speakers, so provenance
is enforced on fresh installs and rebuilds only. Distributing images or
supporting third-party speakers would need a migration/check path that records
or rebuilds already-installed `librespot`, `nqptp`, `shairport-sync`, and
CamillaGUI bits; until then that path would be machinery guarding nothing.

Last verified: 2026-08-26 (versions, mirror filenames, checker surfaces, and
the uv.lock / constraints-pi.txt split re-checked against
`deploy/provenance.toml`, `deploy/install.sh`, and
`scripts/check-provenance.py`)
