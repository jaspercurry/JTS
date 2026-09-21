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

A stop of the web unit during a session (a deploy's SIGTERM) ends the holder
without a restore. The record is then recovered at the next web start, or
earlier by any CamillaDSP graph load; `/state` and doctor disclose it until
then. This is ADR-0193's accepted shape.

This replaces the server side of PR #5416's A/B listen card. The separate UI
change removes that card and uses the fixed cardioid compare contract.

## Level match — 2026-09-21

The applied rear section is previewed against the newest banked round with
a front on-axis bearing pair. Search the newest 128 rounds by banked time;
the window counts rounds, not bank entries, because the bank also holds one
directory per authored candidate. Do not filter by applied identity: the
pair normally predates the rear section it seeds. Behind and other
non-bearing poses cannot supply this number.
The selector lives beside the path readers, which cannot import the rear
model without an import cycle.

On 1024 logarithmically spaced bins from 40 Hz to 16 kHz (upper endpoint
excluded), let `delta(f) = change_db(f) + relative_charge` inside the preview
curve's covered band, and zero outside it. The broadband difference is the
flat power mean `10 * log10(mean(10 ** (delta(f) / 10)))`. The rear is
low-passed; the pair only supplies woofer-band evidence. Add back
`relative_charge = H_on - H_off` because the preview models separately
compiled graphs, while the live switch retains the applied headroom in
both states. The preview stage owns this charge.

Attenuate the louder state by the absolute difference, rounded to two decimal
places. Below 0.05 dB, apply zero trim and name neither state as louder.
Missing applied rear, pair round or front pose, refused or unreadable preview,
and non-finite or greater-than-6 dB differences report `unavailable`. Any
level-path error leaves the mute switch usable with zero trim. The household
volume and durable graph remain unchanged.

The process caches the result by applied candidate fingerprint, apply time
and pair round ID, including preview refusals. The selector caches each
immutable round's record-only front-pair check within its bounded window;
only the selected round's preview builds spectra. A lock prevents concurrent
requests from repeating the work.

`_consume_linearization_chain` consumes `_linearization_boost_allowance_db`
during graph classification. Compare trim increases that allowance, but
does not change a branch or emit a durable proof. Startup, convergence,
doctor and apply checks use persisted or newly composed graph text. The
multiroom live check compares running text with the persisted graph first;
a compare graph fails that equality before classification. There is no
periodic proof from running compare graphs. Tests pin maximum-trim admission
and the live boundary's refusal without a durable write.

If the idle hold or holder fails after the graph swap, restore the applied
graph before returning the error, using the session token to avoid undoing
a newer owner.

### Level match cache and endpoint — 2026-09-21

The cache key is now `(candidate_fingerprint, applied_at, campaign-root
st_mtime_ns)`. A warm lookup reads the applied profile and stats the campaign
root once; it never walks the bank. Banking a round creates a directory there
and changes that key. The process memo remains, with an atomic JSON copy at
`rear_compare_level.json` beside the audition record in `/run`. It contains
the key and public level fields only, and survives web process idle exit;
reboot clears it. Missing, corrupt or mismatched files are cache misses.
A failed cache write leaves the process memo usable.

The studio measured a 2.3 s preview and about a 1 s bank walk on a Pi 5.
Only a cache miss on GET computes. POST uses cached data only and does not
wait for a preview in progress; a cold flip reports `unavailable` with
`cache_miss` and uses zero trim. POST builds the block once and changes only
its state and expiry after the flip. The projection maths is unchanged.

The card fetches GET `./cardioid-compare` on mount, independently of EQ boot.
GET `./state` no longer carries the block, so a cold analysis cannot delay
the editor's state response. A failed card fetch leaves the card hidden.

### Limits of the level match — 2026-09-21

The number is broadband POWER, not loudness: an unweighted power mean over
40 Hz–16 kHz. A bass-only difference is diluted across about 8.6 octaves (on
jts3 the rear's +2 dB below 60 Hz becomes about 0.3 dB). The two states are
therefore matched in overall level and NOT matched in bass tone; the card says
so. A bass-matched "off" needs front compensation and is a separate decision.

A failed level (no pair round, a refusing round, a preview error) is cached
like a success, so a tune that cannot be matched costs one walk per key, and
it is logged (`event=active_speaker.rear_compare_level`). Re-banking a round in
place does not move the campaign root's mtime; that staleness ends at the next
apply or reboot.

While a compare session runs, the live graph is not the approved graph, so
`classify_active_bass_extension_graph` answers not-allowed; an operation that
waits for a settled graph (for example making this speaker a multiroom
follower) can fail until the session ends. Fail-closed, bounded by the deadline.
