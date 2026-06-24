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
   It does **not** claim any daemon is currently healthy (install.sh
   doesn't even restart the hardware-gated daemons — the deploy wrapper's
   reconcile does).

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

### 3. Derived audio state is repaired best-effort, never a manifest gate

Generated CamillaDSP sound YAML is a cache of saved JTS intent, not the
source of truth. During install's runtime-unit bring-up, after outputd/Camilla
readiness and statefile repair, `install.sh` runs
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

## The broken-vs-idle seam (Workstream C)

`jasper-doctor` already distinguishes a **crash-looped / failed** daemon
(`active=failed` or `activating` → `fail`) from a **cleanly stopped /
parked** one (`inactive` → not flagged) in `check_service_runtime_state`.
That is the mechanism for "broken vs intentionally idle."

Today, with no mic attached, the AEC reconciler doesn't yet park voice
cleanly — jasper-voice crash-loops until systemd start-limits it, so the
doctor reports it `fail` (problem #6). That's why `surface_system_health`
is **advisory, not a gate**: gating on doctor `fail` would mis-fire on a
correctly-no-mic box. Once **Workstream C** makes the reconciler park
voice cleanly (doctor → not-a-fail), the deploy gate can tighten to "any
doctor core-fail blocks." The seam is intentional and documented; B does
not reclassify hardware expectations — that's C's job.

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
- `tests/test_lib_deploy_direction.py` — the direction guard that now
  reads an honest manifest (unchanged, still green).

## Needs real-hardware confirmation

These were validated hardware-free (mocked ssh, captured journal text,
sourced bash helpers). Confirm on a Pi:

- A genuinely OOM-prone update on a 1 GB box (or a memory-cgroup-confined
  build) actually emits the expected kernel `task_memcg=` lines for the
  victims, so `report_oom_collateral` classifies them. (Pi OS Trixie is
  cgroup-v2; older kernels without `*_memcg` fall back to comm-only.)
- `jasper-doctor`'s post-reconcile report on a no-mic box reads as
  expected once Workstream C lands.
- A deliberately-failed install (e.g. inject a failing step) leaves the
  prior manifest and does not restart daemons.

---

Last verified: 2026-06-21
