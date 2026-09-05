# Lane `vision-context` — principle 3 (context management), measured

HEAD `f4ff89731979abfb0f27a4afb3f220d22d21e78c`. Read-only against the repo
(one artifact a view wrote into the checkout was moved out; `git status` clean).

**39 real invocations measured** (31 `jasper-round-views`, 4
`jasper-crossover-prescriber`, 3 bundle-dir re-runs, 1 `propose`) plus **25
`--help` renders**. Every number below is measured, not estimated. Scratch:
`<scratchpad>/work/vision-context/` (`matrix.json`, `runs/`, `runs2/`,
`cumulative/`, `subhelp/`, `helps/`).

---

## 1. The materialized fixture round

`<scratchpad>/work/vision-context/materialize.py` imports the product's own
writers via `tests/crossover_v2_banked_round.py:225` (`bank_measure_round`) and
`:302` (`bank_verify_round`), plus `tests/test_active_speaker_crossover_v2_round_views.py:116`
(`_make_round_dir`) for a cloud-bearing round the position-graded views can
actually grade.

| round | shape | files | total bytes | largest file |
|---|---|---|---|---|
| `r1-measure` | stage 1: CHECK + MEASURE (both solos) + one lateral pose + entry baseline | 9 | 66,247 | `positions/lateral_03_a01.json` 29,966 |
| `r2-verify` | stage 2: VERIFY take + flow state carrying `verify_priors.verify_measured` | 6 | 20,896 | `state.json` 9,552 |
| `r3-cloud` | cloud group: 4 graded seats with bearings, graded `spec` | 4 | 16,961 | `cloud_verify.json` 16,434 |

Per-file listing of `r1-measure`:

```
   268  bundle/37da56d28733/admission_authority.json
   498  bundle/37da56d28733/artifact_manifest.json
   631  .../crossover_v2/capture-1/positions/check_01_a01.json
  3570  .../crossover_v2/capture-1/positions/entry_baseline_04_a01.json
 29966  .../crossover_v2/capture-1/positions/lateral_03_a01.json
 29890  .../crossover_v2/capture-1/positions/measure_02_a01.json
   119  .../crossover_v2/capture-1/round_receipt.json
  1196  bundle/37da56d28733/info.json
   109  state.json
```

**Grid caveat (disclosed).** The fixture grids are 90 bins (`GRID`), 256
(`SOLO_GRID_HZ`), 301 (`VERIFY_GRID_HZ`). A real analyzed curve is 480 bins
(`jasper/audio_measurement/analysis.py:237`) and the lateral evidence basis is
121 bins (`jasper/active_speaker/crossover_v2/spatial.py:464-465`). Every
artifact byte size below therefore **understates a real round by roughly
1.3×–5×**. Treat the sizes as a floor.

---

## 2. The full measurement table

`jasper-round-views` = `python -m` on `jasper.cli.round_views`;
`jasper-crossover-prescriber` = `jasper.cli.crossover_prescriber`. Each run got
a pristine copy of the rounds tree so artifact attribution is exact.
"stdout curves?" = any JSON list of >16 numbers on stdout, with the longest run.

