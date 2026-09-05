# ADR-0235: Attached hardware has one owner per fact, and the shell holds no hardware facts

- **Date:** 2026-09-05
- **Status:** Proposed
- Refs: #4027. Builds on ADR-0232, ADR-0233, ADR-0234. Surveyed at 27c4d2119;
  citations re-verified after a rebase to 5c033072d (no change to any file
  this ADR cites).

## Context

The owner's target: any audio hardware attached to the Pi that JTS has a
config for just works. Detectable hardware (USB DACs, USB mics, I2S HATs
with an ID EEPROM) is used on appear, released on disappear. Undetectable
hardware gets one toggle. Every device is one declarative registry row, and
contracts are explicit at each boundary: registry -> classifier ->
reconciler -> doctor and /state -> wizard. Failures are observable
(`event=` logs, /state, doctor). An external maintainer adds a device by
adding one row.

Eight read-only surveys covered the DAC registry, the output reconciler and
its env files, boot-config ownership, the microphone path end to end,
doctor and /state, the wizards, the tests, and open issues/PRs/docs.

### What already matches the target (leave alone)

- The DAC registry is declarative. Adding an undetectable I2S profile
  touches only `jasper/audio_hardware/dac.py` (one `DacProfile` row);
  `selectable_i2s_hat_profiles` (`jasper/audio_hardware/usb_port_role.py:346`),
  the wizard payload (`jasper/web/sound_setup.py:356`) and the JS select
  (`deploy/assets/sound-profile/js/main.js:594`) are generic over it.
- One reader per fact holds for mic presence, output hardware, and
  transport park: /state and the doctor call the same function
  (`jasper/control/state_aggregate.py:1232,1264,1373`;
  `jasper/cli/doctor/_evidence.py:265,275`, `audio_runtime_ring.py:237-239`).
- The doctor framework is one decorator, one `CheckResult` contract
  (`jasper/doctor_contract.py:38-64`), pinned order keys.
- The wizard writes intent only for undetectable hardware
  (`/var/lib/jasper/i2s_hat.env` via `write_i2s_hat_intent`,
  `usb_port_role.py:390`) and carries no save/restore/undo machinery.
- The output reconciler has no dead functions and every `JASPER_*` key it
  writes has a reader in `rust/` or `jasper/`.
- The measurement-mic registry (`jasper/audio_measurement/mic_identity.py:49`)
  is one owner; voice selection excludes those ids
  (`deploy/bin/jasper-aec-reconcile:1225`) and measurement selection requires
  them (`jasper/audio_measurement/wired_capture.py:129`). The two agree by
  construction, not by contract; this ADR makes it the contract (R5).

### Gaps at HEAD

Output side:
- G1. Decorative registry fields. `DacProfile.outputd_sink` is required and
  validated (`dac.py:271-272`) but never read; the reconciler emits
  `JASPER_OUTPUTD_SINK` from literals
  (`deploy/bin/jasper-audio-hardware-reconcile:1863,1950,1994`), disagreeing
  with the registry's `alsa` (reconciler emits `single_alsa`).
  `requires_same_usb_bus` (`dac.py:176`) is consumed only by its own
  validator (`dac.py:293`); the classifier hardcodes the check
  (`jasper/output_hardware.py:437-445,528`). `udev_rule` (`dac.py:185`) is
  read by one test; the installer hardcodes the path
  (`deploy/lib/install/systemd-units.sh:756-757`). `known_profile_ids`
  (`dac.py:903`) and `supports_physical_output_count` (`dac.py:1017`) have
  no production caller.
- G2. Hardware facts in shell. The Apple dongle label `usb-c to 3.5mm`
  lives in `dac.py:414,769`,
  `deploy/bin/jasper-audio-hardware-reconcile:889`, and
  `deploy/lib/jasper-apple-dongle.sh:10`. The composite channel width is a
  bash literal, `jasper-audio-hardware-reconcile:1878`.
- G3. Shell regex-parses `output_hardware.json`. `sed` extractions at
  `jasper-audio-hardware-reconcile:379-380,398-400,433-437,443,466-468,491`
  plus a separate `python -c json.load` at `:406-417`. A renamed key returns
  empty instead of failing.
