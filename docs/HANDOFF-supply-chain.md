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

- Release archives, `.deb` files, and model files record a SHA-256.
- Git source builds record an immutable commit, even when the operator-
  friendly version remains a tag name.
- Firmware top-level PlatformIO inputs record exact versions or commits.
- Known gaps are represented as `[[surface]]` entries instead of being
  hidden in prose.

Run the local check before changing install/build fetches:

```sh
python3 scripts/check-provenance.py
```

That check validates manifest shape and verifies the known fetch-bearing
surfaces still have provenance entries:

- `deploy/install.sh`
- `pyproject.toml` direct URL dependencies
- `jasper_aec3/pyproject.toml` build requirements and direct URL dependencies
- `firmware/dial/platformio.ini`
- `firmware/satellite-amoled/platformio.ini`
- `jasper/wake_models.py`
- `jasper/aec_engines/dtln_models.py`

## Pinned And Verified Today

`deploy/install.sh` verifies these downloaded artifacts with
`sha256sum -c` before installing or staging them:

- CamillaDSP `v4.1.3` aarch64 release archive.
- Raspotify `0.48.1` arm64 `.deb` carrying librespot `0.8.0-ea81314`.
- CamillaGUI `4.1.0` Linux bundles for `aarch64`, `x86_64`, and
  `armv7l`.
- Curated external wake model `jarvis_v2.onnx`.
- DTLN-aec ONNX model stages listed in `jasper/aec_engines/dtln_models.py`.

`deploy/install.sh` also verifies checked-out git source trees before
building:

- `nqptp` pinned to commit `c925f27c1fd12e4033ac477e5a405969b0b0260b`.
- `shairport-sync` tag `4.3.7`, commit
  `0b1c4391ffd398e7b145eb4b98416261380adeea`.
- `webrtc-audio-processing` tag `v2.1`, commit
  `846fe90a289f58b7c9303a635142aa2c7caa93e5`.

The Python direct git dependency for `pycamilladsp` is pinned to the
`v4.0.0` tag commit in `pyproject.toml` rather than the tag name.

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

The Rust fan-in daemon commits `rust/jasper-fanin/Cargo.lock`.
`install.sh` builds that binary crate from `rust/jasper-fanin` with
`cargo --locked`, so lock drift fails deploy instead of resolving live.
The provenance checker fails if the lockfile disappears or no longer
covers the crate's direct dependencies.

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
  lock. The next Python supply-chain slice should choose one shared
  artifact (`uv.lock` or generated hash requirements), commit it, and
  make install/CI consume it deliberately.
- **openWakeWord bundled model helper.** `openwakeword.utils.download_models()`
  still downloads the package's stock models outside JTS's explicit
  registry. Replacing that helper with an explicit hash-checked model
  registry is the right follow-up.
- **PlatformIO transitive/toolchain resolution.** Top-level firmware
  inputs are exact, but PlatformIO still consults its package registry
  for toolchains and metadata.

## Update Workflow

When adding or changing a network fetch:

1. Add or update the entry in `deploy/provenance.toml`.
2. Prefer immutable URLs and commits. If the upstream only exposes a
   mutable tag or branch, resolve it to a commit and verify the checkout
   before build.
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

Python install determinism remains the highest-leverage supply-chain
follow-up, but it needs a deliberate design choice: either promote
`uv.lock` to the shared source of truth or generate hash requirements
from it, then update install/CI together so there is only one
dependency-management story.

For the current private fleet, this slice is intentionally fresh/rebuild
focused. Existing installed renderer binaries are not fingerprinted and
forced through reinstall because there are only two known speakers and
both are operator-owned development boxes. If we ever distribute images
or support third-party speakers, add a migration/check path that records
or rebuilds already-installed `librespot`, `nqptp`, `shairport-sync`,
and CamillaGUI bits.

Last verified: 2026-05-27