| # | command | exit | stdout L/B | stderr L/B | stdout curves? | artifact written (bytes) | prints path? | prints size? | prints next cmd? |
|---|---------|------|-----------|-----------|----------------|------------------|--------------|--------------|------------------|
| 1 | `entry r1-measure` | 0 | 0/0 | 1/262 | no | `entry_state_grade.json` (3,223) | yes | no | no |
| 2 | `per-seat r1-measure` | 1 | 5/247 | 1/202 | no | — | n/a | no | no |
| 3 | `agreement r1-measure` | 1 | 5/242 | 1/197 | no | — | n/a | no | no |
| 4 | `co-metrics r1-measure` | 1 | 5/243 | 1/198 | no | — | n/a | no | no |
| 5 | `directivity r1-measure` | 1 | 5/244 | 1/199 | no | — | n/a | no | no |
| 6 | `cloud-binding r1-measure` | 0 | 0/0 | 1/249 | no | `cloud_binding.json` (442) | yes | no | no |
| 7 | `forward-model r1-measure` | 0 | 0/0 | 1/359 | no | `forward_model.json` (13,508) | yes | no | no |
| 8 | `spec-sweep r1-measure` | 1 | 5/243 | 1/198 | no | — | n/a | no | no |
| 9 | `gate-sweep r1-measure` | 1 | 5/267 | 1/214 | no | — | n/a | no | no |
| 10 | `frequency r1-measure` | 0 | 0/0 | 1/169 | no | `frequency_view.json` (8,218) | yes | no | no |
| 11 | `inventory r1-measure` | 0 | 0/0 | 1/1149 | no | `inventory.json` (6,658) | yes | no | yes (17 templates) |
| 12 | `delay-landscape r1-measure --fc-hz 2000` | 1 | 5/349 | 1/304 | no | — | n/a | no | no |
| 13 | `delay-confirm r1-measure --fc-hz 2000` | 1 | 5/347 | 1/302 | no | — | n/a | no | no |
| 14 | `classify-features r1-measure/bundle` | 2 | 5/434 | 1/384 | no | — | n/a | no | no |
| 15 | `distortion r1-measure/bundle --dumps … --state …` | 2 | 5/240 | 1/190 | no | — | n/a | no | no |
| 16 | `close-reference --distance --fc-hz 2000 --driver-diameter-in 6.5` | 0 | 21/608 | 1/194 | no | — | n/a | no | no |
| 17 | `close-reference --far-round … --close-round … --fc-hz 2000` | 2 | 0/0 | 9/818 | no | — | n/a | no | usage only |
| 18 | `frozen r1-measure r2-verify` | 1 | 5/248 | 1/203 | no | — | n/a | no | no |
| 19 | `repeat r1-measure r2-verify` | 1 | 5/242 | 1/197 | no | — | n/a | no | no |
| 20 | `repeat-floor r1-measure r2-verify` | 2 | 0/0 | 2/192 | no | — | n/a | no | usage only |
| 21 | `forward-model r1-measure --measured-round r2-verify` | 0 | 0/0 | 1/466 | no | `forward_model.json` (25,762) | yes | no | no |
| 22 | `per-seat r3-cloud` | 0 | 0/0 | 1/287 | no | `per_seat.json` (13,130) | yes | no | no |
| 23 | `agreement r3-cloud` | 0 | 0/0 | 1/211 | no | `agreement.json` (6,268) | yes | no | no |
| 24 | `co-metrics r3-cloud` | 0 | 0/0 | 1/341 | no | `audibility_co_metrics.json` (536) | yes | no | no |
| 25 | `directivity r3-cloud` | 0 | 0/0 | 1/259 | no | `directivity.json` (21,117) | yes | no | no |
| 26 | `cloud-binding r3-cloud` | 0 | 0/0 | 1/247 | no | `cloud_binding.json` (440) | yes | no | no |
| 27 | `spec-sweep r3-cloud` | 0 | 0/0 | 1/368 | no | `spec_gate_sensitivity.json` (4,120) | yes | no | no |
| 28 | `gate-sweep r3-cloud` | 1 | 5/265 | 1/212 | no | — | n/a | no | no |
| 29 | `frequency r3-cloud` | 0 | 0/0 | 1/167 | no | `frequency_view.json` (32,501) | yes | no | no |
| 30 | `entry r3-cloud` | 0 | 0/0 | 1/186 | no | `entry_state_grade.json` (393) | **no** | no | no |
| 31 | `inventory r3-cloud` | 0 | 0/0 | 1/1147 | no | `inventory.json` (6,598) | yes | no | yes (9 templates) |
| 32 | `delay-landscape <bundle> --fc-hz 2000` | 0 | **284/6886** | 1/90 | **yes (65)** | `delay_landscape.json` (6,825, **written to CWD**) | in JSON only | no | yes (3 × `jasper-angle-capture stage …`) |
| 33 | `delay-confirm <bundle> --fc-hz 2000` | 1 | 5/366 | 1/321 | no | — | n/a | no | yes (`jasper-null --bundle-dir`) |
| 34 | `classify-features <bundle>` | 1 | 5/808 | 1/715 | no | — | n/a | no | no |
| 35 | `prescriber status r1-measure` | 0 | 17/1716 | 0/0 | no | — | n/a | no | partial (`jasper-seat-level`, bare) |
| 36 | `prescriber packet r1-measure` | 0 | 3/621 | 0/0 | no | `packet.json` (52,096) | yes | **yes** | no |
| 37 | `prescriber status r3-cloud` | 0 | 17/1868 | 0/0 | no | — | n/a | no | partial |
| 38 | `prescriber packet r3-cloud` | 0 | 3/675 | 0/0 | no | `packet.json` (73,859) | yes | **yes** | no |
| 39 | `prescriber propose --packet … --prescription …` | 1 | **0/0** | 1/89 | no | — | n/a | no | no |

