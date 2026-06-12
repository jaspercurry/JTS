# Handoff: Supply Chain Provenance

Current state first. This doc is the canonical reference for install and
build-time third-party inputs: what JTS fetches, how those inputs are
pinned, and what remains intentionally unresolved.

## Current Policy

JTS treats deploy-time network inputs as part of the appliance's trusted
computing base. If a script downloads code, a binary, firmware tooling,
or a model that later runs on the speaker, that input needs an entry in
[`deploy/provenance.toml`](../deploy/provenance.toml).

The manifest is deliberately small and operational:

- Release archives, source archives, `.deb` files, and model files
  record a SHA-256.
- Source builds consume commit archive URLs where practical. The
  immutable commit stays recorded even when the operator-friendly
  version remains a tag name.
- Install-time source builds consume byte-exact JTS release-asset
  mirrors for upstream GitHub/GitLab auto-generated source archives.
  The upstream commit archive URLs stay in provenance as
  `upstream_url` / `upstream_resolved_url`.
- Firmware top-level PlatformIO inputs record exact versions or commits.
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
- `firmware/dial/platformio.ini`
- `firmware/satellite-amoled/platformio.ini`
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

- `nqptp-c925f27c1fd1.tar.gz` mirrors upstream
  `https://github.com/mikebrady/nqptp/archive/c925f27c1fd12e4033ac477e5a405969b0b0260b.tar.gz`;
  SHA-256 `d2c2fe5d2574d447a817b1585e82c38f4c98774dac8284e5a3f17e188a3a75f9`.
- `shairport-sync-0b1c4391ffd3.tar.gz` mirrors upstream
  `https://github.com/mikebrady/shairport-sync/archive/0b1c4391ffd398e7b145eb4b98416261380adeea.tar.gz`;
  SHA-256 `7ef3a6ba1cbd67bb200f018ddcd3e8dbe40da98b3c1776aee6c7b832632c6865`.
- `webrtc-audio-processing-846fe90a289f.tar.gz` mirrors upstream
  `https://gitlab.freedesktop.org/pulseaudio/webrtc-audio-processing/-/archive/846fe90a289f58b7c9303a635142aa2c7caa93e5/webrtc-audio-processing-846fe90a289f58b7c9303a635142aa2c7caa93e5.tar.gz`;
  SHA-256 `ddf4e540b9f4291e140cc2ab4560f3eb4fce07ef6212a94d980843bfbf9a4588`.

CamillaDSP `v4.1.3`, Raspotify `0.48.1`, and CamillaGUI `4.1.0`
already consume upstream release assets rather than auto-generated commit
archives, so they do not need JTS mirrors in this slice.

The Python dependency for `pycamilladsp` uses a direct commit archive
URL in `pyproject.toml` with a `#sha256=` fragment. This keeps the
base Pi install from needing `git` just so pip can fetch that package.
It is tracked under the Python dependency accepted gap because mirroring it
requires a `pyproject.toml` dependency URL change, not an install.sh
source-build URL change.

Python dependency determinism is partially started but not complete.
Several direct runtime dependencies are exact-pinned in
`pyproject.toml`, other direct dependencies are bounded where upstream
compatibility matters, and [CONTRIBUTING.md](../CONTRIBUTING.md)
recommends `uv sync` for local contributor setup. The repository does
not currently commit a shared Python lock artifact, and deploy/CI still
install from `pyproject.toml` through pip resolution.

The two PlatformIO firmware projects now pin their shared git library
dependency by commit and use exact top-level registry versions rather
than semver ranges. The pioarduino platform archive has a recorded hash
in the manifest, but PlatformIO itself does not consume that hash yet.
Normal speaker installs copy the optional firmware source tree but do
not run PlatformIO unless the operator explicitly sets
`JASPER_BUILD_OPTIONAL_FIRMWARE=1`; maintainers use
`scripts/check-firmware-builds.sh` when touching firmware or
PlatformIO pins.

Rust audio daemons commit lockfiles for their binary crates:
`rust/jasper-fanin/Cargo.lock` and `rust/jasper-outputd/Cargo.lock`.
`install.sh` builds both crates with `cargo --locked`, so lock drift
fails deploy instead of resolving live. The provenance checker fails if
either lockfile disappears or no longer covers the crate's direct
dependencies.

## Accepted Gaps

These are real and intentionally left for later slices:

- **Apt packages.** `install_deps` uses package names from the current
  Raspberry Pi OS / Debian repositories. Apt signatures protect
  transport and repository integrity, but installs are not snapshot-
  pinned.
- **Python runtime/build dependencies.** Deploy still uses pip
  resolution from `pyproject.toml`, and `jasper_aec3` build isolation
  resolves `jasper_aec3/pyproject.toml` requirements. Do not duplicate
  the local-development `uv sync` story with an unrelated deploy-only
  lock. Python lock adoption is deliberately deferred while `main` is
  moving quickly; when resumed, choose one shared artifact (`uv.lock` or
  generated hash requirements), commit it, and make install/CI consume
  it deliberately.
- **PlatformIO transitive/toolchain resolution.** Top-level firmware
  inputs are exact, but PlatformIO still consults its package registry
  for toolchains and metadata.
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

## Staff-Level Review Notes

This slice intentionally does not attempt a full SBOM, Nix-style
hermetic build, or distro snapshot. That would be too large for the
current project shape and would slow the Pi bring-up path. The value
here is smaller and concrete: the artifacts JTS downloads directly are
now visible, mostly immutable, and checked before use.

The openWakeWord package-helper gap is closed without changing the
operator-facing wake model strings. The `/wake/` picker can still save
stock names like `hey_jarvis`, while install now stages the exact ONNX
package-resource files those names resolve to and verifies their hashes.
The active/fallback stock model is treated as runtime-critical; inactive
stock options are optional so a transient upstream download failure does
not block unrelated deploys.

Python install determinism remains valuable, but it is intentionally not
the next slice while `main` is changing quickly. When it comes back, it
needs a deliberate design choice: either promote `uv.lock` to the shared
source of truth or generate hash requirements from it, then update
install/CI together so there is only one dependency-management story.

The 2026-06-01 install-productization slice removed the base install's
direct `git` fetches for `nqptp`, `shairport-sync`,
`webrtc-audio-processing`, and `pycamilladsp`. The 2026-06-12
source-mirroring slice moved the three install.sh source-build archives
to byte-exact JTS release assets while retaining upstream commit archive
URLs as provenance. Optional firmware builds may still involve
PlatformIO's git-backed library handling, but that path remains opt-in
behind `JASPER_BUILD_OPTIONAL_FIRMWARE=1`.

For the current private fleet, this slice is intentionally fresh/rebuild
focused. Existing installed renderer binaries are not fingerprinted and
forced through reinstall because there are only two known speakers and
both are operator-owned development boxes. If we ever distribute images
or support third-party speakers, add a migration/check path that records
or rebuilds already-installed `librespot`, `nqptp`, `shairport-sync`,
and CamillaGUI bits.

Last verified: 2026-06-12
