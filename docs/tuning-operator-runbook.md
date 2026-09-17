# Tuning toolbox — operator runbook

## Entry contract

Register the wired microphone with `jasper-mic-calibration`; set its capture control to 100%, and confirm its serial and calibration. Run `jasper-seat-level` once at the mark with the current microphone. Each run holds one level; the session gain is the default. The 85 dB SPL commissioning stop watches every take. Code owns capture, limits, graph composition, and evidence. The human or arm owns microphone movement. The LLM chooses the experiment, candidate, and interpretation. Never claim an unmeasured graph or moved microphone.

## The loop

1. Run `jasper-round run --program <speaker|room|bass>` for a measurement, or `jasper-round trial <fp>` to compare a whole document with base. Use `--candidates a,b,c` to compare two or three candidates at each pose before the mic moves. See the playbook's Document section. Use `run --dry-run` to inspect a custom plan without sound.
2. Join at each pose. With `--mover human`, open the returned page, follow its pose prompt, and use its in-place, Retake, or Done action. With `--mover arm`, run `jasper-angle-capture serve`. With `--mover confirmed`, call `jasper-round placed --run <id>` only after the person confirms placement. End a run nobody joins with `jasper-round stop --run <id>`.
3. Run `jasper-round wait --run <id>`. Read `index.md` first; the packet holds applied layers, set limits, series statistics, and a fit for each selected Speaker take and role. Add `--verbose` to see the view results. Use `status` to inspect progress without granting placement.
4. Select evidence by set. `speaker-fit`, `room`, and `repeat` use `jasper-round-views <verb> <round-dir> --set <set-id>`. Use `jasper-round-views sweep <round-dir> --scope round --set <set-id>`. Use `jasper-round-views bass-fit-table <round-dir…> --candidate <candidate.json>`. `inventory` lists exact available commands.
5. Author one prescription document. Run `jasper-crossover-prescriber judge <doc> --round <round-dir> --set <set-id>`, then `compose <doc> --base <fingerprint|saved> --round <round-dir> --set <set-id>`.
6. Trial the composed fingerprint with the same loop. Then run `jasper-round apply <fingerprint>`. Apply requires a banked complete trial of that graph, intact trial evidence, matching identity, and a proved layer stack. Its verification dimensions are advice, not another gate.

Use `jasper-round reset` to reset all tuning while keeping the measured level-match trims, or add `--keep-timing` to also keep timing. Timing is the physical arrival difference between the drivers. Once measured with confidence it does not change with EQ, room or bass work. Reset it only if you moved or replaced a driver, changed the enclosure, or changed the crossover so much that you want a fresh read. This reset verb is the only way to re-measure timing.

## Speaker

`speaker/mark` takes two measurements at the design mark. Driver caps still bind the fader. Use `speaker-fit`, `repeat`, and `sweep`; measure the composed full graph before apply.

## Room

Room defaults to `room/seat`: the three `seat_express` poses with the human mover, summed and ungated through the applied candidate, including its applied bass extension; room is off only when the run composes a candidate without it. Follow the page prompts; use Retake or Done there. `room/arm` keeps the three `room_quick` bearings for smoke tests. A room candidate trial uses the seat set; `trial <fp> --mover arm` selects the smoke set. The commissioning stop still applies. The room layer stops at the applied speaker's trusted floor, clamped to room bounds. Use `room` for the document and trial at the same poses.

## Bass

`jasper-round run --program bass --dry-run` lists the session level and offsets −5, −10, and −15 dB without sound. Each level uses the banked ambient bands to check SNR over the bass target band. An explicit `--level-db L --dry-run` checks only that level.

`jasper-round run --program bass` (or `jasper-round trial <fp>` for a bass candidate) runs the admissible level ladder at one pose under one hold, and `wait` joins the levels into the packet. `--level-db L` keeps one level, whose packet carries its bass view without a join.

## Evidence and recovery

Keep completed valid takes. Do not pool changed poses, levels, graphs, or calibration. Fix the named fault's action, then continue with the same loop. After an apply timeout, inspect saved state before another write. A losing candidate stays banked.

