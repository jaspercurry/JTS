# ADR-0145: Remote updates stay a laptop deploy

- **Date:** 2026-08-26
- **Status:** Accepted (the recommendation stood unbuilt from 2026-05-15;
  recorded here when HANDOFF-remote-updates.md was retired to an archive)

## Context

The owner asked for a "Check for updates" button on `/system/`: the speaker
checks GitHub, pulls validated code, installs it, restarts the daemons — so a
fix can ship without SSH from a laptop on the same LAN. A full option survey
was written (`git pull` from the dashboard, GitHub Releases + Pi poll, a
self-hosted Actions runner on the Pi, RAUC/Mender A/B partitions, a venv-swap
symlink flip) with a staged build-out and a failure/rollback strategy.

Stage 1 of that plan — GitHub Actions CI — shipped (`.github/workflows/tests.yml`,
2026-05-23) and paid off immediately. Stages 2 and 3 (auto-release, the button)
never did, and the reasons compounded: the update machinery is ~300–500 lines
of new code that can wedge the speaker, the management surface has no
authentication (anyone on the home WiFi could click it), and the actual driver
behind the request — "I am not always on the LAN" — has a boring answer.

## Decision

**`bash scripts/deploy-to-pi.sh` from a laptop stays the only deploy path, and
Tailscale is the answer to being off the LAN.** A mesh VPN puts laptop and Pi on
one virtual network from anywhere; the existing script then works from a coffee
shop with no code changes, ~10 minutes of setup, no new LAN attack surface, and
no new failure modes.

**If the button is ever built, it is Option B**: CI tags a release on a green
`main`, the Pi polls the GitHub Releases API, compares against
`/var/lib/jasper/build.txt`, and applies with `git checkout <tag>` +
`install.sh` — the long-running task on `jasper-control` (never on the
idle-exiting page server), a rollback to the snapshotted prior SHA on a failed
`jasper-doctor`, and an audible cue on the failure that does not roll back.

## Consequences

- The only trigger that would genuinely justify building it is a household
  member or a no-laptop operator needing to update, not a travelling
  maintainer. That is the revisit condition.
- Authentication is a prerequisite, not a follow-up: the consequence of an
  unauthorised click here is bricking the speaker, unlike the other
  unauthenticated wizard buttons.
- Explicitly rejected shapes, so they are not re-surveyed: a dashboard
  `git pull` (no validation gate, no rollback story); a self-hosted Actions
  runner on the Pi (push, not pull — it deletes the click-to-update UX the
  request was about, and runs workflow code on the speaker); RAUC/Mender A/B
  partitions (they assume you build the OS image, and JTS runs stock Raspberry
  Pi OS — revisit only if JTS is ever distributed to households, at which point
  the base image changes too).
- The architectural win the survey found still holds and is worth keeping true:
  `deploy/install.sh` is idempotent, so an updater re-runs it rather than
  reimplementing per-layer update logic. The one layer that stays out of any
  such scope is XVF3800 firmware (out-of-band DFU, not in install.sh's blast
  radius).
- The full survey, the five failure modes, the auth ladder, and the prior-art
  reading live in
  [`historical/remote-updates-design-space-2026-05.md`](../historical/remote-updates-design-space-2026-05.md).