`--help` sizes (run paths need hardware, not run):

| tool | help bytes | lines |
|---|---|---|
| `jasper-null` | 3,472 | 68 |
| `jasper-measure` | 3,247 | 63 |
| `jasper-round` | 2,323 | 46 |
| `jasper-angle-capture` | 2,377 | 51 |
| `jasper-crossover-prescriber` | 2,479 | 54 |
| `jasper-seat-level` | 2,715 | 50 |
| `jasper-round-views` (top) | 4,595 | 65 |
| 19 subcommand helps | 254 – 2,607 each; **18,840 total** | — |

### Where the curves actually live

| artifact | bytes | % of bytes inside numeric lists | longest list | scalar summary at top? |
|---|---|---|---|---|
| `packet.json` (r3) | 73,859 | 18% | 90 | yes (14 block-availability flags) |
| `packet.json` (r1) | 52,096 | 5% | 90 | yes |
| `frequency_view.json` (r3) | 32,501 | **55%** | 90 | no (`schema`, `normalization`, `runs`) |
| `forward_model.json` (r1+r2) | 25,762 | **71%** | 256 | yes (`predicted_minus_measured.max_abs_db/rms_db`) |
| `directivity.json` (r3) | 21,108 | 46% | 90 | **no** at top; per-band scalars buried in `rows[].bands[]` |
| `per_seat.json` (r3) | 13,124 | **70%** | 90 | **none at all** |
| `delay_landscape.json` | 6,825 | 46% | 65 | yes (`best_coordinate_us`, `best_predicted_null_depth_db`) |
| `inventory.json` (r3) | 6,464 | 0% | — | counts only, no sizes |
| `agreement.json` (r3) | 6,261 | 7% | 2 | yes (11 feature rows, all scalar) |

Total view artifacts beside `r3-cloud` after 8 successful views: **84,896 bytes
across 9 files**, described by **3,213 bytes of stderr one-liners**.

---

## 3. `inventory` — verbatim

`jasper-round-views inventory <r1-measure>` writes `inventory.json` (6,658 B)
and prints **nothing on stdout**. Its entire answer is one 1,149-byte stderr line:

```
inventory: 0/17 artifact(s) present; missing: jasper-round-views entry <this-round>, jasper-round-views frozen <other-round> <this-round>, jasper-round-views per-seat <this-round>, jasper-round-views repeat <this-round> <other-round>, jasper-round-views agreement <this-round>, jasper-round-views co-metrics <this-round>, jasper-round-views directivity <this-round>, jasper-round-views cloud-binding <this-round>, jasper-round-views forward-model <this-round>, jasper-round-views spec-sweep <this-round>, jasper-round-views gate-sweep <this-round>, jasper-round-views frequency <this-round>, jasper-round-views delay-landscape <this-round's bundle> --fc-hz <applied-corner>, jasper-round-views delay-confirm <this-round's bundle> --fc-hz <applied-corner>, jasper-round-views close-reference --far-round <this-round> --close-round <other-round> --close-m M, jasper-round-views distortion <this-round's bundle> --dumps <ring> --state <flow-state>, jasper-round-views classify-features <this-round's bundle> -> /…/r1-measure/inventory.json
```

