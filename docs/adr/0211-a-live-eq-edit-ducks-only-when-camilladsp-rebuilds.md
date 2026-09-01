# 0211 — A live EQ edit ducks only when CamillaDSP rebuilds

Date: 2026-09-01. Status: accepted.

## Decision

The live preference-EQ edit path (`/eq/`, `sound_setup._live_draft_profile`)
sends every edit to CamillaDSP as one whole config through
`set_active_config_raw`, and ducks the fader across that write exactly when
CamillaDSP will rebuild its pipeline. `jasper.sound.live_edit.plan_live_edit`
decides that by CamillaDSP's own rule: a changed `devices`, `pipeline` or
`mixers` section, a changed filter set, or a filter whose kind changed
(`Biquad` to `Conv`) is a rebuild and ducks; any change confined to filters'
`parameters` — a biquad's `type` included — is written in place and does
not duck. `PatchConfig` is no longer used on this path.

## Why

Two gestures still ducked on the deployed build by ear: retyping a band
(Peaking to Lowshelf) and toggling Simple and PEQ mode. Both reduced to one
line: `_parameters_only_moved` refused any non-numeric change inside a
filter's `parameters`, so a biquad `type` string moving fell back to the
ducked swap. The Simple/PEQ toggle hits the same line because the Sub-bass
and Treble Simple bands are shelves, and converting them into advanced slots
retypes an idle Peaking slot. Bypass with a Highpass/Lowpass/Notch band
retypes the same way.

That refusal was ours, not CamillaDSP's. Its `config_diff`
(`src/config/utils.rs`, v4.1.3) rebuilds only on the sections named above
and on a filter's outer kind changing; for a `Biquad` whose parameters
differ in any way it takes the `FilterParameters` path, and
`Biquad::update_parameters` recomputes the coefficients from the whole new
parameter block with the filter's state kept. A retype is therefore as
silent as a gain drag.

`SetConfig` and `PatchConfig` both end in that same diff. `PatchConfig` is an
RFC 7386 merge patch and `BiquadParameters` denies unknown fields, so a
Peaking-to-Highpass patch would leave a stale `gain` key behind and be
rejected; sending the whole config has no such hazard and is the write the
swap path already makes. One write vehicle, one decision (duck or not).

## Consequences

- The plan carries no patch payload and no numeric checks; it is a
  structural comparison of two CamillaDSP-normalized configs, about half the
  code it replaced.
- A curve preset change still ducks, honestly: preset filters exist only for
  non-flat presets, so switching presets changes `pipeline.names` and
  CamillaDSP rebuilds. Making that quiet would mean fixed curve slots in the
  frame, a separate decision.
- `CamillaController.patch_config` stays for its other callers.
- The verdict that counts is a fader trace on hardware, not the tests:
  `scripts/eq-duck-probe.py` polls the fader while a gesture is made and
  reports the deepest excursion.

Supersedes nothing; refines the rule `jasper/sound/live_edit.py` stated at
#3309's rejection (ADR-0177 still holds: the decision is a comparison of
graphs, never a caller's declaration).
