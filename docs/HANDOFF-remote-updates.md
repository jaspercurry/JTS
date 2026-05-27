# Remote software updates / CI deploy pipeline — design space

**Status (2026-05-15):** Research only. No implementation in flight.
This document captures the option space, the recommended staged
build-out, the integration points already present in the codebase,
and the open questions, so the work can be picked up coherently
later. [PLAN.md](../PLAN.md)'s "Remote software updates / CI deploy
pipeline" entry references this file.

The motivating user request:

> A button on the management dashboard (under `/system/`) that says
> "Check for updates" — checks GitHub, pulls the latest validated
> code, installs it, restarts the daemons. So I can ship fixes
> locally, test them locally, merge to GitHub, CI checks and
> compiles, and the speaker pulls the update without me needing to
> SSH from a laptop on the same LAN.

This doc deliberately does not spec an implementation. It surveys
the design space honestly so the right approach can be picked when
the work is prioritised.

---

## Today's deploy flow + what's missing

Per [CLAUDE.md](../CLAUDE.md) "Deploying code changes to the Pi", the
only supported path is `bash scripts/deploy-to-pi.sh` from the
developer laptop. The script:

1. Captures local SHA + branch (`git rev-parse`, with a `-dirty`
   suffix if the working tree is uncommitted)
2. `rsync`s to `pi@jts.local:/home/pi/jts/` (excludes `.git/`,
   `.venv/`, `*.egg-info`, etc.)
3. SSHs and runs `sudo bash deploy/install.sh` with
   `JASPER_DEPLOY_SHA{,_FULL}` and `JASPER_DEPLOY_BRANCH` env vars
   set
4. `install.sh` writes `/var/lib/jasper/build.txt` with those vars
   plus `JASPER_INSTALL_AT` timestamp, then `pip install -e
   /opt/jasper`, migrates units, restarts `jasper-voice` and
   `jasper-control`

What's missing:

- **CI gate.** There is no `.github/workflows/` directory. Nothing
  stops a broken commit from rsyncing to the Pi. "Works on my
  laptop" is the only quality bar.
- **Out-of-LAN deploy.** The script needs SSH access to
  `jts.local`. If the developer is travelling, no deploy.
- **Multi-operator deploy.** Anyone wanting to deploy needs an SSH
  key trusted on the Pi and the JTS repo checked out locally. Today
  this is a one-developer project, so this is fine; the moment a
  second household or non-Jasper operator is in the loop, it isn't.

---

## Is this even a good idea? Honest framing

For a single-Pi single-developer project, the manual flow above is
genuinely the simplest thing that works. Building OTA is real
engineering with real risks.

### Reasons to build it

- The developer isn't always near a laptop + LAN (travel, work,
  etc.).
- Family members could trigger updates without involving the
  developer.
- Forcing CI discipline catches the "works on my laptop" surprises
  before they hit the speaker.
- Foundation if a second speaker is ever built, or one is handed to
  a non-Jasper operator.

### Reasons to push back on yourself

- Approximately 300–500 LoC of new update + healthcheck + rollback
  machinery to write and maintain.
- LAN-only web UI with **no authentication** today (captured in the
  private memory note `feedback_jts_http_not_https.md` and confirmed
  across `jasper/web/*.py`): anyone on the home WiFi
  could brick the speaker by clicking the button. Not just the
  developer.
- Failure modes are scary: mid-update power cut, bad release breaks
  wake-word path, system-package install hangs on a stale apt
  mirror. The [resilience ladder](HANDOFF-resilience.md) exists
  precisely because the speaker must keep responding to wake under
  reasonable abuse.
- A "production speaker must be resilient and plug-and-play" rule
  is already enforced for hardware events (per the memory note of
  the same name); a remote-update flow that can wedge the speaker
  works against that rule, not with it.

### The cheaper alternative worth considering first

