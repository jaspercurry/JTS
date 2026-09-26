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
(`gemini`, `openai`, `openai_live`, or `grok`). That provider performs the realtime
speech-to-speech turn.

Endpointed adapters close the conversation as soon as playback drains. GPT-Live
streams the admitted microphone during the active conversation, including pauses
and assistant speech, waits up to five seconds for an answer to start after each
user utterance, and holds it open for a follow-up after playback drains (two
seconds by default; `JASPER_FOLLOWUP_TIMEOUT_SEC=0` skips this follow-up wait);
its session closes when JTS ends that conversation. The closing chirp marks listening ending.

Voice tools may also send tool results back to that same voice provider so it
can answer the question. For example, Gmail and Calendar tools are read-only
against Google, but matched message or event content can be included in the
tool result that the voice provider sees. Home Assistant responses can also be
included when the household enables that integration.

Operator diagnostics can upload audio when explicitly run; those commands
describe the provider and data path before capture or upload.

## What Stays On The Device

Wake-event telemetry lives under `/var/lib/jasper/wake-events/`, including
`wake-events.sqlite3` plus per-event WAVs. Audio is an oldest-first ring capped
by `JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES` (default 128 MiB). SQLite rows remain; WAV
paths are marked rolled off when audio is pruned, and a row whose audio is gone
is deleted at the first voice-daemon start after it turns a year old.

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

Opt-in USB gadget forensics is also local-only and contains no audio or
application content. Its RAM timeline records controller, interrupt, and USB
network counters; deliberate captures retain only a bounded tail plus the
gadget's technical state.

System logs stay in journald on the speaker unless an operator exports them,
for example with `scripts/fetch-pi-logs.sh`. OpenAI transcript events log
metadata such as character counts, not transcript text; that keeps both normal
INFO logs and flight-recorder DEBUG dumps free of household utterances.
Content-bearing tool payload previews for Gmail, Calendar, and Home Assistant
are redacted at INFO, and Home Assistant's natural-language tool argument is
also redacted.

## Conversation History

Capture is **default-off**. The household can enable it or clear all saved
turns at `/assistant/chat/`. This is a shared LAN history, not a per-member
account or a text-chat input. Turning capture off does not delete saved rows.

The wizard writes `JASPER_CONVERSATION_CAPTURE` in
`/var/lib/jasper/conversation_history.env`. The capture writer reads this file
fresh for each write; the file overrides the process setting. Capture is skipped
when the turn is marked voice-assistant-paused. This control does not stop the
cloud voice service from receiving audio during an otherwise active turn.

History uses `/var/lib/jasper/conversation_history.db`
(`JASPER_CONVERSATION_HISTORY_DB`), separate from `usage.db` and wake-event
storage. Each row contains an id, UTC timestamp, provider, nullable perceived
user transcript, nullable assistant transcript, optional JSON data, and a
nullable usage-session id. The perceived command is what speech recognition
heard, not a verified account of what was said. The reserved `tool_calls_json`
column is currently null in production writes; tool arguments are not saved
there. This database stores no audio.

Text comes from the active provider's native events; history adds no separate
speech-to-text pass. OpenAI and Grok use the shared transcript path. Gemini
requests input and output transcription and also records a `voice_turn` marker,
a transcript-availability flag, and tool names when present. GPT-Live stores
user/assistant transcript deltas with start/end times and voice-usage data in
addition to the text. Missing provider text stays null; a turn can contain only
metadata. The page displays that absence instead of inventing a transcript.

Rows stay on the speaker; history has no upload or cloud-sync path. People on
the trusted household LAN can read them through the page and
`/assistant/chat/data.json`. The JSON response (`schema_version: 1`) contains
capture/store status, retention settings, statistics, and newest-first turns.
It accepts a `since` timestamp and a `limit` (default 50, capped at 200).
Provider/session and JSON fields are included in these rows; the page is not
an access-control boundary. See the trust boundary below.

After each successful capture write, retention removes rows older than
30 days and keeps at most 500 rows by default. Configure these limits with
`JASPER_CONVERSATION_HISTORY_RETENTION_DAYS` and
`JASPER_CONVERSATION_HISTORY_MAX_ROWS`; blank or zero disables the respective
limit. Pruning is write-triggered, not a background expiry timer. Store or
pruning failures are reported without blocking the voice turn, so retention
is best effort. Clear-all deletes the stored rows; there is no per-row delete
control in the UI. Capture does not put transcript text in system logs.

Implementation: [store and settings](../jasper/conversation_history.py),
[capture writer](../jasper/voice/conversation_capture.py), and
[web/API](../jasper/web/chat_setup.py). The design decision is
[ADR-0337](adr/0337-conversation-history-is-local-opt-in-native-text.md).

## Voice Assistant Pause and USB Microphone Scope

The dashboard's **Voice assistant** Pause control persists its legacy internal
flag at `/var/lib/jasper/mic_mute.env`. When paused, the normal wake/audio legs
do not feed a voice turn, wake-event telemetry records the paused state instead
of treating it like an ordinary listening event, the wake-corpus recorder
refuses to start and stops if pause is enabled mid-recording, and
`jasper-wake-enroll` refuses or stops the same way.

Pause is not a hardware-wide microphone mute. If the household has explicitly
enabled **Use JTS as a computer microphone** on `/assistant/wake/`, that independent
switch is the sole end-user authority for the USB export and audio continues
while the voice assistant is paused. The USB microphone preference is off by
default; when it is on, live room audio leaves the Pi only across the physically
connected USB cable and is consumed by whichever computer app opens that input.
The adjacent source selector normally follows JTS voice but can explicitly
export a supported XVF's raw physical microphone for comparison, without echo
cancellation. That raw choice changes only the USB cable export; it does not
change voice, wake, or cloud-provider routing.

Voice-assistant pause also does not cover every operator-initiated measurement
path. Room correction and active-speaker sweep flows are explicit
setup/calibration actions that use the browser or measurement mic after the
operator starts them.

## Trust Boundary

The management surface is designed for a trusted household LAN and is not a
multi-user authenticated web app. Local setup pages and controls are meant to
be used by people who already control the speaker and network. See
[SECURITY.md](../SECURITY.md) for the current threat model, reporting path, and
known LAN-trust limitations.
