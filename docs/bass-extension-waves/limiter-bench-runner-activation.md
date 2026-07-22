# Bass Extension — bench-runner temporary-graph-activation seam (addendum)

> **Addendum to [`limiter-bench-runner-protocol.md`](limiter-bench-runner-protocol.md).**
> It fixes the *exact* mechanism the bench runner uses to temporarily activate a
> candidate on the **live** CamillaDSP graph and restore it — the single most
> hardware-safety-critical part of the runner — grounded in the Wave-0 spike
> evidence, so that code has a settled, safe, reviewable seam. **It does not edit
> the frozen [`limiter-evidence-protocol.md`](limiter-evidence-protocol.md)** and
> **changes no authorization**: hardware playback still requires the gate-2
> independent safety review at zero findings *and* Jasper's supervised on-device
> session.

## Why this exists

The amendment and the frozen protocol both said "apply … with the Wave-0
micro-stepped `PatchConfig` mechanism." A deep read of the current code found
that phrase under-specified for implementation: the live bass apply path
(`apply_bass_extension` → `recompose_active_baseline_for_bass_extension` →
`controller.reload`) rebuilds the whole YAML and reloads — it does **not** use
`patch_config` — and the *structural* bass block (adding `bass_ext_lt` +
`bass_ext_subsonic` + the owner pipeline step for a fresh commission) cannot be
"micro-stepped" as a parameter change. This addendum resolves that from the
Wave-0 evidence and the primitives that actually exist.

## Wave-0 evidence (the basis)

From [`docs/research/2026-07-16-bass-extension-spikes/README.md`](../research/2026-07-16-bass-extension-spikes/README.md):

- **Spike 1 — transition mechanism (`R1 confirmed`):** live `PatchConfig` with
  mandatory micro-stepping is the product mechanism for *audible* transitions;
  single hard `PatchConfig` and `SetConfigFilePath+Reload` are borderline
  (~1 dB burst) and are **not** the live mechanism. Crucially, live confirmation
  used **"identity LT appended via validated `SetActiveConfig`, one stepped
  transition during a −35 dB main-volume tone, then exact restore … full state
  restoration verified; `clipped_samples=0`"** — i.e. `SetActiveConfig` inside a
  low main-volume window, followed by an exact restore, is validated.
- **Spike 2 — persistence:** a `PatchConfig` change survives volume writes but is
  **reverted by `Reload`**. `PatchConfig`/`SetActiveConfig` mutate the *running*
  config; the on-disk config file is untouched.

## The settled mechanism

**Invariant (the fail-closed anchor): the runner NEVER writes the on-disk
CamillaDSP config file during a campaign.** Every activation mutates the
*running* config only (`set_active_config_raw` / `patch_config`). The predecessor
on-disk file therefore remains the recovery point, and a `reload()` restores it —
including after a crash or cancel.

All controller primitives below exist today on `jasper/camilla.py`'s
`CamillaController`: `get_config_file_path`, `get_active_config_raw`,
`set_active_config_raw`, `patch_config`, `reload` (each already routed through the
`camilla_graph_mutation` writer lock). The read-back proof functions
(`view_from_camilla_dict`, `bass_extension_block_valid`, `filter_param_matches`)
exist on `jasper/active_speaker/graph_safety.py`.

Per target/candidate, at the safe floor:

1. **Snapshot the predecessor** — record `get_active_config_raw()` +
   `get_config_file_path()` + the predecessor profile/preset, and confirm the
   running config matches the on-disk file (so `reload` is a valid restore).
2. **Fade to the safe floor** (existing ramp / `safe_playback`) and **prove the
   main volume is at/below the floor before any mutation.**
