# JTS measurement corpus — index

> WO-0 deliverable 1 of the attribution-stage plan, §7.
> Plan: [`docs/attribution-stage-plan.md`](../../historical/attribution-stage-plan.md).
> Machine-readable companion: `corpus-index.json` in the gitignored bulk-data
> directory `captures/wo0-retrospective-20260729/` (regenerate with
> `python3 build_corpus_index.py` there).
> Read-only sweep, 2026-07-29. Laptop numbers recomputed on every run;
> Pi numbers from the SSH sweep recorded in the JSON's `pi_snapshot`.

## Totals

| | roots/bundles | bytes | files |
|---|---:|---:|---:|
| Laptop `captures/` | 35 | 1,887,842,667 (1.89 GB) | 1,147 |
| jts3 `active_speaker/sessions` | 12 | 176,073,051 | 165 |
| jts3 `correction/sessions` | 41 (1 aggregate entry) | 71,155,678 | 213 |
| jts3 `xover-capture-dump` | 1 ring | 86,534,031 | 91 |
| **Combined** | **49 index entries** | **2,222,364,888 (2.22 GB)** | **1,616** |

**Date range: 2026-06-30 → 2026-07-29.** The acoustics work proper starts
2026-07-11; everything before that is room-correction shells.

## Where the data actually lives — four stores, no shared index

1. **`/Users/jaspercurry/Code/JTS/captures/`** (laptop, gitignored) — the
   agent/owner session archives. Best-documented tier: `xover-e0-2026-07-21`
   and `flat-linearization-20260725` each ship a MANIFEST/REPORT that names
   its own corpus, its matching method, and its own known defects. This is
   the shape the harness should copy.
2. **`/var/lib/jasper/active_speaker/sessions/`** (jts3) — the crossover-v2
   commissioning bundles. Holds the *evidence tree* (check/measure/candidate/
   cloud JSONs) and the accepted cloud WAVs.
3. **`/var/lib/jasper/correction/sessions/`** (jts3) — the room-correction
   bundles. Structurally richer per-bundle (acoustic_quality, position_analysis,
   runtime_integrity, repeat_captures) but **dormant since 2026-07-16**.
4. **`/var/lib/jasper/xover-capture-dump/`** (jts3) — the operator-enabled raw
   retention ring. The only place the raw CHECK/MEASURE WAVs survive; 90-file
   cap, currently full.

Nothing joins them. A capture's identity lives in three unrelated namespaces
at once — the bundle id (`8c2d69a5bfbd`), the capture-session id
(`cap_6lv47oF_qkVtwk9Fg6Liaw`), and the dump's epoch-microsecond stamp
(`1785340835947079`) — and the only reliable join is the SHA-256 of the WAV
bytes. (That is how this index proved the phase mislabel; see the catalog.)

## Crossover line — the 12 jts3 commissioning bundles

Chronological. `session_window` is `started_at → updated_at` from each
bundle's own `info.json`; note that a bundle's `started_at` is the moment the
*previous* bundle was retired, so the window is "the slot", not "the capture".

