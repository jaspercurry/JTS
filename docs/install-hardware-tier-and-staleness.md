# Hardware-tier awareness & the stale-update question — design note

> **Status: design note + recommendation (Workstream D, 2026-06-21).**
> This is the investigation output for Workstream D of the install/update
> resilience planning brief (tier-aware install + cross-SKU testing + the
> version-skew risk question) — since landed and deleted per ADR-0199. Its
> sibling workstreams landed too: A (memory-safe builds) as
> [ADR-0163](adr/0163-installer-builds-run-the-inverse-of-the-audio-daemon-memory-policy.md),
> B (atomic/recoverable updates) as ADR-0172–0174, and C (hot-plug
> resilience) in the mic/AEC and output-hardware reconcilers' udev-triggered
> wiring already in the tree. This note records
> *findings, decisions, and a scoped PR*; it is not ongoing operational
> truth. Once the recommended work lands, the code plus the operational
> docs it updates (`install.sh`, the deploy script, the test files) are
> the truth.
>
> **2026-07-27 resolution of the WebRTC-build question:** keep the small,
> distro-linked AEC3 v1 binding as the unconditional software fallback, but
> move vendored v2/BEST_A into an explicit, contained background install.
> Statements below about `build_webrtc_v2_for_aec3` describe the
> 2026-06-21 incident-era installer, not current code.

## Bottom line up front

1. **Tier is not profile.** The installer's only hardware-ish branch
   today (`detect_default_install_profile`: "model says Zero 2 W →
   streambox") conflates *product role* (does this box run the voice
   brain?) with *hardware capability* (RAM / CPU / arch). They are
   orthogonal. The box that bricked — `jts2`, a **1 GB Pi 5 on the
   `full` profile** — proves it: small hardware, full product role.
   Recommendation: add an explicit, **detected hardware tier**
   (RAM/CPU/arch) computed once up front, orthogonal to the install
   profile, as the vocabulary the build knobs converge onto. *Tier
   answers "how do I build safely here." Profile answers "what do I
   build at all."*

2. **The stale-update risk is real but it is a *build-path* risk, not a
   migration risk.** Migrations are convergent and run every deploy, so
   being 748 commits behind runs the *same* migration set as being 1
   behind — no pile-up. What being far behind *does* do is invalidate
   build caches (the webrtc provenance marker, `Cargo.lock`, the pip
   constraint set), forcing exactly the expensive, OOM-prone
   *rebuilds* a current box skips. **Staleness × low-RAM is the
   dangerous quadrant**, and it is owned by Workstream A (safe builds)
   and B (atomic updates), not by a stepwise updater.

3. **Stepwise / checkpointed updates are NOT warranted.** They would
   build and install N intermediate SHAs, each paying the full
   cold-cache build cost on the constrained box — *amplifying* the real
   risk, not reducing it — for a migration hazard that does not exist
   by construction. Recommendation: reject stepwise; fix the build (A)
   and the atomicity (B); and in D's lane, **surface skew × tier in the
   deploy preflight** so the operator sees the dangerous quadrant before
   pulling the trigger.

4. **Never conditionally skip the software-AEC fallback.** That invariant is
   now satisfied by building the small distro-linked v1 binding on every full
   install. The several-minute vendored v2/BEST_A build is an optional
   enhancement, because removing v2 does not remove the fallback. Hardware
   remains dynamic and the reconciler may still move a chip-AEC box onto v1.

