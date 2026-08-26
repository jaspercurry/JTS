# Handoff: install/update as a transaction

Operational truth for **how a JTS update either fully succeeds or leaves
the speaker no worse than before, and never reports false success.** This is
the landed half of Workstream B in
[install-update-resilience-plan.md](install-update-resilience-plan.md); the
originating incidents are archived in
[install-update-incidents-2026-07.md](historical/install-update-incidents-2026-07.md).
Read this for what the code does now.

## The one invariant

> **The build manifest (`/var/lib/jasper/build.txt`) is advertised only
> for a build that installed cleanly. A failed update never advances it.**

Everything below serves that line. It is what makes the deploy
direction-guard, the `/system` "Software" card, and the next operator's
mental model trustworthy.

## Two honest claims, two layers

A deploy answers two different questions; conflating them was the bug.

1. **"Did the install *process* complete?"** — owned by `install.sh`,
   recorded in the build manifest. Hardware-independent: it means every
   required/core build compiled, every file installed, the venv resolved,
   and units loaded. Explicit optional enhancements are outside this claim.
   It does **not** claim any daemon is currently healthy. `install.sh` may
   bounce the core audio graph while loading the new code and derived
   state; the deploy wrapper's post-install doctor/reconcile layer owns the
   separate "healthy or correctly idle" claim.

2. **"Is the running system healthy, or correctly idle?"** — owned by
   `scripts/deploy-to-pi.sh` post-restart, via `jasper-doctor`. This is
   where voice / AEC bridge / renderer state is surfaced, and where the
   broken-vs-idle distinction lives. It is surfaced, never gating —
   [ADR-0173](adr/0173-post-deploy-health-is-surfaced-never-gating.md).

Keeping these separate is what lets a no-mic speaker honestly report
"install completed, voice idle for missing hardware" instead of either
lying ("all good") or falsely failing ("voice broken").

## How the invariant is held

### 1. The manifest is the verified-install marker, written last

`write_build_manifest` is the **final mutation** in both `main()` paths
(`deploy/install.sh`), immediately before the non-mutating
`run_doctor_summary`. Under `set -euo pipefail`, reaching that line proves
every build/install/migration step above succeeded. A mid-install abort
leaves the **prior good manifest** untouched.

- The write is **atomic** (tempfile + `mv -f`) — a torn write can't leave
  a half-line the direction-guard misreads.
- It records `JASPER_INSTALL_STATUS=ok` — the explicit "install process
  completed" claim the deploy verifier checks.
- Nothing may write the manifest earlier; pinned by
  `test_build_manifest_not_written_during_python_runtime_install`.

Because the manifest writes last, anything needing the deployed SHA
*during* install must not read it. The landing-page `app.css` cache-bust
calls `resolve_build_sha_short` (deploy env → git → prior manifest →
`unknown`) — the same value the manifest will record.

Socket-activated wizard HTML has a related timing boundary: a request can
start a wizard after new code is installed but before the final manifest
replacement. `canonical_page()` therefore reads the tiny local manifest on
each HTML render rather than caching its first value for the process lifetime.
This is outside every JSON polling path. The deploy wrapper also fetches
`/system/` and requires its exact `app.css?v=<deployed-sha>` token, so a 200
from `/system/data.json` cannot hide browser-visible stale design assets.

### 2. Deploy verification covers real system health, not just the web path

`scripts/deploy-to-pi.sh` keeps its management-surface probe (nginx →
wizard → jasper-control) as the hard gate, and adds, after
restart/reconcile:

- **`verify_manifest_advanced`** — confirms the Pi's manifest now records
  the deployed full SHA **and** `JASPER_INSTALL_STATUS=ok`. This is the
  deploy-side proof of the invariant: a mismatch means the install didn't
  run to completion, and the deploy fails loudly.
- **`surface_system_health`** — runs `jasper-doctor` post-reconcile and
  prints its report (voice, AEC bridge, renderers). Advisory, not a gate.

Both read the Pi over ssh and so are **skipped under interactive sudo**
(where `ssh -tt` corrupts captured output), mirroring the existing
identity and direction guards. Passwordless sudo (BRINGUP Phase 2.5) is
the posture that gets fully-verified deploys.

