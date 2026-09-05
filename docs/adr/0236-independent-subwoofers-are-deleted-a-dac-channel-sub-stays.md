# ADR-0236: Independent subwoofers are deleted; a subwoofer on a DAC channel stays the active-speaker crossover's concern

- **Date:** 2026-09-05
- **Status:** Accepted. Supersedes [ADR-0126](0126-a-subwoofer-crossover-executes-on-the-receiver.md).

## Context

ADR-0126 gave one vocabulary two designs: a **local-DAC sub** (an
`output_topology` `subwoofer` group on a spare amp channel) and a
**wireless/bonded sub** (a `multiroom` `channel="sub"` member, with a
receiver-side low-pass and the complementary mains high-pass). Only the
second was ever built out — a `/rooms/` add-subwoofer flow, jasper-outputd's
`Sub`/`Lr4LowPass`/`Lr4HighPass` filter path, and `jasper/bass_management.py`'s
wireless-sub precedence branch — and none of it shipped to a household.

The owner ruled 2026-09-05: a subwoofer that is just another channel on the
user's DAC/amplifier is part of the active-speaker crossover model and stays;
anything more — an independent, wireless, or bonded-multiroom sub, and its
add-a-subwoofer flow — is deleted now and can be rebuilt properly later if
wanted. This code had not shipped; there was no legacy to support.

## Decision

**Keeps** (local-DAC sub, the active-speaker crossover's concern):
`output_topology.py`'s `"subwoofer"` group kind, its validators,
`subwoofer_speaker_groups()`, and `crossover_fc_hz`; `jasper/active_speaker/`'s
`LocalSubwoofer` and every reader; the `/sound/bass/` display; and
`jasper/camilla_emit.py`'s shared `BASS_MANAGEMENT_CORNER_HZ_*` constants.

**Deletes** (independent/wireless/bonded sub): the `/rooms/` add-subwoofer UI
and HTTP surface; jasper-control's grouping-crossover write path
(`JASPER_GROUPING_CROSSOVER_HZ`, `_MAINS_HIGHPASS`, `_SUBWOOFER_PRESENT`);
`multiroom.config`'s `channel="sub"`, its crossover/highpass fields, and
`bond_has_subwoofer()`; the reconciler's, `state.py`'s, and `tts_route.py`'s
wireless-sub fan-out; jasper-outputd's `dac_content.rs` `Sub`/`Lr4LowPass`/
`Lr4HighPass` filter path and its config keys (`JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ`,
`_HP_HZ`) — the `Biquad` type in `jasper-tts-protocol` stays (loudness uses
it); only the two constructor helpers went — and jasper-doctor's
`check_grouping_sub_corner` / `check_grouping_local_vs_wireless_sub`.

`jasper/bass_management.py` is deleted in full: once its wireless-sub
precedence branch was cut, the module did one thing — read the local sub's
corner — so that read folded into `output_topology.bass_management_corner_hz()`
and the module stopped existing.

**No migration shim.** A speaker with a stale `JASPER_GROUPING_CHANNEL=sub`
in `/var/lib/jasper/grouping.env` fails loud on its next reconcile rather than
degrading gracefully — an owner decision, not an oversight: spare Pis exist
and nothing shipped, so there is no fleet to migrate.

Landed as: #4052 (`/rooms/` dead UI), #4064 (`/rooms/` HTTP), #4062 (control
write path), #4076 (multiroom core + doctor), #4079 (outputd), #4078
(`bass_management.py` + `/sound/bass/`), #4081 (comment seams + spike script).

## Consequences

- The bass-management corner is read from exactly one place:
  `output_topology.bass_management_corner_hz()`. Nothing arbitrates between a
  local and a remote bass owner, because there is no remote bass owner.
- jasper-outputd's STATUS JSON drops `dac_content.main_highpass_hz`; the
  wire-format change is accepted, nothing reads that field going forward.
- A wireless/bonded subwoofer is deleted, not deferred. If wanted again, it
  is a new design against `multiroom.config`'s current `ALLOWED_CHANNELS`,
  not a revival of the deleted `"sub"` channel or its outputd filter path.
- ADR-0110, ADR-0112, and ADR-0122's passing references to a wireless/bonded
  sub stay as historical record; they described a design that is now gone,
  not current behavior.