If the *real* driver is "I'm not always on the LAN to deploy",
install **Tailscale** on the Pi. Tailscale is a mesh-VPN service
that puts the laptop and Pi on a virtual LAN reachable from
anywhere; `deploy-to-pi.sh` then works from a coffee shop without
code changes. ~10 minutes of setup, no new attack surface for LAN
peers, no new failure modes.

Tailscale covers ~80% of the practical benefit for the
single-developer case. The "Check for updates" button is the
*better* answer if the trigger needs to be "family member clicks a
button" or "developer-without-a-laptop opens a phone browser" — but
if the driver is purely "laptop not on LAN", Tailscale is the
boring win and should be done first.

**My recommendation:** do the CI half (Stage 1 below) regardless,
because it pays off immediately. Only build the button half once a
concrete reason a family member or no-laptop scenario justifies it.

---

## The four layers of "updates" (conceptual frame)

Most self-update tutorials gloss over this. JTS has four distinct
layers that change at different rates and want different mechanisms.

| Layer | What changes | Today's mechanism | OTA implications |
|---|---|---|---|
| **App code** | Python files in `jasper/` | rsync + `pip install -e` (no-op on file changes since editable) | Easy. Just git pull + restart. |
| **Python deps** | `pyproject.toml` pins (e.g. `google-genai==1.13.0`, `openai>=2.36.0`, `scipy>=1.13,<2`) | `pip install -e .` re-resolves on each install.sh run | Medium. Needs network, can fail on PyPI hiccup. |
| **System packages** | `apt install ...`, source-built shairport-sync, librespot .deb, `libwebrtc-audio-processing-dev`, etc. | `install.sh` walks each section idempotently | Hard. Needs root, slow (3–5 min if shairport rebuilds), can break the audio chain mid-install. |
| **Firmware** | XVF3800 DFU image, ESP32 dial firmware, ESP32 satellite firmware | Out-of-band: BRINGUP.md DFU procedure for the chip; `jasper-dial-onboard` / `jasper-satellite-onboard` for the ESP32s | **Out of scope for OTA.** Different code paths, physically attached, not in `install.sh`'s blast radius. |

The key takeaway: a button that only handles Layer 1 is genuinely
simple. A button that handles Layers 1+2+3 is approximately what
`install.sh` already does — so the right framing isn't "write an
updater" but "make `install.sh` safely re-runnable from the
dashboard".

The good news: **`install.sh` is already idempotent.** That's most
of the battle.

---

## Option survey — simplest to most industrial

### Option A: `git pull && bash install.sh` from the dashboard

**What.** Button POSTs to a new `jasper-web` (or better,
`jasper-control`) endpoint that shells out roughly to:
`cd /home/pi/jts && git pull origin main && sudo bash
deploy/install.sh && sudo systemctl restart jasper-voice
jasper-control`.

**Versioning.** None — always whatever's at `main` right now.

**Pros.** ~50 LoC. Reuses `install.sh` entirely. Almost the same
code path as `deploy-to-pi.sh`.

**Cons.** No validation gate (broken commit on `main` ships
immediately). No "what version am I on" vs "what's available" UI.
Hard to roll back (would need `git log` to find a known-good SHA).

**Verdict.** Good for hacking, bad as a destination. Skip.

### Option B: GitHub Releases + Pi polls **(recommended)**

**What.** CI tags a release on a green build of `main` (e.g.
`v2026.05.15-abc123`). Pi-side updater hits
`https://api.github.com/repos/jaspercurry/JTS/releases/latest`,
compares the tag against `JASPER_GIT_SHA` in
`/var/lib/jasper/build.txt`, and the dashboard shows
"You're on `abc1234`. Latest is `def5678` (released 2 h ago)". Click
→ `git fetch && git checkout <tag> && install.sh`.

**Versioning.** Explicit semver-ish tags. Easy "you're on X, latest
is Y" UI.

**Pros.**

- CI gate is real.
- Rollback = `git checkout` previous tag.
- GitHub Releases API is unauthenticated for public repos (60
  req/hr/IP without a token — plenty for a poll-on-button-click
  flow).
