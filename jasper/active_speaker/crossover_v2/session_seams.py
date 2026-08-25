# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The three things a tuning session opens once, and what each owes it.

``docs/REFACTOR-TUNING-2026-08.md`` §1 draws the engine as one session object
over three columns — **volume owner · session graph · capture record** — and
this module is those three columns as contracts. Each is filled by its own
wave: the graph by wave 6, the volume claim by wave 5, the record store by
waves 3 and 4. Cutting all three now, before any of them is built, is what
stops three waves each cutting a seam of its own shape.

**Why a lifetime and not a call.** Every one of the three is a thing today's
code does per stimulus and the plan does once per session: two config swaps and
two ducks per stimulus become one graph install per session (measured at
``≈0.94 s`` of pure duck ramp per swapping stimulus, `08 §Test 2`); nine
colliding fader writers inside one session become one ranked claim; and a
record that is re-derived from WAVs every round becomes one banked shape. A
seam whose contract is *open once, use many, close once* is what makes those
three collapses expressible.

**This module is vocabulary, not logic** — the register :mod:`.capture_source`
established. It declares what a provider owes and nothing about how any
provider satisfies it, so the wave that builds a real one is free, and so a
test double is a peer of production rather than a mock of it.

**What is deliberately NOT here.**

* **The apply/rollback transaction.** §3 marks it *"not a target. Ever."* It is
  not a seam of this session, it is not a verb, and it does not move.
* **A mover.** MS-17: the engine below the front-end seam holds zero
  arm-specific and zero wizard-specific code, so no contract here names one and
  no field carries one. Placement is a precondition
  :data:`~.playback_transaction.STAGE_READY` satisfies.
* **A capture provider.** :class:`~.capture_source.CaptureAnswer` already owns
  what a capture answer is, and this refactor consumes it rather than minting a
  second one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from .playback_transaction import PlaybackTransaction

__all__ = [
    "SESSION_SLOTS",
    "SLOT_CAPTURE_RECORD",
    "SLOT_SESSION_GRAPH",
    "SLOT_VOLUME_CLAIM",
    "EngineSeams",
    "RecordStore",
    "Recommender",
    "SessionGraph",
    "VolumeClaim",
]

#: The graph the session measures THROUGH, installed once.
SLOT_SESSION_GRAPH = "session_graph"
#: The session's hold on the fader, taken once at the one declared level.
SLOT_VOLUME_CLAIM = "volume_claim"
#: Where this session's evidence lands.
SLOT_CAPTURE_RECORD = "capture_record"

#: Named so a caller can say WHICH slot rather than quote a string, and so a
#: reader can count them. Three, and the plan adds no fourth.
SESSION_SLOTS = (SLOT_SESSION_GRAPH, SLOT_VOLUME_CLAIM, SLOT_CAPTURE_RECORD)


@runtime_checkable
class SessionGraph(Protocol):
    """One measurement graph, installed once and patched per candidate.

    **The install must satisfy all three properties at once** (MS-13, and
    `09`'s correction PC-8): **role-routed** — role to output channel;
    **crossover-free** — or every driver is measured through the crossover the
    session is designing, which is the circularity the whole program exists to
    break; and **per-driver protected** — the tweeter high-pass **and** the
    soft-clip limiter together, on exactly the tweeter output channels. The
    emitter's ``_assert_program_graph_proven`` is the fail-closed return
    contract, and a session-scoped graph must pass it **once, before the first
    stimulus** — not once per stimulus, and not never.

    **Isolation does not come from here.** Three axes, named once so nobody
    flattens them (PC-7): **lane = transport · graph = routing · WAV =
    isolation.** Channel content rides the stimulus WAV, and the correction lane
    was measured passing stereo bit-exactly with an idle channel at exact
    digital zero.

    **MS-1 binds the install and gets stronger here, not weaker.** Every
    ``dataclasses.fields(ActiveEmitDevices)`` field must be derived from
    ``active_emit_devices(...)`` and forwarded — a subset poisons one stimulus
    today and every angle, retry and driver once the graph is session-scoped.
    """

    def install(self) -> str:
        """Install the graph and return its fingerprint.

        The fingerprint is provenance a record carries — which graph the
        evidence was measured through — never a gate. A host that cannot name
        the graph returns ``""``, and the record says so, because a capture that
        measured the speaker honestly must not be thrown away over a missing
        label.
        """
        ...

    def patch(self, changes: Mapping[str, Any]) -> None:
        """Change what one candidate needs, without re-installing.

        The cheap half of *"structural swap once, patch per candidate"*, which
        ships twice already as prior art: the bass bench's swap/patch split and
        ``/correction/``'s ``_load_measurement_baseline``.
        """
        ...

    def restore(self) -> None:
        """Put back whatever the install displaced.

        Called on every path out of a session, including the failing ones.
        """
        ...