Below the deploy wrapper's 1.2 GB threshold the health surface uses the
stdlib-only `deploy/bin/jasper-deploy-health` instead of importing the full
doctor stack under memory pressure. It reads the canonical
`/var/lib/jasper/install_profile` marker before deciding what must run:

- A missing, unreadable, or empty marker retains the backwards-compatible
  `full` assumption. Legacy `endpoint` / `satellite` markers normalize to
  `streambox`; any other token fails closed before probing services.
- Both profiles require the control, fan-in, outputd, CamillaDSP, mux, nginx,
  and core web-socket surfaces. `jasper-input` is required on `full` only, and
  a `streambox` intentionally parks voice and AEC, so the probe neither
  requires nor warns about those two there.
- AirPlay (`shairport-sync` + `nqptp`), Spotify Connect (`librespot`), and
  Bluetooth audio (`bluealsa` + `bluealsa-aplay`) follow the fixed source
  expectations in `/var/lib/jasper/source_intent.env` — `enabled` requires the
  source-owned unit active, `disabled` requires it inactive. Bluetooth also
  proves RF-kill and BlueZ `Powered` match intent. USB On additionally requires
  the UAC2 card and a present, healthy (`idle` or `capturing`) fan-in direct
  lane; USB Off requires both absent. A confirmed bonded follower uses parked
  source/mux expectations without rewriting intent (drift either way fails).
- The intent reader is stdlib-only, reads at most 64 KiB + 1 byte, decodes
  strict UTF-8, uses the final assignment for each recognized key, and fails
  closed on an unreadable, oversized, invalid-UTF-8, malformed, or
  unknown-`JASPER_SOURCE_INTENT_*` input rather than guessing; unrelated env
  keys are ignored. A missing file or key retains that source's shipped default
  (USB Off; the other three On).
- Fan-in is sampled twice around a one-second interval and must show no xrun
  increase and recent watchdog progress. Outputd is also sampled twice: both
  snapshots must report the ALSA backend, zero xruns, recent progress, and
  increasing process uptime; cumulative empty/EAGAIN startup counts may be
  nonzero, but any increase during the sample fails. Counter and progress
  fields are strict nonnegative JSON integers and uptime a finite nonnegative
  number — booleans, strings, negatives, missing fields, malformed entries, and
  an empty list all fail closed, so a malformed or restarted status surface
  cannot certify a deploy. Each control-socket response is capped at 256 KiB
  with a two-second absolute deadline.

The deploy probe owns this low-memory certification policy; the canonical
source keys, defaults, runtime convergence, and desired/effective semantics
live in [HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md). The
low-memory report is the same advisory end-state surface as the full doctor
report; it does not broaden the manifest's verified-install claim.

USB gadget installation deliberately establishes one safe baseline before
source replay: `jasper-usbsink.service` disabled/stopped and, where the
resolved transport permits, the gadget network-only. It does not interpret USB
intent or advertise UAC2 before its data plane exists. A converged NCM-only
gadget is left bound so a deploy over USB does not flap its management link.
During a Zero peripheral→host migration NCM remains available while the active
controller is still peripheral; audio is withdrawn, and reboot then activates
host mode and removes the UDC. An upgrade arriving with prior derived USB
enablement, activity, or a visible UAC2 card is recomposed once to remove stale
audio. The shared source coordinator is the sole owner of replay; a failed park
or stale-UAC2 cleanup fails closed rather than leaving a host-visible source
without a consumer. Deploy health consumes the coordinator's effective USB
status, so saved On plus hardware-unavailable is certified only when the
marker, UAC2 function, and DIRECT lane are all down.

### 3. Derived audio state is repaired best-effort, never a manifest gate

Generated CamillaDSP sound YAML is a cache of saved JTS intent, not the
source of truth. During install's runtime-unit bring-up — after outputd
readiness and statefile legality checks, before the explicit CamillaDSP
restart — `install.sh` runs `jasper-sound reconcile-current-dsp --fail-open`
under an outer 30 s process timeout, refreshing only a currently-loaded
JTS-owned `sound_current.yml` from `sound_profile.json` and
`sound_settings.json`. So DSP-renderer fixes take effect on deploy instead of
waiting for someone to open `/sound/`.

