# Brief 06 — Kill the verified doc-drift list

Mission: review §4 (Phase 0.8) + §5.3 — every item below was verified against
`main @ 6772b81a`. These are the docs newcomers follow first; several actively
mislead (one stale path is migrated away by install.sh *because it breaks
voice*).

Branch: `codex/doc-drift`. File fence: `AGENTS.md`, `README.md`, `BRINGUP.md`,
`QUICKSTART.md`, `docs/HANDOFF-aec.md`, `docs/HANDOFF-xvf3800.md`, `PLAN.md`.
Do NOT touch CONTRIBUTING.md (brief 03) or SECURITY.md (brief 04). README
gets one added line from brief 02 (PRIVACY link) — different region, merges
cleanly.

**Re-verify each item against current main before editing** — AGENTS.md and
the multiroom docs changed on 2026-06-12; some items may be partially fixed.
For every claim you correct, re-check the truth in code first (rule: docs can
drift, code is truth). One PR is fine; group commits by file.

## AGENTS.md

1. TTS socket path: `JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-outputd/tts.sock`
   (~line 779) is stale — code default is `/run/jasper-fanin/tts.sock`
   (jasper/config.py, jasper-voice.service). ⚠️ The 2026-06-12 outputd-TTS
   work may have changed this again — read `jasper/config.py` and
   `deploy/systemd/jasper-voice.service` FIRST and write what's true now.
   Same fix in BRINGUP.md (~line 1150).
2. "Enable multi-leg wake OR-gate" section (~1955-1972) tells operators to
   hand-append `JASPER_MIC_DEVICE_RAW/_DTLN` to `/etc/jasper/jasper.env` —
   contradicting the reconciler-single-writer rule the same file establishes,
   and install migrations strip those vars. Rewrite the section to use the
   /wake/ toggles or `POST /aec/leg`, per the profile system.
3. "Architecture in one paragraph" (~2182-2186) says "up to three UDP
   streams"; the bridge defines five+ output ports (chip beams :9887/:9888,
   corpus :9879). Update to the current port topology or replace the list
   with a pointer to the bridge module docstring.
4. Stale claim "the JTS project venv is 3.9" (~2506) — pyproject floors at
   3.11 and the actual venv is 3.12/3.13; re-verify whether the separate
   PIO venv dance is still needed and rewrite accordingly.
5. The two duplicated behavior-rule blocks (lines ~24-59 vs ~2697-2812)
   overlap; merge into one section with project-specific reinforcements as
   sub-bullets. Keep every rule that appears in either copy.

## README.md

6. Delete/rewrite the stale "Known marginal items" paragraph (~303-308): chip
   AEC "isn't usable" contradicts the recommended-profile sections; the
   0.7-second refractory is now 0.2 (`WAKE_REFRACTORY_SEC`); the RAM figure
   contradicts the resource table 600 lines later.
7. Transit extension bullet (~222-230): claims "~9-12 edits… three
   voice_daemon.py wiring edits"; the canonical checklist
   (jasper/transit/__init__.py docstring) is 7 items with NO voice_daemon
   edit. Replace with the 7-item summary, defer to the docstring.
8. Repo-layout tree (~347-388): add `firmware/satellite-amoled/`, fix the
   dial's "phase 1: volume only" note, mark curated listings with ellipses.
9. The multiroom atlas entry (~530) said "Proposed design (not yet built)"
   while the HANDOFF ships a working dataplane — re-verify against the
   current HANDOFF-multiroom (it changed 2026-06-12) and align.
10. Rule-number nit: "per AGENTS.md rule #9" (~720) should be #10 (historical
    tagging) — or cite by rule name so renumbering can't re-break it.

## Cross-doc anchors

11. Seven dangling "BRINGUP.md Phase 2A.5 / 2A.2" references — QUICKSTART.md
    ~371, README.md ~914, AGENTS.md ~1886+1922, docs/HANDOFF-aec.md ~2071,
    docs/HANDOFF-xvf3800.md ~201+1421 — point at headings renamed away
    ~2026-05-07 (DFU flashing now lives under "XVF firmware: switch to
    6-channel variant via DFU"). Repoint all seven as real markdown links to
    the current heading so the docs-links CI guards them from now on.
12. PLAN.md: the "⚠️ Urgent" banner dated 2026-05-11 predates the bridge
    fixes that landed 2026-05-19; add a dated status line (open /
    mitigated-by / superseded) rather than deleting history.

## Acceptance

- `python3 scripts/docs-linkcheck.py` (or the repo's invocation — check the
  docs-links workflow) passes over the edited files; every corrected claim
  names its code source in the commit message.
- `pytest tests/test_docs_impact.py tests/test_docs_handoff_freshness.py
  tests/test_doc_staleness_sweep_20260604.py -q` green — the staleness-sweep
  test pins some README/AGENTS sentences; if an edit trips it, update the
  test's pinned expectation in the same commit with a note.
- Bump `Last verified:` footers on the two HANDOFFs you touched.
