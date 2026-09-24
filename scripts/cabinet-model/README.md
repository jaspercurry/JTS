# Cabinet model (optional, laptop-side)

Predicts a cardioid speaker's woofer pair with no room, and at the seat with the wall behind the
speaker. It can also fit the rear stage for the seat. It multiplies near-field takes of each woofer
by a Boundary Lab (BEM) solve of the cabinet. Only the capture runs on the speaker.

This is not part of the toolbox's normal loop ([ADR-0353](../../docs/adr/0353-the-cabinet-model-is-an-optional-laptop-aid.md)).
The speaker needs nothing extra. The one output the speaker reads is an ordinary prescription
document, which goes through the same judge → compose → apply gates as any other.

## You need

- **Boundary Lab and a solved case of the cabinet.** Neither is in this repo. Both live in the
  CAD repo (`jaspercurry/sand-cube`, checked out as `CAD - Enclosure`; `$CAD` below):
  `workbench/designs/boundary_lab_mac/README.md` there installs Boundary Lab 0.4.2 (macOS, Julia)
  and builds (`system_mesh.py`) and solves (`study.py`) a case from the enclosure model. The case
  needs sources named `front` and `rear` facing +z and -z, a horizontal polar in
  `observations.json` whose origin sits on the cabinet's front face, and `../source-facts.json`
  with `cabinet.depth`.
- **A calibrated measurement mic on the speaker** (the runbook's entry contract), a ruler, and a
  quiet room. Turn off the fridge and the air conditioner for the low end.
- **The repo venv on the laptop.** Run the tools from the repo root with `.venv/bin/python`, and
  keep every output under `captures/` (gitignored): the recordings are private and large. The
  steps use `D=captures/<speaker>-nearfield-<date>`. `--png` needs matplotlib (the `plots` extra).

## Steps

1. **Capture.** Run `nearfield-plan.py` on the speaker (its `--help` has the commands). It plays
   the raw woofers: the rear stage and the bass boost are cleared for the takes. The mic tip goes
   on the dust-cap axis, level with a ruler across the surround (~15 mm from the cap). Take each
   spacing as its own run: 3–4 front takes and one rear take at `--gap-m 0.0146`, then one of each
   at `--gap-m 0.0296` (for gate 2). The Pi's pair check refuses near-field takes (the far woofer
   is ~30 dB down) and asks again, so one `placed` can give two recordings; each is kept as a
   numbered attempt. Set the fader from one measured take (the mic reads ~33 dB above the 1 m
   level) and keep the peak under the 85 dB SPL stop.

2. **Pull and analyze.** One folder per run (`<run>` is the `wired-*` id that `jasper-round run`
   printed). Read the SNR per band (trust a band above ~20 dB) and the take spread.

   ```bash
   mkdir -p $D/nf_front && ssh pi@<speaker> "sudo sh -c 'cd \$(dirname \$(dirname \$(ls -d /var/lib/jasper/active_speaker/sessions/*/crossover_v2/<run>))) && tar -cf - summed crossover_v2/<run> evidence/v1/artifacts/crossover_v2/<run>'" | tar -x -C $D/nf_front
   .venv/bin/python scripts/cabinet-model/nearfield-analyze.py $D/nf_front $D/nf_rear --out $D/nearfield.npz
   .venv/bin/python scripts/cabinet-model/nearfield-analyze.py $D/nf_front30 $D/nf_rear30 --out $D/nf30.npz --compare $D/nearfield.npz
   ```

3. **Transfer.** Integrate the solved case to the mic spots on each woofer's axis. Gate 1 (the
   integral reproduces the solver's own probes) must pass. Gate 2 checks the model's level step
   against the `--measured-step` values that step 2's `--compare` printed.

   ```bash
   .venv/bin/python scripts/cabinet-model/bem-transfer.py --case "$CAD/build/workbench/boundary_lab_mac/<case>" \
       --out $D/transfer.npz --measured-step front=-2.37 --measured-step rear=-2.27
   ```

