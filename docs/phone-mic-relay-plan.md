# Phone-mic capture relay — design & build plan

> **Status: DEPLOYED, fresh-install default, hardware-validation pending.** The
> transport (capture-spec schema, Cloudflare Worker + R2 relay, static capture
> page, Pi-side session/poll/pull/decrypt/verify) and the daemon adapters are
> implemented, deployed at `capture.jasper.tech` / `relay.jasper.tech`, and
> covered by hardware-free tests. Fresh installs seed
> `JASPER_CAPTURE_RELAY_BASE=https://relay.jasper.tech` and
> `JASPER_CAPTURE_ORIGIN=capture.jasper.tech`; operators can set
> `JASPER_CAPTURE_RELAY_BASE=disabled` to keep the older on-Pi same-origin path,
> or replace the relay/capture origin with a self-hosted deployment. Blank legacy
> values are repaired to the public defaults during install/update so a stale
> Pi does not silently fall back to local HTTPS.
>
> **Kinds wired today:** room correction (`POST /relay/level-match` then
> `/relay/capture`), active crossover (`POST /crossover/level-match` then
> `/crossover/relay-capture`), and **sync**
> (`POST /sync/relay-capture`) — both ride one kind-agnostic seam
> (`RelayCaptureKind` + `_run_relay_capture` in `correction_setup.py`); a new kind
> is a descriptor, not a new handler. **A USB-C measurement mic plugged into the
> phone is supported:** the room level-check page runs a guided setup on
> `capture.jasper.tech` (permission → mic choice → calibration choice). Room's
> position count is speaker-owned and is not collected from the phone.
> Level-ramp specs set `setup_validation=true`, so vendor serial lookup
> / calibration-file parsing preflights through the Pi before the phone shows
> Start. The full setup is sent exactly once and frozen under a session-scoped
> SHA-256 binding for the level stream. Later Room links are signed capture-only
> specs carrying the Pi-owned position/total; their authenticated `armed` event
> reports the realized device, which the Pi compares with the level-check
> microphone and calibration before sound. Raw serials/calibration text are not
> persisted in browser storage or repeated through the relay. Crossover retains
> its compact setup binding under the Active-owned flow. Calibration is still
> applied
> **Pi-side during analysis, never at record
> time** (it's a post-hoc FR correction in
> `jasper.correction.acoustic_quality.analyze_capture`);
> the phone records raw and reports *which* mic it used in the opaque `armed`
> event. A device-aware gate (`_relay_device_calibration_block`, before playback
> and again after capture) refuses a vendor curve on the phone's built-in mic but
> allows it for the matching USB mic. Supported measurement-mic model options
> are Pi-owned data in
> the `CaptureSpec`, derived from `SUPPORTED_MODELS`, so adding a mic is a Pi
> registry change rather than a separate Cloudflare page edit. The phone also
> records a passive noise-floor window before the Pi plays anything, and the Pi
> publishes `sweep_complete` so the phone stops from real sweep progress rather
> than a fixed timer.
> The crossover kind additionally holds a 14-second controlled quiet interval
> before every role-sized ESS and carries the entire raw WAV back to the Pi.
> A bounded signal locator selects separate, real, equal-length signal and
> quiet crops after relay latency. The Pi runs both through the same regularized inverse,
> applies the signal-owned arrival window and reflection gate to both (ambient
> noise never chooses its own random argmax), applies the same calibration
> domain. It never guesses a prefix, tiles noise, zero-pads a counterfactual,
> or lets noise select an IR argmax. It admits driver evidence through the
> three-repeat kernel (one bounded fourth try; at least two accepted). The
> protected level ramp therefore sets playback headroom only and never supplies
> the acoustic SNR verdict. An authenticated phone-activity watchdog covers the
> full quiet interval and playback: backgrounding or an expired recorder
> cancels the host task, kills/reaps `aplay`, and completes volume rollback
> before household audio resumes. Selecting a UMIK-2 preselects the UMIK-2 model only;
> it does not auto-match a calibration. Browser labels do not reliably expose
> the serial, so the operator enters it and explicitly validates the vendor
> calibration once; after validation, the existing
> Pi-side bound setup carries that calibration into later
> driver legs without placing the raw serial in browser storage.
>
> The Pi poller has two bounded 120-second phases: the initial operator/page
> wait must reach a validated `armed` event, then that event refreshes the
> deadline exactly once for host playback and encrypted upload. Repeated
> `armed` state cannot renew it. These Pi-side clocks are separate from the
> relay's 900-second privacy TTL and the phone recorder's kind-specific 30- or
> 45-second hard deadline. Timeout diagnostics distinguish “never armed” from
> “armed but never uploaded” so `/status.relay` and structured logs preserve
> the failed phase.
>
> **Cooperative host Stop — LANDED (2026-07-14):** Crossover level and sweep
> relays expose one host Stop control. Stop sets the owning run to `stopping`
> and signals the same worker that owns polling and the armed callback. The
> global relay slot remains held while that worker cancels/reaps playback,
> completes graph and exact-or-emergency volume restoration, drains its relay
> cleanup, and exits. Only then may the host publish terminal `stopped` and
> admit another action. A Stop observed before or after a relay poll prevents a
> later armed callback. A meter-only level ramp atomically chooses Stop or
> non-stoppable `committing` after its protected ramp and volume restoration; it
> has no WAV upload phase. A sweep instead chooses Stop or non-stoppable
> `finishing` after playback and rollback while the phone closes, encrypts, and
> uploads; this prevents host DELETE from racing an in-flight relay PUT. A
> verified upload then moves `finishing` to non-stoppable `committing` until
> evidence persistence is terminal. Explicit operator Stop is expected control
> flow: a level ramp posts terminal `ramp.state="cancelled"`, a sweep posts
> `sweep_cancelled`, both are logged as `capture_relay.stopped`, and neither
> plays a failure cue. Timeout, phone abort, integrity failure, and failed
> cleanup remain failures under §12.
>
> **Crossover relay kind — LANDED (P7, 2026-07-03):** `POST
> /correction/crossover/relay-capture` (the third `RelayCaptureKind` caller)
> plays the driver/summed capture sweep on `armed` — reading the play payload's
> REAL shape (top-level `status` + nested `playback.audio_emitted`, top-level
> `test_level_dbfs`/`sweep_meta`, the same read as the same-origin JS) — and
> feeds the verified WAV into the same `record_*_capture` analysis. Measurement mutual-exclusion is
> server-computed twice (refused at POST while room/balance/sync is active,
> re-checked at armed time); the `crossover_sweep` spec floors the phone's hard
> recording deadline at 30 s (`hard_timeout_ms`, the `room_sweep` contract).
> Lane D raises the crossover-only floor to 45 s so its controlled 14 s quiet
> capture, per-driver sweep, config load, and `sweep_complete` round trip cannot
> race the phone deadline; the room and sync relay floors remain 30 s. The
> preceding near-field level step owns microphone
> and calibration setup; its identity is bound to the protected applied speaker
> profile. Driver capture reuses that in-memory binding. The production summed
> branch reopens the same calibration id and recorder SHA-256 from the durable
> comparison set, validates the phone-reported realized device, and records the
> authenticated `summed_reference_axis_v1` acknowledgement. Its browser body is
> exactly `{kind:"summed"}`: region, polarity, graph, delay, attempt, ordinal,
> and admission remain server-owned. The shared crossover relay transport owns
> phone liveness, Stop/drain/purge, and finishing/commit phases; the existing
> Active commissioning host owns the normal/reverse/bounded-delay sequence and
> transient graphs. Legacy direct summed routes remain pre-audio refused.
> The
> acoustic proof (real-driver sweep + phone `getUserMedia`/CSP) is parked as
> H2. **Sync relay fixed in the same pass (pre-existing):**
> `sync_flow.relay_run_and_consume` never posted `sweep_complete` and
> `build_sync_marker_spec`'s bare 3.4 s window doubled as the phone's hard
> deadline — together they deadline-killed every sync relay capture; the sync
> path now publishes `sweep_started`/`sweep_complete` (after marker playback
> truly ends) and carries the same 30 s deadline floor. **Deferred (seam-ready,
> documented):** **balance burst** — needs new
> non-interactive L/R level analysis (no existing consumer; the live balance flow
> is interactive); the **room-correction page's forked capture helper** — AGENTS
> forbids migrating it onto the shared module without an on-device browser pass.
> The **sync relay** still requires the sync session to be started first from a
> device that can reach the Pi (the cert-blocked phone can't press Start) — a
> 2-device flow; a phone-only bootstrap (like the room relay's auto-create) is a
> follow-up.
>
> **Still owed before operational:** (a) **on-device validation** on a real iPhone
> (Safari) + Android (Chrome) — the capture-page device picker's
> enumerate/permission/label behavior, the live `getUserMedia`/CSP/Wake-Lock path,
> and the background sweep/marker playback are unit-tested only; (b) tuning
> `JASPER_CAPTURE_ALIGNMENT_THRESHOLD` against on-device sweeps; (c) an audible failure cue
> (jasper-web → jasper-voice bridge; failures currently surface on the capture page
> + `/status.relay` + `event=capture_relay.*` logs). **Validate on jts3/jts5, never
> the production jts.local.** The current operational truth for the on-Pi
> `/correction/`, `/balance/`, `/sync/` flows remains
> [HANDOFF-correction.md](HANDOFF-correction.md) (same-origin self-signed-cert
> capture). **Once fully validated, convert this doc to the HANDOFF shape with a
> `Last verified:` footer.**

---

## 1. The problem

The room-correction flow (and `/balance/`, `/sync/`, crossover) needs to capture
the user's **phone microphone in a web browser**, on **iOS Safari and Android
Chrome**, with **no per-user setup**. Browsers only expose `getUserMedia` in a
**secure context** — a page served over HTTPS with a **publicly-trusted
certificate** (or `localhost`). The JTS speaker is a **LAN-only Raspberry Pi
behind home NAT with no publicly-trusted cert**.

Today's stopgap (a self-signed cert on the Pi) is not a both-platforms answer:

- **iOS Safari:** the user can tap "proceed" through the warning and the mic
  works (a per-site cert exception). Fragile, and Apple keeps tightening it.
- **Android Chrome:** **blocks the microphone on cert-error origins** — there is
  no click-through that yields a working mic.

Goal: a path that works on both platforms, needs no per-phone setup, requires no
trusted cert on the Pi, and is **maintainable by a solo open-source maintainer
for hundreds-to-thousands of independent builders.**

---

## 2. The decision, and why (do not relitigate)

Every line here is a conclusion we earned the hard way. Treat it as settled:

- `getUserMedia` requires a **trusted-HTTPS secure context**. No exceptions on
  either platform.
- A **self-signed cert on the Pi** fails on Android (mic blocked on cert-error
  origins) — not viable as the default.
- **Per-Pi real certs (ACME DNS-01) are rejected.** They are **O(N) and
  security-critical**: every device needs its own cert *issued and renewed
  forever* through a DNS trust-anchor the maintainer operates (and cert lifetimes
  keep shrinking toward 47 days). That cost scales with the fleet and never stops.
  **This is the deciding constraint.**
- A **cloud HTTPS page cannot reach the Pi over the LAN** (iOS ships no Local
  Network Access; mixed content is blocked), and the **cloud cannot dial into the
  Pi** (home NAT). So "wrap the Pi's page" only works via a full reverse tunnel —
  all traffic through your cloud, the worst-scaling option — rejected.
- **Therefore:** serve the capture page from a **trusted cloud origin**
  (jasper.tech), and move audio to the Pi through a **relay the Pi pulls from**.

**The O(1)-vs-O(N) insight is the whole game.** "Infra" is not one thing:

- Cert-on-the-Pi infra is **O(N) and security-critical** — one cert per device,
  renewed forever, via a trust-anchor you run. That is what cannot be maintained.
- The relay is **O(1) and dumb** — *one* static page + *one* stateless Worker +
  object store serves the **entire fleet identically**. No per-device record, no
  cert, and nothing to renew. Production may add one shared Pi registration
  secret to prevent arbitrary internet clients from minting sessions, but that
  secret does not decrypt audio and does not create per-device state. A million
  Pis hit the same ~100 lines. Its maintenance does **not** grow with fleet size.
  That property is the reason it wins.

---

## 3. What we are changing (the baseline)

This is a **transport change**, not a new pipeline. Today (see
[HANDOFF-correction.md](HANDOFF-correction.md)):

- The page is served **from the Pi** — nginx 443 (self-signed) →
  `127.0.0.1:8770` (`jasper-web` correction backend).
- The browser captures the mic (`getUserMedia` + AudioWorklet), encodes a **WAV
  Blob in-page**, and **same-origin `POST`s** it to the Pi
  (`upload-capture` / `upload-noise`, `Content-Type: audio/wav`).
- The Pi reads the raw WAV (`MAX_WAV_BODY_BYTES = 32 MB`, requires 48 kHz) and
  runs analysis. The page polls the Pi's own `/status`.

**The transport — record-in-browser then upload-a-WAV — already works and is the
simplest possible shape.** The only broken part is the **secure context** (the
Pi's self-signed cert). This plan changes *only the transport*:

- The capture page moves to a **trusted cloud origin** (jasper.tech).
- The WAV travels **phone → relay → Pi-pulls** instead of same-origin.
- The **Pi-side analysis pipeline is reused verbatim** (the relay-pulled WAV is
  fed into the same `jasper/web/correction_setup.py` analysis, same 48 kHz / mono
  / 32 MB contract). The shared capture helper
  [`measurement-audio.js`](../deploy/assets/shared/js/measurement-audio.js) is
  reused for `getUserMedia` + AudioWorklet + WAV encoding.

---

## 4. Architecture (three pieces)

1. **Static capture page** — hosted on a trusted HTTPS origin (Cloudflare Pages
   or GitHub Pages under jasper.tech). Pure static HTML/CSS/JS. A real cert ⇒
   `getUserMedia` works on both platforms with zero warnings. The page is a
   **fixed, trusted renderer + capture surface** (see §6, §8).
2. **Relay** — one Cloudflare Worker + R2 bucket (or equivalent serverless +
   object store). **Stateless, dumb, free tier.** Holds per-session metadata + one
   encrypted blob, short TTL, token-gated. Treats the capture spec **and** the
   blob as **opaque** — it never interprets either.
3. **Pi side** — mints sessions, registers them with the relay, shows the user a
   tap-through link (or QR), polls the relay, pulls + decrypts the blob, verifies
   it, and feeds the WAV into the **existing** analysis. Outbound HTTPS only
   (NAT-friendly).

The page and the Pi **never talk to each other directly** — they communicate
**only through the relay**, in both directions, for control signals *and* audio.

---

## 5. The guided UX, and how coordination works

This is the part that is easy to get confused about, so it is spelled out.

**Roles:** the **phone is the microphone** (it records the room). The **Pi is the
speaker + the brain** — it *plays* the stimulus and *analyzes*. The Pi does **not
record** anything; it already knows the stimulus because it generated it, and it
aligns the phone's recording against that known stimulus.

**Guided level setup once, then one Start tap per capture.** The speaker page is
intentionally simple and exposes one server-owned next action. The jasper.tech page
owns the phone-only setup the Pi page cannot do reliably: microphone permission,
input choice, and calibration choice (none / vendor serial / uploaded file).
Room's measurement count remains Pi-owned. The automatic level stage validates
and freezes the mic/calibration setup through the Pi before playing its
quiet-start tone. Each later Room link carries signed Pi-owned position metadata,
checks the realized mic against the level identity, and opens directly on
**Start measurement**; that tap does both:

1. records a short passive room-noise floor, then starts the sweep recording
   **locally** on the phone (instant — `getUserMedia`),
   and
2. drops an **`armed`** flag in the relay, which the Pi (already polling) sees and
   uses to **play the stimulus**.

The user never perceives the split. The only architectural difference from today
is *where the page is hosted* (jasper.tech vs jts.local), and the Pi walks the
user there invisibly via the tap-through link.

**How the page "triggers" the Pi without reaching it:** it does **not** reach the
Pi. It leaves a note in the relay; the Pi is watching the relay; the Pi acts. The
relay is a shared mailbox that carries *control signals* exactly as it carries the
audio. Full sequence:

1. **Pi** mints the session + capture-spec, registers it with the relay, shows
   the tap-link on `jts.local`.
2. **Phone** opens the level-ramp page, asks for microphone permission, and lets
   the user pick the microphone/calibration once. The Pi retains the Room count.
3. **Phone** verifies that the spec's `capture_protocol_version` is in the
   public page's `supported_capture_protocol_versions`, then includes the page
   build/protocol identity in every control event. The Pi validates that identity
   before any setup or armed callback may play audio. Specs from before the
   handshake map narrowly to legacy protocol 1 so the page can be released first.
4. **Phone** posts `{setup_validate:true, setup:{...}, setup_identity:{...}}`;
   the Pi validates/applies it and replies `host_event.phase="setup_validated"`.
   The phone then streams compact level batches, and the Pi raises software gain
   gradually from quiet until stable, restores listening volume, and retains the
   target for sweeps. Unsupported/unknown AGC is refused before the tone.
5. **Phone** opens a signed capture-only Room link carrying the Pi-owned
   position/total, records passive room noise, starts the sweep recording, and
   drops authenticated `armed` metadata with the realized device in the relay.
6. **Pi** — already polling `GET /sessions/:id/status` — sees `armed`, verifies
   the device/calibration against the level-check identity, reasserts the
   retained target inside the measurement window,
   publishes `host_event.phase="sweep_started"`,
   and **plays the stimulus** through the speaker.
7. In the room, the phone's mic (still recording) captures it.
8. **Pi** restores normal listening volume before the measurement window exits,
   then publishes `host_event.phase="sweep_complete"` after playback returns.
9. **Phone** sees `sweep_complete`, keeps the spec's post-roll, encrypts, uploads
   → `PUT /sessions/:id/blob`.
10. **Pi** polls, sees `ready`, pulls the blob, decrypts, verifies, and
   **cross-correlates** against the stimulus it knows it played → aligned
   measurement.

For a Crossover sweep, the speaker page may request Stop while the host is waiting,
preparing, playing, or restoring. The cooperative signal crosses both the relay
polling loop and playback watchdog; `stopping` withholds the next action until
the exact worker and cleanup finish. Once playback and rollback complete, the
host atomically chooses `stopping` or `finishing`. The latter hides Stop while
the phone closes, encrypts, and uploads, then advances to `committing` only for
the synchronous evidence write. This is distinct from `sweep_complete`, which
ends the phone's recorder window but is not evidence authority.

The Pi's existing status-poll loop (it already polls to learn when the audio is
ready) carries the "armed, go play" signal too — **one loop, two jobs.**

**Why "trigger at the same instant" is the wrong frame — and unnecessary.** You
do *not* need (and cannot get, across two independent devices over a cloud relay)
a sample-synchronized trigger. The phone records a **window**; the Pi plays
**inside** it; the Pi aligns in software by cross-correlation. The up-to-~1 s gap
between "phone armed" and "the Pi's next poll" is simply absorbed by the recording
window (the stimulus lands a second or two in). This is exactly how REW and every
sweep-based acoustic tool work. The two things to get right:

- **Guarantee the stimulus lands fully inside the recording.** The Pi plays *only
  after* it sees `armed`; the phone stops only after the Pi reports
  `sweep_complete` plus post-roll, with `duration_ms` as a hard timeout. This is
  a **race** to avoid ("don't play before the phone is recording"), more than an
  alignment subtlety.
- If you ever need to tighten the ~1 s, swap the Pi's polling for a long-poll or
  WebSocket to the relay (Cloudflare Durable Objects). Not needed for a
  record-and-align measurement.

---

## 6. The capture spec — agnostic contract + server-driven UI

**One page + one relay serves every measurement kind.** The Pi sends an **opaque**
JSON `capture_spec`; the **page interprets** it; the **relay never parses** it.
Adding a new measurement kind therefore needs **zero relay changes** — only Pi +
page understanding. (Same pure-data-registry boundary used elsewhere in JTS; see
[extensibility.md](extensibility.md).) An acceptance criterion (§15) enforces the
boundary so it cannot erode.

For room correction, `duration_ms` is now the hard recording timeout; normal
recorder completion is the Pi's `host_event.phase="sweep_complete"` plus
`post_roll_ms`.

```
capture_spec:
  kind: "room_sweep" | "balance_burst" | "sync_marker" | "crossover_sweep" | "noise_floor" | <string>
  sample_rate_hz: 48000
  channels: 1
  duration_ms: 30000          # hard timeout; normal completion waits for sweep_complete
  pre_roll_ms: 500            # legacy/fallback margin; Pi still plays only after armed
  post_roll_ms: 650
  position: 1                  # optional signed, Pi-owned display progress
  total_positions: 6          # supplied as a pair with position
  presentation_variant: "trust_repeat"  # copy only; never state authority
  constraints:                # measurement-critical: do NOT let the browser process the signal
    echoCancellation: false
    autoGainControl: false
    noiseSuppression: false
    voiceIsolation: false
  stimulus:                   # optional; null => passive record (no Pi playback)
    played_by: "pi"           # the Pi emits the sweep/burst; the phone only records
    label: "log sweep 20 Hz - 20 kHz"   # display/telemetry only
  ui:                         # SERVER-DRIVEN UI as DATA (see below) — never executable markup
    theme: { accent: "sage", font: "figtree" }      # allowlisted tokens -> CSS variables
    screen:
      - { type: "heading", text: "Room measurement" }
      - { type: "steps",   items: ["Stand at the couch", "Hold the phone up", "Stay quiet for 10s"] }
      - { type: "level_meter", source: "mic" }
      - { type: "button",  label: "Start", action: "begin_capture" }
  output:
    format: "wav"             # mono 16-bit PCM WAV at sample_rate_hz
  max_upload_bytes: 33554432  # 32 MB cap; mirror the Pi backend limit
  return_url: "http://jts5.local/correction/"  # Back to speaker after upload
```

### The page is agnostic; the Pi owns the intelligence

- **Relay:** agnostic by construction — opaque bytes. Volume leveling, frequency
  response, sync, crossover are identical to it.
- **Page:** a generic "render the spec + record per the spec" tool. New kind =
  new spec, no page rewrite (a genuinely new *interaction* may need page code; the
  common "record with these constraints" case does not). Room-correction
  calibration options are likewise data (`calibration_models`) emitted by the Pi,
  not a model list compiled into the page.
- **Pi:** owns all per-measurement logic (it already does, in
  `correction_setup.py` etc.). It builds the right spec and runs the right
  analysis.
- **Return navigation:** the Pi mints `return_url` from the local request Host
  and the measurement route (`/correction/`, `/correction/sync`, ...). The page
  sanitizes it again and renders it only as a plain post-upload anchor, never as
  executable code or a cross-origin fetch.

### Server-driven UI: the screen comes from the Pi — as DATA, not code

The Pi drives the **content, copy, layout, steps, theme, and per-kind
choreography** via the `ui` field, so UI changes ship with **Pi software updates**
— **no web deploy** for the common case. The page ships a small, fixed, **trusted
renderer** that maps known component types (`heading`, `steps`, `level_meter`,
`button`, …) to DOM **safely** (escaped text via `textContent`; theme via a
CSS-variable allowlist).

- **Ships from the Pi (no web deploy):** all copy, layout, ordering, instructions,
  theme, button labels, which controls show, supported calibration mic models,
  and entirely new measurement kinds.
- **Needs a renderer update (web publish):** a genuinely *new component type* the
  renderer cannot draw (e.g. a spectrogram widget). Rare — and the static
  page/renderer bundle can live in the JTS repo and publish to Pages as one step
  of the normal release, so even that is not a disconnected web deploy.

**Why DATA and not executable HTML/CSS/JS — this is a security boundary, not a
style choice. See §8.**

---

## 7. Relay API (minimal, stateless, opaque)

Tokens are bearer tokens in a header. Sessions + blobs auto-expire at `ttl_s`
(default 900 s) and are deleted after the Pi pulls. The relay **never** receives
`content_key` and **never** interprets `capture_spec` or the blob.

- `POST   /sessions` — Pi registers `{session_id, capture_spec, upload_token,
  pull_token, ttl_s}`. *Hardening:* store the two tokens as SHA-256 hashes.
- `GET    /sessions/:id/spec` — phone fetches `capture_spec` (auth: upload_token).
- `POST   /sessions/:id/event` — phone posts the full setup once as
  `{setup_validate:true, setup, setup_identity}`, streams token-scoped compact
  `level_batch` events during automatic leveling, and later posts `{armed:true}`
  so the Pi can trigger the stimulus (auth: upload_token). Capture events include
  passive `noise_floor`, phone-reported `device`, and only the frozen setup
  binding — not raw calibration contents.
- `GET    /sessions/:id/phone-status` — phone polls `{state, host_event}` (auth:
  upload_token) so it can wait for Pi progress, especially `sweep_complete`,
  without seeing pull-only blob/integrity fields.
- `POST   /sessions/:id/host-event` — Pi posts bounded progress metadata such as
  `{phase:"sweep_started"}` / `{phase:"sweep_complete"}` / `{phase:"sweep_failed"}`
  (auth: pull_token).
- `PUT    /sessions/:id/blob` — phone uploads `IV ‖ ciphertext` + integrity
  `{plaintext_len, sha256}` (auth: upload_token); enforce `max_upload_bytes` +
  Content-Type at the Worker; set state `ready`.
- `GET    /sessions/:id/status` — Pi polls `{state, size, integrity, event,
  host_event}` (auth: pull_token).
- `GET    /sessions/:id/blob` — Pi pulls ciphertext (auth: pull_token).
- `DELETE /sessions/:id` — Pi purges (auth: pull_token).

---

## 8. Security & privacy

### E2E encryption via the URL fragment (why it works)

`content_key` (AES-256-GCM) is minted by the **Pi** and delivered to the phone
**only in the URL fragment** of the tap-link
(`https://capture.jasper.tech/#s=<id>&u=<upload_token>&k=<base64url(content_key)>&a=<spec_mac>`).
The fragment is the one part of a URL that **browsers never transmit to any
server** — so the key reaches the page's JavaScript (which uses it to encrypt the
WAV) while the relay, which only ever sees what is *sent* in requests, **never
receives it**. The relay stores **ciphertext only**. Even the maintainer cannot
read room audio. `pull_token` stays on the Pi (never in the link).

Relay control metadata is **not** the encrypted WAV. The Worker stores
short-lived JSON events so the phone and Pi can coordinate setup/progress. For
room correction that includes the phone-reported mic label/device id, passive
noise-floor scalar, position count, and any calibration serial or uploaded
calibration file text. This metadata is bounded, token-gated, TTL-limited, and
deleted after pull, but it is not end-to-end encrypted by the WAV `content_key`.
Do not put room audio or long-lived secrets in control events.

### End-to-end control integrity (the relay is not an authority)

`upload_token` / `pull_token` authenticate *who may access* a session. They do
**not** prove the *content* was not swapped — a compromised relay passes its own
token check **and** serves whatever content it likes. JTS therefore derives a
separate HMAC-SHA-256 key from the fragment-only `content_key`:

- The Pi MACs the **exact opaque capture-spec bytes**, bound to `session_id`, and
  puts that tag in the phone-link fragment as `a=`. The page verifies the tag
  before parsing or rendering the spec. The relay can withhold the spec, but it
  cannot change instructions, protocol, acknowledgement binding, or return URL
  without a visible integrity failure.
- Protocol-v2 phone events are one relay-opaque authenticated envelope. The MAC
  covers the exact JSON payload, a monotonic sequence, and `session_id`. The Pi
  verifies that envelope **before** reading page identity, setup, device,
  acknowledgement, abort, or `armed`; relay edits and prior-session replay fail
  before a host callback can play audio.
- Protocol 1 remains readable for old Pi/page pairs, but raw protocol-1 events
  cannot satisfy a protocol-v2 session or create v2 crossover evidence. New Pi
  links carry the spec MAC for every kind, including protocol 1.

The matching reusable implementations are
`capture-page/js/transport-integrity.js` and
`jasper/capture_relay/integrity.py`. The Worker remains byte-opaque and holds no
new secret. This is integrity/authenticity, not metadata confidentiality: the
relay can still see bounded control-event JSON.

### Why the UI is DATA, not CODE (the load-bearing rule)

The capture page **holds the microphone** (`getUserMedia`) **and** the E2E
`content_key` (in its fragment, readable by its own JS). Letting the relay deliver
**executable** UI to that page is **XSS into your trusted, mic-enabled,
key-holding origin**:

- A compromised/buggy relay (or any tamper point on the untrusted Pi→relay→phone
  path) runs its code in the jasper.tech origin → reads `content_key` and
  exfiltrates the audio, **or** simply grabs the live mic stream — eavesdropping
  in someone's home. This **undoes the very E2E guarantee** you built so you would
  not have to trust the relay with the audio.
- **The blast radius is the whole domain and all users.** jasper.tech is *one*
  trusted origin for the entire fleet; code execution there can persist (service
  worker, cached assets) and phish every user — not one session.
- **"HTML/CSS without JS" is not a clean category.** HTML executes JS through many
  non-`<script>` vectors (`onerror`, `onload`, `onclick`, `javascript:` URLs,
  `<svg>`, `<iframe>`, `<meta refresh>`, `<base>`); CSS can exfiltrate DOM data
  (`background-image` + attribute selectors) and redress/phish the UI. "HTML minus
  JS" really means "HTML run through a maintained sanitizer + strict CSP," where a
  single bypass is the full XSS.
- **The safe path is also the cheaper path.** A fixed renderer drawing **data**
  (§6) gives the same "UI ships from the Pi" benefit for *less* code than a
  sanitizer and with **zero injection surface** — the worst a fully hostile
  payload can do is show wrong text. This is not a "calibration audio is
  low-stakes, ship the risky thing" trade: there is no extra cost to the safe
  option, and the real asset at risk is a **live home microphone + a fleet-wide
  trusted domain**, not the sweep file.

### Other relay hardening

- **Dual size cap:** enforce `max_upload_bytes` at **both** the Worker (reject
  before it hits storage, so a leaked token cannot fill the bucket) and the Pi.
- **Per-session rate limit** on the endpoints so a leaked `upload_token` cannot
  hammer the bucket within its TTL.
- **Optional Pi registration secret.** A Cloudflare Worker secret can gate
  `POST /sessions` so only configured Pis mint sessions; the value lives outside
  the open-source repo and is not an audio decryption key.
- **Short TTL + delete-after-pull.** Relay compromise is bounded to short-lived
  ciphertext + non-secret specs.

---

## 9. Measurement validity (no-silent-failure *beyond* transport)

JTS's no-silent-failure doctrine must extend past transport to **whether the
number is trustworthy.** An intact-but-invalid measurement is the worst failure
mode for a tool whose entire job is a trustworthy result.

- **Realized-constraints verification (refuse, per-kind).** After `getUserMedia`,
  verify the realized settings — actual sample rate, normalized capture channel
  count, and that
  EC/AGC/NS came back `false` (WebKit has historically *ignored*
  `echoCancellation:false`). AGC/NS silently left on does not corrupt the file in a
  way that looks wrong — it quietly flattens the level differences you are
  measuring. So for kinds that demand clean samples (`room_sweep`) **refuse**
  rather than warn; let the spec set refuse-vs-warn **per kind**. Keep the raw
  source-track width as diagnostics, but do not confuse a multi-channel USB
  source with the mono channel-zero artifact produced by `createMonoRecorder`.
- **Level-ramp AGC is stricter.** Automatic leveling requires explicit realized
  `autoGainControl === false`. Missing/unknown is not treated as false: the phone
  posts a token-scoped refusal and the Pi never starts the tone. A future manual
  lock mode needs its own acknowledged protocol; it must not be inferred from
  AGC-compressed samples.
- **…with a device-capability fallback.** Because some iOS builds *cannot* honor
  `echoCancellation:false`, a hard refuse could refuse every iPhone. Instead probe
  the realized constraints and, if clean capture is impossible on this device,
  fail **gracefully and labeled** ("this phone can't do a clean room measurement —
  here's the fallback"), never a dead-end refuse.
- **Integrity hash.** The phone sends plaintext length + SHA-256; the Pi verifies
  after decrypt, **before** analysis — any truncation/corruption fails loud.
- **Alignment confidence.** Cross-correlation alignment confidence is designed as
  a first-class check: a weak/ambiguous correlation peak should fail loud the same
  way a bad hash does. (Intact-but-misaligned is exactly the failure the byte hash
  cannot catch.) The gate itself (`capture_relay/alignment.py`,
  `assert_alignment_confident`) is implemented and unit-tested but **not yet wired**
  into the capture path — no production flow calls it today (alignment-threshold
  tuning is deferred; see the footer). See §11.
- **Clock drift (per-kind guidance).** The phone's mic clock and the Pi's playback
  clock are independent crystals that drift (~tens of ppm → ~1 ms over a 10 s
  window). Negligible for magnitude **frequency response** and for **level /
  volume-leveling**; it is the actual signal for **sync / timing**. So timing-
  sensitive comparisons should be made **within a single recording** (both
  references in one window → drift is common-mode and cancels), not across separate
  captures (independent drift).

---

## 10. Browser page specifics (validated)

- `getUserMedia` works on a trusted origin on iOS Safari + Android Chrome; the
  **mic-permission prompt is the only unavoidable visible step** — tell the user
  to tap Allow.
- Reuse [`measurement-audio.js`](../deploy/assets/shared/js/measurement-audio.js)
  (getUserMedia + AudioWorklet + mono WAV encode + RMS-to-dBFS). AudioWorklet is
  solid on iOS 15+; `MediaRecorder` remains a fallback for future fixed-length
  one-shot kinds.
- **iOS lifecycle:** `AudioContext` starts suspended → `resume()` inside the tap
  handler. Keep the page **foreground** during capture — backgrounding / screen
  lock kills the mic track. Hold a **Screen Wake Lock** during capture and listen
  for `visibilitychange` to **abort visibly and notify the Pi on its next relay
  poll** rather than capture garbage; tell the user the screen must stay on. Keep
  it a normal Safari tab, **not** an installed PWA. An audible cue for this path
  is still deferred until jasper-web has a cue bridge.
- **Clean samples:** enforce EC/AGC/NS = false in constraints **and** verify the
  realized settings (§9).

---

## 11. Pi side specifics

- **Outbound HTTPS only** (no inbound; works behind NAT; the Pi already has
  internet for voice providers).
- Mint `session_id` / `content_key` / tokens with a CSPRNG. Build the
  `capture_spec` for the active flow. Register, render the tap-link / QR, poll,
  pull, decrypt, **verify integrity** (plaintext length + SHA-256), then hand the
  WAV to the **existing** analysis (`correction_setup.py`'s pipeline) — same 48 kHz
  / mono / 32 MB contract as today. The cross-correlation **alignment-confidence**
  gate (`capture_relay/alignment.py`, §9) is built and unit-tested but **not yet
  wired** into this pull path — the deployed verify step is integrity only.
- If `stimulus.played_by == "pi"`: start playback when the phone posts
  `{armed:true}`; rely on cross-correlation for alignment (no tight sync).

---

## 12. Failure handling + cues

Every leg surfaces a clear UI state today: link/session expired, relay
unreachable, upload failed, the Pi never sees `armed` within a timeout,
decrypt/integrity fail → explicit message + retry on the phone or
speaker page, plus `event=capture_relay.*` logs. (The alignment-confidence gate
that would add a weak-correlation failure to that list is built but not yet
wired — §9/§11.) Audible cues remain a required
follow-up for failures where the household must act (`CueDef.play(...)` from the
cue registry; see [HANDOFF-audible-feedback.md](HANDOFF-audible-feedback.md)),
but the current jasper-web relay adapter has no cue bridge. The relay is a shared
dependency **only at commissioning**; an outage breaks **new** measurements only
(existing applied corrections are unaffected) — say so in the UI when the relay
is unreachable. Pre-tone setup/ambient failures publish the same token-scoped
terminal ramp error as in-ramp failures and remain observable briefly before
relay cleanup, so the phone cannot be left waiting on a purged session.
Explicit operator Stop is not a failure: it emits stopped lifecycle events and
phone/UI state without a failure cue. Level ramps publish terminal
`ramp.state="cancelled"`; sweeps publish `sweep_cancelled`. The host first
exposes `stopping`, retains the one relay slot through playback/graph/volume/
purge cleanup, and only then publishes terminal `stopped`. A level ramp
atomically enters `committing` after its restored ramp. After a sweep's
cancellable audio boundary, `finishing` owns phone close/encrypt/upload and
`committing` owns evidence persistence; both are visible, non-stoppable phases
with no forward action. If a cancelled
finalizer exceeds the 45-second recovery warning threshold, the host emits the critical
`correction.async_cancel_drain_timeout` event and remains fail-closed until the
finalizer actually exits. A timeout or cleanup failure remains a failure and
keeps its normal logging and cue policy.

---

## 13. Out of scope (consciously deferred, not discarded)

- **Per-Pi trusted certs / ACME DNS-01 / acme-dns** — rejected: O(N),
  security-critical infra the maintainer cannot run at fleet scale (§2).
- **WebRTC LAN-direct passthrough** — **validated, deferred.** Adversarial
  research (2026) confirmed: a trusted cloud page *can* open a WebRTC
  `RTCPeerConnection` to a private-LAN-IP Pi on **iOS 26 Safari and Chrome 142+
  today** (WebRTC ICE to private IPs is *not* gated by Local Network Access;
  WebRTC gating is unscheduled — only WebSockets/WebTransport gate at Chrome 147);
  raw PCM over an RTCDataChannel is **lossless** (16 KiB chunks + `bufferedAmount`
  backpressure); the Pi needs **no CA cert** (RFC 8827 DTLS-fingerprint model,
  signaling integrity is the one thing to get right). Two correctness notes for
  whoever builds it: gate "ready" on `connectionState` + the data channel's
  `onopen`, **not** `iceConnectionState`, and do **not** treat ICE `disconnected`
  as terminal (it is transient). WebRTC keeps audio on the LAN and adds real-time
  access — a **future optimization layered on this relay**, sharing this exact
  session/spec machinery. **Build the relay so no relay-specific decision
  forecloses it.** Revisit if telemetry shows LAN-direct success would materially
  help, or if you want real-time during-capture feedback. The relay remains the
  **floor that always works** (guest Wi-Fi, AP isolation, VLANs block P2P; the
  relay does not).
- **Bring-your-own-domain** — a separate, *zero-maintainer-infra* power-user path
  (domain owners point their own DNS at their Pi and run their own cert). Not this
  build.

---

## 14. Build and release order

The numbered implementation history below is not the deployment order. The
public page and Pi are released independently, so protocol changes ship
**page first**: publish a page whose `version.json` supports both the old and
new protocol, verify `https://capture.jasper.tech/version.json`, and only then
deploy Pis that emit the new `CaptureSpec.capture_protocol_version`. The exact
commands and old-protocol retirement step live in
[`capture-page/README.md`](../capture-page/README.md). The page identity rides
every phone event; the Pi logs `event=capture_relay.page_compatible` or
`event=capture_relay.page_incompatible` before tone playback. This is the live
pairing proof, not a guess based on the Pages dashboard.

1. **Capture-spec schema** + a Pi-side builder for `kind="room_sweep"`.
2. **Relay** Worker + object store: the endpoint set, TTL, token gating, **dual
   size cap**, per-session **rate limit**; opaque spec + blob.
3. **Static page**: read fragment → fetch spec → guided room setup
   (permission/mic/calibration/count) → passive noise floor → `armed` → wait for
   Pi `sweep_complete` → encrypt → upload. Reuse `measurement-audio.js`.
4. **Pi**: register, render tap-link, poll, apply phone setup, publish host
   progress, pull, decrypt, verify, feed existing analysis; wire `{armed}` →
   stimulus playback.
5. **E2E encryption** (WebCrypto on the phone / AES-GCM on the Pi), WAV
   **integrity** (length + SHA-256), and fragment-key-derived HMAC integrity for
   the exact spec plus protocol-v2 phone control events.
6. **Measurement-validity gates**: realized-constraints verify + device-capability
   fallback; **alignment-confidence** check; per-kind clock-drift handling.
7. **Failure states**; relay-unreachable messaging; **Screen Wake Lock** +
   `visibilitychange` visible abort. Audible cues are deferred until the
   jasper-web → jasper-voice cue bridge exists.
8. **Generalize**: add `balance_burst` / `sync_marker` / `crossover_sweep` kinds —
   **page + Pi only, zero relay changes** (prove the boundary).
9. **Cross-device QR** variant (drive from a laptop, measure with a phone — same
   flow, QR instead of tap-through).
10. **Pi-side telemetry**: kind, success/latency, to tune the flow.

---

## 15. Acceptance criteria

- A **fresh iPhone (Safari)** and a **fresh Android phone (Chrome)**, neither with
  any cert installed, complete a guided room measurement end-to-end — no cert
  warnings, no app.
- The decrypted WAV on the Pi is **bit-identical** to what the phone recorded
  (verify via the integrity hash) and is **48 kHz mono**.
- The **relay never receives `content_key`** and stores **only ciphertext** for
  room audio (verify by inspecting stored objects). Control metadata is bounded
  and short-lived, as described in §8.
- Adding `kind="balance_burst"` requires edits to **only the Pi and the page —
  zero relay changes.**
- A **weak/ambiguous cross-correlation alignment fails loud** (not a silently-wrong
  measurement).
- EC/AGC/NS left on (or capture not clean) → **refuse / labeled-degrade per kind**,
  never a silently-flattened measurement.
- **Killing the relay mid-flow** → clear UI error, **not** a silent hang; existing
  applied corrections still work. Add the audible cue once the web cue bridge
  exists.
- **Auto-lock / backgrounding mid-capture** → the wake lock holds; if it still
  happens, the capture **aborts visibly and notifies the Pi on its next relay
  poll**, never uploads garbage. Add the audible cue once the web cue bridge
  exists.
- The **UI renders only from data** (no executable markup path): a payload
  containing `<script>` / `onerror=` / `javascript:` is rendered inert (escaped
  text), never executed. A regression test asserts this.
- A relay-modified spec, raw/modified protocol-v2 event, or authenticated event
  replayed from another session fails before `on_armed` can play a stimulus.

---

Last updated: 2026-07-15 — Room defaults are speaker-owned (six positions,
flat target, balanced strategy, and an automatic main-seat trust repeat). The
Room level check no longer collects a phone-owned position count; later Room
links carry signed position/total metadata and authenticate the realized
microphone against the Pi-retained level identity before playback. The trust
repeat uses the same Room relay handler and state machine; its generic
`presentation_variant` changes phone copy only and cannot own sequencing,
timeout, or admission. Repo-pinned capture page build 20260715.3 adds the
repeat-specific phone copy and renders host sweep cancellation as expected
control flow; the page entry and relay-client import carry the matching
`20260715-3` cache key. A transient phone-side status-poll failure no longer aborts a
bounded level walk; small page-side control requests abort after three seconds
through response-body parsing, so returned headers with a stalled body cannot
freeze mic batches. The Pi uses a separate 1.5-second level-control socket
timeout plus an async wall-clock deadline,
publishes at most one queued host event before the next status refresh, and
bounds one retry plus that status read to 4.75 seconds inside the default
eight-second feed-loss guard. The Pi gives idempotent host-progress writes one retry
after a timeout, 429, or relay 5xx. External publication is intentionally
pending coordinator release. Active-crossover capture uses
role-sized sweeps,
a signal-bounded controlled quiet crop, paired-window deconvolved per-band SNR,
and the server-owned three-repeat admission loop; selecting a UMIK-2 preselects
only the miniDSP UMIK-2 model/mode. Browser labels do not contain a trustworthy
serial, so the operator must still enter and validate it once; there is no
automatic calibration-file match. Capture specs are MAC-bound to their fragment
links and protocol-v2 phone events are authenticated end to end before any
correctness-critical field can reach playback; protocol 1 remains compatible
but cannot satisfy v2 evidence. The level stage preflights and freezes the
microphone/calibration setup; successful leveling restores listening volume
immediately, and sweeps assert the retained target only inside their guarded
playback windows. Crossover retains its compact setup binding and bounded
browser-storage lifetime. The page/Pi compatibility version is checked before
tone and published at `capture.jasper.tech/version.json` with a page-before-Pi
release contract; the crossover page is a serialized single-next-action flow;
blank legacy `JASPER_CAPTURE_RELAY_BASE` / `JASPER_CAPTURE_ORIGIN` values now
migrate to the public relay defaults on install/update; explicit
`disabled`/`off`/`0`/`none` remains the persistent on-Pi fallback opt-out. Prior
2026-07-03/2026-07-01 updates: `/correction/` room relay uses a guided phone
setup, passive noise-floor capture, Pi host-progress events, and
stop-on-`sweep_complete` recording; supported calibration mic models are emitted
from the Pi registry in `CaptureSpec`; `/sync` and crossover ride the generic
relay seam. Optional Pi registration secret implemented (hardware-free).
Deferred: balance burst, room-helper dedupe, full on-device validation
(iPhone/Android, on jts3/jts5), alignment-threshold tuning, and an audible
failure cue.
