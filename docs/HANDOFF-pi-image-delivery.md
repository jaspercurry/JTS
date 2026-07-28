# Handoff: Raspberry Pi Image Delivery

This is the canonical product-delivery plan for getting from a blank SD card
to a working JTS speaker with Raspberry Pi Imager. It owns the boundary
between the OS image, the application installer, release artifacts, first-boot
work, updates, and redistribution readiness. Exact artifact mechanics live in
[`HANDOFF-first-party-arm64-artifacts.md`](HANDOFF-first-party-arm64-artifacts.md);
third-party status lives in [`../LICENSE-third-party.md`](../LICENSE-third-party.md).

## Current State

JTS does **not** publish a custom Pi image yet. The supported path remains
Raspberry Pi OS Lite (64-bit) plus `scripts/onboard.sh`, which stages the
current checkout and runs `deploy/install.sh`.

The first image-delivery foundation now has two deliberately separate pieces:

1. A manual native-arm64 release-artifact workflow packages the JTS-owned
   compiled runtime (`jasper-fanin`, `jasper-outputd`, and the `jts_ring` ALSA
   plugin) with deterministic metadata, checksums, install destinations, ELF
   dependency evidence, and the notices that apply to those files. The normal
   installer can consume an explicitly staged, commit-matched bundle through
   the same runtime-layout code, with transactional activation and source-build
   fallback only when no bundle was requested.
2. The normal install keeps standard software echo cancellation available,
   while the slower enhanced WebRTC AEC3 engine is an optional background
   install. It is progressively disclosed on the System page and cannot make
   the core install fail or wait.

Neither piece is a redistributable OS image by itself. They establish the
artifact and optional-feature contracts that an image can consume without
forking JTS installation logic.

## Product Goal

For the first public setup video, the intended flow is:

1. Download one versioned `.img.xz`.
2. Select **Use Custom** in Raspberry Pi Imager.
3. Enter hostname, Wi-Fi, user, and SSH settings in Imager.
4. Write the card and boot the Pi.
5. Reach `http://<hostname>.local/` within a few minutes, with core playback,
   setup, and measurement surfaces ready.
6. Configure secrets and optional integrations in the browser.

The first image should target the **Pi 5 full-speaker profile**. The same
arm64 artifacts may serve other 64-bit Pis, but a Pi Zero 2 W streambox image
has different product capabilities and needs its own boot-time evidence before
it is advertised.

“A few minutes” is a release gate to measure, not a promise inferred from build
steps. The first image candidate should target:

- no Rust, C, or WebRTC v2 compilation before the management UI is useful;
- no update from a mutable branch during first boot;
- a visible first-boot state instead of an apparently dead speaker;
- core readiness in at most five minutes on the supported Pi/storage pair.

## The Architectural Rule

**The image is a cached, versioned input to the installer; it is not a second
installer.**

`deploy/install.sh`, its libraries, reconcilers, registries, and migrations
remain the single owners of runtime layout and host configuration. An image
builder may:

- start from a pinned Raspberry Pi OS Lite arm64 base;
- preload verified release artifacts, package caches, and the JTS source
  snapshot;
- arrange for the existing installer to run in an image-build or first-boot
  mode;
- seed a versioned release manifest.

It must not carry a parallel list of services, paths, environment keys, or
post-install steps. If image creation needs a new primitive, add that primitive
to the normal installer and call it from both paths.

This keeps a fast-moving `main` survivable: normal developer deploys, a fresh
stock-OS install, and a custom image all converge through the same code.

## What Belongs in Which Layer

| Layer | Owns | Must not own |
|---|---|---|
| Base OS image | Boot firmware, kernel, Debian/Raspberry Pi OS packages, Imager-compatible first-user/network customization | JTS secrets or mutable `main` |
| JTS release manifest | Exact JTS commit, artifact digests, base-image identity, package/cache inventory, notice-bundle digest | Runtime state |
| Release artifact bundle | Architecture-verified first-party compiled files, install paths, hashes, build evidence, applicable notices | OS packages or an assertion that the whole image is compliant |
| `deploy/install.sh` | Runtime filesystem layout, dependencies, migrations, units, reconciler setup, validation | Image partition construction |
| First-boot service | Apply Imager personalization, invoke the installer against pinned local inputs, expose bounded progress, retry safely | Long source builds or release selection |
| `/etc/jasper` and `/var/lib/jasper` | Operator intent, wizard-owned state, device identity, install/build status | Golden-image data copied from a build machine |

