# vision-config-ladder — config ladders at a held position

Read-only survey at HEAD `f4ff89731`. Every claim cites `path:line` at that HEAD.

---

## 0. Headline

The loop **exists in three partial forms and is complete in none.**

| Form | What it varies at a held pose | Measures or predicts | Config identity banked? |
|---|---|---|---|
| `jasper-angle-capture --candidates fp1,fp2,…` (wizard round) | banked candidates' **alignment axes only** (polarity, delay, level-match) | measures | yes — `candidate_id` on every take |
| `jasper-measure --specs <file>` | the same variant axes, plus a **free-text** `candidate_id` label | measures | yes — but the label is unvalidated and does not change the graph |
| `jasper-round-views forward-model … --position-deg P --candidate-json {A,B,C}.json` | arbitrary filters/trims/polarity/delay, offline | **predicts** | yes — `candidate` block, but the default artifact name collides |

Nothing measures three genuinely different DSP graphs (different crossover
corner, different linearization EQ) at one held pose. The door for that —
republish-then-apply — is **HTTP-only and unsequenced**, and the runbook already
says so in as many words (`docs/tuning-operator-runbook.md:480-483`).

---

## 1. What a "round" is, and whether it records WHICH CONFIG

### 1a. The four verbs

`jasper/cli/round.py:353-429` — `open` (`_cmd_open`, `round.py:133`), `wait`
(`:188`), `apply` (`:208`), `bank` (`:238`). Explicitly no runner, no state
file, no resume (`round.py:14-21`). `bank` reaches no wizard
(`round.py:29-31`, `:282`).

### 1b. What a banked round directory contains

Owned by `jasper/active_speaker/round_bank.py:5-31` and assembled at
`round_bank.py:230-257`:

| Path | Written by | Optional? |
|---|---|---|
| `bundle/<session-id>/…` | hard-linked live bundle (`round_bank.py:232-236`) | no |
| `state.json` | crossover-v2 flow state | yes |
| `design-draft.json` | active-speaker design draft | yes |
| `applied-profile.json` | applied baseline profile SSOT | yes |
| `repeat-floor.json` | measured repeat floor SSOT | yes |
| `declared-geometry.json` | declared rig geometry SSOT | yes |
| `provenance.json` | `round_bank.py:244-253` | no |

Absent SSOT documents are named in `provenance.missing`
(`round_bank.py:237-242`). `provenance.json` keys: `banked_at_utc`,
`session_id`, `source`, `installed_sha`, `git_absent`, `missing`
(`round_bank.py:245-253`). The round id is the receipt's own `round_id` when
readable, else the session id (`round_bank.py:143-165`).

Inside the bundle, `round_receipt.json` is routed by
`jasper/active_speaker/crossover_v2/record_store.py:130-133` and built by
`jasper/active_speaker/crossover_v2/round_evidence.py:676-728`.

### 1c. **Yes — a round records which config was live, in four independent places**

| Field | File:line | Meaning |
|---|---|---|
| `round_receipt.entry_graph_fingerprint` | `round_evidence.py:709`; source `crossover_v2_flow.py:3861-3867` → `crossover_v2/coordinator.py:136-152` | the tuning-scope hash of the graph the round ENTERED on |
| `round_receipt.applied_graph_fingerprint` | `round_evidence.py:715` | the graph after the round's apply |
| `round_receipt.proposal_fingerprint` / `_kind` | `round_evidence.py:713-714` | what the round proposed |
| **every take's** `graph_fingerprint` | `crossover_v2/spatial.py:835` | which graph THAT capture played through |
| **every take's** `candidate_id` | `crossover_v2/spatial.py:837` (field at `:776`) | which banked candidate that capture was measuring |
| `info.json` `fingerprints.graph_fingerprint` | `bundles.py:440-442`, back-filled at `bundles.py:914-925` | **`None` at open**; only `record_apply` ever fills it, so a measure-only bundle leaves it null |