Row shape (`jasper/cli/round_views/inventory.py:55-67`) — **exactly five keys**,
identical for present and missing rows:

```json
{
  "artifact": "directivity.json",
  "path": "/…/r1-measure/directivity.json",
  "present": false,
  "produced_by": "jasper-round-views directivity <this-round>",
  "producer_needs_more_than_this_round": false
}
```

**Does it name the exact runnable command? PARTIAL.** `produced_by` is a
template built from the `TAKES_*` constants at
`jasper/cli/round_views/_common.py:52-58` — `<this-round>`, `<other-round>`,
`<this-round's bundle>`, `<applied-corner>`, `<ring>`, `<flow-state>`, `M`.
Twelve of 17 rows carry only `<this-round>`, and the payload already holds
`round_dir` — so those twelve are one string substitution away from runnable
and are not substituted. Five rows carry a placeholder inventory genuinely
cannot fill. **Zero of 17 rows are copy-pasteable as printed.**

**Does it say how big the present artifacts are? NO — measured.** Ran the 8
views that succeed on `r3-cloud`, then `inventory`: `8/17 artifact(s) present`,
and every one of the 17 rows still carries the same 5 keys with no `bytes`,
no `mtime`, and no verdict. The 84,896 bytes sitting beside the round are
invisible to the tool whose job is to say what the round has.

**It also names producers this round cannot run.** On `r1-measure` it names 17;
measured, **5 produce an artifact and 12 refuse or error** (rows 2–5, 8, 9,
12–15, 18-equivalent, 34). `producer_needs_more_than_this_round` distinguishes
*argparse* arity, not *evidence* preconditions.

---

## 4. `crossover-prescriber status` next-actions — verbatim

```
next:
  - no crossover region is banked (field_null), so a blend prescription has no bound and is refused by name
  - no declared driver band is available (no driver design draft was supplied) — pass --drivers <design draft JSON>, or declare the drivers at http://jts.local/sound/setup/; without it a per-driver prescription has no bound and is refused by name
  - no applied profile is available (no applied baseline profile was supplied) — pass --applied-profile <applied baseline profile JSON>; without it this packet cannot name the correction the graph already carries, and a per-driver prescription's displacement is unknown
  - pass --state <flow state JSON>: `stage` refuses without it
  - no seat-level measurement reference is banked — measurement sessions ride the -20 dB main-volume fallback; `jasper-seat-level` sets the seat to the default 75-80 dB SPL target (--target-db-spl states another) and banks the reference
  - run or apply a round at http://jts.local/sound/crossover/
```

**Are they runnable commands? NO — 0 of 6.** Line 1 names no remedy at all.
Lines 2–4 are bare flag fragments with angle-bracket placeholders and no
program name. Line 5 names a program in backticks with no arguments and no
path. Line 6 is a URL — an out-of-band browser step. This is by design:
`jasper/cli/crossover_prescriber.py:863-866` — *"Artifact dependencies, not a
workflow: each line is the consequence of one artifact being present or
absent… Nothing here sequences anything."*

Two further measured facts about `status`:

- It opens with a reading order (`crossover_prescriber.py:1006-1012`) naming
  three docs by absolute path and **no size**. Measured:
  `docs/tuning-methodology.md` 55,257 B / 872 lines,
  `docs/tuning-operator-runbook.md` 93,826 B / 1,375 lines,
  `docs/measurement-loop-doctrine.md` 23,117 B / 380 lines —
  **172,200 bytes, 2,627 lines**, ordered read before anything else, with the
  cost undisclosed.
- `jasper-round apply` exists (`pyproject.toml:200`, `jasper-round --help`),
  but the string `jasper-round` appears in **neither** status output. Status
  routes apply to a browser.