- G4. Swallowed events. `event=hardware.boot_config_changed` is printed to
  stdout by `usb_port_role.py:944-947`; the udev path captures stdout
  (`reconcile_i2s_hat_boot`, `jasper-audio-hardware-reconcile:452-480`) and
  never re-emits it. `event=hardware.usb_role_resolved` is computed once in
  Python (`usb_port_role.py:934-942`) and again by the bash caller's own
  field extraction. A silently re-written managed I2S block has no event of
  its own: only the port-role `changed` flag gets an `event=` line
  (`usb_port_role.py:943-957`), never the `i2s_hat_boot_config_changed`
  flag.
- G5. Write-only marker. `/run/jasper-output-hardware/reconcile.degraded`
  (`jasper-audio-hardware-reconcile:68,1855`) is read only by
  `write_reconcile_stamp` (`:164`). No doctor check or /state field sees a
  box that skips DAC-format and latency-floor writes pass after pass.
- G6. Orphaned boot-config block. When an EEPROM HAT is removed, `manage_hat`
  (`usb_port_role.py:819`) goes false; with no intent file either, nothing is
  touched and the managed `dtoverlay` block persists (deliberate:
  `usb_port_role.py:805-809`). On a shared-OTG board the stale overlay keeps
  the USB port in peripheral mode (`usb_port_role.py:422`). Nothing
  discloses it.
- G7. God files. `usb_port_role.py` (961 lines) carries USB port-role
  resolution, I2S HAT boot-config ownership (`:324-410,666-771`), and
  config.txt parse/render primitives (`:257-321,596-625`); the docstring
  names only the first. `jasper/cli/doctor/audio.py` (1857 lines) holds 637
  lines of active-speaker graph checks with no ALSA I/O (`:1220-1857`).
  `jasper/output_hardware.py` (1352 lines) bundles schema, classification,
  ALSA probing, mixer math, and topology cross-check.
- G8. Dual writer, no lock. `/var/lib/jasper/outputd.env` is written by the
  reconciler (`jasper-audio-hardware-reconcile:600-614,819-851`) and by
  `jasper/fanin/coupling_reconcile.py:26-29`, chained back to back by
  `kick_fanin_coupling_auto_if_needed` (`:1756`). Disjoint keys by
  convention; no incident on record.

Input side:
- G9. No general mic registry, and four partial ones: `FIRMWARE_VARIANTS`,
  `CHIP_BEAM_PLANS`, `FIRMWARE_UPDATE_TARGETS` in
  `jasper/mics/xvf3800.py:421,347,518` (one family, data fused with chip
  command sequences `:287-323` and procfs probing `:615-633`);
  `SUPPORTED_MODELS` in `mic_identity.py:49`; adapter mics in
  `jasper/accessories/reconcile.py:185`.
- G10. XVF facts duplicated in bash. Card names: `xvf3800.py:43-49` vs
  `deploy/bin/jasper-aec-reconcile:80`. Mixer control names and max:
  `xvf3800.py:578-580` vs `jasper-aec-reconcile:1317-1320`. "6 channels
  means AEC-capable": `xvf3800.py:205` vs `jasper-aec-reconcile:1284`.
  `stream0` capture parsing: `xvf3800.py:615-633` vs
  `jasper-aec-reconcile:1259-1276`. `udp:9876` compared as a string in
  `jasper/voice/input_policy.py:109`.
- G11. `deploy/bin/jasper-aec-reconcile` (2307 lines) is the input side's
  classifier consumer, reconciler, policy engine, and service lifecycle
  owner. It also holds the output-DAC chip-AEC gate (`:721-900`) and writes
  outputd's `JASPER_OUTPUTD_CHIP_REF_*` keys then restarts outputd
  (`:1000-1127`, `:1985`). Its main flow has ten exit points (`:2045-2307`).
- G12. Silent deafness on unplug. Mic removal writes the absence marker
  (`jasper-aec-reconcile:2021`) and stops voice (`:2025`) with no cue. The
  only mic cue, `no_room_microphone` (`jasper/cues/registry.py:144`), fires
  only from the manual-turn refusal (`jasper/voice_daemon.py:4620`). Ready-
  marker publish/revoke (`:187-204`) and the absence marker's success path
  have no `event=` line.
