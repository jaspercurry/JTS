# Handoff: install/update as a transaction

Operational truth for **how a JTS update either fully succeeds or leaves
the speaker no worse than before, and never reports false success.** This
is the landed half of Workstream B in
[install-update-resilience-plan.md](install-update-resilience-plan.md)
(problems #3, #4, #5, #7). Read the plan for the originating incident;
read this for what the code does now.

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
   build compiled, every file installed, the venv resolved, units loaded.
   It does **not** claim any daemon is currently healthy. `install.sh` may
   bounce the core audio graph while loading the new code and derived
   state; the deploy wrapper's post-install doctor/reconcile layer owns the
   separate "healthy or correctly idle" claim.

2. **"Is the running system healthy, or correctly idle?"** — owned by
   `scripts/deploy-to-pi.sh` post-restart, via `jasper-doctor`. This is
   where voice / AEC bridge / renderer state is surfaced, and where the
   broken-vs-idle distinction lives.

Keeping these separate is what lets a no-mic speaker (jts2) honestly
report "install completed, voice idle for missing hardware" instead of
either lying ("all good") or falsely failing ("voice broken").

## What changed

### 1. The manifest is the verified-install marker, written last

`write_build_manifest` used to run at the *start* of `install_jasper` —
before the OOM-prone WebRTC build and the Rust builds. On jts2 the build
OOM-killed mid-run, `set -e` aborted install.sh, and the manifest had
*already* recorded the new SHA. The box advertised a successful update it
hadn't done, and the next deploy's direction-guard treated it as
up-to-date (problem #4).

Now `write_build_manifest` is the **final mutation** in both `main()`
paths (`deploy/install.sh`), immediately before the non-mutating
`run_doctor_summary`. Under `set -euo pipefail`, reaching that line proves
every build/install/migration step above succeeded. A mid-install abort
leaves the **prior good manifest** untouched.

- The write is **atomic** (tempfile + `mv -f`) — a torn write can't leave
  a half-line the direction-guard misreads.
- It records `JASPER_INSTALL_STATUS=ok` — the explicit "install process
  completed" claim the deploy verifier checks.
- The early calls in `deploy/lib/install/python-runtime.sh` are gone
  (pinned by `test_build_manifest_not_written_during_python_runtime_install`).

The landing-page `app.css` cache-bust used to read the manifest mid-run.
Because the manifest now writes last, it would have read the *prior* SHA
and shipped a stale cache key. It now calls `resolve_build_sha_short`
(deploy env → git → prior manifest → `unknown`) — the same value the
manifest will record — so the cache key matches the installed build.

Socket-activated wizard HTML has a related timing boundary: a request can
start a wizard after new code is installed but before the final manifest
replacement. `canonical_page()` therefore reads the tiny local manifest on
each HTML render rather than caching its first value for the process lifetime.
This is outside every JSON polling path. The deploy wrapper also fetches
`/system/` and requires its exact `app.css?v=<deployed-sha>` token, so a 200
from `/system/data.json` cannot hide browser-visible stale design assets.

### 2. Deploy verification covers real system health, not just the web path

`scripts/deploy-to-pi.sh` kept its management-surface probe (nginx →
wizard → jasper-control) as the hard gate, and adds, after
restart/reconcile:

- **`verify_manifest_advanced`** — confirms the Pi's manifest now records
  the deployed full SHA **and** `JASPER_INSTALL_STATUS=ok`. This is the
  deploy-side proof of the invariant: a mismatch means the install didn't
  run to completion, and the deploy fails loudly.
- **`surface_system_health`** — runs `jasper-doctor` post-reconcile and
  prints its report (voice, AEC bridge, renderers). **Advisory, not a
  gate** — see the broken-vs-idle seam below.

Both read the Pi over ssh and so are **skipped under interactive sudo**
(where `ssh -tt` corrupts captured output), mirroring the existing
identity and direction guards. Passwordless sudo (BRINGUP Phase 2.5) is
the posture that gets fully-verified deploys.

On boxes below the deploy wrapper's 1.2 GB threshold, the health surface uses
the stdlib-only `deploy/bin/jasper-deploy-health` instead of importing the
full doctor stack under memory pressure. The probe reads the canonical
`/var/lib/jasper/install_profile` marker before deciding what must run:

- A missing, unreadable, or empty marker retains the backwards-compatible
  `full` assumption. Legacy `endpoint` / `satellite` markers normalize to
  `streambox`; any other token fails closed before probing services.
- Both profiles require the control, fan-in, outputd, CamillaDSP, mux, nginx,
  and core web-socket surfaces. AirPlay (`shairport-sync` + `nqptp`), Spotify
  Connect (`librespot`), and Bluetooth audio (`bluealsa` + `bluealsa-aplay`)
  follow the fixed source expectations in `/var/lib/jasper/source_intent.env`.
  The parser covers all four canonical sources, so a missing file/key retains
  that source's shipped default (USB Off; the other three On).
  `enabled` requires each source-owned unit active and
  `disabled` requires it inactive. Bluetooth additionally proves RF-kill and
  BlueZ `Powered` match intent. USB On additionally requires the UAC2 card and
  a present, healthy (`idle` or `capturing`) fan-in direct lane; USB Off requires
  both absent. A confirmed bonded follower uses parked
  source/mux expectations without rewriting intent (drift in either direction
  fails). The
  Bluetooth pairing agent remains advisory when Bluetooth is enabled and is
  not warned about when Bluetooth is intentionally disabled. The reader is
  stdlib-only, reads at most 64 KiB + 1 byte, decodes strict UTF-8, uses the
  final assignment for each recognized key, and fails closed on an unreadable,
  oversized, invalid-UTF-8, malformed recognized value, or unknown key in the
  owned `JASPER_SOURCE_INTENT_*` namespace instead of guessing; unrelated env
  keys are ignored. `jasper-input` remains required on `full`
  only. A `streambox` intentionally parks voice and AEC, so the probe neither
  requires nor emits optional-unit warnings for those two services there.
  The deploy probe owns this low-memory certification policy; the canonical
  source keys, defaults, runtime convergence, and desired/effective semantics
  live in [HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md).
- USB gadget installation deliberately establishes one safe baseline before
  source replay: `jasper-usbsink.service` disabled/stopped and, where the
  resolved transport permits, the gadget network-only. It does not interpret
  USB intent or advertise UAC2 before its data plane exists. A converged
  NCM-only gadget is left bound so a deploy over USB does not flap its
  management link. During a Zero peripheral→host migration, NCM remains
  available while the currently active controller is still peripheral; audio
  is withdrawn, and reboot then activates host mode and removes the UDC. An
  upgrade arriving with prior derived
  USB enablement, activity, or a visible UAC2 card is recomposed once to remove
  stale audio. The later shared source coordinator is the sole owner of replay:
  for canonical On it enables the mirror, arms fan-in direct capture, then
  recomposes UAC2 and starts the process-free readiness marker; Off stays NCM-only. A
  failed park or stale-UAC2 cleanup fails closed rather than leaving a
  host-visible source without a consumer. Deploy health consumes the source
  coordinator's effective USB status, so saved On plus hardware-unavailable is
  certified only when the marker, UAC2 function, and DIRECT lane are all down.
- Fan-in is sampled twice around a one-second interval and must show no xrun
  increase and recent watchdog progress. Outputd must report its ALSA backend,
  zero xruns / empty periods / EAGAINs, and recent progress. All counter and
  progress fields are strict nonnegative JSON integers (booleans, strings,
  negative values, missing fields, malformed input entries, and an empty input
  list fail closed); this prevents a malformed status surface from certifying
  a deploy. Each control-socket response is also capped at 256 KiB with a
  two-second absolute deadline, so a peer cannot defeat the probe's low-memory
  purpose by streaming forever or returning an unbounded payload.

This low-memory report remains the same advisory end-state surface as the full
doctor report; it does not broaden the manifest's verified-install claim.

### 3. Derived audio state is repaired best-effort, never a manifest gate

Generated CamillaDSP sound YAML is a cache of saved JTS intent, not the
source of truth. During install's runtime-unit bring-up, after outputd
readiness and statefile legality checks but before the explicit CamillaDSP
restart, `install.sh` runs
`jasper-sound reconcile-current-dsp --fail-open` under an outer 30 s process
timeout. This deliberately refreshes only a currently-loaded JTS-owned
`sound_current.yml` from `/var/lib/jasper/sound_profile.json` and
`/var/lib/jasper/sound_settings.json`, so DSP-renderer fixes take effect on
deploy instead of accidentally waiting for someone to open `/sound/`.

This reconcile is **not** part of the verified-install claim. It fails open,
prints a structured result into the deploy transcript, skips unsaved
`sound_audition.yml` previews, and leaves the current legal graph in place on
failure or timeout. That keeps the manifest invariant clean: the install can
complete honestly even if a derived-cache refresh needs a later manual retry.

### 4. Collateral OOM kills are surfaced, never silent

Problem #2/#5: a build OOM-killed nginx *and* jasper-voice, and the
tooling exited silently. Now the deploy captures the Pi's clock before
install and, afterward (on success **or** failure), scans the kernel log
for the install window (`report_oom_collateral`):

- Parses victims two ways — the cgroup `task_memcg=/system.slice/<unit>`
  names the **systemd unit** reliably (a venv console-script daemon like
  jasper-voice is execve'd as `python3`, so its `comm` is misleading), and
  the `(comm)` / `task=comm` fields give the human-friendly process name
  (and the only signal for build tools, which run in a transient ssh
  scope, not a named `.service`).
- A **live production daemon** OOM-killed during the build (`nginx`,
  `jasper-*`, `shairport-sync`, `librespot`, …) is **surfaced as a loud
  per-unit `✗` warning** but does **not** gate the deploy. The OOM is
  *history*; pass/fail is owned by the **end-state** gates (management
  probe, `verify_manifest_advanced`, advisory doctor). A daemon systemd
  already restarted must not fail an otherwise-healthy deploy (the inverse
  false-failure trap, which would bite a 1 GB Pi on a large update); one
  that's still down is caught by the end-state gates. The plan asks the
  tooling to *"say so, not exit silently"* — surfacing satisfies that.
- A build-tool OOM (cc1plus, cargo) is surfaced as context too — it already
  shows up as an install failure under `set -e`.

The pure parsers live in `scripts/_lib.sh` (`oom_killed_units`,
`oom_killed_comms`, `oom_unit_is_production`) and are unit-tested against
captured kernel-log text.

### 5. A failed install leaves live services running

On install failure, `deploy-to-pi.sh` exits **before** the
restart/reconcile section. The running daemons keep their old code in RAM,
the manifest still points at the prior good build, and the operator is
told what failed (and any collateral). "No worse than before" holds in the
immediate term; re-deploying converges.

### 6. Rust build-cache staging is content-based, not mtime-preserving

Cargo's freshness check is mtime-based: a unit recompiles only when a
source file is *newer* than the fingerprint from the last compile. The
old staging chain preserved mtimes end to end (`rsync -a` laptop →
checkout → `/var/cache/<name>-build`), so a changed source whose
checkout mtime predated the cache's last build landed "in the past",
cargo declared the crate **Fresh**, and the install shipped the stale
binary while the manifest honestly said `ok` — the box lied at the
*binary* layer, below both honest-claim layers above. Bit twice on
hardware: the 2026-07-02 stale `jasper-usbsink-audio` (404ing endpoints)
and 2026-07-10 stale `jasper-outputd` (a merged journal-spam fix never
went live; `cargo build -v` in the poisoned cache said `Fresh` in
0.03 s while the staged source contained the fix — three same-day
deploys, the first compiled pre-fix source at 17:23, the later two
staged the fixed source with a preserved 17:14 mtime).

`stage_rust_crate` (`deploy/lib/install/rust-daemons.sh`) now stages
every crate with `--checksum` and **without** time preservation
(`-rlpgoD` = `-a` minus `-t`): unchanged files are skipped (mtime kept —
no spurious rebuilds), changed files land stamped *now* (always newer
than the last fingerprint). `rust_build_cache_reset_if_stale_format`
heals already-poisoned caches: on `RUST_BUILD_CACHE_FORMAT` mismatch
(marker `.jts-build-cache-format` in each cache dir) it clears
`target/` once, forcing one full rebuild — expect one slow deploy per
box after this ships. Pinned by `tests/test_rust_build_cache_staging.py`.

## The broken-vs-idle seam (Workstream C)

`jasper-doctor` already distinguishes a **crash-looped / failed** daemon
(`active=failed` or `activating` → `fail`) from a **cleanly stopped /
parked** one (`inactive` → not flagged) in `check_service_runtime_state`.
That is the mechanism for "broken vs intentionally idle."

**Workstream C shipped 2026-06-21** (#924, `f662622c`). With no mic
attached, the AEC reconciler now writes `/var/lib/jasper/voice-input-absent`
and `jasper-voice.service`'s `ConditionPathExists=!…` makes systemd skip the
start cleanly — the unit reads `inactive`, not `failed`/`activating`, so
`check_service_runtime_state` no longer flags a correctly-no-mic box. The
old crash-loop-until-start-limit behavior (problem #6) is gone.

`surface_system_health` nonetheless stays **advisory, not a gate**, but the
reason has moved: the mic-specific misfire is fixed, yet the broader
broken-vs-idle reclassification of *other* missing-hardware daemons (absent
XVF, a renderer whose hardware isn't present) is still the hot-plug
workstream's job. Until every missing-hardware daemon reads as "idle, not
broken," gating on any doctor `fail` would still mis-fire, so the deploy
wrapper surfaces `jasper-doctor` rather than gating on it. When that
reclassification is complete the gate can tighten to "any doctor core-fail
blocks." The seam is intentional and documented; B does not reclassify
hardware expectations — that's C's (and the hot-plug workstream's) job.

## Rollback / resume / A-B — what we built and why not more

The plan asks for "the cheapest version that meets *never worse than
before* on a 1 GB Pi." Analysis:

| Option | Cost | Decision |
|---|---|---|
| **Honest manifest** (done) | ~0 | **Ship.** The box never lies about what it runs; the direction-guard is trustworthy. |
| **Don't restart on failed install** (already true) | 0 | **Keep.** Live services keep serving old code through a failed update. |
| **Idempotent, resumable install** (already largely true) | 0 | **Keep.** Fingerprint caches + guarded creates + check-before-write migrations mean "resume = re-deploy" converges. |
| **Surface failure + collateral** (done) | small | **Ship.** Operator knows immediately and can act. |
| **Full A-B generations** (two `/opt/jasper`, symlink flip on verified success) | high | **Defer.** ~2× the heavy venv on disk and symlink-flip surgery across every unit path, StateDirectory, reconciler, and wizard. |

**Decision: defer full A-B.** The four cheap pieces above already deliver
"never worse than before" for the common case. A-B's *only* marginal
benefit is protecting the narrow window where a **failed update is
followed by a reboot before the operator re-deploys** — a reboot would
then load partially-updated `/opt/jasper`. Given the honest manifest +
loud failure + idempotent resume, that window is short and visible.

**Residual risk (documented, accepted):** reboot during a
failed-update window. Revisit A-B (or a cheaper staging-path atomic-swap
of the Python tree) if that becomes an observed failure mode.

## Operational quick reference

```sh
# What the Pi actually runs (now honest — only a verified install advances it):
ssh pi@jts.local 'sudo cat /var/lib/jasper/build.txt'
#   JASPER_GIT_SHA=…  JASPER_GIT_SHA_FULL=…  JASPER_GIT_BRANCH=…
#   JASPER_INSTALL_AT=…  JASPER_INSTALL_STATUS=ok

# A normal deploy now also prints, after the management probe:
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
  victim was a live production daemon (so it's never silent), without
  failing a deploy whose end state is healthy.
- **Interactive-sudo deploys** skip the manifest + health capture with a
  printed notice (can't capture cleanly through `ssh -tt`).

## Tests (the pins)

- `tests/test_install_helpers.py` — manifest records `…STATUS=ok`, atomic
  write, `resolve_build_sha_short` precedence, manifest-written-last (not
  in python-runtime), app.css uses the resolver.
- `tests/test_deploy_oom_collateral.py` — the `_lib.sh` OOM parsers
  (incl. pipefail-safety) and the real `report_oom_collateral` body
  (production-daemon kill gates; build-tool kill doesn't; silent on no
  OOM).
- `tests/test_deploy_wiring_guards.py` — deploy-to-pi.sh wires up the
  manifest gate, OOM scan, health surfacing, install-rc capture, and the
  interactive-sudo skip.
- `tests/test_deploy_health_script.py` — the real AF_UNIX `STATUS` exchange,
  profile-specific required / observed units, strict fan-in and outputd status
  schemas, bounded response size/time, xrun/progress verdicts, persisted AirPlay
  source intent, and fail-closed invalid profile behavior.
- `tests/test_lib_deploy_direction.py` — the direction guard that now
  reads an honest manifest (unchanged, still green).
- `tests/test_rust_build_cache_staging.py` — `stage_rust_crate`
  content-based staging (changed source lands newer than the last build
  stamp; unchanged source keeps its mtime), the one-time
  `RUST_BUILD_CACHE_FORMAT` purge, and the single-rsync script-shape
  contract.

## Needs real-hardware confirmation

These were validated hardware-free (mocked ssh, captured journal text,
sourced bash helpers). Confirm on a Pi:

- A genuinely OOM-prone update on a 1 GB box (or a memory-cgroup-confined
  build) actually emits the expected kernel `task_memcg=` lines for the
  victims, so `report_oom_collateral` classifies them. (Pi OS Trixie is
  cgroup-v2; older kernels without `*_memcg` fall back to comm-only.)
- `jasper-doctor`'s post-reconcile report on a no-mic box reads as
  expected: Workstream C (#924, 2026-06-21) parks voice cleanly, so the
  box shows voice `inactive`/parked rather than a crash-loop `fail`.
- A deliberately-failed install (e.g. inject a failing step) leaves the
  prior manifest and does not restart daemons.

---

Last verified: 2026-07-14 (verified-manifest asset timing and exact
browser-visible `/system/` asset-token gate rechecked against
`jasper/web/_common.py` and `scripts/deploy-to-pi.sh`; low-memory deploy-health
source-intent contract re-verified for AirPlay, Spotify Connect, Bluetooth,
USB Audio Input, and bonded-follower parking against
`deploy/bin/jasper-deploy-health`; profile and status-schema contracts were
previously re-verified 2026-07-12;
broken-vs-idle seam previously re-verified against
`jasper-voice.service`'s `ConditionPathExists`, the doctor's
`check_service_runtime_state`, and the deploy wrapper's advisory
`surface_system_health` — Workstream C confirmed shipped #924; other
sections not re-verified this pass)