By contrast `packet` is the toolbox's model citizen for principle 3: 621 bytes
on **stdout**, naming the path **and its byte size** — *"packet 67661cfb4693d2db
round=r1 -> /…/packet.json (52096 bytes)"* — plus which of 14 blocks are
unavailable and 11 not-evaluated fields. Measured across all 39 invocations,
`packet` is **the only one that prints a byte size**
(`jasper/cli/crossover_prescriber.py:269-277`).

---

## 5. Can the LLM know what to look at next, and how big it is?

### After bank — PARTIAL
`inventory` is the right verb and it exists. It names 17 producers, gives real
absolute paths, and distinguishes multi-round producers. It does **not** say
how big the round's own evidence is (66,247 B for `r1-measure`), does **not**
say which producers this round shape can actually run (5 of 17, measured), and
does **not** emit a runnable command. Cheapest honest path today: run all 17
and read 12 refusals (~3 KB of refusal text plus 12 wasted turns).

### After views — NO for size, PARTIAL for content
The one-liners are genuinely small (167–1,149 B; median 259 B) and each names
its artifact's path. But they carry counts and availability, almost never
measured values. `directivity r3-cloud` says *"4 seat(s), 0 not-evaluable,
against 1 onax seat(s); angles recorded"* — **zero dB figures** — while
`directivity.json` holds `level_offset_db`, `shape_rms_db`, `shape_max_db`,
`shape_max_hz` per seat per band. Nothing anywhere states a byte size, so the
LLM cannot decide whether opening the file is affordable until it has opened it.

### After prescribe — YES
`packet` gives summary + path + size + which blocks are missing, on stdout, in
621 bytes. `propose`'s refusal is 89 bytes and names the offending fields —
but on **stderr with 0 bytes on stdout** (the opposite convention from
`jasper-round-views`, whose refusals are JSON on stdout), and it names no
schema pointer and no next command.

### After apply — NO
`jasper-round apply` is a real CLI verb, but nothing in `status` names it, and
nothing anywhere marks the previous round's 84,896 bytes of view artifacts as
stale once a new graph is applied. The LLM must re-bank, re-run `inventory` on
a new directory, and infer staleness itself.

### Where it opens a wall of curves
Named, with measured byte sizes (fixture grids; multiply by ~1.3–5× for real):

- **`per_seat.json` — 13,124 B, 70% numeric, and zero scalar summary of any
  kind.** Top-level keys are `banked`, `round_dir`, `curve_grid_hz` (90 floats),
  `norm_band_hz`, `seats` (4 × `normalized_db` of 90 floats), `verify_pose`.
  There is no verdict, no spread, no worst-seat. To learn anything the LLM must
  read all four curves. This is the worst offender per byte.
- **`frequency_view.json` — 32,501 B, 55% numeric**, 10 numeric lists, no
  scalar summary at top.
- **`directivity.json` — 21,108 B, 46% numeric.** The scalars the LLM needs
  *are already in the file*: a band-scalars-only projection measures **1,430 B
  — a 93% saving** — and is never offered.
- **`forward_model.json` — 25,762 B, 71% numeric**, longest list 256. Best of
  the four: `predicted_minus_measured.max_abs_db`/`rms_db` are scalars, and the
  stderr line does name the acceptance verdict.
- **`delay-landscape` prints 6,886 B / 284 lines of its document directly to
  stdout**, including three 65-point float arrays — the only invocation of 39
  that puts curves in the transcript. Deliberate and documented
  (`jasper/cli/round_views/delay.py:94-95`), and it does print three runnable
  `jasper-angle-capture stage …` commands in `confirm_with`, which is the best
  next-command affordance measured anywhere. Its 90-byte `optimum_line`
  (`delay.py:125`) already exists and would carry the answer alone.