The comparability anchor itself is `tuning_scope_fingerprint`
(`crossover_v2/tuning_scope.py:29-44`) — the candidate layer and below, with
preference-EQ slots content-scrubbed (`tuning_scope.py:47-70`). The measurement
door computes it at entry as `entry_scope_fingerprint`
(`crossover_v2/door.py:76-81`, `session_graph.py:94-100`,
`session_graph.py:266-289`) and latches `comparability_boundary` when the graph
moves mid-session (`session_graph.py:103-111`, `:278-289`) — **disclosure, never
a gate** (`tuning_scope.py:24-26`).

**Honesty limit:** `entry_scope_fingerprint` hashes the entry config *file*, so
a live-only `set_active_config_raw` swap that left the statefile alone is
invisible to it (`session_graph.py:250-254`). `jasper-audition` is exactly such a
swap (`active_speaker/audition.py:5-12`).

### 1d. Can two rounds be told apart by config? **Yes, PARTIAL.**

The evidence packet republishes both receipt fingerprints
(`crossover_v2/evidence_packet.py:2829-2830`) and the bundle identity
(`evidence_packet.py:2813-2818`, fields at `:160-167`). So a reader with two
banked rounds can compare `round.entry_graph_fingerprint` and
`round.applied_graph_fingerprint`.

But: (a) a `jasper-measure` bundle never applies, so its
`identity.graph_fingerprint` stays null (`bundles.py:442`, `:919-920`); (b) the
per-take `graph_fingerprint` names the **measurement** graph the door installed,
not the config under test (`crossover_v2/door.py:76-80`); (c) no round-views
verb prints either fingerprint except `entry` (`cli/round_views/grades.py:74`)
and `repeat-floor`'s provenance rows
(`crossover_v2/round_views.py:1185-1197`).

---

## 2. Rounds vs positions

**A round is one walk of MANY positions**, but a one-position round is fully
expressible.

- The walk is stated ahead of the session by `jasper-angle-capture plan|stage`
  and taken from a one-slot spool (`active_speaker/angle_capture_spool.py:72`,
  `:166-191`). Staging twice is last-wins (`angle_capture_spool.py:172-174`);
  staging while a session is live is refused as
  `measurement_session_already_live` (`angle_capture_spool.py:104-107`).
- `--program spot --azimuth N [--elevation M]` is **one pose**
  (`cli/angle_capture.py:663-684`), so *one round = one position* is a
  supported shape.
- **One position AND one config: yes** — `--program spot --candidates fp1`.
  The stops expand POSE-MAJOR, CANDIDATE-MINOR, i.e. adjacent stops at one
  place (`active_speaker/angle_capture.py:422-437`), and `candidate_id` sits on
  the *stop*, not the walk, "since a candidate cycle is adjacent stops at one
  pose" (`angle_capture.py:213-216`, field at `:221`).

### What `jasper-measure` banks on its own

`jasper/cli/measure.py:5-15`: opens the speaker once, plays what one
`MeasureSpec` asks for, banks the takes, prints their ids; **grades, adopts and
restores nothing**. ONE placement per run — a second `--position` is refused
`measure_one_position_per_run` (`measure.py:60-61`, `:311-321`). A `--specs`
file may name N specs against that one placement; a file whose entries disagree
about the pose is refused `measure_specs_mixed_pose` (`measure.py:66-67`,
`:382-393`).

**It ties to no round and to no config identity.**
- Its report per spec is `candidate_id`, `kind`, `graph_fingerprint`,
  `record_ids`, `stimuli`, `stubs` (`measure.py:823-857`). The
  `graph_fingerprint` is `session.graph_fingerprint`, the MEASUREMENT graph
  (`measure.py:819`, `session.py:604`).
- It banks **no round receipt** — the four routes it can reach never include
  `ROUND_RECEIPT_KIND` for a measure-only run, and it opens its own bundle
  with `calibration_id=""` (`measure.py:705`).
- The measurement graph it plays through is emitted from the **declared**
  preset/topology, not from the live config
  (`measure.py:698-704`, `measurement_emit.py:35-54`), and the preset comes
  from the staged crossover preview
  (`measure.py:210-215` → `crossover_v2/conductor_context.py:374-378` →
  `commission_wiring.py:119-133`, `:163-171`).
