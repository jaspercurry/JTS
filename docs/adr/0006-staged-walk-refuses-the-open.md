# ADR-0006: A staged request the session cannot honour refuses the open — it never degrades to a different session

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

An operator stages an angle walk as a single-use document; the session reads it
at open. When the document is unreadable, out of contract, or names a stop the
seam will not accept, the code used to journal the problem and return `None`,
which is also what "nothing was staged" returns — so the session opened in its
ordinary shape.

The ruling that ended that lives in one function docstring,
`jasper/web/correction_crossover_v2.py:2997-3029`, and nowhere else. The
nearest doc, `docs/testing-tooling.md:2540-2545`, still describes the
superseded journal-and-degrade behaviour.

This ADR extracts the ruling before the code moves
(`docs/REFACTOR-TUNING-2026-08.md` §0 rule 1, §6 R7). It matters more after
the refactor than before it: the LLM-plus-robot-arm front end is a staged-request
producer by construction, and it has no operator watching a WARNING line.

## Decision

**A staged request the session cannot honour refuses the open.** Quoted from
`correction_crossover_v2.py:3002-3016`:

> **A staged walk this session cannot honour REFUSES THE OPEN**
> (:class:`CrossoverV2Refused`), with the producing module's own slug in the
> sentence. …
>
> It used to journal every one of them and return ``None``, and the session
> then opened in its ordinary 3-capture shape: an operator who staged a walk
> got a measurement that silently answered a different question, with the only
> evidence a WARNING line on a box they were not reading. A refusal costs
> nothing here — this runs before any state is opened — so the loud direction
> is also the cheap one. The document is single-use either way, so the operator
> restages after fixing what was named.

Three parts bind:

1. **Answering a different question is the failure mode**, not losing the
   walk. A session that quietly measures something else produces a record that
   looks valid and is not.
2. **Refuse where a refusal is free.** Before any state is opened, the loud
   direction and the cheap direction are the same one; that is what makes this
   a refusal rather than a disclosure under ADR-0002's test.
3. **`None` means one thing.** *"``None`` means NOTHING WAS STAGED — an
   ordinary session — and nothing else."* A sentinel that also carries "and
   something went wrong" is how the degrade path existed at all.

**A consumption record is read back, never asserted.** From
`correction_crossover_v2.py:3022-3025`:

> ``consumed`` on the journal line is READ BACK from the spool, never
> asserted: its two unreadable arms deliberately do not consume, so a
> permissions mistake refuses every session until it is fixed rather than
> silently destroying the evidence of itself.

## Consequences

- The engine's `measure` verb inherits the rule at its open: a preset or
  prescription it cannot serve refuses, naming the producing module's slug, and
  does not fall back to a default shape.
- A front end that stages requests must expect refusals at open and restage.
  That is cheap for both front ends — the document is single-use either way.
- Deliberately given up: sessions that "work anyway" when the staged request is
  broken. Those sessions were producing records that answered a question nobody
  asked.
- `docs/testing-tooling.md:2540-2545` contradicts this ruling and needs the
  correction when a wave next touches it.
