# Brief: Wave 0 of the Bank-style room, transition and bass program (decisions only)

You are working in the JTS repo (`jaspercurry/JTS`, a hardware-agnostic smart-speaker
stack: Raspberry Pi + CamillaDSP + a multi-channel DAC, tuned by an LLM operator
through a wired UMIK-2 on jts.local). This wave records **four owner decisions as
ADRs** and adds **pointer-only amendments** to two plan documents. It changes **no
product code and no tests**. Output: one docs-only PR the owner can merge, plus a
short report (format in §6).

## 1. Read first

1. `AGENTS.md` at HEAD (rules, non-negotiables, the ADR rule: append-only, dated,
   one decision per file, supersede never edit).
2. `docs/adr/README.md` and `docs/adr/TEMPLATE.md` — follow them for numbering,
   headings and how supersession is recorded. `ls docs/adr | sort | tail` gives the
   next number (0254 was the highest on 2026-09-08; verify at your HEAD).
3. ADRs you will cite or supersede: 0018 (bass extension stays parked), 0121
   (room boosts headroom-compensated), 0192 (gating can go lower than 1 kHz; nearfield
   parked), 0203 (structure-first recommissioning), 0212 (way-1 reuses the 2-way
   layer stack), 0222 (relay deleted; wired mic on jts.local is the only capture
   path), 0226 (constrained hardware), 0229 (bass plan docs exempt from deletion),
   0231 §5 (room correction and speaker tuning are separate products; shared math
   lives only in `jasper/audio_measurement/`), 0236 (local-DAC sub kept), 0237
   (stdout is the answer).
4. `docs/room-correction-regime-plan.md` (the standing room plan: decisions D1–D7,
   ladder RC1–RC5; only RC1 landed), the module docstring of
   `jasper/audio_measurement/room_boundary.py` (the boundary SSOT and the policy
   question it deliberately leaves open), `docs/measurement-loop-doctrine.md` §1a
   (the layering rule), `docs/tuning-master-plan.md` ruling R13.
5. `docs/HANDOFF-bass-extension-plan.md` lines 60–145 (what the program does),
   276–292 (the transport-agnostic substrate), 379–386 (which driver is extended),
   501–508 and 526–564 (nearfield fit), 906–918 (why the limiter evidence protocol
   exists), 1043–1048 and 1071–1077 (latency; room-boost stacking), and
   `docs/bass-extension-waves/README.md`.

Every `file:line` below was read at main `27c892b3f` (2026-09-08). Verify at your
HEAD; the tree wins. If a premise is false, say so in the PR body rather than
writing an ADR on a false premise.

## 2. Background you need to write these well

**Bank's method** (Balázs Bank, AES 134, 2013, "Combined quasi-anechoic and in-room
equalization of loudspeaker responses"): (1) gate the in-room impulse response to
the first reflection and correct the loudspeaker's direct response at high
resolution above the frequency where the gate stops being trustworthy; (2) below a
transition frequency, correct loudspeaker and room **jointly** with a smoothed
minimum-phase equalizer designed on the response measured **through** stage 1,
from several listening-area positions; the transition is set by the gate actually
achieved, not chosen a priori; in-room EQ must not reach above it because it
brightens the direct sound. Room FIR is rejected (pre-echo, position dependence).

**How JTS already maps onto it.** The speaker stage is `jasper/active_speaker/`
(gating with two published floors, `1/T` and `2.5/T`, in
`jasper/audio_measurement/gating.py:143-212`; per-driver minimum-phase biquads;
hardware-proven on 2026-08-31). The room stage is `jasper/correction/` (six-position
cloud around one seat, power-mean average, cuts-only bells 20–350 Hz, a shipped
accept-or-revert loop). The layering rule (`docs/measurement-loop-doctrine.md` §1a)
already measures the room **through** the applied speaker tune, which is exactly
Bank's stage 2. The transition is a constant: `ROOM_BOUNDARY_DEFAULT_HZ = 350.0`
(`room_boundary.py:115`), clamped `[250, 500]` (`:121-122`); the module docstring
says whether the ceiling should follow the measured trusted floor "is a room-layer
policy question, deliberately not answered here". The trusted floor is already
carried on the applied candidate (`exclusion_evidence`, made candidate-borne by
regime-plan RC1) and persisted by `persist_applied_baseline_profile`
(`jasper/active_speaker/baseline_profile.py:3562`); nothing in `jasper/correction/`
reads it. The room product still captures through the browser's `getUserMedia`
(`jasper/correction/session.py:48,270,1135`; `jasper/web/correction_handlers.py:201`;
`jasper/web/correction_room_flow.py:22-29`) even though ADR-0222 rules the wired mic
the only capture path — a live divergence. Its analysis metrics default their low
edge to 50 Hz because of the iPhone mic's high-pass
(`jasper/audio_measurement/analysis.py:281`).

