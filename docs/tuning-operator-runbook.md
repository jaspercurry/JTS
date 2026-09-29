# Tuning toolbox — operator runbook

## Entry contract

Register the wired microphone with `jasper-mic-calibration` and confirm its serial and calibration. Run `jasper-seat-level` once at the mark with a calibrated microphone. Each run holds one level; the session gain is the default. Keep the session gain across rounds so a DSP change's loudness effect stays visible. The 85 dB SPL commissioning stop watches every take. Code owns capture, limits, graph composition, and evidence. The human or arm owns microphone movement. The LLM chooses the experiment, candidate, and interpretation. Never claim an unmeasured graph or moved microphone.

## The loop

First run `jasper-crossover-prescriber status` without a round. Read `applied`,
`last_banked`, and `next` for the current layers, recent rounds, and next program.
`last_banked` keeps the latest round per applicable program: `round_id`, `round_dir`,
`banked_at`, `status`, and `stale`. `stale: true` means its applied identity differs or it was
banked at or before the last apply; only current rounds guide the next action.
With a current-identity round, all applicable layers applied, and none stale,
`next` is `{"program": null, "reason_code": "complete"}`. Without a current round,
`next` uses applied layers; `never_measured` means no profile is applied.
`next_commands` lists commands; add a round path for its evidence.

Run the tuning programs in order: speaker → rear → bass → room (skip rear if there is no rear driver).
Graph layer order is not program order: a composed graph stacks speaker → room → bass ([ADR-0303](adr/0303-a-trial-plays-the-candidate-as-composed.md)), and the row order in [`measurement_programs.py`](../jasper/active_speaker/measurement_programs.py) owns program order.
Re-run room after any upstream change, even when round history is unavailable.
The rear program's hand loop of record is the [Seat trial](tuning-playbook.md#seat):
preview from the pair at the mark, compare at the seats, then fit room from the chosen set.

1. Run `jasper-round run --program <speaker|rear|bass|room>` for a measurement (a program runs its default preset; name another preset such as `rear/pair`, and `--layout` picks one of the layouts it offers), or `jasper-round trial <fp>` to compare a whole document with base. A trial runs the program its document states and banks under it; a document that spans programs trials the first it states of rear, bass, room, speaker. `trial` takes composed documents; trial a fitted or migrated candidate with `run --program <program> --candidates base,<fp>`. `trial` also takes `run`'s plan flags: `--mover` picks that program's trial layout the mover can walk, and `--layout` names another. Use `--candidates a,b,c` to compare two or three candidates at each pose before the mic moves; add `--poses 0` to compare them at one spot. See the playbook's Document section. Add `--dry-run` to `run` or `trial` to price the plan without sound. `run --request '{"program": "bass", "layout": "seat_express"}'` takes the same run as one JSON object keyed by the flags' own names; the measure page posts its choice as such a request, and the speaker resolves both with one resolver. `jasper-round presets --json` lists every preset with what it plays and when to use it, and each layout's poses, the outputs its driver poses play on this speaker, and one level's captures and seconds.
2. Join at each pose. With `--mover human`, open the returned page, follow its pose prompt, and use its in-place, Retake, or Done action. With the arm, run `sudo -n /opt/jasper/.venv/bin/jasper-round run --mover arm --attest-rig-clear --wait`; add the plan flags from step 1. The flag is the person’s statement that the full sweep path is clear. `jasper-round run --mover arm --attest-rig-clear --wait` owns the arm. The retired `jasper-angle-capture serve` keeps its menu row until that path is proven on jts3 hardware, and is then deleted. Never start `serve` next to a waited arm round: two walkers would drive one arm. With `--mover confirmed`, call `jasper-round placed --run <id>` only after the person confirms placement. A layout can pin its mover (for example, `rear_behind` pins `human`); another `--mover` is refused with `walk_mover_mismatch`. End a run nobody joins with `jasper-round stop --run <id>`.
3. If the run omitted `--wait`, run `jasper-round wait --run <id>`. Read `index.md` first; the packet holds applied layers, set limits, series statistics, and a fit for each selected Speaker take and role. Add `--verbose` to see the view results. Use `status` to inspect progress without granting placement.
4. Select evidence by set. `jasper-round list` shows banked rounds and `jasper-round show <round-id>` their set and take ids; views take a round id or path. `speaker-fit`, `room`, and `repeat` use `jasper-round-views <verb> <round-dir> --set <set-id>`. Use `jasper-round-views sweep <round-dir> --scope round --set <set-id>`. Use `jasper-round-views bass-fit-table <round-dir…> --candidate <candidate.json>`. `jasper-round-views catalog --program <program>` lists every tool that program's rounds can use, with the question each answers; `inventory` lists exact available commands. Compare two view answers only when their `schema` and `parameters` agree; `jasper-round-views --help` names the envelope fields every answer shares.
5. Author one prescription document. Predict driver/blend from a branch diagnostic round, room from a room round, or rear calibration from a pair round with `jasper-crossover-prescriber judge --preview <doc> --round <round-dir> --set <set-id>`; bass has no preview. Run `jasper-crossover-prescriber judge <doc> --round <round-dir> --set <set-id>`, then `compose <doc> --round <round-dir> --set <set-id>`. The document's `base` (a fingerprint or `saved`) is the one base selector.
6. Trial the composed fingerprint with the same loop when you need evidence to choose; apply does not require a trial. Then run `jasper-round apply <fingerprint>`. Apply composes the banked candidate's graph, refuses a crossover below the declared floor or a graph that fails the graph-safety proof, and loads the graph into CamillaDSP.