### Where the only summary is on stderr
**Every successful `jasper-round-views` view except two.** Measured: of 17
exit-0 `jasper-round-views` runs, **15 wrote 0 bytes to stdout** and put the
entire human answer (167–1,149 B) on stderr. Documented as intentional at
`jasper/cli/round_views/__init__.py:23-27`: *"prints a one-line human summary
to stderr either way"*. The two exceptions are `close-reference --distance`
(608 B doc on stdout) and `delay-landscape`.

Consequences, both measured:

- `out=$(jasper-round-views directivity $R)` captures **the empty string**.
  An LLM driving over SSH and reading stdout gets nothing unless it knows to
  add `2>&1`.
- `inventory`'s **entire** answer — all 17 producer commands, the whole point
  of the verb — is one 1,149-byte stderr line, the single most losable line in
  the toolbox.
- The convention **inverts on failure**: refusals print JSON on **stdout**
  (`__init__.py:147-148`). So `2>/dev/null` shows output only when a view
  *fails*, and `1>/dev/null` shows output only when a view *succeeds*. Neither
  redirection yields a coherent walk.
- The prescriber uses the **opposite** convention (summary on stdout, refusal
  on stderr — row 39: 0 B stdout / 89 B stderr). Two tools in one walk with
  mirrored stream contracts.

### Refusal documents (verbatim, for the refusal-shape question)

```
$ jasper-round-views per-seat r1-measure            # exit 1
{
  "detail": "/…/r1-measure: evidence packet carries no position evidence",
  "reason": "round_views_refused",
  "status": "refused"
}
```
```
$ jasper-round-views gate-sweep r1-measure          # exit 1
{
  "detail": "{\"looked_for\": \"**/summed/summed_*.json\", \"round_dir\": \"/…/r1-measure\"}",
  "reason": "round_no_captures",
  "status": "refused"
}
```
```
$ jasper-round-views repeat r1-measure r2-verify    # exit 1
{"detail": "/…/r1-measure: evidence packet carries no graded spec",
 "reason": "round_views_refused", "status": "refused"}
```
```
$ jasper-round-views delay-confirm <bundle> --fc-hz 2000   # exit 1
{"detail": "/…/null_runs: no measured inverted row at fc=2000 Hz; play the
 delay-landscape coordinates with jasper-null --bundle-dir first",
 "reason": "delay_confirm_no_measured_rows", "status": "refused"}
```
```
$ jasper-round-views distortion <round>/bundle …    # exit 2
{"detail": "no crossover_v2 round artifacts under evidence/v1 — the bundle must
 hold info.json beside evidence/v1/artifacts/crossover_v2/<capture-session-id>/",
 "reason": "round_views_unreadable_round", "status": "unreadable"}
```
```
$ jasper-crossover-prescriber propose …            # exit 1, stdout EMPTY
refused (prescription_malformed): unknown prescription field(s): actions, schema_version
```

