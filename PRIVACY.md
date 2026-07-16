# Privacy

JTS is a household LAN speaker, not a cloud service. It has no project
analytics, telemetry account, or background "phone home" channel. The
integrations you configure still contact their own upstream services when you
enable or use those features.

## What Leaves The Device

After the wake word fires, the live voice turn sends microphone audio from the
wake interaction, including up to about 0.6 seconds of audio captured
immediately before the wake word fired, to the configured voice provider
selected by `JASPER_VOICE_PROVIDER` in `/var/lib/jasper/voice_provider.env`
(`gemini`, `openai`, or `grok`). That provider performs the realtime
speech-to-speech turn.

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
(`JASPER_USAGE_DB`). It stores provider usage, token counts, estimated cost,
and time-billed connection intervals. It does not store speech audio or
transcripts.

Audio troubleshooting history lives in
`/var/lib/jasper/audio_health_incidents.json`. It is a local-only ring capped
at 20 incident records. Its allowlisted health evidence contains no audio,
speech transcripts, or track metadata.

Conversation history is separate from spend accounting. When
`JASPER_CONVERSATION_CAPTURE=1` (or the matching wizard-owned
conversation-history env file enables capture), JTS stores text-only turns in
`/var/lib/jasper/conversation_history.db` by default
(`JASPER_CONVERSATION_HISTORY_DB`): the perceived user command transcript, the
assistant transcript or research report, provider/session metadata, and optional
feature metadata such as a research job id. It never stores speech audio in this
database, and capture is skipped while the voice assistant is paused. Capture is
default-off; retained rows stay on the speaker, are pruned by the configured
conversation-history retention window and row cap, and can be cleared from
`/chat/`.

System logs stay in journald on the speaker unless an operator exports them,
for example with `scripts/fetch-pi-logs.sh`. OpenAI transcript events log
metadata such as character counts, not transcript text; that keeps both normal
INFO logs and flight-recorder DEBUG dumps free of household utterances.
Content-bearing tool payload previews for Gmail, Calendar, and Home Assistant
are redacted at INFO, and Home Assistant's natural-language tool argument is
also redacted.

## Voice Assistant Pause and USB Microphone Scope

The dashboard's **Voice assistant** Pause control persists its legacy internal
flag at `/var/lib/jasper/mic_mute.env`. When paused, the normal wake/audio legs
do not feed a voice turn, wake-event telemetry records the paused state instead
of treating it like an ordinary listening event, the wake-corpus recorder
refuses to start and stops if pause is enabled mid-recording, and
`jasper-wake-enroll` refuses or stops the same way.

Pause is not a hardware-wide microphone mute. If the household has explicitly
enabled **Use JTS as a computer microphone** on `/wake/`, that independent
switch is the sole end-user authority for the USB export and audio continues
while the voice assistant is paused. The USB microphone preference is off by
default; when it is on, live room audio leaves the Pi only across the physically
connected USB cable and is consumed by whichever computer app opens that input.

Voice-assistant pause also does not cover every operator-initiated measurement
path. Room correction and active-speaker sweep flows are explicit
setup/calibration actions that use the browser or measurement mic after the
operator starts them.

## Trust Boundary

The management surface is designed for a trusted household LAN and is not a
multi-user authenticated web app. Local setup pages and controls are meant to
be used by people who already control the speaker and network. See
[SECURITY.md](SECURITY.md) for the current threat model, reporting path, and
known LAN-trust limitations.
