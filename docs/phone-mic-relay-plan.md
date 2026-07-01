# Phone-mic capture relay — design & build plan

> **Status: BUILT (hardware-free), gated default-off, not yet validated on
> device or deployed.** The transport (capture-spec schema, Cloudflare Worker + R2
> relay, static capture page, Pi-side session/poll/pull/decrypt/verify) and the
> daemon adapters are implemented and covered by hardware-free tests. The relay
> capture path is **gated + default-off** (inert unless `JASPER_CAPTURE_RELAY_BASE`
> is set), so the standard on-Pi flow is byte-identical until an operator opts in.
>
> **Kinds wired today:** room correction (`POST /relay/capture`) and **sync**
> (`POST /sync/relay-capture`) — both ride one kind-agnostic seam
> (`RelayCaptureKind` + `_run_relay_capture` in `correction_setup.py`); a new kind
> is a descriptor, not a new handler. **A USB-C measurement mic plugged into the
> phone is supported:** the room capture page now runs a guided setup on
> `capture.jasper.tech` (permission → mic choice → calibration choice → position
> count). Calibration is still applied **Pi-side during analysis, never at record
> time** (it's a post-hoc FR correction in `MeasurementSession._smooth_capture`);
> the phone records raw and reports *which* mic it used in the opaque `armed`
> event. A device-aware gate (`_relay_device_calibration_block`, POST-capture)
> refuses a vendor curve on the phone's built-in mic but allows it for the
> matching USB mic. The phone also records a passive noise-floor window before
> the Pi plays anything, and the Pi publishes `sweep_complete` so the phone stops
> from real sweep progress rather than a fixed timer.
>
> **Deferred (seam-ready, documented):** the **crossover** relay kind — when it
> lands it MUST add its OWN calibration guard at
> `web_measurement.capture_calibration` (it loads by `calibration_id`, NOT
> `session.mic_calibration`, so the room/sync gate won't cover it — same silent
> mis-calibration class on a different path); **balance burst** — needs new
> non-interactive L/R level analysis (no existing consumer; the live balance flow
> is interactive); the **room-correction page's forked capture helper** — AGENTS
> forbids migrating it onto the shared module without an on-device browser pass.
> The **sync relay** currently requires the sync session to be started first from a
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
> capture). **Once validated + deployed, make the relay the default and convert
> this doc to the HANDOFF shape with a `Last verified:` footer.**

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

**Guided setup, then one Start tap.** The speaker page is intentionally simple:
Start creates the one-time relay link and mirrors progress. The jasper.tech page
owns the phone-only setup the Pi page cannot do reliably: microphone permission,
input choice, calibration choice (none / vendor serial / uploaded file), and
measurement count. Once setup is complete, the user taps **Start measurement** on
the phone, and that tap does both:

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
2. **Phone** opens the jasper.tech page, fetches the spec, asks for microphone
   permission, lets the user pick mic/calibration/count, and records passive room
   noise.
3. **Phone** starts the sweep recording and drops setup + `armed` in the relay →
   `POST /sessions/:id/event {armed:true, noise_floor:{...}, setup:{...}}`.
4. **Pi** — already polling `GET /sessions/:id/status` — sees `armed`, applies
   setup (position count/calibration), publishes `host_event.phase="sweep_started"`,
   and **plays the stimulus** through the speaker.
5. In the room, the phone's mic (still recording) captures it.
6. **Pi** publishes `host_event.phase="sweep_complete"` after the actual playback
   path returns.
7. **Phone** sees `sweep_complete`, keeps the spec's post-roll, encrypts, uploads
   → `PUT /sessions/:id/blob`.
8. **Pi** polls, sees `ready`, pulls the blob, decrypts, verifies, and
   **cross-correlates** against the stimulus it knows it played → aligned
   measurement.

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

For room correction, `duration_ms` is now the hard recording timeout; the normal
stop condition is the Pi's `host_event.phase="sweep_complete"` plus
`post_roll_ms`.

```
capture_spec:
  kind: "room_sweep" | "balance_burst" | "sync_marker" | "crossover_sweep" | "noise_floor" | <string>
  sample_rate_hz: 48000
  channels: 1
  duration_ms: 30000          # hard timeout; normal stop waits for Pi sweep_complete
  pre_roll_ms: 500            # legacy/fallback margin; Pi still plays only after armed
  post_roll_ms: 650
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
```

### The page is agnostic; the Pi owns the intelligence

- **Relay:** agnostic by construction — opaque bytes. Volume leveling, frequency
  response, sync, crossover are identical to it.
- **Page:** a generic "render the spec + record per the spec" tool. New kind =
  new spec, no page rewrite (a genuinely new *interaction* may need page code; the
  common "record with these constraints" case does not).
- **Pi:** owns all per-measurement logic (it already does, in
  `correction_setup.py` etc.). It builds the right spec and runs the right
  analysis.

### Server-driven UI: the screen comes from the Pi — as DATA, not code

The Pi drives the **content, copy, layout, steps, theme, and per-kind
choreography** via the `ui` field, so UI changes ship with **Pi software updates**
— **no web deploy** for the common case. The page ships a small, fixed, **trusted
renderer** that maps known component types (`heading`, `steps`, `level_meter`,
`button`, …) to DOM **safely** (escaped text via `textContent`; theme via a
CSS-variable allowlist).

