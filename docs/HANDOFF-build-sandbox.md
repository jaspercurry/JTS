# Handoff: memory-safe, production-isolated installer builds

Canonical reference for how `deploy/install.sh` and the opt-in enhanced-AEC
background job run heavy compile/build steps without OOM-killing live
production daemons during an in-service update. This is **Workstream A** of
[install-update-resilience-plan.md](install-update-resilience-plan.md)
(problems #1 and #2); the decision and its rationale are
[ADR-0163](adr/0163-installer-builds-run-the-inverse-of-the-audio-daemon-memory-policy.md).
The AEC delivery split is owned by
[HANDOFF-enhanced-aec.md](HANDOFF-enhanced-aec.md): mandatory v1 is a quick
core-install build; vendored v2 is opt-in and reuses this containment policy.

## The one invariant

**No installer build step may starve or kill a live production daemon.**
A build that runs out of memory must die *itself* — never nginx,
jasper-voice, jasper-camilla, or any other running service.

## Current state (what this ships)

Every heavy build in the installer now goes through **one** policy in
[`deploy/lib/install/build-sandbox.sh`](../deploy/lib/install/build-sandbox.sh):

1. **RAM-aware parallelism** — `build_sandbox_jobs <kb_per_job>` computes
   `clamp(MemTotal / kb_per_job, 1, nproc)`. The installed
   `jasper-contained-build --jobs-cpp` entry point applies the canonical
   `kb_per_job=1500000` policy to optional v2. Lower parallelism ⇒ lower
   *peak* RAM ⇒ lower OOM probability.
2. **cgroup containment** — `run_contained_build <label> -- <cmd…>` runs
   the build inside a transient `systemd-run --scope` whose properties
   make it the **preferred OOM victim** and yield CPU/IO to audio
   daemons. Containment changes *who dies* under pressure; (1) only
   changes *how likely* pressure is. Both are required.

The build's scope properties are the deliberate **inverse** of the
audio-daemon policy — positive `OOMScoreAdjust` (`900`), swap allowed, low
`CPUWeight`/`IOWeight` (`20`), a soft ~85 % `MemoryHigh`, and no hard
`MemoryMax` by default. `build_sandbox_props` is pinned by tests. Why that
inversion, why containment is load-bearing rather than `-j` alone, and why the
runtime OOM ladder does not cover the build window:
[ADR-0163](adr/0163-installer-builds-run-the-inverse-of-the-audio-daemon-memory-policy.md).

On low-memory hosts (`RUST_LOW_MEMORY_BUILD_THRESHOLD_KB`, currently
~1.2 GB), install also creates a temporary high-priority build swap file
(`/var/tmp/jasper-build.swap` by default) and removes it on exit. The
swap is for installer children only; audio-path services remain protected
by their `MemorySwapMax=0` slices after runtime restarts. The same
low-memory path parks runtime units before Rust daemon builds so a 1 GB
full speaker does not compile `jasper-fanin` while voice/AEC/web Python
heaps are resident. JTS2 validated this path on 2026-06-29 after the old
live-build path repeatedly OOM-killed `rustc`.

**The park is undone on every exit path, not just success.** `install.sh`
traps `install_exit_cleanup`, which tears down the build swap and then calls
`unpark_low_memory_build_units`. Before it stops anything,
`park_low_memory_build_units` snapshots which of its units were actually
running — both phases, the renderers/output-owner/mux parked by
`park_audio_clients_for_core_graph_restart` **and** the graph plus control
plane it stops itself — and the trap restarts exactly that set, in the units'
own `After=` order (output owner and graph, then mux, then renderers and the
control plane). Two categories are deliberately *not* restored: units that were
already stopped before the install began (a source the household turned off at
`/sources/`), and units *this install* turned off — `disabled`, `masked` or
`masked-runtime` at restore time but not at the snapshot, which is how a
`full` → `streambox` conversion's deliberate brain park survives the trap.
That second test is a **change** in enablement, not a state:
`jasper-snapclient`/`-snapserver` run
while permanently disabled on a bonded speaker (the grouping reconciler starts
them; systemd never does), so skipping every unit that is merely off *now*
would strand them with nothing to re-run the reconciler before the next boot.
Without this, any abort in the build window left the speaker silently dead —
every daemon exited cleanly, so nothing looked broken; the box just vanished
from AirPlay and stayed gone (issue #2178, hit twice on a Pi Zero 2 W).

### Observability

Each containment decision is logged to both the deploy transcript and
journald (mirroring `memory-resilience.sh`'s `_mem_log`):

```
journalctl -t jasper-install | grep event=build_sandbox
# event=build_sandbox.contained   label=webrtc-aec3 unit=jts-build-webrtc-aec3-1234.scope
# event=build_sandbox.uncontained label=nqptp reason=systemd-unavailable-or-disabled
# event=build_sandbox.swap_enabled path=/var/tmp/jasper-build.swap size_mb=2048 priority=200
# event=build_sandbox.low_memory_build_park stopping runtime units before constrained Rust builds
# event=build_sandbox.low_memory_build_unpark parked=9 restored=9 failed=0
# event=build_sandbox.low_memory_build_unpark_failed unit=jasper-mux.service recover=systemctl start jasper-mux.service
# event=build_sandbox.low_memory_build_unpark_failed unit=bt-agent.service recover=systemctl unmask bt-agent.service && systemctl start bt-agent.service && systemctl mask bt-agent.service
# event=build_sandbox.low_memory_build_unpark_skip unit=jasper-input.service state=disabled left off on purpose
```

The unpark summary is emitted on every exit path, so `restored=0` ("the trap
ran and found the install had already restarted everything") is
distinguishable from the trap never running. `..._failed` is the line to grep
for after a household reports the speaker missing from their output list. Its
`recover=` is a command the operator can actually run *and* one that lands back
on the pre-install state: a unit that was masked while running (masking does
not stop one) refuses `start` until it is unmasked, so that case names `unmask`
first rather than repeating the command that just failed — and closes with
`mask` again, because stopping at `unmask && start` would silently delete a
mask the operator set on purpose, handing back a unit that a boot, a reconciler
or another unit's `Wants=` can start again. The mask's scope is carried through
(`mask --runtime` for a `masked-runtime` unit), so a /run-scoped mask is not
promoted to a permanent /etc one.

journald is persistent (PR #160), so the decision survives the watchdog
reboot a real build-OOM can trigger — which is exactly when you need to
know whether the build was contained.

## Build inventory (the class this generalizes)

| # | Build | Where | Tool | Profile | Now bounded? | Now contained? |
|---|-------|-------|------|---------|--------------|----------------|
| 1 | optional webrtc-audio-processing v2.1 + v2 binding | `jasper-enhanced-aec-install` via `jasper-contained-build` | `meson compile` C++ −O3 + isolated `pip wheel` wrapper | full, explicit opt-in | yes (`kb_per_job=1.5 GB`) | yes |
| 2 | mandatory jasper_aec3 v1 pybind11 binding | `python-runtime.sh install_jasper` | `pip`→`cc1plus` −O0 wrapper | full | n/a (single ext; content-cached) | yes |
| 3 | jasper-fanin | `rust-daemons.sh` | `cargo build --release` | full + streambox | cargo `-j` + temporary build swap/runtime park on low-memory hosts | yes |
| 4 | jasper-outputd | `rust-daemons.sh` | `cargo build --release` | full + streambox | cargo `-j` + temporary build swap/runtime park on low-memory hosts | yes |
| 5 | shairport-sync | `renderers.sh install_renderers` | `make` C autotools | full + streambox | yes (`kb_per_job=0.4 GB`) | yes |
| 6 | nqptp | `renderers.sh install_renderers` | `make` C autotools | full + streambox | yes (`kb_per_job=0.4 GB`) | yes |

The per-toolchain `kb_per_job` budgets reflect real peak RAM per
translation unit: C++ `-O3` ≈ 1.5 GB (webrtc's worst TU), C `-O2`
≈ 0.3–0.4 GB. Rust manages its own `-j` via `CARGO_BUILD_JOBS`; on
low-memory hosts `rust_cargo_build_env` also disables LTO and sets
`opt-level=0` so the source build fits.

Prebuilt artifacts remain the open follow-up for the Zero 2 W tier, where the
binding constraint is CPU time rather than RAM and containment does not help.
The two Rust daemons and the ring plugin already have a bundle lane
([HANDOFF-first-party-arm64-artifacts.md](HANDOFF-first-party-arm64-artifacts.md));
shairport-sync / nqptp / the webrtc archive do not, and
[HANDOFF-supply-chain.md](HANDOFF-supply-chain.md) owns that provenance
question.

`rust-daemons.sh` enables its low-memory cargo profile (jobs=1, no LTO)
below **1.2 GB**, so Zero-class and 1 GB Pi 5 boxes build Rust with the
same constrained profile. This is based on the 2026-06-29 JTS2 deploy
failure where the old 768 MB cutoff left a 991 MB Pi compiling
`jasper-fanin` with fat LTO and `rustc` was OOM-killed after AEC3 had
already been skipped. 2 GB+ boxes keep the normal release profile.

## Knobs

All read by `build-sandbox.sh`; all have safe defaults.

- `JASPER_BUILD_SANDBOX=auto|1|0` — containment on/off (`auto` =
  root + systemd present).
- `JASPER_BUILD_SANDBOX_OOM_SCORE_ADJ` (default `900`) — build's OOM
  preference; must stay strongly positive.
- `JASPER_BUILD_SANDBOX_MEMORY_HIGH` — override the computed ~85 %
  soft throttle.
- `JASPER_BUILD_SANDBOX_MEMORY_MAX` — opt-in hard cap (off by default).
- `JASPER_BUILD_SANDBOX_CPU_WEIGHT` / `_IO_WEIGHT` (default `20`).
- `JASPER_BUILD_SANDBOX_RUNTIME_MAX` — opt-in wall-clock cap (off by
  default; a slow Zero 2 W build must not be killed mid-compile).
- Test injection (mirrors `JASPER_RUST_MEMINFO_FILE`):
  `JASPER_BUILD_MEMINFO_FILE`, `JASPER_BUILD_NPROC`.

## What is verified vs. needs real hardware

**Unit-verified (hardware-free, `tests/test_install_helpers.py`):**
- `_ram_bounded_jobs` math across the Pi 5 SKU range and per-toolchain
  `kb_per_job` budgets; the runtime `jasper-contained-build --jobs-cpp`
  entry point uses that exact shared C++ budget.
- `build_sandbox_props` encodes the inverse policy: positive
  `OOMScoreAdjust`, **no `MemorySwapMax=0`**, low CPU/IO weight,
  `MemoryAccounting=yes`.
- `run_contained_build` degrades to a direct, unmodified exec when
  systemd is absent (the CI/macOS/container path) and never double-runs.
- Every core heavy build call-site routes through `run_contained_build`;
  the optional enhanced-AEC job reaches the same function through the
  installed `jasper-contained-build` entry point.

**Needs a real Pi (flag in the PR):**
- That `systemd-run --scope` actually contains a `meson`/`cargo`/`make`
  subtree on Pi OS Trixie during `ssh sudo bash install.sh`, that build
  stdout still streams to the deploy transcript, and that the `sudo -u pi`
  cargo build behaves correctly nested inside the scope.
- The end-to-end OOM behavior on a 1 GB Pi: induce memory pressure during
  the webrtc build and confirm the kernel kills the build, **not** nginx
  or jasper-voice (`journalctl -k | grep -i 'killed process'`).
- Zero 2 W (512 MB) viability of the source builds at all (CPU-time, not
  just RAM) — informs the prebuilt-artifact follow-up.

## Related

- [install-update-resilience-plan.md](install-update-resilience-plan.md)
  — the parent brief (Workstream A here; B = atomic/recoverable updates,
  C = hot-plug, D = tiering + stale-update).
- [HANDOFF-supply-chain.md](HANDOFF-supply-chain.md) — provenance policy;
  owns any prebuilt-artifact follow-up.
- [HANDOFF-resilience.md](HANDOFF-resilience.md) — the runtime memory
  resilience stages (the OOM ladder + cgroup slice this build policy
  complements but does not depend on).

Last verified: 2026-08-26 (every knob, helper name, and event string
re-checked against `deploy/lib/install/build-sandbox.sh` and the
`install_exit_cleanup` / `park_low_memory_build_units` call sites in
`deploy/install.sh`; the inverse-policy rationale moved to ADR-0163 and the
settled prebuilt-vs-source trade-off was dropped)