4. **Predict.** Compare the live graph with any rear-calibration or prescription document:

   ```bash
   graph=$(curl -s http://<speaker>:8780/state | python3 -c 'import json,sys; print(json.load(sys.stdin)["audio"]["camilla_active_config_path"])')
   ssh pi@<speaker> "sudo cat $graph" > $D/live.yml
   .venv/bin/python scripts/cabinet-model/predict.py --transfer $D/transfer.npz --nearfield $D/nearfield.npz \
       --dsp $D/live.yml --png $D/live.png --xmax-mm 14.7
   ```

5. **Design the rear stage (optional).** Measure the wall gap first; the fit depends on it. The
   document keeps the front chain at 0 dB and writes any branch gain above unity as the same flat
   boost on both rear branches (ADR-0327); the headroom charge pays for it. Then
   judge, compose and apply the document with [the runbook's loop](../../docs/tuning-operator-runbook.md#the-loop),
   and keep the old fingerprint to go back.

   ```bash
   .venv/bin/python scripts/cabinet-model/rear-design.py --transfer $D/transfer.npz --nearfield $D/nearfield.npz \
       --live $D/live.yml --out $D/prescription.json --wall-gap-m 0.2
   ```

## Last run: jts3, 2026-09-23

Levels are predict.py's: per unit front drive, 0 dB = the front woofer alone, on axis, 400–600 Hz.

- Data (on the owner's laptop, gitignored): `captures/jts3-nearfield-2026-09-23/`. Folders
  `nf_front_run` (attempts 1–4 at 15 mm, 5 at 30 mm, fader −32 dB) and `nf_rear26_run` (1 at
  15 mm, 2 at 30 mm, fader −26 dB); these runs mixed spacings, hence the attempt ranges
  (`nf_front_run:1-4`). CAD case `system-24mm-compound`, run `full-q4` (its 40–1000 Hz points).
- Results: front fc 84 Hz, Qtc 1.02; rear fc 89 Hz, Qtc 1.09. The rear woofer plays 6–7 dB below
  the front at the same drive (cause not found yet). Gate 1 2.8e-4. Gate 2: model −2.29 / −2.42 dB,
  measured −2.37 / −2.27 dB.
- The old tune (candidate `0a03d90d…`, graph `3a5840073709`): front minus behind peaks +14.1 dB
  at 200 Hz; seat −11.9 dB at 100 Hz and −7.0 dB at 500 Hz; seat roughness 5.1 dB.
- `rear-design.py` fit: bass low-pass 148 Hz; cancellation band-pass 41–751 Hz; both rear branches
  +3 dB; headroom charge 8.5 dB. Seat (0.2 m wall gap): 0.0 dB at 100 Hz, −0.9 dB at 500 Hz,
  roughness 1.3 dB. It sends more bass backward for the wall to return, so the free-field front
  response dips at 60–100 Hz. With a 0.1 / 0.3 m gap the roughness is 2.3 / 3.1 dB.
- Trial on jts3: candidate `5bda730c…`, written in the old form (front chain −3 dB, which lowered
  the woofers 2.5 dB against the tweeter). The old stage's 54/59 Hz room notches were copied by
  hand into every chain (common EQ); a room round should replace them.
- Loudest level before 14.7 mm cone travel, 1 m, free field: 85 dB at 25 Hz, 88 dB at 30 Hz.

## Limits

- Trust 30–600 Hz. Below ~30 Hz room noise takes over; above ~600 Hz the measured 15 → 30 mm
  step stops being flat, so the near-field reading is position-sensitive there.
- Only the rear stage's rear/front ratio is modelled. Its front chain, the woofer chain, room cuts
  and the bass boost are not in the prediction.
- One image source stands in for the wall. There are no room modes, and both amplifier channels
  are assumed to have the same latency.
- `nearfield-analyze.py` models the played path with the repo's graph walker and a plain Loudness
  bass boost; it stops on a shaped boost, so `nearfield-plan.py` clears the bass layer.
- The model has not been checked against a gated far-field measurement yet.

Related: #5684 (near-field takes in the web flow), #5692 (shaped bass boost).