| bundle | window (EDT) | capture session | build | files | what happened |
|---|---|---|---|---:|---|
| `fcc4f4168d12` | 07-25 13:39 → 07-27 12:55 | `cap_vOpd_e2k…` | d30e5ba81 | 4 | short session, no evidence tree |
| `70fc554f3b55` | 07-27 12:55 → 12:58 | `cap_OQE72fQm…` | 52182859e | 6 | check+measure, no cloud |
| `f1d873fa57a1` | 07-27 12:58 → 12:58 | — | 52182859e | 3 | 7-second empty slot |
| `d5b171fa81a5` | 07-27 12:58 → 13:09 | `cap_4NUGqx3y…` | 52182859e | 30 | **first real cloud session**; echo-band ran outside its calibrated regime (#1763) |
| `0351ec1880a7` | 07-27 13:09 → 07-28 08:37 | — | 52182859e | 3 | empty overnight slot |
| `186d6a5311b4` | 07-28 08:37 → 12:47 | `cap__wwcU5n3…` | a7134fb4d | 30 | applied a profile that then failed its own VERIFY and stayed applied (#1813, #1809, #1810) |
| `cf073b6bc5fd` | 07-28 12:47 → 12:48 | `cap_unL3A2KW…` | db0ca6ba9 | 4 | 70-second session |
| `f5054a780a59` | 07-28 12:48 → 13:06 | `cap_obowsANF…` | db0ca6ba9 | 4 | short, check only |
| `e0d5385c09d0` | 07-28 13:06 → 16:22 | `cap_lMo1I-yx…` | db0ca6ba9 | 11 | clean check (SNR 51.8/25.2 dB), ε 30.8 ppm, delay 22.8 µs, snap found |
| `9445639e508f` | 07-28 16:22 → 07-29 08:27 | `cap_-Us10xOR…` | 790c10864 | 6 | **the #1838 field run** — MEASURE 24 dB too quiet, CaptureTimeout, applied nothing |
| `7f54494228cc` | 07-29 08:27 → 11:58 | `cap_J2OLTNvz…` | 0a3d78128 | 30 | **the Fc-forensics session** — applied `b0d89e3e`; source of #1855/#1857/#1867/#1868/#1869 |
| `8c2d69a5bfbd` | 07-29 11:58 → 12:00 (open) | `cap_6lv47oF_…` | 8b2b1fb5a | 34 | **the noon re-run** — VERIFY failed twice (3.665 → 3.820 dB max vs 1.5 dB tol), then `capture_timeout` |

Six of twelve slots hold ≤6 files. **Two hold nothing but `info.json`** — the
slot machinery mints a bundle whether or not a capture ever happens.

**Phase reach**, counted by which `*_program.wav` each bundle holds:

| phase | bundles reaching it |
|---|---:|
| CHECK | 10 / 12 |
| MEASURE | 7 / 12 |
| cloud_measure | 5 / 12 |
| VERIFY | 4 / 12 (+1 standalone, below) |
| candidate produced | 4 / 12 |

`fcc4f4168d12` is the odd one: it holds a `verify_program.wav` and **no**
check or measure — evidence that the shipped 1-entry re-verify machinery
(`prepare_v2_verify`) has been exercised at least once, which matters to
[#1873](https://github.com/jaspercurry/JTS/issues/1873)'s claim that there is
no reachable way to re-take just the verification.

### The two sessions the plan names

- **`cap_J2OLTNvzmApF0cEAgxrIZw`** (~08:27) is in bundle **`7f54494228cc`**,
  not in the 08:27-mtime bundle `9445639e508f` — that one holds the *previous*
  evening's #1838 run. Anyone navigating by directory mtime will land on the
  wrong bundle.
- **`cap_6lv47oF_qkVtwk9Fg6Liaw`** (~12:00) is in bundle **`8c2d69a5bfbd`**,
  and is the session still loaded in
  `/var/lib/jasper/active_speaker_crossover_v2_state.json` (78 KB; carries
  `verify_priors.predicted_sum` as 513 freq/mag pairs, the candidate, the
  cloud, and `pre_apply_profile` for the morning's applied `b0d89e3e`).

## Room line — 41 bundles, one paragraph

`2026-06-30 → 2026-07-16`, 71.2 MB. **25 of 41 are empty shells** whose only
content is `info.json` + an empty `captures/` dir and whose error reads
`capture never arrived — tap Start`. Eight carry an analysis payload; four
reached an acceptance verdict, all `accept`:

| bundle | date | verify rms / max (dB) |
|---|---|---|
| `e8230dc6549e` | 07-11 16:22 | 3.630 / 8.362 |
| `070e1f0b772b` | 07-15 07:20 | 3.248 / 7.776 |
| `e1150dae8f0e` | 07-15 07:28 | 3.193 / 8.016 |
| `cb9189c28620` | 07-16 00:43 | 2.169 / 8.864 |

Every session used `target_choice=flat`, `strategy_choice=balanced`. The
largest (`cb9189c28620`) accepted with its own `acoustic_quality` reading
`level=warn`, `snr_level=low`, `noise_capture_count=0` and the recommendation
*"remeasure or capture a noise floor before stronger advice"*.

**The room line has produced no data in 13 days**, so it cannot answer any
question about the current build. WO-7 (room-line adoption) is planning
against a stale corpus.

## Retention ring — `xover-capture-dump`

Enabled `2026-07-22T13:06`. 45 WAV + 45 JSON + the `ENABLED` marker = 91
files against a `XOVER_CAPTURE_DUMP_MAX_FILES = 90` cap and a 300 MB byte cap
(`jasper/web/correction_crossover_v2.py`). **The ring is full**: it now drops
its oldest capture on every new one, and at ~15 captures per cloud session it
holds roughly two session-days. The window it currently covers is
`2026-07-28 08:41 → 2026-07-29 12:09` — the 07-22 session that #1868 cites as
its case study has already rolled off and survives only in the laptop archive
`captures/xover-e0-2026-07-21/capture-dump-archive-20260722/`.

Sidecar contents are excellent (full `analysis_diagnostic_summary` on pass and
fail). Three problems: the `phase` label is wrong on 32 of 45 files (see the
catalog), files are mode 0600 root:root, and there is no index — the only join
back to a session is the WAV's SHA-256.

## Laptop archives, largest first

| archive | size | files | span | one line |
|---|---:|---:|---|---|
| `xover-e0-2026-07-21/` | 914 MB | 538 | 07-21…07-24 | E0 reproducibility experiment + overnight/bakeoff/honesty-guard follow-ons. Alignment confidence never cleared 0.60 in 7 runs. |
| `flat-linearization-20260725/` | 492 MB | 333 | 07-24…07-27 | S0 studio session (16 positions across 3 legs), cdhorn live runs 1–7, phase-0 replay of 87 captures, two-path-inversion NO-GO gate. |
| `iloud-comparison-20260727/` | 102 MB | 82 | 07-27 | Desk A/B vs an iLoud. Found the 7–11 dB-dark tweeter band. Holds the program's only measured preference target. |
| `jts3-hardware-20260711-*` ×12 | 260 MB | 95 | 07-11 | Per-driver/summed hardware capture bundles from the first commissioning day. Three of the twelve are **empty directories**. |
| `jts3-level-*` / `jts3-local-level-*` ×6 | 79 MB | 83 | 07-11 | Level/gain instrumentation runs (UMIK block timelines vs speaker state). |
| research docs ×9 | 130 KB | 11 | 07-24…07-29 | Deep-research prompts + results (gating v2, room correction, bass, enclosure diagnostics, the measure→diagnose→prescribe dissertation). |
| loose `usb-mic-*.wav` ×4 | 4.9 MB | 4 | 07-15 | USB mic-export lab captures sitting unfiled at the `captures/` root. |

### Named curve artifacts (readable without re-deconvolving a WAV)

- `iloud-comparison-20260727/analysis/analysis_desk.json` →
  `curves.gated.jts3` and `curves.gated.iloud`, 900-point log grid,
  anchor-normalised 300–3000 Hz. **The measured iLoud target curve.**
- `flat-linearization-20260725/cdhorn-live-session/curves.json`,
  `s0-analysis/*.npy` (7 arrays), `phase0-forensics/results.json`,
  `inversion-prototype/gate_results.json`.
- `xover-e0-2026-07-21/sigma-seeding-20260723/sigma_curves.json`,
  `overlay-20260722/*.json`, per-run `run<N>_summary.json`.
- On the Pi: each bundle's `evidence/v1/artifacts/crossover_v2/<cap>/
  {candidate,cloud_measure,check}.json`, plus the live
  `active_speaker_crossover_v2_state.json`.

## Corpus-wide data smells

1. **Bundle manifests do not describe their bundles.** Every
   `artifact_manifest.json` under `active_speaker/sessions/` lists exactly one
   artifact (`info.json`) while the bundle holds 3–34 files. Coverage: 3–33%.
2. **Bundle `info.json` is a stub.** `captures: []`, `summed_captures: []`,
   `apply: null`, `verification: null`, `mic.calibration_id: ""` — even on
   `7f54494228cc`, which contains a full 8-position cloud and an apply. The
   mic identity is recorded in the *dump sidecars*
   (`minidsp-minidsp_umik2-b7343c0c625b`) but not in the bundle.
3. **`state` is a slot marker, not an outcome.** `abandoned` is written when
   the next session opens. The room line has the same shape: bundles that
   accepted and applied a correction end at `idle`.
4. **Directory mtime lies about which session a bundle holds** (see
   `9445639e508f` above).
5. **Three empty laptop directories and four loose WAVs** at the `captures/`
   root — retries and one-offs that were never cleaned up or filed.
6. **Cross-store identity requires content hashing.** There is no id that
   survives from the dump sidecar to the bundle to the laptop archive.

## What WO-2 (quick-sweep harness) should take from this

The two best archives in the corpus (`xover-e0-2026-07-21`,
`flat-linearization-20260725`) are hand-written agent artifacts, and both do
the three things the product stores do not: a MANIFEST that enumerates the
corpus and its matching method, a REPORT that carries its own dated
corrections, and file naming that says what a file is. The product stores have
better *numbers* (the sidecars' `analysis_diagnostic_summary` is genuinely
excellent) and worse *self-description*. The harness's job is the union: the
sidecar's numeric honesty plus the archive's index-and-manifest discipline,
with one identifier that survives every hop.

---

*Generated 2026-07-29 by WO-0 Agent A (read-only). Sources: filesystem sweep
of `captures/`; read-only `ssh pi@jts3.local` listings of
`/var/lib/jasper/{active_speaker,correction}/sessions` and
`/var/lib/jasper/xover-capture-dump`; each bundle's own `info.json`; the
retained sidecars; and the issue/archive references named per row.*
