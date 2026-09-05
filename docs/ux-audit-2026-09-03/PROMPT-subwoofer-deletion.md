# Prompt: delete independent-subwoofer machinery

Hand this file whole to an orchestrating agent. It is self-contained; the
inventory below was produced 2026-09-05 against `main` at `c6329be2d` by a
read-only Opus agent and must be re-verified at HEAD before each PR.

## Ruling (owner, 2026-09-05)

A subwoofer that is **just another channel on the user's DAC/amplifier**
taking low frequencies is part of the active-speaker crossover channel model
and **stays**. Anything more than that — an independent subwoofer device, a
wireless/networked sub, a sub as a bonded multiroom member or role, an
add-a-subwoofer flow — is **deleted now**; it will be added back properly
later if wanted. The **bass extension** module stays parked (ADR-0018). Room
correction and the measurement visualisation stay. This code has not
shipped; there is no legacy to support.

Two designs share one vocabulary (ADR-0126 §Context): a **local-DAC sub**
(an `output_topology` `subwoofer` group on a spare amp channel — KEEP) and a
**wireless/bonded sub** (`multiroom` `channel="sub"` member — DELETE). That
is the line.

## How to run it

You orchestrate; Sonnet implements pure deletions, Opus does the seams
marked "surgery". One PR per numbered step below, in order. Each PR: the
implementer re-verifies its inventory at HEAD, deletes, runs
`scripts/test-fast` (and `cargo test -p jasper-outputd` for step 4b), runs
`/simplify` then `/code-review` medium, resolves findings, pushes. Deletions
are unbounded in size (AGENTS.md); additions stay under ~40 lines per PR.
Verify no caller before deleting anything: registries, `pyproject.toml`
entry points, systemd `ExecStart`, `deploy/bin`, udev, CI, `importlib` /
`getattr`, `docs/doc-map.toml`. Before step 4 lands, confirm on every
deployed Pi that `/var/lib/jasper/grouping.env` carries no
`JASPER_GROUPING_CHANNEL=sub`; if one does, unbond it by hand first
(no migration code).

## Decisions already made (do not re-ask)

- No migration shim for bonded subs: fail-loud on a stale `channel=sub` is
  acceptable (spare Pis, nothing shipped).
- `channel="mono"` (a single mono cabinet) is not a sub. Keep.
- The mains high-pass complement of a wireless sub
  (`JASPER_OUTPUTD_DAC_CONTENT_HP_HZ`, `Lr4HighPass`, `main_highpass_hz` in
  outputd STATUS JSON) goes with it. The STATUS wire-format change is fine.
  The `Biquad` type in `jasper-tts-protocol` stays (loudness uses it); only
  the two constructor helpers in `dac_content.rs` go.
