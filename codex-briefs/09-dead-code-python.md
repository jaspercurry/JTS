# Brief 09 — Delete the dead Python layers (web legacy + voice session)

Mission: review §5.2 — documented-dead code that misleads navigation. AGENTS.md
itself calls the web block "a removal candidate". Deletion-only PRs: no
behavior changes, no refactors-while-here.

Branch: `codex/dead-code`. File fence: `jasper/web/_common.py` (legacy block
only), `tests/test_web_common.py` (its pinning tests), `jasper/voice/session.py`,
`jasper/voice/gemini_session.py`, `tests/test_gemini_session.py`,
`jasper/wake.py`, AGENTS.md (ONLY the two paragraphs describing the deleted
legacy primitives — leave everything else to brief 06; coordinate via rebase
if both are open).

NOTE: brief 04 adds a new guard helper near the top of `_common.py`; you are
deleting a block elsewhere in the file. Different regions — rebase merges
cleanly; just rebase before push.

## PR 1 — `_common.py` legacy design system (~360 lines, ~27% of the file)

- Delete `wrap_page`, `PAGE_STYLE`, `NAV_BACK_HTML`/`NAV_BACK` + its CSS,
  `TOGGLE_CSS`, `DIALOG_CSS`, `dialog_helpers_js` — zero product callers since
  the canonical_page migration completed (verify with grep before each
  deletion: the only hits should be _common.py itself, their pinning tests,
  and AGENTS.md prose). **Keep `toggle_html`** — it's still live; it renders
  markup styled by app.css (the deleted TOGGLE_CSS was a drifted stale twin —
  54×30px vs the live 44×24px).
- Delete the pinning tests in `tests/test_web_common.py` (the wrap_page
  contract tests, `test_canonical_banner_classing_mirrors_wrap_page`, dialog
  twin tests). Re-read each test before deleting: if it pins something that
  survives (e.g. canonical_page's banner classing on its own), rewrite it
  against the surviving symbol instead of deleting.
- Fix the two adjacent nits in the same file while the diff is open (they're
  inside the deleted/adjacent region): the false "lazy" comment over the
  duplicate bottom-of-file `import json as _json` (move imports to top, drop
  the alias), and — leave the CSRF `http_only` question ALONE (brief 04's
  territory if anyone's).
- Update the AGENTS.md "Web wizard conventions" paragraphs that describe the
  legacy primitives as "unused but not yet deleted" → now deleted.

## PR 2 — dead voice session layer

- `jasper/voice/session.py`: delete the `VoiceSession` Protocol (the legacy
  ~241-314 block) — zero production consumers (verify:
  `grep -rn "VoiceSession" jasper/ tests/`). Keep `LiveConnection`/`LiveTurn`
  (the live protocols) untouched.
- `jasper/voice/gemini_session.py`: delete the legacy `GeminiLiveSession`
  class (~326 lines at the file tail) — only `tests/test_gemini_session.py`
  references it. Port the still-valuable assertions in that test file to
  `GeminiLiveConnection` (the live adapter) rather than deleting coverage
  wholesale; drop tests that only exercise the dead class's plumbing.
- `jasper/wake.py`: delete the uncalled `feed()` method (verify zero callers).
- Run the full hardware-free suite for the voice package:
  `pytest tests/test_gemini_session.py tests/test_voice_daemon*.py
  tests/test_wake*.py -q`.

## What NOT to do

- Do not strip the 495 vestigial `# noqa: BLE001` markers here — that
  tree-wide churn is queued LAST (see README wave 2) to avoid conflicting
  with every open PR.
- Do not touch `rust/jasper-outputd`'s "stranded" modules — the 2026-06-12
  TTS work may have revived them; that's a wave-2 re-verify task.
- No API renames, no docstring rewrites beyond the deleted-thing references.

## Acceptance

- `grep -rn "wrap_page\|PAGE_STYLE\|DIALOG_CSS\|dialog_helpers_js\|VoiceSession\|GeminiLiveSession"
  jasper/ deploy/ tests/` → no hits (docs prose may mention history; AGENTS.md
  updated).
- `pytest tests/test_web_common.py tests/test_web_wizard_conventions.py
  tests/test_gemini_session.py -q` green; `ruff check .` clean (deletions often
  orphan imports — clean them).