- **Its records carry no `phase` and no `curves`** (`session.py:576-608`),
  which is why `read_lateral_take` (`position_cycle.py:217-218`) and
  `read_take_curves` (`position_cycle.py:250-253`) both reject them. The packet
  says this itself: "the engine's own capture record carries a candidate id and
  no phase at all" (`evidence_packet.py:960-962`).
  **Consequence: no `round-views` verb can grade a `jasper-measure` take.**

---

## 3. Config switching — what can actually be put live

| Tool | What it switches | Reachable from a shell? | Gate |
|---|---|---|---|
| `jasper-crossover-prescriber stage` | nothing live — writes ONE pending prescription for the NEXT round (`cli/crossover_prescriber.py:536-596`) | yes | one-file slot, **last-wins, not refused** (`prescription_spool.py:77-79`, `:293-295`) |
| `jasper-round apply --expected-fingerprint` | applies the candidate the wizard is publishing (`cli/round.py:208-235`) | yes | pre-flight fingerprint refusal (`round.py:23-27`) |
| `jasper-basic-profile apply` | back to structure+trim only — **one fixed config**, not an arbitrary one (`cli/basic_profile.py:5-19`) | yes | `baseline_candidate_fingerprint_mismatch` (`basic_profile.py:28-32`) |
| `jasper-audition start --layer baseline\|full` | **two layers only** (`active_speaker/audition.py:33-35`); a live-only raw swap (`audition.py:5-12`) | yes | refuses while a measurement session is live, `audition_measurement_session_active` (`audition.py:144-161`) |
| `POST /crossover/v2/republish` | **any banked candidate, by fingerprint** (`web/correction_crossover_v2_republish.py:5-25`) | **NO — no CLI verb, and `WizardClient` knows only status/session/verify/apply** (`active_speaker/wizard_client.py:28-35`) | apply-path gates |
| `jasper-active-speaker commission-load --preset` | a per-driver commissioning config into the running graph, armed silent (`cli/active_speaker.py:1383-1424`) | yes | single-flight; blocks audition (`audition.py:162-168`) |
| `jasper-angle-capture --candidates` | cycles banked candidates' **alignment axes** at each pose | yes | refuses a candidate carrying linearization EQ or no readable crossover: `walk_candidate_not_measurable` (`angle_capture.py:609-613`, `:643-694`) |

**Cheapest way today to do A → measure → B → measure → C → measure → roll back:**

1. *Measured, alignment-only:* one wizard round with
   `jasper-angle-capture stage --program spot --azimuth P --candidates A,B,C
   --mover human`, then `jasper-round open/wait`. One mic placement, three
   adjacent stops, three takes each stamped with its `candidate_id`. Rollback is
   free — the round applies nothing (`docs/tuning-operator-runbook.md:65-66`).
   This is the only *measured* ladder that exists.
2. *Measured, variant-axis-only, no round:* one
   `jasper-measure --specs ladder.json` at one `--position P`, three entries
   differing in `candidate_id` + variant axes. Also rollback-free (the door
   restores its entry graph, `door.py:209-219`). But see §5 — the label does not
   move the graph.
3. *Predicted, arbitrary configs:* three `jasper-round-views forward-model
   <round> --position-deg P --candidate-json {A,B,C}.json --out {A,B,C}.json`
   over ONE physical measurement. Nothing plays, nothing is switched, nothing
   needs rolling back (`cli/round_views/forward_model.py:5-15`). Doctrine caps
   what this may claim: **"A prediction is not a receipt"**
   (`docs/tuning-methodology.md:857`).

**Refusals that bite the ladder:** `walk_candidate_not_measurable` for any
candidate with linearization EQ (`angle_capture.py:651-657`) — so an EQ ladder
is refused outright; `measurement_session_already_live` for staging a second
walk mid-round (`angle_capture_spool.py:104-107`); `audition_measurement_
session_active` for auditioning while measuring (`audition.py:159-161`). There
is **no** "only one staged candidate" refusal — both spools are last-wins.