@runtime_checkable
class VolumeClaim(Protocol):
    """The session's one hold on the fader — one of four claim kinds.

    Wave 5 collapses 18 production-reachable fader writers, **nine of them
    colliding inside one crossover-v2 session with no owner arbitrating**, into
    one owner exposing four ranked claim kinds: household · transient-duck ·
    **session-measurement** · commissioning. This protocol is the
    session-measurement claim's side of that owner.

    **One declared level, and the same one all session.** Five overlapping
    notions of "the level" collapse to one, which is also exactly what ruling
    S8's level recipe requires of the measurements it compares — *same drive
    voltage across every per-driver measurement, no gain touched between them.*

    **The 0 dB ceiling is not this seam's to relax.** ``devices.volume_limit``
    stays ``0.0`` and ``CamillaController.set_volume_db`` clamps positive
    writes; the owner becomes the write door's only caller, never its exception.
    """

    def acquire(self, level_db: float) -> None:
        """Take the session-measurement claim at the declared level."""
        ...

    def prove(self) -> float | None:
        """Read the fader back and return what it actually reads.

        **MS-14, and the shape ruling S10 preserves.** The level is proven
        before any audio, and ``None`` means *not proven* — which refuses to
        BANK the capture, never to play the stimulus and never to try again.
        An absent number must not read as an absent problem (#2198, #2085), so
        ``None`` is a distinct answer here and not a zero.
        """
        ...

    def release(self) -> None:
        """Give the fader back, on every path out of the session."""
        ...


@runtime_checkable
class RecordStore(Protocol):
    """Where this session's evidence lands — the one shape, one writer.

    Two writes, because the plan has two: :meth:`bank` takes ONE capture record
    (wave 4's five blocks — identity · place · stimulus-and-path · honesty ·
    **the curve**), and :meth:`persist` takes the session's own durable state
    (wave 3's ``persist_conductor_state``, today an 854-line function that is
    *"a schema writer with no schema"*).

    **Complete records are what make ``analyze`` re-runnable forever.** Ruling
    S3: a banked session can be re-analyzed by any analysis that did not exist
    when it was captured, which is the whole return on banking
    ``DriverResponse`` with its phase instead of re-deriving it from WAVs.

    **An integrity refusal still banks.** The discriminator is #2087's: would
    measuring again plausibly fix it? Yes, and the capture is a defect that is
    refused **and still banked**. No, and it describes the room, the rig or the
    result — disclose it and recommend, never block.
    """

    def bank(self, record: Mapping[str, Any]) -> str:
        """Write one capture record; return the id that finds it again."""
        ...

    def persist(self, state: Mapping[str, Any]) -> str:
        """Write the session's durable state; return the id that finds it."""
        ...


#: What ``recommend`` delegates to, given the ids of everything banked.
#:
#: Not extracted, and deliberately: the prescriber's ``packet → propose → stage
#: → status`` path is already shipped and already decoupled, and §3's wave-3
#: table says in as many words **do not re-extract** it. The engine's job is to
#: have a verb that reaches it, so a front end asks the session rather than
#: assembling a CLI invocation of its own.
Recommender = Callable[[Sequence[str]], Mapping[str, Any]]


@dataclass(frozen=True)
class EngineSeams:
    """Everything a :class:`~.session.TuningSession` needs from outside itself.

    Frozen and injected, exactly as ``V2FlowSeams`` is: every side effect the
    session can have crosses one of these four fields, so a test double is a
    complete substitute rather than a partial one.

    ``play`` sits here beside the three lifetime slots even though ruling S1
    calls it *internal to* ``measure``. The distinction it draws is about
    VOCABULARY — the front end and the LLM never name a play transaction — not
    about who owns the object. Playing audio is a side effect, and side effects
    are injected.
    """

    graph: SessionGraph
    volume: VolumeClaim
    records: RecordStore
    play: PlaybackTransaction
    recommend: Recommender