- **Ships from the Pi (no web deploy):** all copy, layout, ordering, instructions,
  theme, button labels, which controls show, and entirely new measurement kinds.
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
- `POST   /sessions/:id/event` — phone posts `{armed:true}` so the Pi can trigger
  the stimulus (auth: upload_token). Room correction also includes passive
  `noise_floor`, phone-reported `device`, and setup metadata (`total_positions`,
  calibration choice).
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
(`https://capture.jasper.tech/#s=<id>&u=<upload_token>&k=<base64url(content_key)>`).
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

### Tokens gate the channel, not the integrity of the content

`upload_token` / `pull_token` authenticate *who may access* a session. They do
**not** prove the *content* was not swapped — a compromised relay passes its own
token check **and** serves whatever content it likes. This is the crux of the next
point.

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
  verify the realized settings — actual sample rate, channel count, and that
  EC/AGC/NS came back `false` (WebKit has historically *ignored*
  `echoCancellation:false`). AGC/NS silently left on does not corrupt the file in a
  way that looks wrong — it quietly flattens the level differences you are
  measuring. So for kinds that demand clean samples (`room_sweep`) **refuse**
  rather than warn; let the spec set refuse-vs-warn **per kind**.
- **…with a device-capability fallback.** Because some iOS builds *cannot* honor
  `echoCancellation:false`, a hard refuse could refuse every iPhone. Instead probe
  the realized constraints and, if clean capture is impossible on this device,
  fail **gracefully and labeled** ("this phone can't do a clean room measurement —
  here's the fallback"), never a dead-end refuse.
- **Integrity hash.** The phone sends plaintext length + SHA-256; the Pi verifies
  after decrypt, **before** analysis — any truncation/corruption fails loud.
- **Alignment confidence.** Cross-correlation alignment confidence is a
  first-class check: a weak/ambiguous correlation peak fails loud the same way a
  bad hash does. (Intact-but-misaligned is exactly the failure the byte hash
  cannot catch.)
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
  for `visibilitychange` to **abort-and-cue** rather than capture garbage; tell
  the user the screen must stay on. Keep it a normal Safari tab, **not** an
  installed PWA.
- **Clean samples:** enforce EC/AGC/NS = false in constraints **and** verify the
  realized settings (§9).

---

## 11. Pi side specifics

- **Outbound HTTPS only** (no inbound; works behind NAT; the Pi already has
  internet for voice providers).
- Mint `session_id` / `content_key` / tokens with a CSPRNG. Build the
  `capture_spec` for the active flow. Register, render the tap-link / QR, poll,
  pull, decrypt, **verify** (integrity + alignment), then hand the WAV to the
  **existing** analysis (`correction_setup.py`'s pipeline) — same 48 kHz / mono /
  32 MB contract as today.
- If `stimulus.played_by == "pi"`: start playback when the phone posts
  `{armed:true}`; rely on cross-correlation for alignment (no tight sync).

---

## 12. Failure handling + cues

Every leg surfaces a clear UI state **and**, where the household must act, an
audible cue (`CueDef.play(...)` from the cue registry; see
[HANDOFF-audible-feedback.md](HANDOFF-audible-feedback.md)) — never strand the
user. Link/session expired, relay unreachable, upload failed, the Pi never sees
`ready` within a timeout, decrypt/integrity/alignment fail → explicit message +
retry + the relevant cue. The relay is a shared dependency **only at
commissioning**; an outage breaks **new** measurements only (existing applied
corrections are unaffected) — say so in the UI when the relay is unreachable.

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

## 14. Build order

1. **Capture-spec schema** + a Pi-side builder for `kind="room_sweep"`.
2. **Relay** Worker + object store: the endpoint set, TTL, token gating, **dual
   size cap**, per-session **rate limit**; opaque spec + blob.
3. **Static page**: read fragment → fetch spec → guided room setup
   (permission/mic/calibration/count) → passive noise floor → `armed` → wait for
   Pi `sweep_complete` → encrypt → upload. Reuse `measurement-audio.js`.
4. **Pi**: register, render tap-link, poll, apply phone setup, publish host
   progress, pull, decrypt, verify, feed existing analysis; wire `{armed}` →
   stimulus playback.
5. **E2E encryption** (WebCrypto on the phone / AES-GCM on the Pi) + **integrity**
   (length + SHA-256).
6. **Measurement-validity gates**: realized-constraints verify + device-capability
   fallback; **alignment-confidence** check; per-kind clock-drift handling.
7. **Failure states + cues**; relay-unreachable messaging; **Screen Wake Lock** +
   `visibilitychange` abort-and-cue.
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
- **Killing the relay mid-flow** → clear UI error + cue, **not** a silent hang;
  existing applied corrections still work.
- **Auto-lock / backgrounding mid-capture** → the wake lock holds; if it still
  happens, the capture **aborts + cues**, never uploads garbage.
- The **UI renders only from data** (no executable markup path): a payload
  containing `<script>` / `onerror=` / `javascript:` is rendered inert (escaped
  text), never executed. A regression test asserts this.

---

Last updated: 2026-07-01 — `/correction/` room relay now uses a guided phone
setup, passive noise-floor capture, Pi host-progress events, and
stop-on-`sweep_complete` recording; `/sync` still rides the generic relay seam.
Optional Pi registration secret implemented (hardware-free). Deferred:
crossover relay (needs its own `calibration_id` guard), balance burst,
room-helper dedupe, full on-device validation (iPhone/Android, on jts3/jts5),
alignment-threshold tuning, and an audible failure cue.
