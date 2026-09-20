# Runbook: measured rear-null search on jts3 (informal two-mic rig, #5405)

You babysit a measuring loop on the speaker `jts3` (`ssh pi@192.168.1.92`). Owner-approved, sound is allowed all night.
Do the work yourself. Do NOT spawn sub-agents. Keep every command output short (pipe through `tail`/`cut`/python).

## Hard rules (never break)
- MEASURE ONLY. Never run `jasper-round apply`, never deploy, never edit files under `/opt/jasper` or `/var/lib/jasper`,
  never change volume, `volume_limit`, or the SPL stop. The applied tune must stay
  `a81d6c56dff1da3635d8e6f9ce11c507d43ab89a28db447319e497a16e2ac4b4` (the runner checks it, exit 14).
- Before EVERY round, in its OWN ssh call, the busy gate must print nothing:
  `ssh pi@192.168.1.92 'ps -eo args | grep -E "[a]ngle-capture serve|[j]asper-round (run|trial|wait)|[j]asper-seat-level|[i]nstall.sh|[a]record "' < /dev/null`
- While a round runs: NO other ssh to the Pi except the one wait loop below (extra load causes capture overruns).
- Always add `< /dev/null` to ssh commands. Start background things on the Pi with `ssh -f ... 'setsid nohup ... > /dev/null 2>&1 < /dev/null &'`.
- STOP and report to the conductor when: runner exit/ABORT (codes 10 busy, 11 muted, 12 turntable dead, 13 run refused, 14 tune changed),
  a round result other than `complete` twice in a row (one retry allowed; `spl_ceiling_exceeded` = a loud noise in the room),
  3 failed rounds in total, or anything you do not understand. Never "fix" the product.

## Paths
- Laptop scratch: `SP=/private/tmp/claude-501/-Users-jaspercurry-Code-JTS--claude-worktrees-speaker-tuning-llm-arch-bb1ff5/f447b743-f8a9-4f10-bce1-5ed46c07c2ff/scratchpad/twomic`
- Python: `cd $SP && PYTHONPATH=/Users/jaspercurry/Code/JTS/.claude/worktrees/deploy-jts3-cardioid /Users/jaspercurry/Code/JTS/.venv/bin/python <script>` (cwd must be `$SP`).
- Base document: `$SP/docs/doc-N1.json` (tune N1, fp `5e9afae3757189d66fcda74450fe1ab4ffe6798d59edfa999d6f31227efeb416`).
  Rear-muted zero "Nm": fp `0caaa048e3a0f7f94583bfe1ddb8152f69886c29170ed3c21fb4b9a359dcf5c8` (must be in EVERY round).
- Pi work dir: `/home/pi/ab-5405/` (documents) and `/home/pi/ab-5405/twomic/` (runner `run_round.sh`, results in `rounds/<label>/`).

## One loop step
1. MAKE DOCUMENTS (laptop): copy doc-N1.json, change only what the step says
   (`sections.rear_calibration.rear.cancellation.delay_ms`, `.gain_db` (must be <= 0), or a filter parameter), set a real `rationale` sentence,
   save as `$SP/search/<label>/doc-<tag>.json`. Cancellation `delay_ms` may go from -1.06 to +3.0 (common_delay_ms 1.06 stays).
2. COMPOSE (silent): `scp` the docs to `/home/pi/ab-5405/`, then for each:
   `sudo /opt/jasper/.venv/bin/jasper-crossover-prescriber compose --base saved <doc>` -> JSON with `candidate_fingerprint`, `ok`. A refusal = skip that candidate and note why.
3. GATE (own ssh call, see above), then START:
   `ssh -f pi@192.168.1.92 'cd /home/pi/ab-5405/twomic && REPEATS=1 setsid nohup ./run_round.sh <label> <fp1,fp2,...> <poses> > /dev/null 2>&1 < /dev/null &'`
   poses: `0` = one summed pose, arm at 0 deg (side mic straight behind, 180 deg) - use this for searching; `rear/express` = arm 0/+20/-20 - use this to confirm.
   Max 10 candidates with pose `0`; max 4 candidates with `rear/express`.
