# ADR-0191: USB transport is not gated on derived state

- **Date:** 2026-08-28
- **Status:** Accepted

## Context

`deploy/usbsink/jasper-usbgadget-compose.sh` composed `uac2.usb0` only after
four conditions held: board/port capability, a bound UDC, canonical `/sources`
intent, **the derived `jasper-usbsink.service` mirror being enabled**, and
**live fan-in reporting the DIRECT lane armed**. The last two encode "is the
rest of the system ready", not "may this exist". The fragment's own comment
described the third gate as making a stale endpoint *self-correcting*: "UAC2
whose consumer is gone is not wanted, so the next converge withdraws it."

Observed on jts3, 2026-08-27 03:35 through 2026-08-28: the host lost the JTS3
sound card entirely. The gadget composed `network=1 audio=0
audio_reason=derived_unit_disabled` on every converge for over a day. The
derived mirror was disabled because `jasper-source-intent-reconcile` failed its
USB On transition, which failed because `jasper-fanin-coupling-auto` exited
non-zero, which exited non-zero because an *opportunistic CamillaDSP self-heal*
could not run against a baseline whose topology fingerprint had gone stale
after a cosmetic `/sources` topology save. The USB work in that same unit — the
combo-key write and the fan-in restart — had both succeeded.

The failure was invisible where it mattered. Withdrawing the function removes
the device from the host's sound menu with no error, no dialog, and no
degraded state to notice. The box had the reason in its journal the entire
time and expressed it by deleting hardware.

Transport composition is not a driver-safety surface. Enabling USB audio never
re-emits the DSP graph: the combo keys are fan-in env and the function change
is ConfigFS. The L0 emit gates (unprotected tweeter, crossover below the
declared protection floor), `devices.volume_limit`, the `set_volume_db` clamp,
`startup_muted`, and the runtime contract all sit at the emit boundary and are
untouched by whether a UAC2 endpoint is advertised.

Per ADR-0186's precedent, each removed gate was checked for a second, unstated
job. Neither has one: the "Python actually imports" property that ADR-0186
protects is already carried by `HARDWARE_ALLOWED_CMD` and `AUDIO_ALLOWED_CMD`,
both of which stay.

## Decision

**Composition follows intent and physical capability only.** `uac2.usb0` is
composed when the board can host a gadget, a UDC is bound, and canonical
`/sources` intent authorizes it. The derived lifecycle mirror and fan-in's
DIRECT lane are **consequences** of that intent, never preconditions for it.

`AUDIO_READY_CMD` and `AUDIO_DATA_READY_CMD` are deleted along with the
`derived_unit_disabled` and `direct_lane_unarmed` reasons; `enabled_direct_ready`
becomes `enabled`, because the name described gates that no longer run.

**Readiness is disclosed, not enforced.** `check_usbgadget_composition` reports
`consumed=<bool>` alongside the observed functions. That line is informational
by construction — an idle box legitimately reads `consumed=False` — and must
not become a warn.

`jasper/cli/doctor/usbsink.py` keeps exactly one Python mirror of the truth
table (`_audio_wanted`); the near-duplicate `_audio_composition_wanted` is
folded into it.

## Consequences

A household that toggles USB Audio Input on gets a sound card whenever the
hardware can provide one. If nothing is consuming the lane, the host plays into
a void and the device is visibly present and silent — a state the household can
see, the doctor names, and an operator can reason about. This is deliberately
preferred to a device that vanishes correctly.

Given up: the self-correcting withdrawal of a stale endpoint. A UAC2 advertised
with no consumer now persists until intent changes. That is the cost of the
trade and it is the point of it.

Not addressed here, and still open: `topology_config_fingerprint` hashes the
whole topology document, so a rename or a position nudge invalidates an applied
baseline exactly as a DAC swap would. That guard is real — emitting a crossover
composed for other drivers is non-negotiable-tier damage — but its blast radius
is not, and narrowing it is a separate decision touching that tier. This ADR
only stops such a refusal from reaching the USB transport.

Rejected alternative: keep the gates and make the upstream reconciler more
robust. Ruled out because it leaves the invisible-failure mode intact — any
future upstream fault would again be expressed as missing hardware rather than
as a reported state.