This reconcile is **not** part of the verified-install claim. It fails open,
prints a structured result into the deploy transcript, skips unsaved
`sound_audition.yml` previews, and leaves the current legal graph in place on
failure or timeout — so the install can complete honestly even if a
derived-cache refresh needs a later manual retry.

### 4. Optional enhanced AEC is a capability-local transaction

Vendored WebRTC AEC3 v2 / BEST_A is deliberately not a build-manifest gate.
The core transaction builds the mandatory distro-linked v1 fallback only.
After the manifest is atomically published, a `PathChanged=` unit may retry
durable enhanced-AEC intent in a bounded background oneshot. Its
download/build/probe/staleness failure is recorded on the feature but cannot
roll back, fail, or falsely advance the core deploy.

Deploy and optional activation share a short lock around source/package
mutation, while the multi-minute optional build runs outside that lock.
Activation rechecks the exact source/ABI fingerprint and writes its own
verified marker last. This is a second, capability-local transaction — not an
extension of `/var/lib/jasper/build.txt`'s claim. See
[HANDOFF-enhanced-aec.md](HANDOFF-enhanced-aec.md).

### 5. OOM collateral is surfaced, never silent

The deploy captures the Pi's clock before install and afterwards — on success
**or** failure — scans the kernel log for that window (`report_oom_collateral`).
A live production daemon killed during the build gets a loud per-unit `✗`; it
does not gate the deploy, because pass/fail belongs to the end-state gates.
Rationale, victim-parsing detail, and the rejected alternative:
[ADR-0174](adr/0174-install-window-oom-kills-are-surfaced-not-gated.md).

### 6. Full-profile unit generation rolls back as one cohort

After the shared support files and core-audio-graph table land,
`install_systemd_units` opens a rollback transaction around the remaining
full-profile unit/helper generation. Each file destination is snapshotted once;
generated Apple-mixer units render into the transaction directory before their
promotion. If any copy or render fails, the installer restores pre-existing
destinations, removes destinations that were new in this generation, runs
`systemctl daemon-reload` against the restored set, and exits before the managed
unit enable/start phase. The transaction commits only after the central
daemon-reload accepts the complete generation.

This is a bounded cohort, not a claim that the whole install is an A/B
filesystem update ([ADR-0172](adr/0172-full-a-b-install-generations-stay-deferred.md)).
The shared core-audio-graph installer has its own all-rows-attempted +
guaranteed-reload contract, and the build manifest remains the whole-install
verified marker.

### 7. A failed install leaves live services running

On install failure, `deploy-to-pi.sh` exits **before** the
restart/reconcile section. The running daemons keep their old code in RAM,
the manifest still points at the prior good build, and the operator is
told what failed (and any collateral). "No worse than before" holds in the
immediate term; re-deploying converges.

A lost SSH transport is not proof of an install failure. If SSH exits 255
while `install.sh` is running, the wrapper exits 255 but reports the remote
outcome as unknown, because that status can also signal a transport failure.
The install may still be running or may complete after the session ends, and
the manifest was not verified. Reconnect and inspect the Pi before deciding
whether to re-deploy.

### 8. Rust build-cache staging is content-based, not mtime-preserving

Cargo's freshness check is mtime-based, and mtime-preserving staging let a
changed source land "in the past" so cargo declared the crate **Fresh** and the
install shipped a stale binary under an honest manifest. It bit twice on
hardware; the forensics are in the incidents archive.

`stage_rust_crate` (`deploy/lib/install/rust-daemons.sh`) stages every crate
with `--checksum` and **without** time preservation (`-rlpgoD` = `-a` minus
`-t`): unchanged files are skipped and keep their mtime (no spurious rebuilds),
changed files land stamped *now* (always newer than the last fingerprint).
`rust_build_cache_reset_if_stale_format` heals an already-poisoned cache: on
`RUST_BUILD_CACHE_FORMAT` mismatch (marker `.jts-build-cache-format` in each
cache dir) it clears `target/` once, forcing one full rebuild.

