# ADR-0329: Cardioid on/off is an audition layer

- **Date:** 2026-09-21
- **Status:** Accepted

## Decision

Extend ADR-0193's audition owner with the `rear_compare` layer family. `off`
mutes the rear output after its branch sum; `on` plays the applied tune at the
comparison trim. Both keep the session open. `normal` restores the durable
anchor's current bytes and ends the session.

Build this layer from the applied graph's text. Change only the rear stage's
named output Gain mute and subtract the supplied trim from
`active_baseline_headroom`. Do not recompose with `rear_muted`: the emitter
then recalculates shared headroom without the rear and can raise the front.
The rear stage owns its Gain naming function. Devices, routing, other filters,
and the persisted config path remain unchanged.

Every swap and restore uses ADR-0211's structural comparison of normalized
running and wanted graphs. Parameter changes write in place through
`CamillaController.set_active_config_raw(duck=False)`; identical graphs need
no write; pipeline changes retain the duck. All writes retain controller
admission and live graph confirmation. This follows ADR-0177's rule that the
graph comparison grants the quiet path, and ADR-0219's accepted trim step.

A web worker owns the deadline beside the foreground CLI owner. They use the
same record, replacement token, writer lock, durable anchor, and measurement
and commissioning interlocks. Each web flip renews the deadline. The web
worker holds the existing idle tracker while active, restores at expiry, and
recovers a dead compare owner at startup. A successful full graph write to the primary CamillaDSP by another
owner clears the record under the shared writer lock, so an old audition
cannot restore over a new measurement or applied tune. Restore checks the
token inside that lock. `/state` and doctor disclose every audition layer;
this is a warning, never a failure.

Loudness matching is allowed for this layer. ADR-0193 rejected compensation
for `baseline` to preserve its trim comparison. Here the owner asks for a
loudness-matched comparison of one output being present or absent. The trim
only attenuates, is bounded to 0–6 dB, and lives only in the running graph.
This change accepts a trim input but uses 0 dB; a later change supplies the
level calculation. The HTTP contract reports level matching as unavailable.

This replaces the server side of PR #5416's A/B listen card. The separate UI
change removes that card and uses the fixed cardioid compare contract.
