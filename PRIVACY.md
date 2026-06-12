# Privacy

JTS is a household LAN speaker, not a cloud service. It has no project
analytics, telemetry account, or background "phone home" channel. The
integrations you configure still contact their own upstream services when you
ask for those features.

## What Leaves The Device

After the wake word fires, the live voice turn sends post-wake microphone audio
to the configured voice provider selected by `JASPER_VOICE_PROVIDER` in
`/var/lib/jasper/voice_provider.env` (`gemini`, `openai`, or `grok`). That
provider performs the realtime speech-to-speech turn.

Voice tools may also send tool results back to that same voice provider so it
can answer the question. For example, Gmail and Calendar tools are read-only
against Google, but matched message or event content can be included in the
tool result that the voice provider sees. Home Assistant responses can also be
included when the household enables that integration.

Operator diagnostics can upload audio when explicitly run. In particular,
`jasper/cli/satellite_validation.py` uses Gemini STT/TTS for satellite
validation and can send captured WAVs to Gemini as part of that diagnostic
workflow.

## What Stays On The Device

Wake-event telemetry lives under `/var/lib/jasper/wake-events/`, including
`wake-events.sqlite3` plus per-event WAVs. Audio is an oldest-first ring capped
by `JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES` (default 1 GB). SQLite rows remain; WAV
paths are marked rolled off when audio is pruned.

Wake-corpus and wake-enrollment clips are raw WAVs saved under the configured
wake-corpus directory (`/var/lib/jasper/enrollment_positives/` on an installed
speaker; `data/enrollment_positives/` for the standalone CLI/dev default) with
member names in filenames. This corpus is not size-capped; it stays until an
operator deletes clips or sessions.

Spend accounting lives in `/var/lib/jasper/usage.db` by default
(`JASPER_USAGE_DB`). It stores provider/model usage, token counts, estimated
cost, and time-billed connection intervals. It does not store speech audio or
transcripts.

System logs stay in journald on the speaker unless an operator exports them,
for example with `scripts/fetch-pi-logs.sh`. OpenAI transcript events log
metadata such as character counts, not transcript text; that keeps both normal
INFO logs and flight-recorder DEBUG dumps free of household utterances.
Content-bearing tool payload previews for Gmail, Calendar, and Home Assistant
are redacted at INFO, and Home Assistant's natural-language tool argument is
also redacted.

## Mic Mute Scope

The persisted household mic mute flag lives at `/var/lib/jasper/mic_mute.env`.
When it is on, the normal wake/audio legs do not feed a voice turn, wake-event
telemetry records the muted state instead of treating it like an ordinary
listening event, the wake-corpus recorder refuses to start and stops if mute is
enabled mid-recording, and `jasper-wake-enroll` refuses or stops the same way.

Mic mute does not cover every operator-initiated measurement path. Room
correction and active-speaker sweep flows are explicit setup/calibration
actions that use the browser or measurement mic after the operator starts them.

## Trust Boundary

The management surface is designed for a trusted household LAN and is not a
multi-user authenticated web app. Local setup pages and controls are meant to
be used by people who already control the speaker and network. See
[SECURITY.md](SECURITY.md) for the current threat model, reporting path, and
known LAN-trust limitations.