## Operational quick reference

```sh
# What the Pi actually runs (only a verified install advances it):
ssh pi@jts.local 'sudo cat /var/lib/jasper/build.txt'
#   JASPER_GIT_SHA=…  JASPER_GIT_SHA_FULL=…  JASPER_GIT_BRANCH=…
#   JASPER_INSTALL_AT=…  JASPER_INSTALL_STATUS=ok

# A normal deploy also prints, after the management probe:
#   ✓ build manifest advanced to <sha> (status=ok, verified install)
#   ==> Post-deploy system health (advisory; does not gate the deploy)
#   …jasper-doctor report (voice / AEC / renderers)…
# and, if anything was OOM-killed during install:
#   ⚠ OOM kills detected … ✗ PRODUCTION daemon killed: <unit>
```

- **Deploy fails** on: management surface down, or the manifest didn't
  advance to the deployed SHA with `status=ok`.
- **Deploy surfaces (non-gating)**: full `jasper-doctor` health, and any
  OOM collateral during the install window — including a loud `✗` when the
  victim was a live production daemon.
- **Interactive-sudo deploys** skip the manifest + health capture with a
  printed notice (can't capture cleanly through `ssh -tt`).

## Tests (the pins)

- `tests/test_install_helpers.py` — manifest records `…STATUS=ok`, atomic
  write, `resolve_build_sha_short` precedence, manifest-written-last (not
  in python-runtime), app.css uses the resolver.
- `tests/test_deploy_oom_collateral.py` — the `_lib.sh` OOM parsers
  (incl. pipefail-safety) and the real `report_oom_collateral` body.
- `tests/test_deploy_wiring_guards.py` — deploy-to-pi.sh wires up the
  manifest gate, OOM scan, health surfacing, install-rc capture, and the
  interactive-sudo skip.
- `tests/test_deploy_health_script.py` — the real AF_UNIX `STATUS` exchange,
  profile-specific required / observed units, strict fan-in and outputd status
  schemas, bounded response size/time, xrun/progress verdicts, persisted source
  intent, and fail-closed invalid profile behavior.
- `tests/test_lib_deploy_direction.py` — the direction guard reading an
  honest manifest.
- `tests/test_install_core_audio_graph_loop.py` — unit-generation rollback,
  including restoring an overwritten file and removing a newly-created one.
- `tests/test_rust_build_cache_staging.py` — content-based staging, the
  one-time `RUST_BUILD_CACHE_FORMAT` purge, and the single-rsync script shape.

## Validated hardware-free (disclosed)

These were exercised with mocked ssh, captured journal text, and sourced bash
helpers, not on a Pi. They are in service; this is a disclosure, not a park:

- A genuinely OOM-prone update emitting the expected kernel `task_memcg=`
  lines for its victims, so `report_oom_collateral` classifies them.
- `jasper-doctor`'s post-reconcile report on a no-mic box reading voice as
  `inactive`/parked rather than a crash-loop `fail`.
- A deliberately-failed install leaving the prior manifest and not restarting
  daemons.

---

Last verified: 2026-08-26 (triage pass — manifest-written-last position in both
`deploy/install.sh` `main()` paths, `verify_manifest_advanced`,
`surface_system_health`, the 1.2 GB `jasper-deploy-health` threshold,
`report_oom_collateral` and the three `_lib.sh` OOM parsers,
`install_systemd_units`' rollback cohort, `stage_rust_crate` /
`rust_build_cache_reset_if_stale_format`, `jasper-sound
reconcile-current-dsp --fail-open`, `voice-input-absent` +
`jasper-voice.service`'s `ConditionPathExists`, and every pin file rechecked
against the code. Prior 2026-08-15: deploy install-status handling and the
SSH-255 transport exception. Prior 2026-07-27: the optional enhanced-AEC
boundary. Prior 2026-07-15: outputd two-snapshot counter-growth, uptime
continuity, source-intent stability gates, the `/system/` asset-token gate,
and the low-memory source-intent contract for all four sources plus
bonded-follower parking.)
