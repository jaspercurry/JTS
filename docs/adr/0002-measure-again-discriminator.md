# ADR-0002: "Would measuring again plausibly fix it?" separates a capture defect from a description of the world

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

The owner's 2026-08-03 ruling on
[#2087](https://github.com/jaspercurry/JTS/issues/2087) converted the G1
predicted-ripple check from a refusal into a disclosure. The ruling's
*general* form — the test that decides which of the tuning stack's ~100
integrity refusals may stop a session — exists nowhere in `docs/`. It lives
only in a comment block inside a god file the refactor rewrites
(`jasper/active_speaker/crossover_v2_flow.py:2314-2367`), plus a rule about
what the disclosing code may not do
(`crossover_v2_flow.py:7220-7240`) and a rule about what its household copy
may not say (`jasper/active_speaker/crossover_envelope_v2.py:2205-2237`).
`docs/HANDOFF-crossover-measurement-v2.md:3916-3945` documents the G1
*instance* — the threshold, the wire fields, the incident — but not the
discriminator, and that file is collapsed to a runbook spine by the tuning
refactor's wave 7e.

The refactor plan (`docs/REFACTOR-TUNING-2026-08.md` §1) adopts this test as
the discriminator for its three-way refusal taxonomy — CLAMP stops, INTEGRITY
refuses but must still bank, DISCLOSURE never blocks — and §0's rule 1 requires
the ruling extracted before the code that carries it moves (§6 R7). This ADR is
that extraction; the source prose is quoted rather than restated.

The evidence that settled it, from the live 2026-08-03 bench validation:

> The live 2026-08-03 bench validation is the case that settled it — 15.244 dB
> refused 58 s after an identically-positioned 11.324 dB capture was accepted,
> both at alignment confidence ~0.677, so confidence was never the
> discriminator the reused reason code claimed it was.

The refusal reused `low_alignment_confidence`'s copy, so a household with a
correctly-placed microphone was told to move it
([#2085](https://github.com/jaspercurry/JTS/issues/2085)) and the attempt meter
then ended the session
([#2086](https://github.com/jaspercurry/JTS/issues/2086)).

## Decision

**The discriminator.** Before a check may stop a session, ask whether measuring
again would plausibly fix what it saw. Yes → it is a defect in the capture;
refuse, and still bank. No → it describes the room, the rig, or the result;
disclose and recommend, never block. Quoted from
`crossover_v2_flow.py:2348-2351`:

> This one number stopped being a veto because a bad ripple describes how well
> two branches can sum in this room on this rig — a thing the household cannot
> act on by moving anything — and not a defect in the capture that measuring
> again would fix.

And the failure mode it generalizes, from `crossover_v2_flow.py:2330-2334`:

> **It is a DISCLOSURE TRIGGER, not a gate — the owner's 2026-08-03 ruling on
> #2087.** It refused captures until then, and the refusal was wrong in the way
> a hard quality ceiling is usually wrong: a household whose room and hardware
> simply sit above a corpus collected on better rigs was told to move a
> correctly-placed microphone, and the session died on the attempt meter.

**Corollary 1 — a disclosure may not grow a gate back.** From
`crossover_v2_flow.py:7222-7225`:

> Records the fact and says so in the journal. It decides nothing — the caller
> has already decided to proceed, and this method must never acquire a branch
> that could change that, or the ruling would quietly grow a gate back.

Nor does it relax anything downstream. From the consumption site,
`crossover_v2_flow.py:7318-7326`:

> **This does not refuse.** The capture is ACCEPTED and carries an honest
> reservation to the household instead of sending them to move a microphone
> that was never the problem, so the reservation changes what the household is
> TOLD and nothing about what is built, fitted, gated, or applied. Every
> accountability gate below still runs unchanged on this candidate, which is
> what keeps "proceed" from meaning "unchecked".

**Corollary 2 — a disclosure names what was observed, never a cause the session
did not separate.** From `crossover_envelope_v2.py:2217-2223`:

> **It names no cause, and that is the load-bearing part.** A high predicted
> ripple is consistent with the room, the microphone, the recording chain, and
> the speaker itself, and this session separated none of them — so the copy
> reports what the measurement saw and stops.

**Corollary 3 — a disclosure qualifies the evidence, never the result.** From
`crossover_envelope_v2.py:2233-2237`:

> **It claims the tuning is rougher EVIDENCE, never a worse RESULT.** The
> measurement says how coherently two branches summed, not how the speaker will
> sound, and the accountability gates that grade the correction itself all
> still ran.

Converting a check does not recalibrate it: *"Nothing about the threshold's
calibration changed; only what crossing it does."* The threshold survives as
the disclosure trigger, and the diagnostic field that carries it takes
disclosure vocabulary (`ripple_disclosure`) rather than a refusal's, because
leaving a refusal's words on an accepting path misleads the reader that field
exists for.

What the ruling did **not** move, stated because a reader will ask
(`crossover_v2_flow.py:2344-2347`): the alignment trust floor, the
delay-plausibility backstop, and the SNR / linearity / glitch verdicts still
refuse. Those are capture defects — measuring again is exactly what fixes them.

## Consequences

- The tuning engine's refusal taxonomy has a decidable test rather than a
  tradition. A new check must answer the question before it may raise; a check
  that answers "no" is a nanny wearing a refusal's costume, and the charter's
  closed non-negotiables list is the only thing that overrides the test.
- INTEGRITY refusals must still bank. A refusal that discards its evidence
  costs a re-measure *and* the record of why, which is the one outcome the
  discriminator cannot repair.
- Deliberately given up: the guarantee that a household never sees a tuning
  built on unusually incoherent evidence. They see it, and they are told, in
  one plain sentence.
- Rejected: keeping a corpus-derived quality ceiling as a veto. A threshold
  calibrated on clean rigs refuses hardest in exactly the rooms that most need
  the help, and it cannot tell "this room is hard" from "this capture is bad".