3. **Activate on the running config only:**
   - **Structural block** (the proposed sealed natural graph — `bass_ext_lt` +
     `bass_ext_subsonic` at the target's exact LT/subsonic params + the owner
     pipeline step, keeping the existing baseline limiter): build the graph with
     the existing `recompose_active_baseline_for_bass_extension` **but never write
     it to the file** — apply it via **`set_active_config_raw`** (the Wave-0
     `SetActiveConfig`-in-a-low-volume-window path). The transition happens at the
     floor, so there is no live-audio burst to micro-step.
   - **Per-candidate limiter change** (candidate pass only — the candidate
     `clip_limit` on the named baseline limiter): apply via **`patch_config`**
     (`{"filters": {<limiter_name>: {"parameters": {"clip_limit": <candidate>,
     "soft_clip": true}}}}`) — the focused running-config patch Spike 1 validated
     and `jasper/multiroom/runtime_balance.py` already uses in production for
     balance trim.
4. **Prove by read-back BEFORE unmuting.** Read `get_active_config_raw()`, parse
   with **`view_from_camilla_dict`** (CamillaDSP's re-serialized dialect — *not*
   `view_from_emitted_text`, which only parses JTS-emitted text), and require
   `bass_extension_block_valid(view, summary)` **and** the limiter
   `filter_param_matches(view, <limiter_name>, filter_type="Limiter",
   params={"clip_limit": <candidate>, "soft_clip": true})` **and** the exact owner
   channels + ordered chain (`bass_ext_lt` → `bass_ext_subsonic` → limiter). **Any
   mismatch → abort before unmute; do not play.**
5. **Measure** — unmute, run the admitted sweep/sustain, capture (the existing
   admitted-playback path). No file write, no persistence.
6. **Restore, fail-closed** — fade to the floor, then **`reload()`** (reverts to
   the untouched predecessor file, per Spike 2), read back, and **re-prove** the
   predecessor graph. Record the restoration receipt.
7. **Abort** (operator Stop or any protocol abort condition) → fade to floor +
   `reload()` + re-prove; preserve partial artifacts. Restore is idempotent:
   reloading the untouched file is always safe.

**On micro-stepping:** the Wave-0 Spike-1 micro-step (≥15 dB-margin param ramp) is
the **Wave-5 runtime scheduler's** concern for *live* retreat transitions during
playback. The bench runner performs every activation at the *silent* floor, so its
safety comes from **"mutate only while proven silent, and prove read-back before
unmute,"** not from micro-stepping an audible transition. The runner must never
unmute on an unproven graph and must never mutate above the floor.

## The activation helper (the seam the runner builds on)

A small, reusable, **bench-only** helper (in the runner's allowlist, e.g.
`jasper/bass_extension/…`) encapsulates steps 1–7 so the runner never open-codes a
graph mutation. Contract:

- **Input:** a `CamillaController`, the `measurement_window()` handle, the
  candidate (recomposed graph raw text + optional limiter `clip_limit`), the
  predecessor snapshot, and the read-back proof inputs (preset + profile summary).
- **Guarantees:** mutates the *running* config only (never the file); proves
  at-floor before mutating and read-back before unmute; **the restore-via-`reload`
  runs on every exit** (a `finally` / `asyncio.shield` path), including crash and
  cancel; raises a **typed** error (never silently proceeds) on any proof or
  restore failure.
- **Boundary:** it performs **no analysis, no bundle I/O, and no persistence** — it
  owns exactly the `activate → prove → (yield for measurement) → restore`
  lifecycle. It is **not** `apply_bass_extension` and calls no profile writer.

This is the "small activation helper" the runner composes; the campaign logic,
manifest, analysis, and bundle emission live above it.

## What this does NOT change

No authorization change. Production wiring of `produce_limiter_thresholds`, the
`review → accepted` commit, and every `apply_bass_extension` /
`bypass_bass_extension` / `recover_pending_bass_extension_apply` caller stay
blocked; the frozen protocol is untouched; hardware playback still requires the
gate-2 independent safety review at zero findings + Jasper's supervised on-device
session. This addendum only fixes **how** the (still-gated) runner mutates and
restores the live graph — replacing the under-specified "micro-stepped
`PatchConfig`" phrase with the exact, Wave-0-grounded, file-untouching,
reload-restorable mechanism above.
