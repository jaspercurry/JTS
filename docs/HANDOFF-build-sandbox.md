# Handoff: memory-safe, production-isolated installer builds

Canonical reference for how `deploy/install.sh` runs its heavy
compile/build steps without OOM-killing live production daemons during an
in-service update. This is **Workstream A** of
[install-update-resilience-plan.md](install-update-resilience-plan.md)
(problems #1 and #2). It does not re-architect AEC (see AGENTS.md
"Architecture is fixed; swap the engine, not the topology") and it does
not change *what* gets built — only the memory/CPU envelope each build
runs inside.

## The one invariant

**No installer build step may starve or kill a live production daemon.**
A build that runs out of memory must die *itself* — never nginx,
jasper-voice, jasper-camilla, or any other running service.

## Current state (what this ships)

Every heavy build in the installer now goes through **one** policy in
[`deploy/lib/install/build-sandbox.sh`](../deploy/lib/install/build-sandbox.sh):

1. **RAM-aware parallelism** — `build_sandbox_jobs <kb_per_job>` computes
   `clamp(MemTotal / kb_per_job, 1, nproc)`. This generalizes the
   PR #899 point-fix (`_webrtc_compile_jobs`, which now just delegates
   with `kb_per_job=1500000`). Lower parallelism ⇒ lower *peak* RAM ⇒
   lower OOM probability.
2. **cgroup containment** — `run_contained_build <label> -- <cmd…>` runs
   the build inside a transient `systemd-run --scope` whose properties
   make it the **preferred OOM victim** and yield CPU/IO to audio
   daemons. Containment changes *who dies* under pressure; (1) only
   changes *how likely* pressure is. They are complementary, and you want
   both.

The two levers map onto the two failure modes seen on jts2 (1 GB Pi 5,
748 commits behind, 2026-06-21): `meson compile` fanned out to `nproc`
`-O3 cc1plus` jobs (lever 1 fixes the fan-out), and when it still
exhausted RAM+swap the kernel OOM-killer took out **nginx** and
**jasper-voice** instead of the compiler (lever 2 fixes the victim).

### Why containment is the load-bearing fix, not just `-j`

Even at `-j1`, a single webrtc `-O3` translation unit
(`audio_processing_impl.cc`) peaks > 1 GB in `cc1plus` — on a 1 GB Pi
that *cannot* fit in RAM and must use swap. Bounding `-j` lowers the odds
of an OOM but cannot guarantee one never happens. Containment guarantees
that *if* one happens, the blast radius is the build.

And the runtime protections we already ship do **not** cover the build
window:

- The OOMScoreAdjust ladder in the unit files
  (`jasper-voice` −500, `jasper-camilla` −900, `jasper-outputd` −950,
  `jasper-control` −600, `jasper-fanin` −800) and the live-write in
  `migrate_memory_resilience` (`_apply_jts_oom_score_adj_live`) both run
  **at the end of `main()` — after every build**. During the build the
  running daemons sit at whatever their *currently-installed* (old) units
  set. On a far-behind box those old units predate the ladder entirely
  (adj 0), so they are prime OOM victims exactly when a build is running.
- **nginx is never in the ladder** (stock Debian unit, adj 0) — it was
  the first thing killed on jts2.
- `jts-audio.slice`'s `MemorySwapMax=0` and the per-unit `MemoryMax`/
  `MemoryHigh` directives are **silent no-ops until the memory cgroup
  controller is enabled** (Stage 2, requires a reboot after
  `migrate_cgroup_memory_enabled` edits `cmdline.txt`). A fresh or
  far-behind box building before that reboot has none of it.

`run_contained_build` sidesteps all of this: `OOMScoreAdjust=` on the
build's own transient scope is a per-process `/proc/PID/oom_score_adj`
write that **works without the memory cgroup controller** (same reason
`_apply_jts_oom_score_adj_live` exists). So even on a never-rebooted,
748-commits-behind box, the build is the kernel's first choice under
global memory pressure.

### The build policy is the *inverse* of the audio-daemon policy

This is the key design point and is pinned by tests.

