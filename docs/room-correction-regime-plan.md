# Room-correction regime — retained proposal

This is the acoustic proposal from the [July research](research/2026-07-27-acoustics-round-2/02-room-correction-competitive.md),
not a current work order or a list of shipped features. The current
[Room reference](room-correction-information-design.md) describes the product.
The [tuning master plan](tuning-master-plan.md) keeps Room expansion outside the
present toolbox scope. D-numbers remain because code and research cite them.

The shared boundary in
[room_boundary.py](../jasper/audio_measurement/room_boundary.py) is implemented.
[strategy.py](../jasper/correction/strategy.py) still composes static strategy
bands from it. A per-room estimator, residual upper tier, and spatially admitted
LF boosts are proposals below, not current correction guarantees. The old RC1–RC5
file inventories and delivery/review ladder have been removed; current repository
rules own review and tests.

Owner rulings of 2026-09-08 ([ADR-0256](adr/0256-the-room-ceiling-follows-the-applied-tunes-trusted-floor-and-room-correction-is-per-cabinet.md)): D1's proposed per-room
estimator is superseded — the ceiling is derived from the applied tune's trusted
floor; D6 and D7 are reaffirmed; D2's residual tier above the ceiling is deferred
to last. D1–D7 stay as written because code and research cite them.

## D1 — Bandwidth: proposed per-room transition

The proposal separates modal correction below a room-dependent transition from
broad residual trends above it. Decay time from a Schroeder integral and the
Schroeder transition frequency are different quantities. The latter estimate,
`f_s = 2000 sqrt(T60/V)`, needs decay time in seconds and room volume in cubic
metres. Room volume is not measured by an impulse response.

The proposed transition was clamped to 250–500 Hz, with a disclosed 350 Hz
fallback when either input was missing, and a residual tier stopping at 1 kHz.
These are proposed policy values, not a per-room calculation currently exposed
by Room. Strategy choice and an estimator's trust range are separate concerns;
`assertive` must not silently inherit a new meaning from a coincident boundary.

The shared band boundary belongs below both product packages because
`audio_measurement` can be imported by Room and Active without either importing
the other's policy. The SNR band tables are measurement vocabulary, not room-fit
boundaries; changing the correction ceiling must not silently change their units
or make historical sessions incomparable.

## D2 — Attribution: proposed residual trend tier

Above the modal band, the proposal corrects only the room's residual relative to
a compatible gated measurement of the applied speaker. A spatial in-room
average alone cannot identify how much of its trend belongs to the speaker.
Use a shared trusted band for scalar level normalization; do not learn a
frequency-shaped alignment that removes the residual being tested.

This would need the applied candidate's actual gated curve, validity floor, and
identity. Missing, stale, or era-absent data removes that residual claim. It must
not silently fall back to flattening the raw in-room curve over the upper tier.
The proposed vocabulary was broad low-Q peaking terms with a small total change,
initially for calibrated microphones. A payload field or preserved exclusion
registry is not proof that this correction path is implemented.

A proposed filter must also survive graph extraction and recomposition. A reader
that understands peaking filters cannot silently retain shelves or FIR. Any such
extension needs its own round-trip behaviour and measured evidence.

## D3 — Shared band: direct-sound and room evidence

The speaker layer owns direct-sound tonal balance where its capture is trusted;
Room owns repeatable room deviation. The nominal speaker-spec edge and Room
ceiling share a code owner. A given capture's trusted floor can be higher than
the table edge, however, leaving a disclosed gap or a smaller overlap. Equality
between constants does not supply missing acoustic evidence.

Below the trusted direct-sound band, do not extend a gated flatness claim merely
to meet a room-fit boundary. In their overlap, avoid correcting the speaker's
own response twice. The current static boundary is not proof that the proposed
residual handoff or per-room adaptation has shipped.

## D4 — Targets: deviation and taste

Room removes repeatable deviation; preference expresses taste. Flat remains the
Room default, while existing named warmth targets remain available. Their
vocabulary and Sound's preference vocabulary have not been made one owner by
this proposal. Do not present a tilted target as a measured property of the room.

## D5 — Proposed low-frequency boost evidence

A high-frequency, gated interference registry cannot classify low-frequency
room nulls. Any proposed LF boost path needs Room's own spatial evidence: a dip
that persists across positions, a shape consistent with the intended model, and
available headroom. One seat cannot establish spatial persistence. Repeating one
seat reduces random error but does not create a spatial survey.

The original proposal allowed up to +6 dB for admitted dips with an explicit
maximum-level cost. That expansion is not shipped household policy. Current
household strategies remain cuts-only; numerical bounds live in the strategy
and graph owners. An average dip is not permission to spend headroom on an
unresolved cancellation.

If this proposal is resumed, define the spatial fraction for the actual position
counts and test the measured effect of boost. Do not borrow a threshold from a
different instrument. Apply/readback, same-seat acoustic verification, and a
later direct-sound recheck establish different claims.

## D6 — Phase/FIR room correction

Room v1 remains IIR magnitude correction. Inverting a room's non-minimum-phase
response at one seat does not repair it throughout the listening area. FIR with
useful low-frequency resolution can require enough latency to conflict with
smart-speaker duties. The [FIR note](../jasper/calibration_agent/corpus/filter-design/fir-room-correction.md)
retains filter classes and latency calculations; the current Room latency
contract remains in force. A source survey is not evidence that JTS offers FIR
room-filter authoring.

## D7 — Spatial protocol

The research suggested diminishing returns across a modest number of listening
positions. That supports testing a small cloud; it does not establish a universal
optimal count. Room's current default and quick choices live in
`jasper.correction.session`. Moving-microphone measurement was a possible future
method, not a supported mode created by this plan.

## Evidence needed to resume expansion

Use identified room bundles and disclose which microphones, rooms, and bands
support each claim. A smaller in-room error alone does not prove that direct
sound was preserved. Keep predicted, measured, and missing evidence separate;
check filter survival and headroom cost as well as the acoustic result. Scope
and authorization must come from the current plan, not this retired PR ladder.