- G13. Possible reboot on unplug. The bridge stalls after 5 s
  (`jasper/cli/aec_bridge.py:664-668`), exits 1, restarts every 2 s under
  `StartLimitIntervalSec=300`, `StartLimitBurst=4`,
  `StartLimitAction=reboot` (`deploy/systemd/jasper-aec-bridge.service:56-58`),
  gated only by the reconciler's ready marker (`:50`). Unproven on hardware.
- G14. Two udev reconcilers on one event, no ordering:
  `deploy/udev/99-jasper-aec-reconcile.rules:12` and
  `deploy/udev/99-jasper-audio-hardware-reconcile.rules:14` both match
  `controlC*`; the output reconciler also kicks the input one
  (`jasper-audio-hardware-reconcile:1690`).
- G15. Stale prose: `jasper/mic_presence.py:57` names `jasper/mics/base.py`,
  which does not exist; `deploy/systemd/jts-mic.slice:3-10` narrates a
  dated stress test; `deploy/systemd/jasper-aec-bridge.service:12-17` is a
  historical note; `jasper/mics/README.md:22-28` contradicts the module's
  own registries; `deploy/udev/99-jasper-aec-reconcile.rules:8-9`
  contradicts `jasper-aec-reconcile:80`.

Docs and tests:
- G16. `docs/PROPOSAL-dac-profile-registry.md` is a 2026-08-04 handoff-style
  doc whose composite claim is stale against `output_hardware.py:526-573`;
  AGENTS.md retired that tier. `docs/audio-paths.md:771` footer date
  (2026-08-26) predates its last commit (2026-09-04).
- G17. `profile_for_hat` (`dac.py:997`) has no direct test. Nineteen
  `pytest.raises(match=...)` prose assertions in `tests/test_dac_profiles.py`
  exist because `DacProfile.__post_init__` raises bare `ValueError`. The
  `<unreadable>` root-sandbox test artifact in
  `tests/test_audio_hardware_reconcile.py` was re-explained in five of the
  last seven PRs and has no fix.

## Decision

### Target shape (both sides)

| Stage | Owner | Contract |
|---|---|---|
| Registry | `jasper/audio_hardware/dac.py`; for mics, `jasper/mics/<family>.py` | Pure data plus validation and lookups. Every field drives a runtime decision or is deleted. One row per device; detection is card label, EEPROM product, or intent toggle. |
| Classifier | `jasper/output_hardware.py` -> `output_hardware.json`; `jasper/cli/xvf_profile.py` -> `/run/jasper-mic-profile/xvf3800.json` | One writer, one schema. The only JSON parser is the dataclass `from_mapping`. Shell consumers get `KEY=value` lines from a Python emitter and `eval` them. |
| Reconciler | `deploy/bin/jasper-audio-hardware-reconcile`; `deploy/bin/jasper-aec-reconcile` | Mechanical applier. Holds no hardware literal (label, channel count, sink kind, mixer name). Every transition it makes is one `event=` line on stderr. Every marker it writes has a reader. |
| Doctor and /state | `jasper/cli/doctor/*`, `jasper/control/state_aggregate.py` | One reader per fact (ADR-0233). A reconciler-owned marker with no reader is deleted or given one. |
| Wizard | `jasper/web/sound_setup.py` hardware slice, `jasper/web/wake_setup.py` | Displays facts; writes intent only for undetectable hardware; re-derives nothing except labeled read-only previews. |

Boot config is three modules: `config_txt` primitives (section/overlay
parsing, atomic write), `i2s_hat` ownership (intent file, profiles, managed
block, collision), and `usb_port_role` (dwc2 resolver plus
`reconcile_boot_config` and the CLI). JTS touches only its own
sentinel-delimited blocks.

### Rulings

- R1. Registry fields are load-bearing or deleted. `outputd_sink` becomes
  the source of `JASPER_OUTPUTD_SINK` (values renamed to what outputd
  parses); the classifier consults `requires_same_usb_bus`; `udev_rule`,
  `known_profile_ids`, `supports_physical_output_count` are deleted.
- R2. The shell never parses JSON and never holds a hardware literal.
  Python emits env lines; bash evals. Applies to both reconcilers and to
  `deploy/lib/jasper-apple-dongle.sh`.