5. **The cross-SKU test matrix needs almost no hardware.** The
   decision layer (tier → build strategy, arch guard, dry-run plan)
   is pure-function and is already driven by synthetic-`/proc`
   injection in `tests/test_install_profile_tiers.py`. Extend that to a
   tier matrix. Only the *execution* layer ("does a contained build
   actually fit in 1 GB?") needs a constrained runner; use a
   memory-cgroup CI smoke (prior art: `scripts/pi-run-diagnostic.sh`)
   per-PR and a canary Pi for ground truth.

The scoped PR that falls out of this note (see
[§ Scoped PR](#scoped-pr-tier-detection--observability--arch-guard)) is
deliberately small and behavior-neutral on supported hardware: detect
the tier, surface it in the install log and both dry-run plans, and
fail-fast (overridably) on an unsupported architecture. It establishes
the tier vocabulary A/B/C plug into and closes the gap where a tier
mismatch wasn't self-evident from the tooling, by putting the tier in the
deploy transcript.

---

## What exists today (evidence)

### The one hardware branch is product-role, not capability

`detect_default_install_profile` in [`deploy/install.sh`](../deploy/install.sh)
is the entire hardware-aware surface of the installer:

```sh
case "${model}" in
    *"Raspberry Pi Zero 2 W"*) printf 'streambox\n' ;;
    *)                         printf '%s\n' "${INSTALL_PROFILE_DEFAULT}" ;;  # full
esac
```

It reads `/proc/device-tree/model` (injectable via `JASPER_PI_MODEL_FILE`)
and picks a *profile*. Profile resolution (`resolve_install_profile`,
persisted to `/var/lib/jasper/install_profile`) is otherwise about
product role: `full` builds the voice brain, `streambox` does not (see
`jasper/install_profile.py` and
[`dumb-endpoint-bringup.md`](dumb-endpoint-bringup.md)). There is **no
RAM/CPU/arch reading that drives build strategy** — the profile is the
only axis, and it answers a different question than "can this box build
the thing safely."

### Build memory-safety is real but scattered and un-unified

Two independent, differently-shaped guards exist, with two different
thresholds and no shared vocabulary:

| Guard | Where | Shape | Threshold |
|---|---|---|---|
| Rust low-memory profile | `deploy/lib/install/rust-daemons.sh` `rust_low_memory_build_enabled` / `rust_cargo_build_env` | binary on/off (jobs=1, lto=false, codegen-units=16, opt=0) | `< 1200000 kB` (~1.2 GB), `RUST_LOW_MEMORY_BUILD_THRESHOLD_KB` |
| WebRTC AEC3 `-j` cap | `deploy/install.sh` `_webrtc_compile_jobs` | graduated `-j` | `~1.5 GB / job`, clamped `[1, nproc]` (point-fixed in PR #899 after the jts2 OOM) |

Plus two more readers of the same `MemTotal` for *runtime* tuning —
`_compute_min_free_kbytes` and `_compute_target_zram_bytes` in
`deploy/lib/install/memory-resilience.sh`. That is **four independent
`awk '/MemTotal/'` reads** with three injection seams
(`JASPER_RUST_MEMINFO_FILE`, direct `/proc/meminfo`, …). Each is
correct in isolation; together they are a thresholds-can-drift hazard
and there is no single place that says "this is a `low` / `constrained`
/ `standard` box."

### The WebRTC fallback is unconditional; the slow enhancement is not

Current `deploy/lib/install/python-runtime.sh` builds
`JASPER_AEC3_BUILD_MODE=v1-only` on every full-profile install. That
preserves the `xvf_software_aec3` fallback the reconciler needs if firmware,
mic, or DAC readiness changes, while removing the vendored v2 compile from
time-to-first-value.

An operator can later request v2/BEST_A from the advanced Software surface.
Its root oneshot uses the same RAM-bounded containment policy, activates only
after source/ABI/digest verification, and never makes the core deploy fail.
Durable intent is retried after a successful build-manifest change, which
keeps it compatible with fast-moving `main`. `streambox` still omits the full
mic/AEC brain by product profile, not hardware tier.

### Architecture is implicitly arm64, with no guard

All prebuilt inputs are 64-bit only and `install.sh`'s own header says
"Raspberry Pi OS Lite (Trixie, **64-bit**)":

- CamillaDSP: `camilladsp-linux-aarch64.tar.gz`
- librespot/raspotify: `…_arm64.deb`
- CamillaGUI: selected by `uname -m`, only `bundle_linux_aarch64.tar.gz`

A box on 32-bit Pi OS (an easy Imager mis-pick on a Zero 2 W, which is
arm64-*capable* but often imaged 32-bit) fails **deep** in a binary
fetch/`dpkg` step with a confusing error, after partial mutation —
never up front with "this needs 64-bit."

### Staleness is surfaced binary-only, decoupled from tier

`scripts/deploy-to-pi.sh` already prints a binary "behind / current vs
`origin/main`" advisory (`classify_installed_vs_main` in
`scripts/_lib.sh`, pinned by `tests/test_lib_deploy_direction.py`). It
is intentionally binary (1 commit behind and 748 behind get the same
"update it" signal). It does **not** know the target box's RAM/CPU, so
it cannot say "far behind *and* low-RAM = the cold-rebuild quadrant."

---

## The stale-update question, answered

> *Does being 748 commits behind independently raise the update's risk,
> or is it just "more migrations"? Are stepwise/checkpointed updates
> warranted?*

Three hypotheses, each tested against the code:

### (a) One-shot migration pile-up — **not a real risk**

Migrations are **convergent and idempotent, and run on every deploy**.
They transform "old shape if present → new shape" against *current
on-disk state*; they do not chain across deploys, and the set that runs
is "every migration that exists at the target SHA" — identical at 1 or
748 commits behind. Evidence from `deploy/lib/install/env-migrations.sh`:

- `migrate_voice_keys_split`: "Already in the secret file … ? Just clean
  any stale operator seed left in jasper.env" then continue — and an
  explicit comment that it must `return 0` cleanly so `set -e` doesn't
  abort re-deploys.
- `migrate_wake_legs_config`: docstring "Idempotent — already-translated
  installs find nothing to migrate. Fresh installs … are a no-op."

Counting the whole set — the `migrate_*` / `retire_*` / `reconcile_*`
functions across `env-migrations.sh`, `memory-resilience.sh`, and
`install.sh`:

```sh
grep -hoE '^(migrate|retire|reconcile|remove)_[A-Za-z0-9_]*\(\) \{' \
  deploy/lib/install/env-migrations.sh \
  deploy/lib/install/memory-resilience.sh \
  deploy/install.sh | sort
```

That returns **16 functions: all 16 convergent, 0 that assume a prior
shape, and 0 that delete household state.**

The last clause is deliberately narrower than "delete anything", because
that phrasing is falsifiable by a grep of the same set: **6 of the 16 call
`rm -f`**. For **5** of them the target is only the `${jasper_env}.bak`
the same function just created with its own `sed -i.bak`
(`migrate_control_host_bind_seed`, `migrate_fanin_coupling`,
`migrate_grouping`, `migrate_transit_config`, `migrate_weather_config`) —
the superseded text's "destructive-but-safe (backup-before-delete or
idempotent `rm -f`)" bucket, which is a real distinction and is restated
here rather than folded away. The **6th**,
`remove_retired_audio_topology_state`, is the one genuine file removal:
its whole body is an `rm -f` of the retired dmix/fanin switch's state file,
which nothing reads and which `jasper-doctor` warns about on presence. It
is an unconditional cleanup rather than a migration, and it is in the set
because the grep above now matches `remove_` too — without that the
denominator would silently omit the one function that deletes anything.

(The earlier "31 total / 28 convergent / 3 destructive-but-safe" figures
are superseded and do not reproduce — the same grep returned 24
immediately before the #2285 deletion wave, and both functions in the set
that removed files, `retire_audio_topology_switch` and
`migrate_retired_source_state`, were among the ones that wave cut.) Every key-rewriting migration cited above still guarded
on both "old key present?" *and* "new key already there?" (e.g.
`migrate_transit_config`: `if [[ -f "${wizard_env}" ]] && grep -qE`;
`migrate_control_host_bind_seed` only rewrote the *exact* `0.0.0.0`
seed), so a box that predates even the old key degraded to a no-op, not a
misfire.

(2026-08-25 update: the fleet-migration gate cleared (owner-confirmed every
live Pi past every candidate migration) and the one-shot relocation loops
named above — `migrate_wake_legs_config`, `migrate_control_host_bind_seed`,
`migrate_transit_config`, `migrate_weather_config`, `migrate_fanin_coupling`,
and the grouping migration — were retired as unreachable; the same grep now
returns **10**. The two still-live ongoing sweeps, `migrate_voice_keys_split`
and `migrate_google_routes_key`, keep running every deploy. The point this
section makes — migrations are convergent and skew cannot pile them up —
still holds; only the roster and the count changed.)

Skew does not multiply migration count or introduce ordering hazards.
Residual: a box so old it predates a migration's recognized "old shape"
leaves a *stale key* (not corruption), and the installer re-installs
the **target** units/configs wholesale anyway (`rsync --delete` of the
package, fresh systemd units, re-rendered ALSA), so the runtime
topology is the target topology regardless of skew.

### (b) Cold / invalidated build caches — **the real amplifier**

At the time of the incident, `build_webrtc_v2_for_aec3` keyed its cache on a
provenance marker:

```sh
local source_id="${WEBRTC_AEC3_COMMIT}:${WEBRTC_AEC3_SHA256}"
if [[ -f "${static_archive}" ]]; then
    if [[ "$(cat "${provenance_marker}")" == "${source_id}" ]]; then
        ... return 0   # skip: current box hits this
    fi
    echo "  webrtc-audio-processing cache lacks expected provenance; rebuilding"
    rm -rf "${src_dir}"   # 748-behind box very likely hits THIS
fi
```

Over 748 commits the pinned `WEBRTC_AEC3_COMMIT` was very likely to have
changed at least once, so the far-behind box does the full ~3-5 min,
OOM-prone rebuild a current box skips. The same shape applies to:

- **Cargo** (`cargo build --release --locked`): a `Cargo.lock` / dep
  bump anywhere in the skew window invalidates the incremental `target/`
  the installer otherwise preserves across runs.
- **pip `[full]`** + `deploy/constraints-pi.pins`: a constraint/dep
  change forces wheel rebuilds of the heavy scientific deps
  (scipy/numpy/onnxruntime).

So being far behind **disproportionately triggered exactly the expensive
build steps that wreck a low-RAM box** — precisely what happened to jts2.
The current split removes vendored v2 from the core transaction; if durable
enhancement intent exists, it rebuilds asynchronously and remains contained.
Cargo/Python cache invalidation still makes staleness a core build-path risk,
so the broader conclusion (safe builds, not stepwise updates) remains.

### (c) Crossing multiple breaking topology changes at once — **low risk by design**

The installer installs the **target** topology wholesale (fresh unit
files with current socket ports/names, fresh nginx site, re-rendered
`/etc/asound.conf`) plus the convergent migrations above. It does not
step through intermediate topologies, so "many topology changes at once"
collapses to "install the current topology once." The residual risk is
a **half-applied** install (a `set -e` abort mid-run leaving new code
with un-restarted daemons), which is ADR-0172's transaction/rollback
concern, not a function of skew.

### Recommendation: reject stepwise updates; surface the quadrant instead

Stepwise/checkpointed updates are **not warranted**:

1. The migration hazard they would address does not exist (migrations
   are convergent).
2. They make the real risk *worse*: N intermediate builds × cold caches
   × a constrained box = more OOM exposure, not less.
3. High complexity (intermediate-SHA fetch, ordering, resume bookkeeping)
   for negative value.

The cheap, high-value staleness mitigation that *is* in D's lane:
**pair the existing "behind `origin/main`" advisory with the detected
tier in the deploy preflight**, e.g. a one-line warning when a box is
both behind and `low`/`constrained` — "this update will likely trigger
cold rebuilds on a low-RAM box; expect a long, memory-pressured build."
That turns a failure that wasn't self-evident into an up-front,
operator-visible signal. (The enforcement that makes it *safe* rather
than just *visible* is A + B.)

---

## Test strategy for the SKU matrix (without owning every board)

Split the matrix into a **decision layer** (cheap, pure, per-PR) and an
**execution layer** (needs a constrained runner / hardware).

### Decision layer — extend the existing synthetic-`/proc` pattern

`tests/test_install_profile_tiers.py` already fakes hardware with zero
boards: it writes a synthetic model file and drives the bash helper via
`JASPER_PI_MODEL_FILE` / `JASPER_RUST_MEMINFO_FILE`, e.g.

```python
model.write_bytes(b"Raspberry Pi Zero 2 W Rev 1.0\x00")
result = _run_install_helper(
    f"JASPER_PI_MODEL_FILE={shlex.quote(str(model))} resolve_install_profile ...")
assert result.stdout.strip() == "streambox"
```

and similarly injects `MemTotal` to assert the Rust low-memory profile
flips at ~1.2 GB. **This is the whole matrix mechanism** — synthetic
system files + sourced-helper invocation. Extend it to a tier table:

| RAM (MemTotal) | nproc | arch | Expected tier | rust profile | webrtc `-j` | arch guard |
|---|---|---|---|---|---|---|
| 512 MB | 4 | aarch64 | `low` | low-mem | 1 | pass |
| 991 MB (jts2) | 4 | aarch64 | `constrained` | release | 1 | pass |
| 2 GB | 4 | aarch64 | `standard` | release | 1 | pass |
| 8 GB | 4 | aarch64 | `standard` | release | 4 | pass |
| 1 GB | 4 | armv7l | `constrained` | — | — | **fail (overridable)** |

The `webrtc -j` and `rust profile` columns are pinned by the shared
`build_sandbox_jobs` / installed `jasper-contained-build --jobs-cpp` and
`rust_*` tests; the new columns (tier label, arch guard) get the same
treatment. Pure unit tests, every SKU, no hardware.

### Execution layer — does a contained build actually fit?

The decision tests can't prove a `-j1` webrtc compile survives inside a
1 GB cgroup. Three options, in increasing fidelity/cost:

- **Memory-cgroup CI smoke (recommended, per-PR or nightly).** Run the
  real webrtc/Rust compile inside a `systemd-run --scope -p MemoryMax=…`
  bound on the x86 CI runner — the exact pattern
  `scripts/pi-run-diagnostic.sh` already uses for bounded Pi work. The
  produced binary won't *run* on x86, but the question is "does the
  *compile* fit," and `cc1plus`/`rustc` memory is a strong (not exact)
  cross-arch proxy. Catches a regression that raises peak build memory
  before it reaches a Pi.
- **QEMU arm64 (nightly).** Real arm64 toolchain → closer to truth, but
  system emulation is slow and flaky; reserve for a nightly, not a gate.
- **Canary Pi lab (ground truth).** A 512 MB Zero 2 W and a 1 GB Pi 5
  that a scheduled deploy targets. The only place that shows real build
  *time*, thermals, and the actual OOM cascade. Highest fidelity,
  highest maintenance.

Recommendation: decision-matrix unit tests as the per-PR gate; one
memory-cgroup build smoke (per-PR if fast enough, else nightly); canary
Pi for periodic ground truth and the things only hardware reveals.

### Guard tests to add

- Tier-decision matrix (the table above), mirroring
  `test_install_profile_tiers.py`.
- Arch guard both ways (supported arch passes; unsupported aborts with a
  clear message and is overridable).
- The new dry-run "Hardware tier" line is covered automatically by
  `test_install_plan_covers_main.py`'s ratchet (a new `main()` step must
  be marked or exempted) — see the PR.
- Keep the shared C++ `build_sandbox_jobs` / `rust_*` threshold tests; the
  tier helper does not change their values (it *names* the regions they
  already act in). Optional v2 reaches the C++ budget through the installed
  `jasper-contained-build` helper.

---

## Scoped PR: tier detection + observability + arch guard

A clear, low-risk win falls out of this investigation. It is
behavior-neutral on every supported (arm64) box; the only behavior
change is the arch guard, which improves a real confusing failure mode
and is overridable.

**Adds** `detect_hardware_tier` to `deploy/install.sh` (beside
`detect_default_install_profile`): reads RAM, nproc, and arch through
injectable seams (`JASPER_HW_MEMINFO_FILE`, `JASPER_HW_NPROC`,
`JASPER_HW_ARCH`, all defaulting to the real system) and prints one
normalized line `ram_mb=… cpus=… arch=… tier=…`. The `low` boundary
**reuses** `rust-daemons.sh`'s `RUST_LOW_MEMORY_BUILD_THRESHOLD_KB` (one
source of truth — below it the Rust low-memory build is already active),
so the only tier-owned constant is the 2 GB `constrained`/`standard`
split (the jts2 OOM band vs. parallel-build headroom).

