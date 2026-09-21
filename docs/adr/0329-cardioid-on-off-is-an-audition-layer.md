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

A compare session opens only when the normalized running graph equals the
applied graph, so opening cannot discard an unsaved EQ draft or another live
edit. Existing compare sessions can flip between on and off.

A session-scoped web holder owns the deadline beside the foreground CLI owner.
They use the same record, replacement token, writer lock and durable anchor.
Measurement and commissioning interlocks guard starts and flips; a restore
is never refused by those interlocks. Each web flip renews the deadline and
starts one holder for its token, under the existing idle hold. The holder
ends at stop, expiry, takeover or record removal. There is no polling thread
when no session exists, and server construction starts no thread.

The web entrypoint recovers once after installing the idle tracker. A compare
owner whose PID is not this process is stale: systemd runs one web instance,
so PID reuse or another user's PID cannot prevent recovery. A successful full graph write to the primary CamillaDSP by another
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
That later change must pin `_linearization_boost_allowance_db`: a non-zero
compare trim makes its graph-derived allowance more generous by that many dB
while the session runs.

This replaces the server side of PR #5416's A/B listen card. The separate UI
change removes that card and uses the fixed cardioid compare contract.