The shape is uniform and machine-readable (`status`/`reason`/`detail`), which
is good. **Only one refusal of the eight measured names a next command**
(`delay-confirm`: *"play the delay-landscape coordinates with `jasper-null
--bundle-dir` first"*). The other seven name the missing evidence but not the
tool that would produce it — even where the tool exists.

### One more measured trap
Four subcommands take a `bundle_dir`, not a `round_dir`
(`classify-features`, `delay-confirm`, `delay-landscape`, `distortion`).
Handed the round directory, `delay-landscape` and `delay-confirm` exit 1
(rows 12–13); handed `<round>/bundle/<session-id>` they work (row 32).
`inventory` says `<this-round's bundle>` but its payload carries only
`round_dir` — the LLM must know that a session id sits between them.
Relatedly, when the input is not a *banked* round, the artifact defaults into
the **caller's CWD** (`_common.py:179-193`, deliberate, #3498): measured, row 32
wrote `/home/user/JTS/37da56d28733-delay_landscape.json`. Only the JSON's `out`
key reveals it; the stderr line does not.

---

## 6. Gaps and the smallest honest change

| # | gap (measured) | smallest honest change | size |
|---|---|---|---|
| 1 | 15 of 17 successful `round-views` runs write **0 B to stdout**; the whole answer is on stderr, invisible to `$(…)` and lost to `2>/dev/null`. Convention inverts on failure and again between the two tools. | Print the same one-liner on **stdout** as well whenever `--out` is not `-`. The summary strings are already built in each module. | small |
| 2 | Only `packet` states an artifact's byte size (1 of 39 invocations). | Introduce one `written_suffix(path)` helper in `_common.py` returning `" -> {path} ({n} bytes)"`, and replace the ~15 `f' -> {written}'` suffixes with it. | tiny |
| 3 | `inventory` rows carry no size and no verdict; 84,896 B beside `r3-cloud` reported as "8/17 present". | Add `"bytes": path.stat().st_size if present else None` to the row dict at `inventory.py:60-66`; put the total in the stderr line. | tiny |
| 4a | `inventory`'s `produced_by` is a template; 0 of 17 rows copy-pasteable though 12 need only `<this-round>` → `round_dir`, which the payload already holds. | Substitute `TAKES_THIS_ROUND` with `str(round_dir)` when formatting `produced_by`; leave the genuinely unfillable placeholders. | tiny |
| 4b | `inventory` names 17 producers where 5 can run (measured on `r1-measure`); `producer_needs_more_than_this_round` tracks argparse arity, not evidence. | Add `runnable_here` per row from the same precondition each view's loader already checks. | medium |
| 5 | No depth-on-demand anywhere: 0 of the 46 distinct flags across 19 subcommands is `--summary`/`--brief`/`--top-n`. Depth is one line or the whole file. | One generic verb, `jasper-round-views summarize <artifact.json>`, that drops every numeric list longer than N and prints the rest. Measured payoff on `directivity.json`: 1,430 B vs 21,108 B (93%). No per-view code. | small |
| 6 | `per_seat.json` (13,124 B, 70% numeric) has **no scalar summary at all**, and its stderr line carries only seat names. | Add per-seat scalars (spread, worst deviation and its Hz) to the payload and name the worst seat in the one-liner. Unlike gap 5 this needs new computation. | medium |
| 7 | `status` next-actions: 0 of 6 runnable; apply routed to a browser though `jasper-round apply` exists. | For the arms whose remedy is a CLI, emit the full command with the paths already in hand — six f-strings at `crossover_prescriber.py:855-940`. | small |
| 8 | `status` orders 172,200 B / 2,627 lines of docs read with no size disclosed. | Print bytes/lines beside each path in `_print_reading_order` (`crossover_prescriber.py:1006-1012`); the paths are already resolved. | tiny |
| 9 | `entry`'s NOT-GRADED arm writes the artifact (393 B, row 30) but omits the path from its summary. | Add the `f' -> {written}'` suffix to the branch at `grades.py:59`, matching `:69-77`. | tiny |
| 10 | 7 of 8 refusals name the missing evidence but not the producer, though `delay-confirm` shows the pattern works. | Give `RoundViewsError` an optional `produced_by` and print it in the refusal record; `ARTIFACT_BY_VIEW` already holds the strings. | small |
| 11 | `delay-landscape` puts 6,886 B / 284 lines and three 65-point arrays on stdout — the only curves in the transcript. | Write the document and print only the existing `optimum_line` (90 B, `delay.py:125`), as the other 17 views do. Deliberate today (`delay.py:94-95`), so it is a disclosed behavior change, not a bug fix. | tiny |
| 12 | 4 subcommands take `bundle_dir`; `inventory` says `<this-round's bundle>` but exposes only `round_dir`, and the session id sits between them. | Resolve and emit the bundle path in those four `inventory` rows — the loader already finds it. | small |

**Could not determine:** whether the stdout/stderr split was a deliberate
decision with an ADR behind it or an accretion — the module docstring
(`__init__.py:23-27`) states the behavior but cites no ADR, and I did not
search `docs/adr/` for one. Reading `docs/adr/` for a stream-convention ruling
would decide it.