**Surfaces it** in `print_install_plan` + `print_streambox_install_plan`
(a "Hardware tier (detected on this host)" line — informative; identical
for `full`/`unset` and across legacy aliases on the same machine, so the
existing plan-equality tests still hold) and at real-install start via a
structured `event=hardware_tier.detected …` log line (stdout +
`logger -t jasper-install`, matching the memory-resilience events).

**Guards arch** via `hardware_tier_preflight`, in the real-install path
only (after the dry-run early return, before any mutation): a
non-`aarch64`/`arm64` arch aborts with a clear "JTS needs 64-bit
Raspberry Pi OS" message unless `JASPER_ALLOW_UNSUPPORTED_ARCH=1` is set
— the same fail-loud-but-overridable idiom as
`JASPER_ACCEPT_INSTALL_PROFILE_CHANGE` / `JTS_ACCEPT_NEW_IDENTITY`. The
new `main()` step is mapped to the "Hardware tier" marker in
`test_install_plan_covers_main.py`'s drift guard (the plan describes it),
and a dedicated test pins that the guard does *not* fire during
`--dry-run` (so the plan tests stay green on x86_64 CI).

**Pins it** with a tier-matrix test mirroring
`test_install_profile_tiers.py`.

**Follow-up map:**

- `rust_low_memory_build_enabled` now covers 1 GB hosts, and low-memory
  installs provision temporary high-priority build swap plus park runtime
  units before Rust builds. Optional v2 uses the canonical installed
  containment helper; memory-resilience readers can still converge further
  on the tier vocabulary.