- After surgery `jasper/bass_management.py` does one thing (read the local
  sub's `crossover_fc_hz`). Fold `active_crossover_corner_hz` into
  `output_topology.py` and delete the module if nothing else remains.
- `/sound/bass/`'s "Bass management" section: drop the "Owned by" row,
  collapse to corner + mains-HP. Its "Bass extension" section is untouched.
- ADR-0126 is **superseded by a new ADR**, never edited or deleted. Passing
  references in ADR-0110/0112/0122 stay as historical record.
- `docs/dumb-endpoint-bringup.md`: prune the wireless-sub paragraphs only;
  no rewrite pass.
- `scripts/multiroom-spike.sh --sub`: read the script; if the third host is
  genuinely sub-specific, delete the arm; if it is really "a third follower
  on cheap hardware", rename it `--endpoint` and keep it.
- `jasper/multiroom/reconcile.py:584-596` and
  `tests/test_multiroom_reconcile.py:689` describe the *active-speaker*
  corner-precedence rule inside a wireless-sub code path: rewrite from HEAD
  ("an active main folds its own mains HP when `preset.local_subwoofer` is
  set"), do not trim mechanically.

## KEEP — do not touch (A: sub as DAC channel; C: bass extension)

- `jasper/output_topology.py` — `"subwoofer"` group kind, `subwoofer_*`
  validators and fields, `subwoofer_speaker_groups()`, `crossover_fc_hz`.
- `jasper/active_speaker/*` — `LocalSubwoofer` and every reader
  (`profile.py`, `staging.py`, `camilla_yaml.py`, `runtime_contract.py`,
  `baseline_profile.py`, `playback_route.py`, `graph_safety.py`,
  `driver_protection.py`, `design_draft.py`, `passive_profile.py`,
  `test_signal_plan.py`, `graph_evidence.py`, `bundles.py`).
- `jasper/camilla_emit.py` `BASS_MANAGEMENT_CORNER_HZ_*` constants (shared
  SSOT). Only their wireless-sub comment lines and the `"sub"` arm of
  `channel_select_sources` (:336) are deletable.
- `jasper/web/sound_setup.py:412,439` layout gate; `jasper/fanin/ring_health.py:1383-1397`.
- `deploy/assets/sound-profile/js/main.js` "Subwoofer add-on" card,
  `renderSubwooferCrossoverControl`, `toggle-output-subwoofer`;
  `active-speaker-ui.js` local sub lane. (Exception: `wirelessSubCta` :2582-2599 goes.)
- `jasper/bass_extension/` (10k lines), `jasper/web/correction_bass_flow.py`
  "Bass extension" section, `deploy/assets/correction/js/bass/main.js`
  (except the `active_endpoint_wireless_sub` branch :55-68),
  `correction_hub.py` bass tab, `correction_setup.py` bass-extension
  classification, `jasper/correction/*`, `tests/test_bass_extension*.py`,
  ADR-0018, `docs/HANDOFF-bass-extension-plan.md`, `docs/bass-extension-waves/`,
  bass-extension research dirs.
- `tests/test_active_speaker_local_subwoofer.py`, `test_output_topology.py`,
  `test_transport_park.py` sub fixtures, `tests/js/active_speaker_ui_test.mjs`.
- `jasper/web/pair_flow.py` (no sub content), `roster`/`BondMember` (serve
  the L/R pair), `multiroom/effective_role.py` and the other multiroom
  modules with no sub role.

## DELETE — inventory and PR order (~1,290 prod + ~1,660 test lines)

**1. `/rooms/` add-subwoofer dead UI (Sonnet, ~360 lines).** Already
unreachable: `rooms_setup.py:459-461` hardcodes
`show_subwoofer_controls: False` and `rooms/js/main.js:741` gates on it.
- `deploy/assets/rooms/js/grouping-view.js:52-100` `subCornerLabel()`, `addSubPlan()`
- `deploy/assets/rooms/js/main.js` imports :51,:57; `groupingHasSubwoofer()` :92-97;
  channel label :128-133; state :421-429; `mainsHpRow` :541-553; add-sub panel
  :555-581; poll branch :725-778; `addSub()` :876-909; `setMainsHighpass()`
  :911-932; listeners :937-944
- `deploy/assets/rooms/rooms.css` `.bond-crossover` :175, `.add-sub-panel`, the "2.1 / sub" note :91
- `deploy/assets/sound-profile/js/main.js:2582-2599` `wirelessSubCta`
- `jasper/web/rooms_setup.py:459-461` flag
- Tests: `tests/js/rooms_grouping_view_test.mjs:15,22,80-120`;
  `tests/js/sound_profile_harness.mjs:360-370, 8305-8360`;
  `tests/test_web_rooms_setup.py:3190` (`{"left","right","sub"}` assert)

**2. `/rooms/` HTTP surface (Sonnet, ~330).**
- `jasper/web/rooms_setup.py:46-48` docstring; `:950-1015` bond sub-detection
  and `crossover_hz`/`subwoofer_present` fan-out; `:1018-1020`, `:1163`
  comments; `:1768-1790` `_grouping_set_body`; `:1793-1917`
  `_set_mains_highpass`; `:1977` route dispatch (`POST /mains-highpass`)
- Tests: `tests/test_web_rooms_setup.py:1438-1500` (`_sub_bond_members` + 3
  crossover tests), `:3129-3195`

**3. jasper-control write path (Sonnet, ~250).** Stops writing
`JASPER_GROUPING_CROSSOVER_HZ`, `JASPER_GROUPING_MAINS_HIGHPASS`,
`JASPER_GROUPING_SUBWOOFER_PRESENT`.
- `jasper/control/server.py:1337-1339`, `:1353-1355`, `:1376`, `:1385`,
  `:1398-1400`, `:1404-1425` `_resolve_grouping_crossover_hz_for_write`,
  `:1433-1435`, `:1469-1478`
- `jasper/control/handlers/grouping.py:88-90, 137-143, 156-166, 191, 206-208`
- Tests: `tests/test_control_server_grouping.py:850-1010`

**4a. multiroom core (Opus: surgery in `reconcile.py`, ~800).** Python
first, so Rust is left holding dead code rather than the reverse muting a
live sub. **Delete `tests/test_audio_safety_pins.py:245-282`
(`test_sub_crossover_corner_constants_match_python_and_rust`) in this PR**
— it greps Rust `SUB_*` literals against the Python constants and couples
4a to 4b.
- `jasper/multiroom/config.py` `"sub"` in `ALLOWED_CHANNELS` :72; :100-120
  crossover/highpass defaults; docstring :169-171; `GroupingConfig` fields
  :212-226; `_DISABLED` :261-263; `_parse_crossover_hz` :344-362;
  `validate_grouping` :495-518; `load_config` :634-682; `bond_has_subwoofer`
  :745-760 (callers: `reconcile.py:604`, `bass_management.py:186`,
  `tests/test_multiroom_config.py:1101`)
- `jasper/multiroom/reconcile.py` :166-175 env names; :584-596 (rewrite, see
  decisions); :600, :604-612; :631-639; :658-661
- `jasper/multiroom/state.py:696-730` (`/state.grouping.subwoofer_present`, `crossover_hz`)
- `jasper/multiroom/tts_route.py:47-52, 78-94` (`parked_sub`/`sub` route kinds)
- `jasper/multiroom/follower_config.py:70-73, 90-93`; `jasper/multiroom/__init__.py:8,12`
- Tests: `test_multiroom_config.py:924-1090`; `test_multiroom_reconcile.py:549-811`
  (`:689` is A-side: rework, not delete); `test_multiroom_state.py:135-175`;
  `test_multiroom_rate_adjust.py:587-660, 923-948`

**4b. jasper-outputd (Opus, ~235 prod + ~485 test).**
- `rust/jasper-outputd/src/dac_content.rs` :59-61 doc; :72-87 `SUB_*`
  constants; :89-123 `low_pass_biquad`/`high_pass_biquad`; :125-195
  `Lr4LowPass`/`Lr4HighPass`; :207-227 `Sub(f64)` variant; :240/:256
  `as_str`/`parse` arms; :277-282; :286-353 Sub arm + HP tail; :611-619,
  :668-690, :729-750 `sub_filter`/`main_highpass*`; tests :950-1257, :1285-1344
- `rust/jasper-outputd/src/config.rs:15, 255-260, 577-615` + tests :1269-1425
  (`JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ`, `_HP_HZ`)
- `rust/jasper-outputd/src/state.rs:1199-1200` `main_highpass_hz` + test :3016
- `rust/jasper-outputd/src/main.rs:454` log field

**5. jasper-doctor (Sonnet, ~235).** After 4a (imports `bond_has_subwoofer`,
`OUTPUTD_DAC_CONTENT_SUB_HZ_ENV`).
- `jasper/cli/doctor/grouping.py:750-803` `check_grouping_sub_corner`;
  `:806-859` `check_grouping_local_vs_wireless_sub`; `:862-870` docstring
- Registry: `jasper/cli/doctor/__init__.py:326` import and `:625` `__all__`
- Tests: `tests/test_doctor_grouping.py:326-380`

**6. `bass_management.py` + `/sound/bass/` surgery (Opus, ~290).**
- `jasper/bass_management.py`: delete docstring :14-35, `OWNER_WIRELESS_SUB`
  :60, `MAINS_HP_UNWIRED_ACTIVE_ENDPOINT` :64-69, the
  `mains_highpass_unwired_reason` field, `_wireless_mains_hp_cleared` and
  branch 2 :184-214; then fold the remainder into `output_topology.py` per
  the decision above. Callers: `correction_bass_flow.py:100-102`,
  `jasper/correction/session.py:1691`.
- `deploy/assets/correction/js/bass/main.js:55-68` wireless branch; "Owned by" row.
- Tests: `tests/test_bass_management.py:176-315`;
  `tests/test_web_correction_bass_flow.py:87-130`

**7. Comment seams + spike script (Sonnet, ~70).**
`jasper/camilla_emit.py:124-145, 273, 322-341`; `active_speaker/profile.py:59`;
`active_speaker/camilla_yaml.py:163`; `jasper/attribution/findings.py:88`;
`scripts/multiroom-spike.sh` `--sub` arm (:44-46, :90-94, :112, :139,
:164-165, :188, :355, :423, :429, :483-490, :530) per the decision above.

**8. Docs and ADR (Sonnet, last).** New ADR superseding ADR-0126:
"independent subwoofers are removed pending a proper design; a sub on a DAC
channel remains the active-speaker crossover's concern." Prune
`docs/dumb-endpoint-bringup.md` :25, :316, :332, :720, :849, :863, :871,
:903; `docs/audio-paths.md:60`; `docs/RESEARCH-pipewire-low-latency.md:109`.
Tick `docs/UX-AUDIT-2026-09-03.md` ledger row P.2 and the `/rooms/` add-sub
row. `docs/doc-map.toml` needs no edit (globs).

Not found, nothing to do: no sub-specific systemd unit, nginx location, udev
rule, `deploy/bin` script, or entry point; no `LFE` token anywhere;
`c/jts-ring-ioplug` has no sub content.

## Report back

Per PR: diffstat, the sentinel line from `scripts/test-fast`, findings
wontfixed and why. At the end: the net line count removed, and any A/B seam
where you chose to keep something the inventory listed, with the reason.