| Property            | Audio daemons (`jts-audio.slice`, voice) | Installer builds (`build-sandbox.sh`) |
|---------------------|------------------------------------------|----------------------------------------|
| `OOMScoreAdjust`    | strongly **negative** (never kill)       | strongly **positive** (`900`, kill me first) |
| swap                | `MemorySwapMax=0` (never swap — latency) | **allowed** (slow is fine; completion matters) |
| CPU / IO weight     | high / default                           | **low** (`CPUWeight=20`, `IOWeight=20`) so the build yields to playback |
| `MemoryHigh`        | throttle to protect latency              | soft throttle (~85 % MemTotal) leaving headroom for PID1/sshd/daemons |
| `MemoryMax` (hard)  | bound the daemon                         | **off by default** — a hard cap that's too low would kill a legitimate single-TU compile and *regress* installs that used to squeak by on swap; opt-in via env |

A latency-critical daemon must never swap and must never be killed. A
build is the opposite: it should lean on swap to finish slowly, and it
should be the *first* thing sacrificed if the box is genuinely going down.

### Graceful degradation

`run_contained_build` contains only when it safely can. In `auto` mode
(the default) it wraps the build iff **root AND `systemd-run` on PATH AND
`/run/systemd/system` exists** (the canonical "systemd is the running
init" check). Otherwise — CI, a macOS dev box, a container without
systemd, `--dry-run` sourcing in tests — it runs the command directly and
unchanged. There is **no post-failure retry**: a contained command's exit
status propagates verbatim, so a real compile failure is never masked by
a second uncontained run. `JASPER_BUILD_SANDBOX=0|1|auto` forces it off /
on / auto.

### Observability

Each containment decision is logged to both the deploy transcript and
journald (mirroring `memory-resilience.sh`'s `_mem_log`):

```
journalctl -t jasper-install | grep event=build_sandbox
# event=build_sandbox.contained   label=webrtc-aec3 unit=jts-build-webrtc-aec3-1234.scope
# event=build_sandbox.uncontained label=nqptp reason=systemd-unavailable-or-disabled
```

journald is persistent (PR #160), so the decision survives the watchdog
reboot a real build-OOM can trigger — which is exactly when you need to
know whether the build was contained.

## Build inventory (the class this generalizes)

| # | Build | Where | Tool | Profile | Now bounded? | Now contained? |
|---|-------|-------|------|---------|--------------|----------------|
| 1 | webrtc-audio-processing v2.1 | `install.sh build_webrtc_v2_for_aec3` | `meson compile` C++ −O3 | full | yes (`kb_per_job=1.5 GB`) | yes |
| 2 | jasper_aec3 pybind11 binding | `python-runtime.sh install_jasper` | `pip`→`cc1plus` −O3 | full | n/a (single ext) | yes |
| 3 | jasper-fanin | `rust-daemons.sh` | `cargo build --release` | full + streambox | cargo `-j` (low-mem profile) | yes |
| 4 | jasper-outputd | `rust-daemons.sh` | `cargo build --release` | full + streambox | cargo `-j` (low-mem profile) | yes |
| 5 | shairport-sync | `renderers.sh install_renderers` | `make` C autotools | full + streambox | yes (`kb_per_job=0.4 GB`) | yes |
| 6 | nqptp | `renderers.sh install_renderers` | `make` C autotools | full + streambox | yes (`kb_per_job=0.4 GB`) | yes |
| 7 | optional ESP32 firmware | `install.sh _build_firmware_if_stale` | PlatformIO | opt-in only | unchanged (opt-in) | **not yet** (see below) |

Before this slice, #1 was the only RAM-aware build, #3/#4 had a binary
on/off low-memory cargo profile (flipping at 768 MB), #5/#6 were a
hardcoded `make -j4`, and #2/#7 had nothing. None were contained.

The per-toolchain `kb_per_job` budgets reflect real peak RAM per
translation unit: C++ `-O3` ≈ 1.5 GB (webrtc's worst TU), C `-O2`
≈ 0.3–0.4 GB. Rust manages its own `-j` via `CARGO_BUILD_JOBS`, so the
cargo builds are contained but keep the existing
`rust_cargo_build_env` profile.

## Decisions and open trade-offs

### Source-build vs. ship prebuilt per-arch artifacts

JTS already ships prebuilt binaries for **CamillaDSP**, **librespot**
(raspotify `.deb`), and **CamillaGUI** (download + `sha256sum -c`), and
mirrors the **source** archives for nqptp / shairport-sync / webrtc to a
GitHub release (`build-deps-v1`) with provenance in
[`deploy/provenance.toml`](../deploy/provenance.toml). Publishing
*prebuilt binaries* for nqptp / shairport-sync / the webrtc static
archive / the Rust daemons would remove source-building on tiny Pis
entirely.

- **Single arch covers the range.** Pi 5 and Pi Zero 2 W both run 64-bit
  Pi OS, so one `aarch64` artifact serves 512 MB → 16 GB. (HANDOFF-supply-chain
  already ships `aarch64` CamillaDSP/CamillaGUI.)
- **Cost.** A build pipeline (CI cross-compile or a build-Pi), a
  provenance entry + `sha256` per artifact, and keeping prebuilt versions
  in lockstep with the source pins. HANDOFF-supply-chain owns this and
  explicitly frames "record/rebuild already-installed renderer binaries"
  as deferred until JTS distributes images / supports third-party
  speakers.

**Recommendation:** containment + bounded `-j` first — it is the robust,
low-cost fix that holds regardless of whether a given input is
source-built or prebuilt, and it ships now with no new release infra.
Prebuilt artifacts are the right **follow-up specifically for the
Zero 2 W tier**, where the constraint is *CPU time* (a 4×A53 may take 30+
min, or be infeasible, on a webrtc/shairport build), not just RAM — and
containment does nothing for build *duration*. Track under the
HANDOFF-supply-chain "next slice" and the Workstream D tiering work
(e.g. is the software WebRTC AEC3 build even relevant on a Zero 2 W
streambox? it is full-profile-only today, so the Zero 2 W never builds
it — that already narrows the prebuilt surface to shairport/nqptp/Rust).

### What happens when a contained build won't fit

With the default policy (soft `MemoryHigh`, swap allowed, no hard
`MemoryMax`) the build throttles and leans on swap, completing slowly.
Only if the *whole box* still hits global OOM does the kernel kill the
build (high `OOMScoreAdjust`) — the install then aborts under `set -e`
with the old version still serving (problem #3 / Workstream B own making
that abort clean + observable). If an operator sets
`JASPER_BUILD_SANDBOX_MEMORY_MAX=`, a single TU exceeding it is killed and
the build fails *observably* rather than silently wedging the box — a
deliberate trade we leave opt-in.

### Rust low-memory threshold (noted, not changed here)

`rust-daemons.sh` enables its low-memory cargo profile (jobs=1, no LTO)
only below **768 MB**, so a **1 GB Pi 5 builds Rust at full `nproc` + fat
LTO** — and the LTO link is itself a memory spike. This slice contains
that build (so it can't kill daemons) but deliberately does **not** lower
the threshold: that would change tested full-speaker behavior
(`test_full_speaker_rust_build_keeps_release_profile`) and the produced
binary. Raising the threshold to ~1.5 GB so 1 GB boxes also drop LTO is a
reasonable follow-up with a measured before/after; it's out of this
surgical slice.

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
  `kb_per_job` budgets; `_webrtc_compile_jobs` still returns its exact
  PR #899 values via delegation.
- `build_sandbox_props` encodes the inverse policy: positive
  `OOMScoreAdjust`, **no `MemorySwapMax=0`**, low CPU/IO weight,
  `MemoryAccounting=yes`.
- `run_contained_build` degrades to a direct, unmodified exec when
  systemd is absent (the CI/macOS/container path) and never double-runs.
- Every heavy build call-site routes through `run_contained_build`.

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

Last verified: 2026-06-28 (removed build #8 — the jasper_resampler pybind11
binding was cut with the usbsink rate-match stage; the inventory is back to
seven builds)
