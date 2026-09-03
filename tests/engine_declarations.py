# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What a test says the speaker IS, before it says anything about measuring it.

The twin's first module, and the cheapest one: a two-way speaker's declarations
as plain values.

**It imports nothing.** Not the engine, not ``numpy``, not another test module.
That is the whole point of it being its own file: a test that needs to name the
crossover corner or the session level should not pay ~1,100 imported modules to
do it, which is the same lesson wave 1a's review learned about
``measure_spec``'s vocabulary. :mod:`tests.engine_twin` is where the engine
arrives; this is where the numbers live.

**It owns its own preset, deliberately.** ``crossover_v2_fixtures`` reaches into
``tests/test_active_speaker_profile.py`` for ``_two_way_preset``, which makes a
1,948-line fixture library depend on a 493-line test file that is itself
imported by 39 others. The twin does not inherit that edge: a declaration a test
needs is declared here, in the file whose job is declarations.

**These are a FIXTURE's numbers, not the speaker's.** They are internally
consistent and nothing more — a tweeter whose floor sits under the corner, a
session level under the hearing clamp. Nothing here is a claim about any real
driver, and no production default should be read off it.

**The numbers match ``crossover_v2_fixtures``'s where they are arbitrary** —
the corner, the level, the caps, the role bands. Not tidiness: a wave-2 or
wave-3 rewrite that swaps the fixture for the twin should not also have to
re-derive every expectation it asserts, and a changed constant would make every
one of those diffs look like a behaviour change.
"""

from __future__ import annotations

#: The two roles a v2 session designs across. The engine is a 2-way flow.
ROLE_WOOFER = "woofer"
ROLE_TWEETER = "tweeter"
ROLES = (ROLE_WOOFER, ROLE_TWEETER)

#: The crossover corner these declarations are built around, in Hz.
FC_HZ = 1600.0

#: Each role's usable band, in Hz. The tweeter's floor sits well under
#: :data:`FC_HZ` so a fixture speaker is never asked to cross below it.
ROLE_BANDS_HZ = {
    ROLE_WOOFER: (150.0, 6000.0),
    ROLE_TWEETER: (300.0, 20000.0),
}

#: Per-role peak cap, dBFS. The tweeter's is far below the woofer's because a
#: compression driver behind a horn needs it to be, and a fixture that made
#: them equal would let a test pass a stimulus plan no real speaker would.
DRIVER_CAPS_DBFS = {
    ROLE_WOOFER: 0.0,
    ROLE_TWEETER: -65.0,
}

#: The ONE declared fader level a session measures at, dB. Ruling S8's recipe
#: turns on it being one: *same drive voltage across every per-driver
#: measurement, no gain touched between them.* Under 0 dB, which is the
#: hearing clamp the engine never relaxes.
SESSION_VOLUME_DB = -20.0

#: A stimulus level, dBFS, for a ladder rung that is not the declared one.
#: Two of them, because one rung is not a ladder.
LADDER_DBFS = (-20.0, -12.0)

#: The horizontal walk a fixture session takes, in signed whole degrees.
#: Negative is LEFT of the design axis as seen from the microphone looking at
#: the speaker — ``spatial.PositionGeometry``'s frame, quoted not re-decided.
WALK_DEG = (-22, 0, 22)

#: What the mover was told, one per :data:`WALK_DEG` entry. Carried because
#: MS-17 puts the prompt on the shared record shape whichever mover satisfied
#: the precondition — an arm-driven record keeps the field rather than growing
#: a second shape.
WALK_PROMPTS = (
    "stand 22 degrees left of the mark",
    "stand at the mark",
    "stand 22 degrees right of the mark",
)

#: A speaker identity and a session identity, so a record's provenance fields
#: are distinguishable in an assertion rather than all being "".
SPEAKER_ID = "twin-speaker"
SESSION_ID = "twin-session"

#: A graph fingerprint. Provenance on a record, never a gate — the engine
#: accepts ``""`` from a host that cannot name its graph, and a test that wants
#: to exercise that path passes ``""`` rather than needing a second constant.
GRAPH_FINGERPRINT = "twin-graph-0001"