- Gating the build manifest on verified success; rollback; broadening
  post-deploy verification beyond the management surface → **Workstream B
  (landed)**.
- Pairing the deploy preflight's skew advisory with the detected tier
  (the "dangerous quadrant" warning) → small follow-up once the tier is
  available remotely; depends on B's richer verification surface.
- Conditional software-AEC fallback skip → **rejected**. Conditional vendored
  v2 → **landed as explicit opt-in** without removing mandatory v1.

---

## Definition-of-done checklist

| Invariant | This note's contribution |
|---|---|
| Install/update degrade *loudly* across 512 MB–16 GB | Tier is detected and surfaced (log + plan); arch mismatch fails loud. Safe *degradation under build pressure* is A. |
| A failed update is observable, never false-success | This note diagnoses *why* (cold-cache rebuilds on low-RAM); the manifest/rollback fix is B (landed). |
| No build step can starve/kill a live daemon | Diagnosed (staleness × low-RAM forces the OOM-prone rebuilds); containment is A. |
| The whole flow is testable without owning every SKU | Test strategy above: synthetic-`/proc` decision matrix + cgroup smoke + canary. |

Last verified: 2026-08-25 (migration/reconcile denominator re-recounted to 10
after the env-migrations retirement wave removed the six one-shot relocation
loops named above; 2026-08-17 pass recounted
against the surviving set after the #2285 deletion wave; historical
WebRTC-build recommendation reconciled with the shipped mandatory-v1 /
optional-v2 split; broader tier/staleness analysis remains the 2026-06-21
design note)
