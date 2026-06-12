# Brief 02 — Privacy: stop the transcript leak, ship PRIVACY.md, close mute gaps

Mission: `docs/REVIEW-2026-06-12-oss-due-diligence.md` §4.2. A smart-speaker
OSS launch is judged on privacy; today household utterances persist verbatim
in the journal and there is no end-to-end privacy statement.

Branch: `codex/privacy`. File fence: `PRIVACY.md` (new), `README.md` (one link
line only), `jasper/voice/openai_session.py`, `jasper/tools/__init__.py`,
`jasper/cli/wake_enroll.py`, `jasper/web/google_setup.py`,
`jasper/web/wake_corpus_setup.py`, plus tests.

## PR 1 — code changes (land first, so PRIVACY.md describes shipped reality)

1. **Demote transcript logging to DEBUG.**
   `jasper/voice/openai_session.py:439` (`event=openai.assistant_transcript`)
   and `:1705` (`event=openai.user_transcript`) log full utterances at INFO
   into persistent journald (and `fetch-pi-logs.sh` copies journals to
   laptops). Change both to `logger.debug(...)`. Rationale to put in a
   comment: the flight recorder (jasper/flight_recorder.py) captures the DEBUG
   ring around failures, so diagnostic value survives while the journal stays
   clean. Keep `user_transcription_failed` (`:1712`) at its current level —
   it carries no content. Check the Gemini/Grok adapters for any equivalent
   content-bearing INFO logs and demote those too if found (scope: transcript
   *content* only, not event markers).
2. **Redact tool payload previews for content-bearing tools.**
   `jasper/tools/__init__.py:329-332` logs `payload=%s` (237-char repr) for
   every tool at INFO — Gmail/calendar payloads put sender/subject/body
   prefixes in the journal. Add an opt-out on the registration path (e.g.
   `@tool(log_payload=False)` or a frozen set of content-bearing tool names)
   so gmail/calendar/home_assistant payloads log as
   `payload=<redacted len=N>`; keep full previews for non-sensitive tools
   (transit, weather, volume). Pin with a test: dispatching a redacted tool
   never emits body text into caplog.
3. **Mute-gate `jasper-wake-enroll`.** `jasper/cli/wake_enroll.py` records the
   same UDP mic legs but never checks the persisted mic-mute flag — the one
   bypass of the PR #119 privacy promise. Mirror RecordingBackend's contract
   (`jasper/wake_corpus/recording_backend.py` `_refuse_if_muted` /
   `MicMutedError`): refuse to start while muted, and stop (with a clear
   message) if mute flips mid-capture. Extend `tests/test_wake_enroll.py`.
4. **Disclose LLM egress in the Google wizard.**
   `jasper/web/google_setup.py` "What this app reads" copy says read-only
   Gmail/Calendar; add one sentence: matched message/event content is sent to
   the household's chosen voice AI provider (Gemini/OpenAI/xAI) to answer the
   question. Keep copy provider-agnostic. Update the wizard's test snapshot if
   one pins the copy.
5. **Corpus retention note.** `jasper/web/wake_corpus_setup.py`: the recorder
   page stores member-named raw clips indefinitely; add a visible line on the
   Begin-a-session card stating where clips live, that they persist until
   deleted, and that recording refuses while the mic is muted.

## PR 2 — PRIVACY.md + link

Write `PRIVACY.md` at repo root (~1 page, plain language, present tense,
provider-agnostic). Cover, with file/path specifics:

- **What leaves the device:** post-wake audio → the configured voice provider;
  tool answers may carry Gmail/Calendar/HA content to that provider; operator
  diagnostics (`jasper/cli/satellite_validation.py` Gemini STT) upload
  captured WAVs when explicitly run. Nothing else phones home; no analytics.
- **What stays on the device:** wake-event WAV ring (1 GB cap, oldest-first,
  `JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES`), wake-corpus clips (uncapped, manual
  delete), spend DB (tokens/cost only, no speech), journald logs (transcripts
  only at DEBUG, see PR 1).
- **Mic mute scope:** which paths honor it (wake legs, telemetry rings, corpus
  recorder, wake-enroll after PR 1) and what it does not cover (e.g. the
  correction sweep mic flow is operator-initiated).
- **Trust boundary:** the management surface is LAN-trusted and
  unauthenticated — point at SECURITY.md for the threat model.
- Link it from README (one line near the top), and add it to
  `docs/doc-map.toml`'s `security-oss-and-maintainer-backlog` subsystem
  (`code` + `docs` arrays) — otherwise
  `tests/test_docs_impact.py::test_root_and_top_level_docs_are_intentionally_mapped`
  fails.

## Acceptance

- `journalctl`-bound INFO logs carry no utterance/message content (caplog
  tests prove it for openai transcripts + gmail dispatch).
- Muted mic blocks wake-enroll start and mid-capture (tests).
- `pytest tests/test_wake_enroll.py tests/test_tools_*.py tests/test_docs_impact.py -q` green;
  `ruff check .` clean.
