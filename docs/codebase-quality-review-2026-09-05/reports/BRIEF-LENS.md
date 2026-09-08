# Phase 2 — cross-cutting lens / scenario brief (read BRIEF.md first)

You sweep the WHOLE tree on one axis (a lens) or follow ONE end-to-end flow across every file and process it
touches (a scenario), ignoring package boundaries. Phase 1 tile reports exist at scratchpad/p1-T*.md and Phase 0
cartography at scratchpad/p0-*.md — read the ones your task names for leads, but re-verify every claim you reuse
against the code at HEAD (cite file:line). Your value is the seam BETWEEN files that no tile owned.

Deliver (≤ 250 lines, tables over prose):
A. One-paragraph verdict on the axis/flow.
B. For a scenario: the happy path as a numbered hop list (file:function per hop, process boundary marked), then
   EVERY failure branch at each hop with: does the error surface (HTTP code / exit code / event= log / /state /
   doctor / cue), get swallowed, or lie?
C. Findings ranked by impact, max 20: severity | file:line | what | evidence | cleanest fix. Mark items already
   in flight (PR/issue number) and items already reported by a tile (cite p1-Txx) — do not re-report those as new,
   but DO say if you confirm, refute, or re-grade them.
D. What only hardware/runtime can prove (be explicit).
E. Coverage: files/functions actually opened.
Scratch scripts go in scratchpad/<your-lens-name>/ — the scratchpad root is shared.
