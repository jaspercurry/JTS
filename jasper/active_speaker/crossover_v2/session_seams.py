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

**Structural, and deliberately not ``@runtime_checkable``** — see
:class:`~.playback_transaction.PlaybackTransaction` for the rule, which applies
to every Protocol seam: ``isinstance`` against a Protocol compares method NAMES
only, so it would buy confidence it cannot deliver. Method bodies raise
:class:`NotImplementedError` so a partial explicit subclass fails loudly.

**Every release is safe after a partial acquire, and safe twice.** The session
calls :meth:`SessionGraph.restore` and :meth:`VolumeClaim.release` on paths
where the matching acquire may have half-run or not run at all, and it calls
them again if a first attempt raised. Both must therefore be idempotent and
must tolerate being called against nothing held — the same shape MS-11 gives
the fan-in gate, where *an indeterminate select still releases* so one surface
can never strand another's.

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
from typing import Any, Mapping, Protocol

from .playback_transaction import PlaybackTransaction

__all__ = [
    "EngineSeams",
    "RecordStore",
    "SessionGraph",
    "VolumeClaim",
]


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

    **These three verbs are ``async``**, so
    :class:`~.session_graph.MeasurementSessionGraph` satisfies them directly:
    the transport is CamillaDSP over a websocket and every production caller is
    already on the event loop. See ADR-0179 for why all the seams took one
    colour rather than one per seam.
    """

    async def install(
        self,
        inverted_roles: tuple[str, ...] = (),
        measurement_delays_us: Mapping[str, float] | None = None,
        level_trims_db: Mapping[str, float] | None = None,
    ) -> str:
        """Install the graph and return its fingerprint.

        ``inverted_roles`` names the driver branches this stimulus needs
        sign-flipped, ``measurement_delays_us`` how much each named branch is
        delayed — R-1's two halves — and ``level_trims_db`` the per-driver
        attenuation that puts the branches at comparable level, without which a
        cabinet whose drivers differ by ~10 dB of sensitivity can form no deep
        reverse null however well aligned it is. All three are on INSTALL
        rather than on a patch because the fingerprint below must name the
        graph the stimulus actually played through: a flip, a delay or a trim
        applied after the install would leave the record pointing at a
        different graph than the one it measured. Empty on every normal
        capture, and a host that cannot flip, delay or trim a branch simply
        installs what it always did.

        The fingerprint is provenance a record carries — which graph the
        evidence was measured through — never a gate. A host that cannot name
        the graph returns ``""``, and the record says so, because a capture that
        measured the speaker honestly must not be thrown away over a missing
        label.

        **May raise**, and the session treats that as "nothing was installed":
        it releases whatever else it had taken and reports the failure up. An
        install that raises after arming half a graph must therefore leave
        :meth:`restore` able to put that half back.
        """
        raise NotImplementedError

    async def patch(self, changes: Mapping[str, Any]) -> None:
        """Change what one candidate needs, without re-installing.

        The cheap half of *"structural swap once, patch per candidate"*, which
        ships twice already as prior art: the bass bench's swap/patch split and
        ``/correction/``'s ``_load_measurement_baseline``. **Wave 6's named
        slot** — it has no caller in this wave by design, and dropping it here
        would mean re-cutting this seam when that wave arrives.
        """
        raise NotImplementedError

    async def restore(self) -> None:
        """Put back whatever the install displaced.

        Called on every path out of a session, including the failing ones,
        including after an :meth:`install` that raised, and again if a first
        restore raised. Idempotent, and a no-op when nothing is installed.
        """
        raise NotImplementedError


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
    A level ladder moves the STIMULUS, never this claim.

    **The 0 dB ceiling is not this seam's to relax.** ``devices.volume_limit``
    stays ``0.0`` and ``CamillaController.set_volume_db`` clamps positive
    writes; the owner becomes the write door's only caller, never its exception.
    """

    async def acquire(self, level_db: float) -> None:
        """Take the session-measurement claim at the declared level.

        **May raise**, including after registering the claim internally. The
        session's failure path calls :meth:`release` regardless, so a claim that
        half-registered is given back rather than stranded.
        """
        raise NotImplementedError

    async def prove(self) -> float | None:
        """The fader reading, but only when it AGREES with the declared level.

        **MS-14, and the shape ruling S10 preserves.** Returns the number the
        fader actually reads when it is within the confirm tolerance of the
        level this claim was acquired at, and ``None`` otherwise — including
        when the fader could not be read at all, and including when the claim
        has been preempted.

        ``None`` therefore means *not proven*, which refuses to BANK the
        capture and never to play the stimulus or to try again. Returning a raw
        drifted reading instead would hand ``measure`` a number to stamp into a
        record while the speaker played at a different one — the 8.712 dB shape.
        The session compares nothing: one prover, one door.

        The tolerance is
        :data:`~jasper.active_speaker.volume_latch.READBACK_TOLERANCE_DB`, via
        :func:`~jasper.active_speaker.volume_latch.fader_matches` — the repo's
        one *"do these two fader dB values agree?"* test, and the confirm
        tolerance wave 5 collapses the other writers onto.

        **Called once per STIMULUS**, not once per spec: a claim can be
        preempted between two positions of one walk, and a proof taken before
        the walk would stamp an unverified level into every record after it.
        """
        raise NotImplementedError

    async def release(self) -> None:
        """Give the fader back.

        Called on every path out of the session, including after an
        :meth:`acquire` that raised, and again if a first release raised.
        Idempotent, and a no-op when nothing is held.
        """
        raise NotImplementedError


class RecordStore(Protocol):
    """Where this session's evidence lands — the one shape, one writer.

    **ONE record, not one capture record** (the 2026-08-26 FOLD ruling): the
    five ``V2FlowSeams`` publishers fold onto :meth:`bank`, discriminated by
    the record's own ``kind``, so a check, a candidate, a cloud result, a
    finding set and a round receipt land through the same seam a capture does.
    Fail-soft stays in a named wrapper at the caller, never in the store.

    **Write-only, since ADR-0198.** Reading a bank back belongs to the
    doors-and-banks tools, over the bundle's own files. This declares the one
    slot the engine drives, and nothing else.

    **An integrity refusal still banks.** The discriminator is #2087's: would
    measuring again plausibly fix it? Yes, and the capture is a defect that is
    refused **and still banked**. No, and it describes the room, the rig or the
    result — disclose it and recommend, never block.
    """

    async def bank(self, record: Mapping[str, Any]) -> str:
        """Write one record; return the id that finds it again."""
        raise NotImplementedError


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

    **These are ENGINE-INTERNAL, and a front end must not reach through them.**
    The modularity claim §1 makes is that both front ends drive the same four
    verbs; a caller that reached ``session.seams.graph.patch(...)`` or
    ``session.seams.records.bank(...)`` would be doing engine work outside the
    engine, and the second of those would bank a record the session never counts
    in :attr:`~.session.TuningSession.banked_record_ids`. The field is public
    because construction and testing need it; the discipline is that only
    :class:`~.session.TuningSession` calls through it. Wave 2 lands the
    enforcement pin, when there is a front end to point it at.
    """

    graph: SessionGraph
    volume: VolumeClaim
    records: RecordStore
    play: PlaybackTransaction