**Can a measurement run while a layer is auditioned? No.** Audition refuses to
start while a measurement session lives (`audition.py:144-161`), and in the
other direction the measurement door installs its own graph over whatever is
live (`door.py:11-13`, `:177`) — silently ending the audition, and invisibly to
`entry_scope_fingerprint`, which hashes the file (`session_graph.py:250-254`).

---

## 4. Comparison — what compares two rounds or two takes today

### TARGET's claim about `frozen`: **FALSE as stated, PARTIAL in spirit**

`HANDOFF-NEXT.md:253-254` says `round-views frozen` "compares one expectation to
one measurement". It takes **two round directories**
(`cli/round_views/grades.py:134-138`: positional `baseline_dir`, `target_dir`)
and produces `measured_delta_db` — the frozen half of TARGET minus BASELINE,
per role (`crossover_v2/round_views.py:721-774`, delta at `:764-767`). The
expectation half (`expected_delta_db`, `declared_tilt_db_per_octave`,
`round_views.py:772-773`) rides *beside* the measured delta, not instead of it.

**What `frozen` costs:** both rounds must have banked a cloud group
(`round_views.py:726-729`), and every target position must have a
`position_id` counterpart in the baseline (`:736-741`). A `--program spot`
one-pose round and a `jasper-measure` run bank no cloud group, so neither is a
`frozen` input.

### The full inventory of multi-input comparisons

| Verb | Inputs | What it compares | file:line |
|---|---|---|---|
| `frozen` | 2 round dirs | measured pooled flatness of target vs baseline, at shared `position_id`s | `grades.py:81-91`, `round_views.py:721-774` |
| `repeat` | N round dirs (`nargs="+"`) | **spread treated as NOISE** — the stop criterion | `repeat.py:112-116`, `round_views.py:1120-1183` |
| `repeat-floor` | N round dirs | banks that spread as the durable floor; rounds "must be touched-nothing fixed-pose repeats" | `repeat.py:118-140` |
| `frequency` | up to **2** sources (round, bundle, or JSON doc) | overlays two runs' curve series | `frequency.py:93-103` |
| `forward-model` | 1 basis round + optional `--measured-round` | predicted sum vs banked VERIFY sum | `forward_model.py:95-138` |
| `close-reference` | `--far-round` + `--close-round` | how much of a far read was the room | `_common.py:58` |
| `directivity` | **1** round dir | each cloud seat vs the on-axis reference — **positions, not configs** | `seats.py:15-20` |

**Nothing compares takes by `candidate_id`.** `grep candidate_id` over
`jasper/active_speaker/crossover_v2/round_views.py`, `jasper/cli/round_views/`
and `crossover_v2/round_inputs.py` returns **zero hits**. `repeat`'s output
carries only directory labels and no fingerprint at all
(`round_views.py:1104-1117`), so running it over an A/B/C ladder would present
config differences as repeat noise with no config identity in the artifact —
the exact misreading methodology warns about
(`docs/tuning-methodology.md:847`).

### What DOES already know about the ladder

`evidence_packet._candidates_block` (`evidence_packet.py:957-1002`) — "Which
candidates this round played, and at which poses… An INVENTORY, not a verdict",
grouping every phase's takes by `candidate_id`, landing at packet key
`candidates` (`evidence_packet.py:2760`, `:2853`). Its own note names the loop
precisely: *"the candidate cycle holds one pose and swaps the graph under it"*
(`evidence_packet.py:997-1001`). Its absence is inventoried with the sentence
*"nothing here says which configurations were played against each other"*
(`evidence_packet.py:2576-2583`).

The query primitive is already written and already filters on exactly the two
axes the loop needs: `record_index.bundle_measurements(bundle_dir, kind=,
phase=, position_deg=, vertical_deg=, candidate_id=)`
(`crossover_v2/record_index.py:121-152`). **It has no CLI exposure** — its only
callers are `evidence_packet.py:2758`, `ring_projection.py:195` and
`position_cycle.py:150`.

---

## 5. The sequence an LLM would run TODAY for P1×{A,B,C} then P2×{A,B,C}

Assuming A/B/C are banked candidates that differ only in alignment (the only
kind the toolbox can measure).