**Bass extension** (`jasper/bass_extension/`, ~10k lines, parked by ADR-0018): a
measured, volume-scheduled Linkwitz-transform family for the household's one bass
system (the local sub chain if present, else the lowest driver way; owner resolved
via `output_topology.bass_management_corner_hz()`), plus a mandatory subsonic
high-pass. Waves 1–3 merged (numerics, profile, graph emission with zero production
callers, enforced by `tests/test_bass_extension_plan_status.py`). Wave 4 partial:
the pure limiter-evidence producer and the bench runner are merged; the bench's
`PlayAndCapture` collaborator (`jasper/bass_extension/bench/executor.py:148`) has no
implementation, so `jasper-bass-extension-bench --live` fails closed (issue #1738
N-7). Waves 5–7 unbuilt. The unbuilt waves' docs assume the deleted phone relay
(`HANDOFF-bass-extension-plan.md:84-86`; `bass-extension-waves/wave-4-commissioning-backend.md:362,389`;
`bass-commissioning-ux.md:230-234`; `wave-7-hardware-validation.md:24-26`). The
measurement substrate (sweep, deconvolution, quality gate, SNR policy, calibration,
ramp, excitation admission) is transport-agnostic. The fit is nearfield only; room
gain is neither modeled nor measured; room-correction boosts and the transform's
boost stack acoustically with only a warning (`HANDOFF:1071-1077`).

**Hardware today and next.** jts3: Pi 5, an 8-channel DAC HAT, one mono 2-way
cabinet (`jasper/active_speaker/presets/*.json`, `layout: mono`). Next: a second
identical 2-way cabinet on the same DAC (a stereo pair), then a 3-way whose third
channel is a cardioid bass/mid: a duplicate feed of the bass role's signal restricted
to a sub-band with its own delay and polarity. The profile vocabulary already has
`SUPPORTED_LAYOUTS = {"mono", "stereo"}`, `SIDES_BY_LAYOUT`, `required_driver_roles`
for 1/2/3 ways and `lowest_driver_role` (`jasper/active_speaker/profile.py:80-136`);
`runtime_contract.py:553-564` knows stereo 2-way and 3-way modes. But linearization
and polarity are keyed by role only, room PEQs are one set applied to both sides
(`jasper/active_speaker/camilla_yaml.py:1790-1809`), and no capture graph solos one
side.

## 3. The four ADRs

Write each with the template's Context / Decision / Consequences. Cite the ADRs and
`file:line` above. Quote the owner's rulings as rulings (they are given below in the
owner's substance, not verbatim). Keep each under ~90 lines; an ADR records a
decision and its why, not a plan.

### ADR A — Every product measures through the wired microphone

- **Context:** ADR-0222 deleted the relay and named the wired mic the only capture
  path; the room-correction product still captures through the browser; the bass
  program's unbuilt waves are written around the relay.
- **Decision (owner, 2026-09-08):** All measurement — speaker tuning, room
  correction, bass extension, anything future — captures through the wired
  microphone plugged into the Pi, driven from the jts.local browser as a
  position-ready walk. No browser-microphone or relay capture path will exist again
  for any product. One household microphone record serves every product. The
  browser path in the room product is scheduled for deletion in the next wave (it is
  not deleted in this PR).
- **Consequences:** name what deletes later (`jasper/correction/browser_audio.py` and
  its plumbing; `deploy/assets/shared/js/measurement-audio.js`; most of
  `deploy/assets/correction/js/main.js`), that the 50 Hz analysis floor loses its
  reason, and that the bass plan's wave 4/6/7 transport text is stale until rewritten.

### ADR B — The room ceiling follows the applied tune's trusted floor; room correction is per cabinet

- **Context:** the boundary SSOT's open policy question; regime-plan D1 planned a
  Schroeder-frequency estimator (RC2) that needs a room volume nothing measures;
  Bank sets the transition from the gate actually achieved; the trusted floor is
  already candidate-borne and persisted with the applied profile; the cloud is
  combined by a power mean with the spatial σ used only as a cut-depth cap
  (`jasper/correction/variance_cap.py`); the design band has a hard edge
  (`jasper/audio_measurement/peq.py:148`).