- R3. A managed boot-config block persists when its EEPROM HAT disappears,
  and the doctor discloses it. Auto-removal is refused: a transient EEPROM
  read failure would cost a reboot cycle either way, and the stale line
  only harms the USB port-role decision on shared-OTG boards. Removal
  condition: an observed EEPROM flap that proves the read is stable.
- R4. Reconciler events go to stderr. Stdout is the payload the shell
  parses; stderr reaches the journal on every invocation path. The bash
  re-derivation of `usb_role_resolved` is deleted.
- R5. No general microphone registry until a second voice-mic family
  exists (the package's own warning, `jasper/mics/README.md`, stands).
  Until then `jasper-xvf-profile --env` is the input side's registry
  emitter and bash holds no XVF literal. The measurement-mic registry
  stays separate (different purpose, stdlib-only). Contract, now written
  down: voice selection excludes measurement-mic USB ids; measurement
  selection requires them.
- R6. Microphone loss parks voice with a cue and never reboots the box. The
  reconciler plays `no_room_microphone` when it writes the absence marker.
  The bridge exits with a park status (the voice daemon's exit-66 pattern,
  `deploy/systemd/jasper-voice.service:219-220`) when its capture card is
  gone, so `StartLimitAction=reboot` cannot fire on unplug. H1 verifies the
  race before the bridge change lands.
- R7. The `outputd.env` dual-writer race (G8) and the two independent "DAC
  present" gates (reconciler park vs `jasper-outputd.service`
  ExecCondition) are disclosed, not guarded. No incident exists. Removal
  condition: an observed clobber or divergence.

The mic reconciler stays bash for this campaign. Decisions migrate into
Python CLIs one at a time via the existing pattern
(`jasper/cli/audio_input_profile.py`, `jasper/cli/xvf_profile.py`); bash
keeps only the apply. The one sanctioned cross-lane write stays: the mic
reconciler owns `JASPER_OUTPUTD_CHIP_REF_*` and restarts outputd, because
the chip-AEC reference is a joint fact.

### PRs, in order (each single-concern, under 400 lines, `/code-review` medium, `/simplify` before push)

| # | PR | Lane | Files | Model | Notes |
|---|---|---|---|---|---|
| 0 | Delete `docs/PROPOSAL-dac-profile-registry.md`; fix `docs/audio-paths.md` footer; add the add-a-row path to the DAC section of audio-paths.md; README points there | docs | 3 | Sonnet | G16 |
| 1 | Registry fields load-bearing (R1) plus one `profile_for_hat` pin | A | dac.py, `__init__.py`, output_hardware.py, reconciler (2 literals), tests | Sonnet | G1, G17 |
| 2 | Reconciler consumes one Python env emission of `output_hardware.json`; delete the `sed` and `json.load` parsers and the `usb-c to 3.5mm` literal; `<unreadable>` skipif | B1 | output_hardware.py, reconciler, its tests | Opus | G2, G3, G17. After PR 1. |
| 3 | `jasper-apple-dongle.sh` derives Apple cards from `output_hardware.json`; delete its regex | B1 | `deploy/lib/jasper-apple-dongle.sh`, `deploy/bin/jasper-headphone-monitor`, `jasper-dac-init` if it still sources it, tests | Sonnet | G2. Touches the Headphone pin path: adversarial review. |
| 4 | Split `usb_port_role.py` into `config_txt.py`, `i2s_hat.py`, `usb_port_role.py` (pure move); fold the duplicated `DEFAULT_UDC_CLASS_DIR` with `jasper/usbgadget.py:19` | B2 | 3 modules, callers, tests split to match | Sonnet | G7. After #4043 merges (it edits `doctor/boot_config.py`). |
| 5 | Boot-config events on stderr; add `i2s_hat_boot_config_changed`; delete bash `usb_role_resolved` re-derivation | B2 | usb_port_role.py, reconciler, tests | Sonnet | G4, R4 |
| 6 | `reconcile.degraded` gets one reader in `output_hardware.py`; doctor `check_output_hardware_state` warns; R3 disclosure check for a managed I2S block whose HAT is absent | D | output_hardware.py, doctor/audio.py, boot_config.py, tests | Sonnet | G5, G6. After #4043. |
| 7 | `jasper-xvf-profile --env` emits candidate card names, mixer control names and max, capture channels; reconciler deletes its literals and `mic_channels` parser | C | xvf_profile.py, xvf3800.py, `jasper-aec-reconcile`, udev comment, tests | Opus | G10, R5 |
| 8 | Input-side `event=` lines on ready-marker publish/revoke, absence marker mark/clear, candidate selection, mixer repair failure | C | `jasper-aec-reconcile`, tests | Sonnet | G12. After PR 7. |
| 9a | Delete stale input-side prose (G15) | C | 5 files | Sonnet | |
| 9b | `input_policy.py` compares against the configured AEC port, not `udp:9876` | C | input_policy.py, test | Sonnet | G10 |
| 10 | Mic loss cue and bridge park exit (R6) | C | `jasper-aec-reconcile`, aec_bridge.py, bridge unit, tests | Opus | G12, G13. Needs H1 and H2 first. |
| 11 | Move the nine active-speaker checks out of `doctor/audio.py` into `doctor/active_speaker.py`; `boot_config.py` reads topology through the evidence memo | D | doctor modules, roster, tests | Sonnet | G7. After #4043. |