| # | Command | Verdict |
|---|---|---|
| 0 | *find the fingerprints of A, B, C* | **impossible without a script.** `find_banked_candidate` / `candidate_artifact_paths` (`candidate_bank.py:69`, `:113`) have no CLI and no view; the only callers are the web host and `angle_capture.py:236`. The LLM must have carried them out-of-band from an earlier envelope. |
| 1 | `jasper-declare-geometry set …` | works |
| 2 | `jasper-angle-capture plan --program spot --azimuth P1 --candidates A,B,C --mover human` | **works but the config identity is not shown.** `ResolvedStop` drops `candidate_id` (`angle_capture.py:478-492`, `resolve_request` at `:514-533`), and the printed walk's stop rows carry index/angle/regime/phase/prompt/screen only (`cli/angle_capture.py:353-363`). The run-level `candidates` list is a *set* (`cli/angle_capture.py:345`), so the LLM cannot tell which stop plays B. |
| 3 | `jasper-angle-capture stage …` (same flags) | works; one-slot, last-wins (`angle_capture_spool.py:166-191`) |
| 4 | `jasper-round open --tier express` / `wait` | works |
| 5 | *human moves mic to P1; three adjacent stops fire* | works |
| 6 | `jasper-round bank <session-dir>` | works |
| 7 | *repeat 2-6 for P2* | works, but **P2 is a second round** — the spool is consumed on take (`angle_capture_spool.py:23`), so each position is its own staged walk and its own banked round. |
| 8 | *compare A vs B vs C at P1* | **impossible without a script.** No view reads `candidate_id`. The packet's `candidates` block gives `candidate_id`, `n_takes` and `poses` — **no `take_id`, no curves** (`evidence_packet.py:984-991`) — and the `lateral_poses` block gives `take_id` and `position_deg` but **no `candidate_id`**, because `_TAKE_FIELDS` omits it (`position_cycle.py:55-56`, consumed at `evidence_packet.py:891-948`). Joining "candidate B" to its curve means joining on `(position_deg, vertical_deg)`, which is **ambiguous exactly in the ladder case** where all three share the pose. |
| 9 | *fallback:* `jasper-round-views forward-model <round> --position-deg P1 --candidate-json {A,B,C}.json --out {a,b,c}.json` | **works but predicts, and silently overwrites without `--out`** — one default artifact name, `forward_model.json` (`_common.py:88`), written at `forward_model.py:81`. |
| 10 | *fallback:* `jasper-round-views repeat <r-P1-A> <r-P1-B> <r-P1-C>` | **works but is DISHONEST** — it is the stop criterion for touched-nothing repeats (`repeat.py:5-14`, `:122-124`) and reports config differences as noise with no fingerprint in the record (`round_views.py:1104-1117`). |
| 11 | *if A/B/C were real DSP graphs, not alignments* | **refused** — `walk_candidate_not_measurable` for any linearization EQ (`angle_capture.py:609-613`, `:651-657`); and the only door that makes an arbitrary banked graph live is `POST /crossover/v2/republish`, which has **no CLI and is not in `WizardClient`** (`wizard_client.py:28-35`). |

The runbook already states step 11's gap verbatim: *"Multiple DSP configs per
position has a door but no wiring: republish-then-apply reaches a named prior
config between takes, and the open part is sequencing — holding a pose's next
capture until the apply has landed."*
(`docs/tuning-operator-runbook.md:480-483`).

---

## 6. The smallest honest affordances, ranked by honesty then size

**A new binary is not the answer** — the round directory already holds the
ladder; nothing reads it back.