- **Decision (owner, 2026-09-08):**
  1. The room layer's ceiling is derived, per applied tune, from the applied
     candidate's disclosed trusted floor, clamped to `[ROOM_BOUNDARY_MIN_HZ,
     ROOM_BOUNDARY_MAX_HZ]`, falling back to `ROOM_BOUNDARY_DEFAULT_HZ` with the
     fallback disclosed when no applied floor is readable. This supersedes
     regime-plan D1's estimator (RC2) and answers `room_boundary.py`'s open question;
     the clamp bounds and the "ceiling ≥ gated spec edge" invariant stand.
  2. The cloud's common trend is the median across positions; the spatial σ stays
     the confidence signal (depth cap); the correction target tapers to flat over
     about one-third octave below the ceiling instead of a hard edge.
  3. Room correction is per cabinet: one set per output side when the layout is
     stereo. (Today's mono cabinet is unchanged.)
  4. Standing: D6 (no room FIR) and D7 (six positions) are reaffirmed. Tier B
     (residual correction above the ceiling) is deferred to last; nothing corrects
     above the ceiling until then.
- **Consequences:** RC2's estimator is not built; RC3's "ceiling goes per-room" is
  satisfied by rule 1; the per-side emitter and reader work is a later wave.

### ADR C — Bass extension resumes, rebased on wired capture and validated in-room below the ceiling

- **Context:** ADR-0018 parked the program by owner ruling and said only a fresh
  owner ruling may change it; this is that ruling. State the merged/partial/unbuilt
  facts and the relay assumptions from §2.
- **Decision (owner, 2026-09-08):**
  1. The program resumes. This supersedes ADR-0018's ruling. The park's enforcement
     tests stay exactly as they are until the PR that lands the first production
     caller, which deletes them in the same PR.
  2. Transport: the unbuilt waves (4 backend, 5 runtime, 6 UI, 7 validation) are
     rebased on the wired capture kernel (`jasper/audio_measurement/wired_capture.py`)
     and the wired session shape; the bench's `PlayAndCapture` is implemented
     against the wired mic. The relay text in the wave docs is stale as of this ADR
     and is rewritten when those waves are picked up, not now.
  3. Science: the nearfield fit and the limiter-evidence protocol stay as the
     protection basis. In addition the extended family is validated in-room, through
     the applied speaker tune, on the room cloud, below the room ceiling of ADR B;
     room gain (in-room minus nearfield model) is published as a number; and room
     boosts, linearization boosts and the transform's boost share one disclosed
     headroom budget with its cost in maximum level.
  4. Scope: sealed, ported and passive-radiator plants (the existing adapters). A
     cardioid bass channel is out of scope for the transform until ADR D's variant
     is designed.
- **Consequences:** ADR-0229's exemption of the plan docs continues (they are the
  live plan); the plan's header gets a pointer note (§4); the sequence of later
  waves is not part of this ADR.

### ADR D — The topology vocabulary is sides × driver roles, and cardioid is a variant of the bass role

- **Context:** the software must work unchanged for a passive 1-way with no active
  crossover, a 2-way, a 3-way, and a 3-way with an active cardioid bass channel;
  one DAC may carry two cabinets; cite the profile vocabulary and the role-only
  keying above.
- **Decision (owner, 2026-09-08):**
  1. Every tuning, room and bass row keys on output side and driver role, never on
     `way_count == 2`, `layout == "mono"`, or a named driver.
  2. A cardioid channel is a **variant of the bass role**: the same source signal,
     restricted to a sub-band, with its own delay, polarity and level, emitted as
     another output of that role. It is not a fourth way. Its design (band, delay
     model, protection) is deferred; this ADR only reserves the vocabulary so that
     per-side and per-role work in later waves does not preclude it.
  3. Per-cabinet facts (room PEQ set, level trim, bass fit check) attach to a side;
     per-model facts (linearization filters, crossover, the bass family shape) attach
     to a role.
- **Consequences:** names the modules that will grow a side axis later (the active
  emitter's room PEQ stage, the round-trip reader, a side-solo capture graph, the
  bass owner's channel set) without changing them here.

## 4. Pointer-only amendments (no rewrites)

- `docs/room-correction-regime-plan.md`: under its status header add one line:
  D1's estimator (RC2) and RC3 are superseded by ADR B; D6/D7 reaffirmed; Tier B
  deferred. Do not rewrite D1–D7.
- `docs/HANDOFF-bass-extension-plan.md` and `docs/bass-extension-waves/README.md`:
  one header note each: the program resumed per ADR C; the relay-based transport
  text in waves 4/6/7 is stale and will be rewritten when those waves are taken up.
  Do not edit the wave files.
- ADR-0018: record supersession the way `docs/adr/README.md` says (a status line on
  the old ADR if that is the convention; otherwise only the new ADR's "Supersedes"
  line). Same for any status convention around regime-plan decisions.
- Do not touch `AGENTS.md`, `README.md`, product code, or tests. If you find a doc
  sentence outside the two plans that now contradicts ADR A or B, list it in the PR
  body under "stale, not fixed here" instead of editing it.

## 5. Mechanics

- Start: `git fetch origin` and confirm `git merge-base --is-ancestor origin/main HEAD`;
  work on the branch your session designates (if none: `claude/bank-wave0-decisions`).
- Validate: `python3 scripts/docs-linkcheck.py --all`; `python3 scripts/docs-impact.py`
  (read its `--help` first; the tree carries `docs/doc-map.toml` and some tests pin
  doc claims, e.g. `tests/test_bass_extension_plan_status.py` asserts the README does
  not claim bass extension has no code); then `scripts/test-fast` — trust only the
  final `==> <lane>: N passed` sentinel.
- Review tier: docs — author judgment plus a sanity look; run `/code-review low` on
  the diff before pushing.
- Fetch again before pushing; `git push -u origin <branch>`; confirm the remote ref
  advanced. Open ONE PR titled "ADRs: wired-only measurement, derived room ceiling,
  bass extension resumes, sides × roles vocabulary". Body: the four decisions in one
  line each, the amended pointer lines, the "stale, not fixed here" list, and the
  validation output sentinels. No model identifiers in commits or the PR.

## 6. Report back

Print: the PR link; the four ADR numbers and titles; every file changed with a
one-line reason; the "stale, not fixed here" list; any premise from §2 you found
false at HEAD and what you did instead.