- Maps cleanly onto existing Python libraries:
  [`tufup`](https://github.com/dennisvang/tufup) (TUF-based,
  cryptographically signed),
  [`updater4pyi`](https://pypi.org/project/updater4pyi/) (GitHub
  Releases-aware). Neither is required, but they exist if you don't
  want to write the polling/comparison code from scratch.

**Cons.** Requires building CI from scratch (no
`.github/workflows/` exists today). Adds a "tag release" step
(manual or auto).

**Verdict.** This is the right shape for JTS. Detailed in
"Recommended path" below.

### Option C: GitHub Actions self-hosted runner on the Pi

**What.** Pi runs the GitHub Actions runner agent; on merge to
`main`, the runner executes a workflow on the Pi itself
(essentially `git pull && install.sh`).

**Pros.** No Pi-side polling code. CI and deploy collapse into one
machine.

**Cons.**

- The Pi is now executing arbitrary code from any workflow you
  author, including from PRs if you're not careful.
- Adds a long-running agent (memory, security surface).
- Couples your CI to your speaker being online.
- **No "Check for updates" button** — it's push, not pull. The
  user lost the manual-click-to-update gate, which was the
  motivating UX.

**Verdict.** Wrong fit for the requested UX. Skip.

### Option D: RAUC / Mender / SWUpdate (A/B partition swap)

**What.** The Pi has two root filesystems (A and B). Update writes a
new image to the inactive slot, switches boot target, reboots. If
the new slot fails to boot or healthcheck, automatic fallback to
the old slot.

**Pros.** The gold standard. **Home Assistant OS uses RAUC** with
Pi 5's built-in "tryboot" for fallback. Steam Deck, IKEA Dirigera,
Deutsche Bahn trains. Atomic. Bulletproof rollback. Cryptographic
signing built in. RAUC's binary is ~512 KB (vs ~6.9 MB for
Mender); RAUC has no required server component.

**Cons.** Designed for systems where you **build the OS image from
source** (Yocto, Buildroot). JTS runs stock Raspberry Pi OS Lite
Trixie. Migrating to A/B partition requires either reflashing the
Pi onto a custom image with a dual-rootfs partition table, or
hand-building the dual-rootfs layout on top of RPi OS. That's a
months-long project, not a feature.

**Verdict.** Wrong scale for JTS today. Mentioning so it's clear
what we're *not* doing. If JTS is ever distributed to non-Jasper
households (a real product), revisit this — likely switch the base
image to Yocto + RAUC at that point. Until then, way too heavy.

### Option E: venv-swap + symlink flip (poor man's A/B)

**What.** Pi keeps `/opt/jasper-current` as a symlink → either
`/opt/jasper-a` or `/opt/jasper-b`. Update clones source + installs
into the inactive one, flips the symlink, restarts services. If
`jasper-doctor` fails, flip back.

**Pros.** A/B-like atomicity at the application layer without
touching the OS partition table. Rollback = flip symlink + restart.
~150 LoC.

**Cons.** Doesn't help with system packages (apt) — only Python and
bin scripts. Doesn't help if the kernel or DAC drivers break. Adds
filesystem complexity (two venvs, two `/opt/jasper-*` trees on a
1 GB Pi if you ever go back to that SKU).

**Verdict.** Sound middle-ground if the failure modes turn out to
include Python-side breakages worth atomic-rollback. Probably
overkill for the actual failure distribution (bad updates are
overwhelmingly "Python code bug" or "dep version mismatch", both of
which `git checkout previous-tag && install.sh` handles).

---

## Recommended path — staged build-out

Build it in three independently shippable stages so you can stop at
any point.

### Stage 1: GitHub Actions CI (no Pi side at all)

Add `.github/workflows/ci.yml`:

- **Triggers.** Every PR and every push to `main`.
- **Jobs.**
  - `pytest` (the existing hardware-free suite per CLAUDE.md
    "Testing").
  - Optionally `ruff` / `mypy` if/when added to the project.
  - Build the `jasper_aec3` pybind11 binding (needs `apt install
    libwebrtc-audio-processing-dev` on the runner — verify Trixie's
    v1.3-3 package is reachable on Ubuntu-latest runners; may need
    a Trixie container).
  - Build the two ESP32 firmwares via `pio run` on a matrix, using
    pioarduino (per AGENTS.md "Toolchain — Arduino-ESP32 v3.x via
    pioarduino").
- **Artifacts** (optional in Stage 1, required in Stage 2): publish
  the prebuilt wheel + firmware bins on green.
- **No deploy step yet.** Just a green check on PRs.

This stage pays for itself immediately. Stop here if you want —
most of the "is this code good?" value is in this stage alone. The
[CLAUDE.md](../CLAUDE.md) "PR flow required for main" rule already
assumes some form of pre-merge validation; CI makes that real.

### Stage 2: Auto-release on merge to `main`

Extend the workflow:

- On push to `main` (after CI passes), tag a release like
  `v2026.05.15-<short-sha>` and publish it via `gh release create`.
- Attach the prebuilt wheel and firmware bins as release assets if
  the Pi-side updater wants to consume prebuilt artifacts (avoids
  rebuilding on the Pi every time).
- Decide release cadence (see "Open questions" below).

Now you have a stable, monotonically advancing "latest validated
version" pointer.

### Stage 3: The "Check for updates" button

**Where it lives.** Per the inventory in the next section,
`jasper/web/system_setup.py` already serves the `/system/` page,
displays the build SHA, and has the POST-button pattern (the
existing `/restart/voice`, `/restart/audio`, `/reboot` handlers
that proxy to `jasper-control`). Wire one more handler that proxies
to a new `jasper-control` endpoint.

**What it does** (sketch, not a spec):

1. **Check phase** — `GET
   https://api.github.com/repos/jaspercurry/JTS/releases/latest`.
   Compare `tag_name` and `target_commitish` against
   `/var/lib/jasper/build.txt`. Display "You're on `abc1234`. Latest
   is `def5678` (released 2 h ago, [view diff on GitHub])".
2. **Apply phase** — POST handler kicks off a background task on
   `jasper-control`:
   ```
   a. Snapshot current SHA → /var/lib/jasper/previous_build.txt
   b. cd /home/pi/jts && git fetch && git checkout <new-tag>
   c. sudo bash deploy/install.sh (writes new build.txt)
   d. systemctl restart jasper-voice jasper-control
   e. sleep 15; run jasper-doctor --json
   f. If doctor passes: success cue (or silent success); done.
   g. If doctor fails:
      - git checkout <previous SHA from snapshot>
      - sudo bash deploy/install.sh
      - systemctl restart jasper-voice jasper-control
      - Play `update_failed_rolled_back` audio cue
      - Surface failure on dashboard
   ```
3. **Live status** — dashboard polls a progress endpoint (or uses
   SSE) so the user sees "Fetching… / Installing… / Restarting… /
   Verifying… / ✓ Done". Source-building shairport-sync is the
   slow step (~3–5 min); a progress bar is necessary.

**Crucial implementation details.**

- The update task must run on `jasper-control` (long-lived), **not**
  on the socket-activated `jasper-web` (which exits after 10 min
  idle per README). `jasper-control` already proxies the
  `/restart/*` and `/reboot` buttons; this is symmetric.
- Add a "currently updating" lock so simultaneous clicks don't
  trample each other.
- Gate the button behind a confirmation modal ("This will restart
  the speaker for ~5 minutes. Continue?").
- Per the AGENTS.md "no silent failure paths" rule, a failed update
  must play an audio cue. Add a new entry to
  [`jasper/cues/registry.py`](../jasper/cues/registry.py)
  (`update_failed_rolled_back`, "Update failed; the speaker rolled
  back to the previous version.") — see
  [HANDOFF-audible-feedback.md](HANDOFF-audible-feedback.md) for
  the pattern.

**Dependency / system-package updates.** `install.sh` handles these
idempotently. The button doesn't need separate logic per layer — it
just re-runs `install.sh`, which walks every section and no-ops
where nothing changed. This is the architectural win: the existing
idempotency does the heavy lifting.

---

## Integration points already in place

Inventory of what exists today that the button can build on (saves
the next implementer from re-discovering it):

- **`/var/lib/jasper/build.txt`** — written by `install.sh` lines
  421–427 with `JASPER_GIT_SHA`, `JASPER_GIT_SHA_FULL`,
  `JASPER_GIT_BRANCH`, `JASPER_INSTALL_AT`. Already the source of
  truth for "current version".
- **`jasper/web/system_setup.py`** — 490+ lines, serves
  `http://jts.local/system/`. Software card shows Version (short
  SHA), Branch, Installed (timestamp), Uptime, Voice provider.
  Existing button pattern: `/restart/voice`, `/restart/audio`,
  `/reboot` handlers at lines 431–443; client-side `fetch(path,
  {method: 'POST'})` at line 419. Adding a "Check for updates"
  button slots straight into this pattern.
- **`jasper/control/`** — `jasper-control` daemon, always-on (not
  socket-activated). Already proxies the restart buttons. The right
  home for the long-running update task.
- **`jasper/cli/doctor.py`** — `jasper-doctor` CLI, ~20 checks (env
  file, API keys, ALSA card detection, disk space, systemd units,
  dongle enumeration). Returns `ok`/`warn`/`fail` per check.
  Callable as `sudo jasper-doctor --json`. The natural post-deploy
  healthcheck.
- **`deploy/install.sh`** (1089 lines) — already idempotent. Writes
  `build.txt`. The button calls this; doesn't need to reimplement
  anything it does.
- **`scripts/deploy-to-pi.sh`** (100 lines) — captures
  `JASPER_DEPLOY_SHA{,_FULL}` and `JASPER_DEPLOY_BRANCH` from
  laptop-side `git rev-parse` before rsync, then passes them via
  env vars to `install.sh`. The Pi-side updater plays the
  laptop-side role here: read the SHA from the checked-out tag, set
  the same env vars before invoking `install.sh`.
- **The cue pattern** (per
  [HANDOFF-audible-feedback.md](HANDOFF-audible-feedback.md))
  for audible failure feedback.
- **`pyproject.toml`** — pip-editable install at `/opt/jasper`
  (install.sh line 476). Deps pinned (e.g. `google-genai==1.13.0`,
  `openai>=2.36.0`, `scipy>=1.13,<2`). `[project.scripts]` block
  defines CLI entry points like `jasper-doctor`.
- **No `.github/workflows/`** — CI is greenfield. Stage 1 is pure
  new construction, no existing workflow to migrate.

---

## Failure surface + rollback strategy

The actually-hard part. The five failure modes worth designing for:

1. **GitHub Releases API unreachable** (network down, GitHub
   incident). The check button times out gracefully and surfaces
   "Couldn't reach GitHub; try again later." No state change.
2. **`git fetch` / `git checkout` fails** (e.g. detached HEAD
   conflict if someone hand-edited a file on the Pi). Abort before
   running `install.sh`. State unchanged.
3. **`install.sh` fails mid-flight** (apt mirror down,
   source-build of shairport fails, pip resolves a conflict).
   Hardest case. The system is now in a half-installed state. The
   rollback `git checkout previous && install.sh` should land the
   system back where it was, but install.sh idempotency under
   "partial prior install" is worth proving with a deliberate
   failure injection test.
4. **`install.sh` succeeds but `jasper-doctor` fails** post-restart
   (e.g. new code crashes on startup). Clean rollback case: git
   checkout previous tag, re-run install.sh, healthcheck again. If
   the second healthcheck also fails, escalate (audio cue + leave
   dashboard banner up; don't loop).
5. **Mid-update power cut.** Worst case. On the next boot, the Pi
   is in an unknown state. `install.sh`'s idempotency should mean a
   manual `sudo bash install.sh` recovers it, but for OTA we'd want
   the Pi to detect "in-flight update marker exists" on boot and
   either resume or roll back. Out of scope for v1; flag as
   known risk.

**Healthcheck signal.** Primary: `jasper-doctor --json` returning
all checks `ok` or `warn` (not `fail`). Secondary candidates worth
considering: `sd_notify` watchdog READY signal from `jasper-voice`
within N seconds (Tier 1 of the
[resilience ladder](HANDOFF-resilience.md)); a deliberate "ping"
endpoint that the updater can curl. Probably want some combination.

**Audio cue on failure.** Per CLAUDE.md, every wake-blocking failure
must trigger an audible cue. A failed update that rolls back is
*not* wake-blocking (the speaker is back on the previous version
and wake still works) — but a failed update that *also* fails to
roll back is wake-blocking and very much needs a cue. Add at least:

- `update_failed_rolled_back` (informational; speaker is fine)
- `update_failed_no_rollback` (urgent; speaker may not respond
  to wake; rate-limited per the audible-feedback pattern)

---

## Auth and security

LAN trust today. Per the private memory note
`feedback_jts_http_not_https.md` and the `jasper/web/*.py` reading,
all wizards run on HTTP with no
authentication — the assumption is "if you're on my home WiFi,
you're trusted". That assumption needs revisiting before this
button ships, because the consequence of an unauthorised click is
much larger than the consequence of, say, an unauthorised AirPlay
reset.

Options, easiest to hardest:

1. **Keep LAN trust, gate behind a confirmation modal** with a
   typed code shown elsewhere (e.g. "type the last 4 chars of the
   current SHA to confirm"). Cheap; raises the bar against an
   accidental or driveby click without adding real auth.
2. **Shared-secret dashboard PIN.** Generated on first install,
   stored in `/etc/jasper/jasper.env` mode 0600, prompted on the
   Update button only. Other buttons stay unauthenticated. Adds a
   small UX wart; modest security upgrade.
3. **Full dashboard auth** (login + session cookie). Larger scope,
   probably better as a separate work item that benefits *all* the
   wizards, not just Update.

This document doesn't pick one. The decision should be made
alongside the broader "dashboard auth model" question that's
implicit in the
[PLAN.md "Configuration web view / management dashboard"](../PLAN.md)
section.

---

## CI specifics worth pre-flighting

Before committing to the recommended path, two CI concerns worth a
quick spike:

1. **C++ binding builds on GitHub-hosted runners.** Does Trixie's
   `libwebrtc-audio-processing-1` v1.3-3 package install cleanly on
   Ubuntu-latest? If not, switch to a Trixie container in CI. The
   binding is already built on the Pi during `install.sh`, so the
   build steps and dep names are known and copyable.
2. **ESP32 firmware builds on CI.** Per
   [`docs/satellites.md`](satellites.md), pioarduino requires
   Python ≥ 3.10. GitHub runners have 3.11+, so this should work,
   but verify before committing to the matrix.

Both are ~30-minute spikes. Worth doing before the Stage 1 work
begins.

---

## Open questions

The decisions that should be made *before* specing an
implementation:

1. **Release cadence.** Every-merge auto-tag (simple, but a
   release every typo fix), manual `git tag` (more discipline,
   higher friction), or somewhere in between (auto-tag only on
   commits with `[release]` in the subject, daily cron, etc.)?
2. **Auth on the button.** Confirmation-only, shared-secret PIN,
   or wait for full dashboard auth? Important if guests + family
   are on the same WiFi.
3. **Failure surface definition.** What counts as "update failed,
   roll back"? `jasper-doctor` non-zero alone, or also voice
   daemon not reaching `sd_notify` READY in N seconds? Probably
   some combination.
4. **System-package rollback.** If `apt install` mid-update fails,
   does the rollback re-run `install.sh` against the old SHA, or
   just `git checkout` and trust the venv to still be consistent?
   This is the structurally-hardest rollback case.
5. **CI runtime feasibility.** Spike the C++ binding build and
   ESP32 firmware build on GitHub-hosted Ubuntu runners before
   committing to the approach.
6. **Firmware updates.** Explicitly out of scope, or wire the
   dial / satellite firmware refresh in alongside? Today
   `jasper-dial-onboard` / `jasper-satellite-onboard` are separate
   physically-attached flows. Could become a "Configure remotes"
   sub-flow per PLAN.md's
   "Configure remotes wizard — the satellite-onboarding sub-page"
   section, but the OTA story is fundamentally different (network
   push to running ESP32, not USB-attached flash). Defer until
   the speaker-side button ships.

---

## Concrete next steps (if/when this is prioritised)

In rough order, each step independently shippable:

1. **One-week spike** — write `.github/workflows/ci.yml` with just
   `pytest`. Land it. No releases, no Pi side. Confirms the runner
   can install JTS's deps.
2. **Add binding + firmware builds** to CI. Cache aggressively.
3. **Auto-release on merge to `main`.** Verify a release shows up
   at `https://github.com/jaspercurry/JTS/releases`.
4. **Pi-side `jasper-check-updates` CLI** (no UI yet) — reads
   `/var/lib/jasper/build.txt`, hits the Releases API, prints "Up
   to date" or "Update available: <tag> (released N min ago)".
   Easy to bench-test.
5. **Wire the dashboard button** to the CLI + apply logic. Add the
   rollback path. Test by deliberately tagging a broken release
   and confirming rollback works end-to-end (this is the test
   that catches the "install.sh idempotency under partial prior
   install" question above).

Steps 1–3 are pure CI work, no Pi changes, no risk to the running
speaker. They're worth doing even if steps 4–5 are deferred or
dropped.

---

## Prior art / sources

The research that informed this document.

OS-level A/B partition update systems (the industrial reference;
not what JTS uses today):

- [Home Assistant OS update system (developer docs)](https://developers.home-assistant.io/docs/operating-system/update-system/)
  — RAUC + Pi 5 "tryboot" for fallback. Closest architectural
  analog to JTS (Pi-based, single-purpose appliance).
- [Home Assistant OS 12 — Pi 5 support announcement](https://www.home-assistant.io/blog/2024/02/26/home-assistant-os-12-support-for-raspberry-pi-5/)
- [SWUpdate vs Mender vs RAUC — Yocto OTA comparison (32blog)](https://32blog.com/en/yocto/yocto-ota-update-comparison)
- [OTA Updates in 2026: RAUC vs SWUpdate vs Mender (ProteanOS)](https://proteanos.com/doc/ota-updates-rauc-swupdate-mender-2026/)
- [Porting Mender to Raspberry Pi 5 + Yocto Scarthgap (Konsulko)](https://www.konsulko.com/mender-raspberry-pi-5)
- [Getting Started with RAUC on Raspberry Pi (Konsulko)](https://www.konsulko.com/getting-started-with-rauc-on-raspberry-pi-2)
- [Mender — OTA Updates for Raspberry Pi (blog)](https://mender.io/blog/ota-updates-raspberry-pi)

Application-level self-update libraries (closer to what JTS would
actually use):

- [tufup — Python self-update framework with GitHub Releases support](https://github.com/dennisvang/tufup)
  — TUF-based, cryptographic signing. Probably overkill but the
  reference for "secure".
- [updater4pyi — GitHub Releases-backed Python updater](https://pypi.org/project/updater4pyi/)
- [selfupdate — git-based Python self-update](https://github.com/beeedy/selfupdate)
- [How to write self-updating Python programs with pip + git (hackthology)](https://hackthology.com/how-to-write-self-updating-python-programs-using-pip-and-git.html)
  — The conceptual walkthrough closest to JTS's actual shape (pip
  editable + git).

GitHub-side mechanics:

- GitHub Releases API:
  `https://api.github.com/repos/<owner>/<repo>/releases/latest`
  (unauthenticated, 60 req/hr/IP without a token).
- `gh release create` for tag-and-publish in CI.

Last verified: 2026-05-27 (research-only status/footer check; no
implementation in flight)