Use `jasper-round reset` to reset everything, including the rear stage, or `jasper-round reset --program {speaker,rear,bass,room}` to reset one program. Both keep the measured level-match trims. Add `--keep-timing` only for everything or `--program speaker`. Timing is the physical arrival difference between drivers. Reset it only after a driver, enclosure, or major crossover change that needs a fresh read.

## Speaker

`speaker/mark` takes two measurements at the design mark; full-speaker sweeps cover 20 Hz–20 kHz from the resolved driver bands (ADR-0328). Driver caps still bind the fader. Use `speaker-fit`, `repeat`, and `sweep`; measure the composed full graph before apply. A speaker document's trial plays each candidate summed at the mark and banks `speaker/mark`.

All measurement programs refuse before sound with `walk_layout_unsupported_for_per_driver_programs` when the layout declares three driver roles (woofer, mid and tweeter); these programs are not built for that layout yet.

## Rear

`jasper-round run --program rear --dry-run` shows the rear measurement plan without sound.

`rear/pair --layout speaker_mark` banks the pair model at the mark. Hand trials of rear documents
bank `rear/seat` with a room view per candidate set; the arm uses `rear_express`.
The [Rear section](tuning-playbook.md#rear) explains the model and its figures.

## Bass

`jasper-round run --program bass --dry-run` lists the session level and offsets −5, −10, and −15 dB without sound. Each level uses the banked ambient bands to check SNR over the bass target band. An explicit `--level-db L --dry-run` checks only that level.

`jasper-round run --program bass` (or `jasper-round trial <fp>` for a bass candidate, or the measure page's bass choice) runs the admissible level ladder at each pose under one hold, finishing a pose before the next, and `wait` joins the levels into the packet. `--level-db L` keeps one level, whose packet carries its bass view without a join.

`bass/axis` pins the arm. By hand, add `--layout seat_express --mover human` (the three seat poses rear and room use); `trial --mover human` picks it. The 85 dB SPL stop still watches every take.

## Room

Room defaults to `room/seat`: the three `seat_express` poses with the human mover, summed and ungated through the applied candidate, including its applied bass extension; room is off only when the run composes a candidate without it. Follow the page prompts; use Retake or Done there. `--layout room_quick` keeps the three bearings for smoke tests. A room candidate trial uses the seat set; `trial <fp> --mover arm --attest-rig-clear --wait` selects the smoke set. The commissioning stop still applies. The room layer stops at the applied speaker's trusted floor, clamped to room bounds. Use `room` for the document and trial at the same poses.

## Near-field

`nearfield/each` plays each woofer the speaker declares alone, with every other output silent: the woofer, then a cardioid's rear woofer. `--driver woofer:rear` (or `--driver woofer`) narrows it to one. Start it from the measure page, which offers it on a mono speaker, or stage it with `jasper-round run --program nearfield --wait --timeout 3600` and send the link; a staged run lives in the web process until the first placement. Each woofer is taken at 15 and 30 mm ([ADR-0362](adr/0362-the-near-field-rows-take-each-woofer-at-15-and-30-mm.md)). For other distances, or a re-seat, pass a JSON pose list, for example `--poses '[{"azimuth_deg": 0, "elevation_deg": 0, "kind": "close", "distance_m": 0.02, "driver": "woofer:rear"}]'` (metres from the dust-cap centre along the axis; past 0.1 m the take is read gated, [ADR-0366](adr/0366-one-pose-model-a-level-found-at-the-pose-and-a-band-stated-from-it.md)), and check the plan with `--dry-run` first. Turn off the fridge and the air conditioner, and put the capsule on the dust-cap axis with its tip level with a ruler laid across the surround. Each placement opens quiet and is retaken at the level that reads 80 dB at the microphone ([ADR-0361](adr/0361-a-near-field-take-levels-itself-to-80-db-at-the-microphone.md)). The takes are reference evidence that no tuning program reads ([ADR-0360](adr/0360-near-field-driver-takes-are-reference-evidence-one-driver-per-pose.md)); read them with `jasper-round-views nearfield <round>`.

`drivers/each` (woofer, a cardioid's rear woofer, then tweeter; `--driver` narrows it to one) plays each driver alone at the mark, on the speaker round's 150 Hz sweep band, with no CHECK or timing take. The microphone stays put; confirm it once per driver, since each driver finds its own level. The takes are gated reference evidence, read with the same view.

## Cabinet model (optional, laptop-side)

Given Boundary Lab and a solved case of the cabinet from the CAD repo, [`scripts/cabinet-model/`](../scripts/cabinet-model/README.md) turns woofer near-field takes into the pair's response without a room and at the seat, and can fit the rear stage for the seat. The speaker needs nothing extra; the output is a prescription document for the loop above ([ADR-0353](adr/0353-the-cabinet-model-is-an-optional-laptop-aid.md)).

## Evidence and recovery

Keep completed valid takes. Do not pool changed poses, levels, graphs, or calibration. Fix the named fault's action, then continue with the same loop. After an apply timeout, inspect saved state before another write. A losing candidate stays banked.

Each take banks how the playback route's counters moved across its capture in `capture_integrity.playback_path` (fan-in lane xruns and catch-ups, ring waits and drops, outputd empty periods, DAC xruns), and the journal logs one `event=active_speaker.take_playback_path` line per take, at warning level when a fault counter moved.

<!-- BEGIN GENERATED TOOL MENU (scripts/generate-tuning-tool-menu.py -- do not hand-edit) -->
| Tool | Does | Authority | Where |
|---|---|---|---|
| `jasper-basic-profile review` | Review what Save to speaker applies: the current candidate with its tuning layers, or without an applied candidate the saved profile or the declared crossover. Nothing is applied. | advisory (`review` reads) | `jasper/cli/basic_profile.py` |
| `jasper-mic-calibration models\|fetch\|upload\|show` | Register the household's measurement microphone: fetch its vendor calibration by serial or store a file you already have, and remember that mic so every measurement resolves its calibration from one record. A box with no record measures uncalibrated. | advisory (`fetch`/`upload` write; `models`/`show` do not) | `jasper/cli/mic_calibration.py` |
| `jasper-seat-level` | Play the room/bass summed measurement sweep and adjust the fader until the calibrated mic's loudest half-second (loudest_half_second_db_spl) reads the target; bank the session gain. | measured | `jasper/cli/seat_level.py` |
| `jasper-angle-capture serve` | Serve the microphone arm against the daemon's position gate. | mutating (`serve` moves the arm) | `jasper/cli/angle_capture.py` |
| `jasper-crossover-prescriber compose` | Judge and compose prescription documents; serve contracts and report applied layers, last banked rounds and the next program. | advisory (judge, contract and status read; compose banks a candidate) | `jasper/cli/crossover_prescriber.py` |
| `jasper-round run\|trial\|placed\|stop\|status\|wait\|apply\|reset` | List measurement presets, run a plan, bank its packet, list and show banked rounds, and apply candidates. | mutating-with-gates (`run`/`trial`/`placed`/`stop`/`wait`/`apply`/`reset` write; `run`/`trial` may move the arm; `status`/`list`/`show`/`presets` read) | `jasper/cli/round.py` |
| `jasper-round-views catalog` | Read measured round evidence, including off-axis directivity and mark-take repeat spread within and between rounds. Answers use stdout; detailed reports use files. | advisory (analysis views save artifacts) | `jasper/cli/round_views/__init__.py` |
| `jasper-audition start\|stop\|status` | Play this speaker at a reduced DSP layer, then put it back | mutating (runtime only; durable graph untouched -- ADR-0193) | `jasper/cli/audition.py` |
| `jasper-declare-geometry set\|show` | Declare measurement rig geometry: speaker/mic heights, distance and optional ceiling, so entanglement_floor_hz has a provenance-labeled, non-measured source on rigs where the measured reflection finder structurally never fires (issue #3502); and optional cabinet-back and side-wall distances for jasper-round-views room. | advisory (`set` writes; `show` does not) | `jasper/cli/declare_geometry.py` |

| Tool | Answers | Needs | Reads | Programs |
|---|---|---|---|---|
| `jasper-round-views repeat <this-round> <other-round>` | How far apart are each driver's mark takes, within each round and between rounds? | two or more rounds holding one driver's MEASURE takes at 0°/0° (speaker/mark, per_driver) | record | all |
| `jasper-round-views candidates <this-round>` | Where do a round's candidates differ most, at each held pose and window? | one round that played two or more candidates at each held pose | record | all |
| `jasper-round-views directivity <this-round> --set <set-id>` | How do each spec band's level and shape change off axis, against the 0°/0° takes? | one driver's set with 0°/0° takes and off-axis bearings (speaker/mark on baseline_express or baseline_full) | record | speaker |
| `jasper-round-views sweep --scope round <this-round> --set <set-id>` | Does each band's and feature's spread across poses grow with the gate (the room) or hold (the speaker)? | takes at two or more poses of one round; --set narrows them to one set | recording | all |
| `jasper-round-views sweep --scope take <this-round> --set <set-id> --take <take-id>` | How does one take's response change through each gate of the ladder? | one take by its id (jasper-round show lists them); --role picks a driver it recorded | recording | all |
| `jasper-round-views impulse <this-round> --set <set-id> --take <take-id>` | When does a take arrive, how clean is its onset, and how far is its peak above the noise? | one take by its id; --role picks a driver it recorded, or summed | recording | all |
| `jasper-round-views group-delay <this-round> --set <set-id> --take <take-id>` | What are a take's phase, group delay and excess group delay, band by band? | one take by its id; --role picks a driver it recorded, or summed | recording | all |
| `jasper-round-views decay <this-round> --set <set-id> --take <take-id>` | How fast does a take's sound decay in each octave (EDT, T20 and T30)? | one take by its id; an ungated seat take (room/seat) reads the room | recording | all |
| `jasper-round-views compare <round-a> <this-round> --a-take <take-id> --b-take <take-id>` | How does take B differ from take A, or from a forecast, through one window and smoothing? | two takes by their ids, from one round or two; or one take and a judge --preview --out forecast | recording | all |
| `jasper-round-views frequency <this-round>` | What frequency response did each take bank, for one or two rounds, bundles or documents? | one or two banked rounds, session bundles or JSON documents whose takes banked curves | recording | all |
| `jasper-round-views distortion <this-round>` | How much H2 and H3 did each driver make, at the drive each MEASURE capture used? | a banked round's MEASURE captures of each driver (speaker/mark, per_driver) | recording | speaker |
| `jasper-round-views dsp-replay <graph.yml> <stimulus.wav> --main-db <db> --out <render-dir>` | What does a graph play for a stimulus, rendered through the native DSP with no audio device? | a CamillaDSP graph, a PCM16 stimulus WAV and the native DSP binary; no round | laptop | all |
| `jasper-round-views dsp-levels <dsp_replay.json> --raw <output.f64le> --window-s <start> <stop>` | What are a dsp-replay render's digital bass-band levels over one time window? | a dsp-replay render: its dsp_replay.json and the copied output.f64le | laptop | all |
| `jasper-round-views classify-features <this-round>` | Is a feature in the response a driver defect, an interference, or the room? | a banked round's summed verify or lateral captures, with the program WAVs they bind to | recording | speaker |
| `jasper-round-views delay-landscape <this-round>` | Which branch delay sums the two drivers best through the crossover? | a take with both drivers' curves at one pose (speaker/mark) and a crossover corner, applied or --fc-hz | record | speaker |
| `jasper-round-views room <this-round> --set <set-id>` | What is the room's median response at the seats, with its ceiling, lasting features and incumbent? | one set of summed takes at the seat poses (room/seat or rear/seat) | record | room |
| `jasper-round-views room-grade <this-round> --set <set-id>` | Does a room set lose any band against its incumbent from the same run? | a room set and its incumbent set from one run, each with its room view (room/seat with candidates) | record | room |
| `jasper-round-views bass <this-round> --set <set-id>` | What are each bass take's response, quiet-window SNR and H2/H3? | one bass set: summed bass sweeps through a candidate graph (bass/axis) | record | bass |
| `jasper-round-views bass-compare <before-round> <this-round> --before-set <before-set-id> --after-set <set-id> --change <change>` | How did the bass change between two sets, across one candidate, volume, demand or diagnostic change? | two bass sets whose bass views are filed, before and after the change | record | bass |
| `jasper-round-views bass-fit-table <this-round> --candidate <candidate.json>` | How much reach, drive and headroom does each bass candidate have at each level? | bass rounds with their bass views: each candidate's takes beside its baseline's at every level | record | bass |
| `jasper-round-views inventory <this-round> --set <set-id>` | Which analysis artifacts does a round have, and which command makes each missing one? | any banked round or live session bundle | record | all |
| `jasper-round-views nearfield <this-round>` | What does each driver radiate close up, band by band and per distance, and does its step match a piston? | each driver's takes alone: near field at 15 and 30 mm (nearfield/each) or at the mark (drivers/each) | record | reference |
| `jasper-round-views repeat --set <set-id> <this-round>` | How much do one set's repeated takes vary in delay, polarity, ripple and trims? | one set with two or more takes at one 0°/0° pose, each with its banked analysis (speaker/mark) | record | speaker |
| `jasper-round-views speaker-fit <this-round> --set <set-id>` | Which driver filters does the fit propose, and which alignment and trims did the round bank? | one speaker/mark set with each driver's take at the mark (per_driver) | record | speaker |
| `jasper-crossover-prescriber judge --preview <document.json> --round <this-round> --set <set-id>` | What would a prescription document's sections do, predicted from a round without playing? | a document and its round: branches/express for driver or blend, room/seat for room, rear/pair for rear | recording | speaker, rear, room |
| `jasper-crossover-prescriber judge --preview --vary <path=value,value> <document.json> --round <this-round> --set <set-id> --out-dir <dir>` | How does a preview change over a grid of a document's values, without playing? | what judge --preview needs, one --vary axis per parameter, and a directory for the variants | recording | speaker, rear, room |
| `jasper-crossover-prescriber contract --round <this-round> --section <program>` | What may a prescription document write for a program: its schema and bounds, evaluated on a round? | nothing; --round evaluates the bounds on that round | record | speaker, rear, bass, room |
| `jasper-crossover-prescriber status` | Where does tuning stand: applied layers, the last banked rounds and the next program? | nothing; a round directory adds its evidence packet | record | all |
| `jasper-round list --program <program>` | Which rounds are banked, newest first, with their program, result and applied identity? | nothing; --program narrows the list to one program's rounds | record | all |
| `jasper-round show <this-round>` | Which sets and selected takes does a banked round hold, by the ids the views take? | a banked round id from jasper-round list, or a round directory | record | all |
| `jasper-round presets --json` | Which measurement presets exist, what does each play, and what do its layouts cost here? | nothing | record | all |
| `.venv/bin/python scripts/cabinet-model/bem-transfer.py --case <case-dir> --out <transfer.npz>` | How does each woofer's near-field pressure carry to the far field, from a solved cabinet model? | a solved Boundary Lab case of the cabinet from the CAD repo, and each woofer's step from the nearfield view | laptop | reference, rear |
| `.venv/bin/python scripts/cabinet-model/predict.py --transfer <transfer.npz> --nearfield <nearfield_view.json>` | What does the woofer pair make with no room and at a seat before a wall, and do far-field takes agree? | bem-transfer.py's output and a nearfield/each round's nearfield view; --farfield adds a drivers/each one | laptop | reference, rear |
| `.venv/bin/python scripts/cabinet-model/rear-design.py --transfer <transfer.npz> --nearfield <nearfield_view.json> --out <prescription.json>` | Which rear stage gives the smoothest response at the seat, the wall behind the speaker included? | bem-transfer.py's output and a nearfield/each round's nearfield view | laptop | rear |
<!-- END GENERATED TOOL MENU -->

## Debugging — where to look first

Use the `link` from `jasper-round run`, or `crossover_url` from `jasper-crossover-prescriber status`; the page is `/sound/speaker/crossover/` over HTTPS with the speaker's local CA, and banked rounds are at `/sound/measurements/`. First read the structured reason, run `jasper-doctor --json`, inspect `:8780/state`, and fetch logs with `bash scripts/fetch-pi-logs.sh`. Tool details and exit codes live in each tool's `--help`; the [methodology](tuning-methodology.md) and [doctrine](measurement-loop-doctrine.md) own science and authority rules. [rear-calibration-tuning-fields.md](rear-calibration-tuning-fields.md) explains the rear calibration document's fields.