No API key, OAuth token, Wi-Fi credential, household credential, SSH host key,
or device identity may be baked into a published image. Machine-unique material
must be generated or supplied after the card is written.

## The Delivery Gradient

There is no legal or technical cliff between “bootstrap” and “full image.”
Every extra byte can be promoted independently when it earns its place.

| Form | Pre-included JTS payload | First-value target | Redistribution surface | Verdict |
|---|---|---:|---|---|
| Stock OS + onboarder | None | 15–20 min today | Smallest JTS binary-distribution surface | Supported fallback |
| Thin bootstrap image | First-boot agent and pinned manifest | 5–15 min | Base OS + bootstrap bytes; fetched payload still has its own terms | Useful intermediate proof |
| Hybrid core image | Installer, Python runtime/cache, verified core renderers, first-party arm64 artifacts | 1–5 min | Every included core dependency needs a closed notice/source record | **Recommended public target** |
| Full appliance snapshot | Core plus models, firmware, every renderer, and optional engines | Near-immediate | Widest compliance, security-update, and hardware-drift burden | Do not build first |

The hybrid is the compromise: pre-install what blocks a new user from hearing,
streaming, configuring, or measuring; move specialist or expensive capabilities
behind optional installs. Enhanced AEC is the first implementation of that
policy.

## Redistribution Boundary

Non-commercial distribution is still distribution. It may make owner outreach
unnecessary as a practical matter, but it does not waive license conditions.

A bootstrap image narrows the work; it does not erase it:

- JTS distributes the base image and every file embedded in it.
- JTS distributes any artifact or source archive hosted in a JTS release.
- When a device fetches directly from Debian, Raspberry Pi, or an upstream
  project, that publisher supplies those bytes; JTS still must obey relevant
  terms in its own wrapper, modifications, and presentation.
- Automating a download is not a license bypass. Opaque firmware or another
  uncleared component should remain absent or user-initiated from its canonical
  source until its terms are understood.

For permissively licensed dependencies, the normal solution is preserving
copyright and license notices, not asking every repository owner for
permission. Copyleft components can also be redistributable, but may require
corresponding source, build scripts, relinking rights, or other exact
obligations. `LICENSE-third-party.md` is the status register; the image release
gate reads it rather than recreating conclusions here.

The first-party arm64 bundle intentionally certifies only its own contents. It
does **not** clear CamillaDSP, librespot/raspotify, shairport-sync, nqptp,
CamillaGUI, models, firmware, Debian packages, or a future whole image.

## Release Model for a Fast-Moving Codebase

`main` is a development stream. An image is an immutable release.

- Never make first boot clone or install the current tip of `main`.
- Build from an exact commit and record the full SHA.
- Give the image version and JTS application version separate identities.
- Record the base-image URL/version/hash, artifact bundle hash, package
  inventory, notice-bundle hash, image-builder version, and build time.
- Publish only after promotion; a green workflow artifact is not automatically
  a public release.
- Let day-to-day Pi development continue through `deploy-to-pi.sh`. Do not
  rebuild an OS image for every merge.
- Check for application updates only after the speaker has reached core value.
  Applying an update should be an explicit, observable transaction, not hidden
  inside the first boot.

A reasonable early cadence is a manually promoted image for a setup-video or
milestone release, then a rebuild when a material security/base-OS fix or a
meaningful onboarding improvement warrants it. Usage can justify automation
later.

## First-Boot Transaction

The image first-boot path should eventually be one bounded state machine:

```text
base boot
  -> apply Imager identity/network/user settings
  -> validate pinned local release inputs
  -> install/reconcile core
  -> run the profile-aware health gate
  -> publish ready state
  -> offer optional features and later updates
```

Required properties:

- Persist a small atomic status document so power loss can resume or retry.
- Treat the verified build manifest as the success marker; write it last.
- Keep the landing/status surface available while nonessential work continues.
- Retain the prior verified payload until a replacement passes health checks.
- Bound network, disk, CPU, and memory work; never let a build OOM the live
  audio services.
- If the network is unavailable, explain what is pending and retry with
  backoff. A missing optional feature must not make core readiness fail.

The optional enhanced-AEC job uses this same design vocabulary—durable intent,
atomic status, staged build, validate-before-activate, and fail-soft retry—at a
smaller scale.

## Image Factory Choice

Use a pinned official Raspberry Pi image-building tool for the first spike.
[`rpi-image-gen`](https://github.com/raspberrypi/rpi-image-gen) is the current
preferred candidate because it builds custom Raspberry Pi images from packaged
inputs and exposes SBOM/CVE-oriented output.
Time-box one proof before committing to it: the produced Trixie arm64 image must
retain Raspberry Pi Imager OS customization, boot headless on a Pi 5, and
accept JTS's existing installer without duplicated system configuration.

If that proof fails, use the Raspberry Pi OS
[`pi-gen`](https://github.com/RPi-Distro/pi-gen) pipeline rather than inventing
a partition/image builder. Pin the builder commit and container/host
environment either way.

## Promotion Gates for the First `.img.xz`

Do not call an image public-ready until all of these are evidence-backed:

1. Every byte deliberately added beyond the pinned base OS appears in a machine
   inventory with origin, version, hash, and install path.
2. Every included non-OS dependency has a resolved redistribution row and the
   required license/notice/source material in the image bundle.
3. The compressed image and its manifest/checksums are reproducible or all
   remaining nondeterminism is identified.
4. Raspberry Pi Imager personalization works from a clean card on the supported
   Imager version.
5. A Pi 5 cold boot reaches the management UI and core-ready state within the
   measured budget.
6. Reboot, power-loss during first boot, no network, missing mic, and missing
   DAC recover without a manual shell repair.
7. `jasper-doctor` passes the full-profile release gate.
8. No build-machine secrets, SSH host keys, device identity, logs, or household
   state survive in the image.
9. The image has a named rebuild/update owner and a documented security refresh
   cadence.

## Implementation Order

1. **Artifact + optional-feature foundation (this slice).** Produce and verify
   first-party arm64 bundles, let `install.sh` transactionally consume an
   explicitly staged commit-matched bundle, and make enhanced AEC optional,
   observable, and backgrounded.
2. **Measure the release-shaped install.** Record timing for every major
   install/first-boot phase on the supported Pi and storage pair so each
   preloading choice is evidence-driven. Define the pinned local release
   manifest that supplies source identity and bundle digests when no `.git`
   checkout exists.
3. **Core redistribution closure.** Resolve notices/source obligations for the
   core third-party bytes that materially improve time-to-value. Do not spend
   effort clearing optional firmware just to make the first image larger.
4. **Thin image spike.** Pin a Raspberry Pi OS Lite arm64 base and image-builder
   revision; prove Imager customization, first-boot status, resume, and health.
5. **Hybrid promotion.** Add only cleared, measured core payloads; emit the
   release manifest, inventory, notices, checksums, and `.img.xz`.
6. **Public release.** Manually promote one tested image. Add a custom Imager
   repository feed or automatic cadence only after more than one release proves
   the process is maintainable.

## Deferred on Purpose

- A/B root filesystems, RAUC/Mender, and signed-boot provisioning.
- Automatic image publication on every merge.
- A custom Raspberry Pi Imager catalog.
- Pre-including optional enhanced AEC, experimental firmware, or every
  integration.
- Claiming whole-image SBOM/compliance from the narrow first-party bundle.

These are future scale tools, not prerequisites for a simple first public
setup.

Last verified: 2026-07-27