| # | Affordance | Size | File it touches | Honesty |
|---|---|---|---|---|
| 1 | Add `"candidate_id"` to `_TAKE_FIELDS` so `lateral_poses[].takes[]` carries the config label beside `take_id` | **tiny** (one tuple entry + one behaviour pin) | `crossover_v2/position_cycle.py:55-56` | **highest.** Turns the ambiguous `(position_deg, vertical_deg)` join into an exact one. Adds no verb, no gate, no claim. Fixes the step-8 impossibility at the root. |
| 2 | Add `take_id` to `_candidates_block`'s per-candidate rows | tiny | `crossover_v2/evidence_packet.py:984-991` | high. Same join, from the other side; pick 1 or 2, not both. |
| 3 | Carry `candidate_id` through `ResolvedStop` into `_walk_payload`'s stop rows | tiny | `active_speaker/angle_capture.py:478-492`, `:514-533`; `cli/angle_capture.py:353-363` | high. `plan` currently prints a walk whose ladder ordering it cannot state, while the executor reads it fine (`web/correction_crossover_v2.py:2088-2118`). Pure disclosure. |
| 4 | Surface `door.entry_scope_fingerprint` in `jasper-measure`'s report, beside the per-spec measurement `graph_fingerprint` | tiny | `cli/measure.py:823-857` + `:721` (the door value already exists, `door.py:76-81`) | high — with the caveat it must be *named* as the entry graph, and disclosed as file-derived (blind to a raw audition swap, `session_graph.py:250-254`). |
| 5 | `jasper-round-views candidates <round-dir>` — one round, group takes by `candidate_id` at each held pose, report each candidate's curve and the pairwise delta; register `candidates.json` in `ARTIFACT_BY_VIEW` | **small** | `cli/round_views/_common.py:80-102` (one row), a new `cli/round_views/candidates.py`, `__init__.py:97-99` (`_FAMILIES`) | high. Reuses `record_index.bundle_measurements(candidate_id=…, position_deg=…)` (`record_index.py:121-152`) and `read_take_curves` (`position_cycle.py:224-256`); free pickup by `inventory` (`inventory.py:50-62`) and the generated tool menu. **`TAKES_THIS_ROUND`** — the ladder is *within* one round, not across N. Requires #1 or #2 first, or it re-derives the ambiguous join. |
| 6 | Document the recipe in the methodology / runbook: the three forms, what each may claim, and that `repeat` is not the comparator | small | `docs/tuning-operator-runbook.md` near `:65-73` and `:480-483` | high, and it is the only affordance that fixes the *misuse* risk of step 10. |
| 7 | Expose `republish` as a `jasper-round republish --fingerprint` verb (new `WizardClient` path + CLI verb) | **medium** | `active_speaker/wizard_client.py:28-35`, `cli/round.py:351-429` | medium. Removes the out-of-band curl, but does **not** close the loop: the runbook's real gap is *sequencing* — holding a pose's next capture until the apply has landed — and it names `awaiting_apply` as explicitly not the seam (`docs/tuning-operator-runbook.md:481-484`). Shipping the verb without the hold would invite an unsequenced ladder that silently measures the wrong graph. |
| 8 | A CLI listing of banked candidates (fingerprint, corner, alignment, measurability verdict) | small–medium | new verb over `candidate_bank.py:69-111` + `angle_capture.py:643-694` | medium-high. Fixes step 0, which is today an out-of-band dependency. Best folded into `jasper-crossover-prescriber status`'s `banked` section (`cli/crossover_prescriber.py:834-852`) rather than a new binary. |

**Do not** widen `repeat` to carry config identity: it is named and documented as
a *noise* instrument (`repeat.py:5-14`), and a metric that means "spread" cannot
also mean "difference" without lying about one of them.

---

## 7. Could not determine

- Whether a real `--candidates` ladder has ever been walked on hardware. Every
  claim here is static; `WAVE-LOG.md` and `HANDOFF-NEXT.md:274-278` both say no
  end-to-end round has been walked with the consolidated binaries. What a
  three-stop cycle actually costs the household (mic drift across three adjacent
  stops, whether one graph swap per stop is audible) is unmeasured.
- Whether `jasper-measure`'s three-spec ladder produces takes any offline reader
  can use at all. I established the records carry no `phase` and no `curves`
  (`session.py:576-608`) and that both take readers filter on `phase`
  (`position_cycle.py:217-218`, `:250-253`), but I did not run the code —
  a WAV-side reader I did not find could still close the gap. Deciding test:
  bank one `jasper-measure` take and call
  `record_index.bundle_measurements(bundle_dir)` then `read_take_curves` on the
  row's path.