4. WAIT (one ssh, run it as a background Bash task with a long timeout):
   `ssh -o ServerAliveInterval=30 pi@192.168.1.92 'L=/home/pi/ab-5405/twomic/rounds/<label>/runner.log; for i in $(seq 1 180); do grep -qE "== done|ABORT" $L 2>/dev/null && break; sleep 10; done; tail -8 $L' < /dev/null`
   The log names the banked round dir `/var/lib/jasper/active_speaker/campaigns/<ROUND>` and must say `"result": "complete"` and `applied after round: a81d6c56...`.
5. COPY: `D=$SP/round-<ROUND>; mkdir -p $D/side; ssh pi@192.168.1.92 'sudo tar -C /var/lib/jasper/active_speaker/campaigns -cf - <ROUND>' < /dev/null | tar -C $D --strip-components=1 -xf -; ssh pi@192.168.1.92 'tar -C /home/pi/ab-5405/twomic/rounds/<label> -cf - .' < /dev/null | tar -C $D/side -xf -`
6. ANALYSE: `twomic_analyse.py --round-dir $D --journal $D/side/journal-takes.txt --side-wav $D/side/side-dayton.wav --side-start-epoch $(cat $D/side/side-start-epoch.txt) --side-cal $SP/dayton-CMM31555.txt --main-cal $SP/calibration_mics/minidsp/minidsp_umik2/minidsp-minidsp_umik2-b7343c0c625b.txt --muted 0caaa048 --out $SP/search/<label>/result.json`
   Read `by_pose[mic][pose].candidates[fp]`: mic `side` = BEHIND the box (the score), mic `main` = IN FRONT.
   SCORE = side mic `score_100_350_db` (ungated level change vs rear-muted, 100-350 Hz; more negative = better). Also read the per-band ungated change behind
   (bands near 89, 111, 143, 178, 224, 283, 356 Hz) and the front `score_100_350_db`. Ignore `win_score_db` at T=6/12 behind (window marker is not trustworthy there); T=25 is a side figure only.
   A side take that the tool refuses or that has a wild value: note it, do not use it.
7. LOG one block per round in `$SP/search/LOG.md`: label, round id, candidates (tag, what changed, fp), table (tag | behind score | behind per band | front score), retakes, anything odd, your decision for the next step.

## Search plan (coordinate search, shrinking steps). N1 measured so far (round 649a313770cb, 3 angles): behind -2.4/-1.8/-2.1 dB, per band at 180 deg: 89 Hz -6.3, 111 Hz -3.6, 143 Hz +1.1, 178 Hz +0.9; front +1.2. A null at 90-110 Hz that fades above 140 Hz looks like a DELAY error of about 1 ms (sign unknown).
- D1 (pose `0`): delay sweep, gain 0: cancellation delay_ms = -1.0, -0.7, -0.4, -0.15, +0.1 (= N1 itself, use its fp), +0.35, +0.6, +0.9, +1.2, plus Nm. 10 takes.
- D2 (pose `0`): around the best delay d*: d* -0.2, -0.1, +0.1, +0.2 at gain 0; and at d*: gain -1.5, -3, -5; plus d* itself again (repeatability) and Nm. If the best of D1 is at an end of the range, extend the range first instead.
- D3 (pose `0`): at the best (delay, gain): low-pass corner of the cancellation branch (the `ButterworthLowpass` filter, freq 300) = 250, 350, 400, 500 Hz. NOTE the low-pass adds group delay about 225/freq ms at low frequencies (0.75 ms at 300 Hz); when you move the corner, change delay_ms by the difference so the low-frequency timing stays the same (e.g. 400 Hz: +0.19 ms later -> delay_ms + 0.19). Plus best-so-far again and Nm.
- D4 (pose `0`): finer steps (delay +-0.05, gain +-1) around the best, only if D2/D3 gained >= 0.5 dB over their start; stop the search when a round gains < 0.5 dB.
- CONFIRM (poses `rear/express`): Nm, N1, best, second best. Report behind score at all 3 rear angles and front scores.
- Front guard for every winner: front `score_100_350_db` must stay >= -2 dB, else note it and prefer the next one.
- Repeatability: the same tune measured twice should agree within ~0.5 dB; if not, note the noise and re-measure the top two before deciding.

## Final report to the conductor (<= 35 lines)
The LOG path, the landscape tables (delay, gain, corner), the best tune (tag, fp, document path, what changed vs N1), its behind score at 3 angles + per band at 180 deg, front score, how repeatable, faults met, and what you would try next. State plainly anything you could not verify.