<!-- BEGIN GENERATED TOOL MENU (scripts/generate-tuning-tool-menu.py -- do not hand-edit) -->
| Tool | Does | Authority | Where |
|---|---|---|---|
| `jasper-basic-profile review\|apply` | Review and apply the basic profile -- the chosen crossover plus per-driver trim, delay and polarity, with no linearization and no blend correction, replacing the live tune and deleting no evidence. | mutating-with-gates | `jasper/cli/basic_profile.py` |
| `jasper-mic-calibration models\|fetch\|upload\|show` | Register the household's measurement microphone: fetch its vendor calibration by serial or store a file you already have, and remember that mic so every measurement resolves its calibration from one record. A box with no record measures uncalibrated. | advisory (`fetch`/`upload` write; `models`/`show` do not) | `jasper/cli/mic_calibration.py` |
| `jasper-seat-level` | Play the room/bass summed measurement sweep and adjust the fader until the calibrated mic's loudest half-second (loudest_half_second_db_spl) reads the target; bank the session gain. PRECONDITION: `amixer -c <card>` shows the mic's capture control at 100%, where its Sens Factor is quoted, or every absolute SPL is wrong by the shortfall. | measured | `jasper/cli/seat_level.py` |
| `jasper-angle-capture serve` | Serve the microphone arm against the daemon's position gate. | mutating (`serve` moves the arm) | `jasper/cli/angle_capture.py` |
| `jasper-measure` | Measure this speaker once, bank the takes, print their ids | measured | `jasper/cli/measure.py` |
| `jasper-crossover-prescriber rear-calibration\|contract\|judge\|compose\|status` | Judge and compose prescription documents; serve contracts and read status. | advisory (judge, contract and status read; compose banks a candidate) | `jasper/cli/crossover_prescriber.py` |
| `jasper-round run\|trial\|placed\|stop\|status\|wait\|apply\|reset` | Run a plan at --spl dB SPL or --levels fader dB, bank its packet, commission a speaker and apply candidates. | mutating-with-gates (`run`/`trial`/`placed`/`stop`/`wait`/`apply`/`reset` write; `status` reads) | `jasper/cli/round.py` |
| `jasper-round-views entry [speaker]\|frozen [speaker]\|repeat [all]\|repeat-floor [all]\|candidates [all]\|agreement [room/speaker]\|co-metrics [room/speaker]\|directivity [room/speaker]\|per-seat [room/speaker]\|cloud-binding [speaker]\|forward-model [speaker]\|sweep [all]\|frequency [all]\|distortion [speaker]\|dsp-replay [all]\|dsp-levels [all]\|classify-features [speaker]\|findings [all]\|close-reference [speaker]\|delay-landscape [speaker]\|delay-confirm [speaker]\|room [room]\|room-grade [room]\|bass [bass]\|bass-compare [bass]\|bass-fit-table [bass]\|inventory [all]\|speaker-fit [speaker]` | Read measured round evidence, including repeat --set spread across takes. Answers use stdout; detailed reports use files. | advisory (analysis views save artifacts) | `jasper/cli/round_views/__init__.py` |
| `jasper-null` | Play the summed reverse null and bank one row per coordinate. Measures only; grades nothing. | measured | `jasper/cli/null_door.py` |
| `jasper-audition start\|stop\|status` | Play this speaker at a reduced DSP layer, then put it back | mutating (runtime only; durable graph untouched -- ADR-0193) | `jasper/cli/audition.py` |
| `jasper-declare-geometry set\|show` | Declare measurement rig geometry: speaker/mic heights, distance and optional ceiling, so entanglement_floor_hz has a provenance-labeled, non-measured source on rigs where the measured reflection finder structurally never fires (issue #3502); and optional cabinet-back and side-wall distances for jasper-round-views room. | advisory (`set` writes; `show` does not) | `jasper/cli/declare_geometry.py` |
<!-- END GENERATED TOOL MENU -->

## Debugging — where to look first

Use the `link` from `jasper-round run`, or `crossover_url` from `jasper-crossover-prescriber status`; the page is `/sound/speaker/crossover/` over HTTPS with the speaker's local CA, and banked rounds are at `/sound/measurements/`. First read the structured reason, run `jasper-doctor --json`, inspect `:8780/state`, and fetch logs with `bash scripts/fetch-pi-logs.sh`. Tool details and exit codes live in each tool's `--help`; the [methodology](tuning-methodology.md) and [doctrine](measurement-loop-doctrine.md) own science and authority rules. Rear calibration document fields are enumerated in [rear-calibration-tuning-fields.md](rear-calibration-tuning-fields.md).