Parallel lanes: output {0, 1, 2, 3, 5}; input {9a, 9b, 7, 8}; doctor after
#4043 {4, 6, 11}. PR 10 waits for hardware.

### Separate decisions (named, not folded into a PR)

- D1. Python-izing or splitting `deploy/bin/jasper-aec-reconcile` (G11).
  Direction: keep migrating decisions to Python CLIs; a split of the bash
  file itself buys little.
- D2. One udev entry point for hot-plug (G14): the output reconciler kicks
  the input one and the input udev rule goes. Interacts with ADR-0224;
  needs a hardware soak.
- D3. `jasper/web/sound_setup.py` is 55% active-speaker DSP (`:1641-4719`
  of 5549 lines); its hardware slice is small and clean. Belongs to the
  web-IA campaign (#4031), not here. `main.js:11-18` already says do not
  blind-refactor.
- D4. `deploy/bin/jasper-deploy-health` still runs on every deploy
  (`deploy/install.sh:2174`, `scripts/deploy-to-pi.sh:563`); ADR-0233 rule 5
  is unimplemented. Belongs to #4028.
- D5. #2574: a conservative profile for one unambiguous unregistered DAC. A
  feature, not hardening.
- D6. `jasper-input.service` is the accessory bridge, not the input path. A
  unit rename is a deploy change.
- D7. Typed `DacProfile` validation errors so the nineteen `match=`
  assertions become code assertions (G17). Test hygiene; low value.
- D8. One registered DAC plus a stray Apple dongle classifies clean with no
  issue (`output_hardware.py:453-457`). Product call.
- D9. `output_hardware.py`'s five concerns (G7). Mixer math and topology
  cross-check are separable; not urgent.
- D10. #3656 looks partially landed (`jasper/multiroom/dac_content_ring.py:91`);
  `headphone_pinned_100` from #3924's review is already gone. Owner closes
  or rescopes.

### Needs the owner and hardware present

- H1. Unplug the XVF3800 on a lab Pi with the journal open: does
  `jasper-aec-bridge` reach `StartLimitAction=reboot` before the reconciler
  revokes the ready marker? Decides the shape of PR 10.
- H2. Audible check of the mic-loss cue (PR 10). Clearance rules apply.
- H3. jts3 Phase 1 (ADR-0232): delete the hand-written line, deploy,
  reboot, probe, soak, flip the Studio row. Unchanged, out of scope.

## Consequences

- Twelve small PRs, most deleting more than they add. Three files get
  smaller by design: the two reconcilers and `usb_port_role.py`.
- A third party adds a DAC by adding one row (already true) and, after PR 2
  and PR 3, without touching any shell file.
- A second voice-mic family is the trigger to extract a `MicProfile` row
  shape; until then the cost of one family is paid once, in
  `jasper/mics/xvf3800.py` and its CLI.
- Two known races (G8, G14) stay disclosed with removal conditions rather
  than guarded.
- ADR-0232, 0233 and 0234 stand. This ADR adds rulings; it supersedes
  nothing.
