# ADR-0164: A Pi image is a cached, versioned input to the installer, not a second installer

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-pi-image-delivery.md was trimmed
  to its operational spine; no `.img.xz` has been built yet — this is the rule
  the first one must obey)

## Context

The supported path from a blank SD card to a working speaker is Raspberry Pi
OS Lite plus `scripts/onboard.sh` running `deploy/install.sh`. A custom
`.img.xz` for Raspberry Pi Imager would cut time-to-first-sound, and the
obvious way to build one is to bake the finished filesystem: services enabled,
paths populated, config written.

That is the trap. `main` moves daily. An image builder that knows the list of
units, paths, environment keys, and post-install steps becomes a second
installer, and the two drift the moment either side changes. The failure is
silent and lands on a new user's first boot — the surface with the least
diagnostic feedback JTS has.

## Decision

**The image is a cached, versioned input to the installer. It is never a
second installer.**

`deploy/install.sh`, its libraries, reconcilers, registries, and migrations
remain the single owners of runtime layout and host configuration. An image
builder may only:

- start from a pinned Raspberry Pi OS Lite arm64 base;
- preload verified release artifacts, package caches, and the JTS source
  snapshot;
- arrange for the existing installer to run in an image-build or first-boot
  mode;
- seed a versioned release manifest.

It must not carry a parallel list of services, paths, environment keys, or
post-install steps. **If image creation needs a new primitive, that primitive
is added to the normal installer and called from both paths.**

Two constraints fall out of the same rule and are equally binding:

- **No machine-unique or household material is baked into a published image** —
  no API key, OAuth token, Wi-Fi credential, household credential, SSH host
  key, or device identity. It is generated or supplied after the card is
  written (non-negotiable #3's compartments do not survive a golden image).
- **First boot never installs the tip of `main`.** An image is built from an
  exact commit with the full SHA recorded, and the image version and the JTS
  application version keep separate identities. Checking for an application
  update happens only after the speaker has reached core value, as an explicit
  observable transaction.

## Consequences

- **Three entry paths converge on one code path.** A developer's
  `deploy-to-pi.sh`, a fresh stock-OS install, and a custom image all run the
  same installer, so a fast-moving `main` stays survivable and an image bug is
  an installer bug with one place to fix it.
- **The image cannot be "fixed up" at build time.** A capability the image
  needs and the installer lacks is a missing installer primitive, not a
  scriptable step in the image recipe. This is the rule most likely to feel
  expensive in the moment and is exactly the one worth holding.
- **Day-to-day development does not rebuild images.** Images are manually
  promoted for a milestone; merges do not trigger them.
- **A published image is auditable.** Base-image URL/version/hash, artifact
  bundle hash, package inventory, notice-bundle hash, image-builder version,
  and build time are recorded, which is what makes gate 1 (a machine inventory
  of every byte added beyond the base OS) achievable at all.
- **The first-party ARM64 bundle stays narrow.** It certifies its own contents
  and clears nothing else in the image; whole-image redistribution is a
  separate question that `LICENSE-third-party.md` registers.

## References

- `docs/HANDOFF-pi-image-delivery.md` — the operational spine: layer
  ownership, first-boot transaction, image-factory choice, promotion gates.
- `docs/HANDOFF-first-party-arm64-artifacts.md` — the artifact contract an
  image consumes.
