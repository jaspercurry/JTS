# JTS3 speaker setup cleanup — 2026-09-22

The simplified commissioning flow was verified on JTS3 at `3d9b19404`.
The final UI build, `c3140f0ea`, was deployed through the normal installer and
verified in the browser after restoring the original tune.
The browser smoke reached a working base configuration and the driver
linearization entry. No acoustic measurement was started. The original tune,
speaker settings, and listening volume were restored after testing.

## User flow

The final run started with Reset speaker setup in the browser and used:

1. Active crossover, three amplifier channels, front/rear cardioid arrangement,
   one mono speaker, outputs 1/2/3.
2. Dayton Audio Epique E150HE-44 front and rear; B&C DE250-8 tweeter;
   sealed woofer enclosures; compression driver; no resistor or L-pad.
3. Save details, Copy prompt, paste the researched JSON, Load values.
4. Save to speaker, copy the linearization prompt, open tuning.

The resulting base used a 2 kHz LR4 crossover, 0 dB woofer trim and an estimated
−25.2 dB tweeter trim. The rear output remained muted pending cardioid tuning.
The base contained no driver linearization. It preserved the 0 dB DSP ceiling
and branch limiters.

The page showed the next form after each save, exposed advanced values under
details, and immediately offered the four applicable tuning programs after
apply. The menu survived a reload. It did not call the unmeasured base measured
or label optional corrections applied. Normal setup and measurement-entry copy
contained no config path, filename, or candidate identifier. The default
measurement plan was selected, with other plans inside Measurement options.

High-frequency driver type is part of the tweeter details, beside its model
and horn information. The DAC output selectors contain only channel assignment.
This placement was checked on the final deployed build.

Copy buttons reported success and produced the expected prompt text in the
page. The browser tool did not independently verify the operating-system
clipboard. The pasted research was the manufacturer-sourced reply from the
preceding commissioning audit, reused with the same physical targets.

## Faults found and fixed during the run

- Reset/layout saves called a synchronous topology writer inside an existing
  event loop. Setup operations now remain synchronous; only the DSP apply is
  dispatched through its asynchronous boundary.
- Empty conditional DOM children appeared as literal `false` text. The page
  now passes only actual children to the native DOM replacement operation.
- A base preview appeared before driver research existed. It now appears only
  when starting values are ready or a setup is active.
- A declared legacy protection floor could be shadowed by an inherited
  researched recommendation. Explicit declared protection retains authority.
- The DSP graph could apply successfully while outputd retained the stereo
  route left by reset. Base apply now invokes the existing topology consumers
  after DSP apply and before restoring normal source selection. A route failure
  returns `needs_attention` instead of a successful save result.

The last fault was verified from the actual hardware, not only the saved graph.
Before the fix, outputd read the two-channel content ring and reported
`content.deaf=true`. After a fresh browser reset and commissioning on the final
build, it read the three-channel active ring, with a live writer, increasing
frame count, `content.deaf=false`, and zero clipped samples.

## Restoration and health

Before mutation, a fresh backup saved all 18 speaker/settings JSON records and
the CamillaDSP state/config directory. Archive SHA-256:
`914e4f6c8688ae720f8e0795d33f787f373f3128bb49bb5952853ada6ffcf88e`.

The original graph was restored through `CamillaController`. Its SHA-256 is
`48676134ca08a0d7b953a8f80d07f3b334eda0c527a9d283f7705e30b387ecaa`.
All 18 saved JSON records were restored; the volume coordinator refreshed only
the volume timestamp during the transition, then the original volume record
was restored after settling. Seven key file hashes were checked against the
backup, including topology, draft, applied profile, sound settings, volume, and
the original graph. All matched. Listening volume was 70% (−15.151515 dB),
unmuted. The test-only authored candidate was moved out of the live campaign
bank; original measurement history was retained.

The restored speaker had a live three-channel audio route and zero clipping.
Doctor passed the audio checks, including the live 0 dB ceiling. Its only
warning was the pre-existing, unrelated missing Google Routes configuration.
The restored UI showed the original driver, cardioid, and bass corrections.

## Separate installer finding

Twice, an installer stop request for fan-in collided with a restart requested
by the coupling reconciler. At 02:42:27 UTC, the restart broker logged that
request during `park_audio_clients_for_core_graph_restart`; fan-in stopped
normally and restarted immediately, while install exited 1. The build manifest
correctly stayed at the prior build. Retrying the normal deployment after the
competing reconcile finished succeeded both times. No deploy guard was bypassed.

This is an installer ownership race, separate from commissioning, tracked in
[audit issue #5470](https://github.com/jaspercurry/JTS/issues/5470).

## Automated validation

The final clean-checkout fast lane at `14c643b68` completed with
`==> test-fast: 10476 passed` (10,391 selected tests and 85 routing-policy tests).
The final setup service type check and documentation map/link checks passed.

The focused checks passed: 20 setup tests including real HTTP requests, 113
commissioning/seat-level tests, eight output-route checks, and 46 UI/round-wizard
tests. The JavaScript setup tests, Ruff, and diff whitespace checks passed.
The final diff received a local review, including the repository's adversarial
checklist for the driver-protection change.

The full merge lane is not green. A run on an earlier revision was stopped at
94% after 26,539 passes. Three UI-fixture/ADR-index failures were fixed and their
checks passed afterward. Two unrelated USB-advisory tests timed out at 30 seconds;
both also failed on unmodified `origin/main` (`d3cd00a1b`). The full lane was not
rerun after the final fixture changes, and this branch was not merged.

## Evidence

Local artifacts are under `logs/jts3-setup-cleanup-20260922/`: before/after
archives, prompt, browser snapshots, runtime snapshots, restored hashes, and
doctor output. The original research is retained with the preceding
[commissioning audit](2026-09-21-jts3-commissioning-smoke.md).
