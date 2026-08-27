# E0 capture protocol — reverse-engineered from origin/main @ 856903ca1

Source tree read: `$WT = /Users/jaspercurry/Code/JTS/.claude/worktrees/jts3-deploy-main`
(clean checkout of `origin/main` at `856903ca10f4d48ab617a638ec4c43eccd140333`,
verified via `git rev-parse HEAD` and the presence of `_play_body` /
`_maybe_retain_capture` in `jasper/web/correction_crossover_v2.py`).

> **That path is a dated provenance record, not a location.** It names the
> throwaway worktree this document was written against in July 2026 and has
> since been deleted; the tool lives at `experiments/e0-capture/` and locates
> `jasper/capture_relay/` from its own path. Read `$WT` below as "the repo
> root at that commit."

This file is the distilled contract `e0_capture.py` implements. Every claim
below cites `file:line` in `$WT`. No live requests were made while writing
this — every fact comes from reading source, not from probing a running Pi.

> **Revival addendum — 2026-08-16 (issue #2636).** The body below is the
> July reading and is left as written; `$WT` no longer exists. Re-checked
> against `origin/main` `40d117229`, offline, by source reading only. Four
> claims moved — read these instead of the sections they name:
>
> 1. **§2 "`raw` is accepted but never read"** is now FALSE.
>    `prepare_v2_session` reads `raw.get("tier")`
>    (`jasper/web/correction_crossover_v2.py:7351`); an absent tier resolves
>    to `DEFAULT_TIER == "full"`. The remote tier is reachable ONLY by
>    posting `{"tier": "remote"}`
>    (`jasper/active_speaker/crossover_v2_flow.py:1089-1093`). Hence
>    `--tier`.
> 2. **§12.2 `capture_page_build` freshness** is settled, not open. The
>    validator checks shape and membership only
>    (`jasper/capture_relay/session.py:812-825`), never an exact build.
>    Protocols 1 and 2 were deleted (`spec.py:50-58`), so the advertised
>    list is now `[3]`.
> 3. **§11 `setup` field.** The page's `setupWirePayload()` was rewritten
>    (`capture-page/js/main.js:154-157`) and now returns the whole
>    `setupState`, including `total_positions`. The calibration sub-object
>    is unchanged; the omission is deliberate and proven unread — see the
>    `setup_wire_payload` docstring in `e0_capture.py`.
> 4. **New since July: `capture_integrity`** (#2151, #2094, #2557). Purely
>    diagnostic, never validated, `None` when absent
>    (`session.py:1858-1863`). This client sends none and loses only that
>    sidecar key.
>
> Everything else in §§1-12 re-read clean.

---

## 1. Three parties, two hops

```
e0_capture.py  <-- relay control/data plane -->  relay.jasper.tech (Worker)  <-- polled -->  Pi (jasper-correction-web)
      |                                                                                              ^
      \----------------------------- HTTPS session-start POST (once) -----------------------------/
```

- The **session-start** call (`POST /correction/crossover/v2/session`) goes
  directly to the Pi's correction web backend (through nginx, HTTPS,
  self-signed cert). It returns a `tap_link` URL whose **fragment**
  (`#s=&u=&k=&a=`) is the only thing that ever needs to reach "the phone."
- Everything else — spec fetch, `armed`/`begin_capture` events, phone-status
  polls, WAV blob upload — goes to the **relay** (`https://relay.jasper.tech`
  by default, `RELAY_BASE` in
  `capture-page/js/config.js:10`), not to the Pi directly. The Pi polls the
  *same* relay session from its side. This is why the hard boundary
  forbids hitting `relay.jasper.tech`: doing so is indistinguishable from
  actually running the measurement.

---

## 2. Session-start request (Pi backend, hit once)

```
GET  https://<host>/correction/                      (mint/read CSRF)
POST https://<host>/correction/crossover/v2/session   (start the v2 session)
```

- Route mounted via nginx HTTPS-only vhost: `location /correction/ { proxy_pass
  http://127.0.0.1:8770/; ... }` — the trailing slash on `proxy_pass` **strips**
  the `/correction` prefix, so the backend's own route table has no
  `/correction` prefix (`deploy/nginx-jasper.conf:588-607`). Self-signed cert
  (`deploy/nginx-jasper.conf:568-578`) → HTTP client must skip verification.
- `/crossover/v2/session` is in the backend's mutating-route allowlist
  (`jasper/web/correction_setup.py:409`) and is dispatched from `do_POST`
  through `_dispatch_crossover` → `_handle_crossover_v2_relay(handler,
  verify_only=False)` (`jasper/web/correction_setup.py:7583-7593`,
  `6570-6612`).
- **CSRF (double-submit cookie).** `GET /correction/` runs `begin_request()`
  (`jasper/web/_common.py:1102-1121`), which mints a `jts_csrf` cookie if
  none is present and renders the SAME token into `<meta name="jts-csrf"
  content="...">` via `csrf_meta_html()` (`jasper/web/_common.py:1025-1029`,
  constants at `:109-110`). The mint is reflected back to the client as
  `Set-Cookie: jts_csrf=<token>; Path=/; Max-Age=2592000; SameSite=Strict`
  (`jasper/web/_common.py:832-839`, sent from `send_html_response` at
  `:1147-1149`). `do_POST` calls `guard_mutating_request(self)` with no
  parsed form (`jasper/web/correction_setup.py:8095`), which — for a
  JSON body — reduces to `_csrf_token_valid(handler, None)`: the request
  must carry the cookie **and** an `X-CSRF-Token` header equal to it
  (`jasper/web/_common.py:955-985`, `988-1013`). Practically: use one
  `requests.Session()` so the cookie jar carries the `jts_csrf` cookie
  from the GET into the POST, and copy that cookie's value into
  `X-CSRF-Token` on the POST.
- **Host/Origin guard.** `mutating_request_allowed` in
  `jasper/http_security.py:210-245` accepts any request whose `Host` header
  normalizes to the configured hostname (`jts3.local` on the lab Pi) or a
  private/loopback IP, **as long as no `Origin` header is sent** (a plain
  `requests` POST doesn't send one) and no `Sec-Fetch-Site: cross-site`
  header is present (also absent by default). So: set `Host: <host>`
  explicitly (harmless when `<host>` is already a hostname; load-bearing if
  `--host` is ever an IP) and send nothing else special.
- **Body.** `_read_json_body` treats an empty body as `{}`
  (`jasper/web/correction_setup.py:1501-1524`), and `prepare_v2_session`'s
  `raw` parameter is accepted but never read
  (`jasper/web/correction_crossover_v2.py:2190-2211`) — so `POST` with JSON
  body `{}` (and `Content-Type: application/json`) is correct and sufficient.
- **Response.** `_handle_crossover_v2_relay` returns
  `{"relay": _run_relay_capture(...)}` and `_run_relay_capture` returns
  `{"tap_link": rc.tap_link, "status": waiting["status"]}`
  (`jasper/web/correction_setup.py:6606-6612`, `839-842`). So the response
  body is:

  ```json
  {"relay": {"tap_link": "https://capture.jasper.tech/#s=...&u=...&k=...&a=...", "status": "waiting"}}
  ```

- **Preconditions that make this call fail with a 400** (all raise
  `ValueError`/`CrossoverV2Refused`, surfaced as
  `{"ok": false, "error": "..."}`, `jasper/web/correction_setup.py:7594-7608`):
  `JASPER_CROSSOVER_FLOW` must resolve to `v2` (the repo default since
  2026-07-19 — `docs/handoff-v2.md` "Flow selector"), no other measurement
  phase may be in progress (`_crossover_blocking_phase`,
  `jasper/web/correction_setup.py:6586-6591`), and the session-volume plan
  must not be in `needs_recovery`
  (`jasper/web/correction_crossover_v2.py:2218-2234`).

## 2b. `/crossover/v2/verify` is a **different, secondary** endpoint — not used here

`prepare_v2_session(verify_only=True)` mints a **separate** relay session
hosting a **1-entry** plan that re-arms VERIFY *only*, from durable post-apply
state — "the §5.2 re-verify action" (module docstring,
`jasper/web/correction_crossover_v2.py:34-38`). The July reading called this a
second preparer of its own; the two converged in #3166 and the flag is now what
picks the stage. It is for
re-running VERIFY later (e.g. from the wizard's "Re-verify" affordance after
a prior apply), not part of the initial CHECK→MEASURE→VERIFY run. `e0_capture.py`
never calls it. See §5 for why the initial VERIFY doesn't need it.

---

## 3. The fragment

`https://<capture-origin>/#s=<session_id>&u=<upload_token>&k=<base64url
32-byte key>&a=<spec MAC>` — built by `PiCaptureSession.tap_link`
(`jasper/capture_relay/session.py:204-222`) and parsed by `parseFragment`
(`capture-page/js/fragment.js:23-44`): `s`→session_id, `u`→upload_token,
`k`→content_key (base64url, 43-44 chars for 32 raw bytes), `a`→spec_mac
(43 chars, optional on very old protocol-1 links but always present for a
v2/v3 session). All four ride the URL fragment, which browsers never send
to a server — the relay never sees the key.

---

## 4. Relay endpoints (phone role) — `capture-page/js/relay-client.js`

Base URL = `RELAY_BASE` (`capture-page/js/config.js:10`,
`https://relay.jasper.tech`). All under `{base}/sessions/{session_id}`
(`_url`, `relay-client.js:68-70`), auth = `Authorization: Bearer
{upload_token}` (`_authHeaders`, `:72-74`):

| Purpose | Method | Path | Notes |
|---|---|---|---|
| Fetch spec | GET | `/spec` | returns raw JSON text (`fetchSpecText`, `:122-129`) |
| Post control event | POST | `/event` | `Content-Type: application/json`; body is either the raw event dict or `{authenticated_event:{...}}` (`postEvent`, `:136-160`) |
| Poll progress | GET | `/phone-status` | returns `{host_event: {...}, ...}` — **phone-safe** view (upload-token scoped), distinct from the Pi's pull-token `/status` (`fetchPhoneStatus`, `:162-173`) |
| Upload capture | PUT | `/blob` or `/blob?index=N` | `Content-Type: application/octet-stream`, `Content-Length`, `X-Plaintext-Length`, `X-Plaintext-Sha256`; body = raw `IV‖ciphertext` bytes (`putBlob`, `:175-198`) |

`index` (0-based `capture_index = attempt - 1`) is omitted only for the very
first un-indexed legacy key; the v3 plan flow always passes it explicitly
(`putBlob` call site, `capture-page/js/main.js:2085`).

---

## 5. Spec shape (protocol v3 / `capture_plan`)

Full dataclass: `jasper/capture_relay/spec.py:554-683` (`to_dict`). Relevant
fields for the crossover v2 flow (`kind="crossover_sweep"`,
`capture_protocol_version=3`):

```jsonc
{
  "schema_version": 1,
  "capture_protocol_version": 3,
  "kind": "crossover_sweep",
  "sample_rate_hz": 48000,          // REQUIRED_SAMPLE_RATE_HZ, spec.py:79
  "channels": 1,                     // REQUIRED_CHANNELS, spec.py:80
  "duration_ms": 25000,              // spec-level recording BACKSTOP timeout, not per-entry
  "pre_roll_ms": 0, "post_roll_ms": 700,
  "constraints": {...}, "stimulus": {"played_by": "pi", "label": "..."},
  "validity": {...}, "ui": {"theme": {...}, "screen": [...]},
  "calibration_models": [...],
  "return_url": "https://<host>/correction/crossover/",
  "setup_validation": false, "setup_binding_id": "", "setup_collect_positions": false,
  "acknowledgement": {"schema_version": 1, "id": "...", "binding_id": "...", "label": "..."} | null,
  "run_token": "",
  "default_setup": {"calibration": {                 // OPTIONAL household-mic hint
      "mode": "serial"|"upload", "model": "...", "serial_display": "...",
      "calibration_id": "...", "resolvable": true      // omitted key == false
  }},
  "capture_plan": {
    "schema_version": 2, "capture_target": 3, "max_attempts": 4,
    "entries": [                                        // 0-based index!
      {"index": 0, "kind_label": "check",   "duration_ms": 25000, "screen": {"title": "...", "auto_advance": "tap"|"countdown"|"on_apply", ...}},
      {"index": 1, "kind_label": "measure", "duration_ms": 20000, "screen": {...}},
      {"index": 2, "kind_label": "verify",  "duration_ms": 15000, "screen": {"auto_advance": "on_apply", ...}}
    ]
  },
  "max_upload_bytes": 33554432
}
```

Field lookup rules (`jasper/capture_relay/spec.py:373-441`,
`capture-page/js/main.js:1387-1391`):
`capture_plan.entries[]` is **0-based**; the wire protocol's
`begin_capture.index` is **1-based**. `entry_for_index(spec, wire_index)` ==
`entries.find(e => e.index === wire_index - 1)`.
`entry.duration_ms` is presentation/analysis data (per-capture declared
acoustic length), **never** the recording deadline — the deadline is the
spec-level `duration_ms` backstop (comment at
`capture-page/js/main.js:1378-1386`).

---

## 6. Auth envelope for every phone event

Because `capture_protocol_version` (3) `>= 2`, `requiredCaptureProtocol(spec)
>= 2` is true (`capture-page/js/capture-protocol.js:9-14`), so
`client.setTransportIntegrity(integrity, {required: true})`
(`capture-page/js/main.js:2412-2414`) — **every** event this session posts
must be wrapped, no exceptions.

`postEvent(event)` first merges in the page-identity object, THEN wraps:

```js
payload = {...event, capture_page: this.capturePageIdentity};
body = await transportIntegrity.authenticatePhoneEvent(payload, ++sequence);
// body === {authenticated_event: {schema_version, sequence, payload: <json string>, mac}}
```

(`capture-page/js/relay-client.js:136-160`). `sequence` starts at 1 (0 before
any event) and must be strictly non-decreasing per session
(`jasper/capture_relay/session.py:309-346`, `PhoneEventVerifier.verify`).

**`capture_page` identity** — validated server-side by `validate_capture_page`
(`jasper/capture_relay/session.py:469-497`) *before any host callback can play
audio*: `schema_version==1`, `capture_protocol_version` an int present in
`supported_capture_protocol_versions`, `spec.capture_protocol_version` also
present in that list, and `capture_page_build` matching `^[0-9]{8}\.[0-9]+$`.
The real page fetches this from `../version.json`
(`capture-page/js/main.js:50-56`), whose content at this commit is:

```json
{"schema_version": 1, "capture_protocol_version": 3,
 "supported_capture_protocol_versions": [1, 2, 3], "capture_page_build": "20260720.1"}
```

(`capture-page/version.json`). `e0_capture.py` hardcodes this exact value
(with a `--capture-page-build` override) rather than fetching
`capture.jasper.tech/version.json` live, to avoid any network dependency on
capture-page's CDN for a hardware-bench tool — flagged as a documented
simplification, not a protocol ambiguity.

**Python-side verify/build API** (verbatim, `jasper/capture_relay/integrity.py`):
`authenticated_phone_event(content_key: bytes, session_id: str, event:
Mapping, *, sequence: int) -> dict` (`:119-150`) is the exact mirror of the
JS `authenticatePhoneEvent` and is what `e0_capture.py` calls directly (the
JS/Python HMAC framing is byte-identical — same domain separator, same
`kind:sequence` string, same big-endian length-prefixed framing,
`:59-72`/`transport-integrity.js:54-71`). `verify_capture_spec_mac(content_key,
session_id, capture_spec_json, observed_mac)` (`:103-116`) is the spec-MAC
check `e0_capture.py` runs right after fetching the spec (mirrors
`verifyAndParseCaptureSpec`, `transport-integrity.js:160-183`).

---

## 7. The per-capture round (mirrors `runPlanCapture`, `main.js:1957-2159`)

For each `(index, attempt)` starting at `(1, 1)`:

1. **Loop:** POST event `{begin_capture: {index, attempt}, setup: <setup
   payload>}` (`beginAndAwaitAuthorization`, `main.js:1916-1939`). Poll
   `GET /phone-status` (`waitForCaptureAuthorized`, `main.js:1738-1822`)
   every `min(1000, max(100, spec.progress_poll_ms||250))` ms, reading
   `status.host_event`:
   - `phase=="capture_authorized"` with matching `index`/`attempt` → proceed.
   - `phase=="capture_refused"` → **terminal for the whole session**
     (`{code, error}}`; `run_capture_plan` raises `CaptureFailed` the instant
     admission is refused — comment at `main.js:1732-1737`,
     `jasper/capture_relay/session.py:150-164`).
   - `phase=="capture_deferred"` with matching `index`/`attempt` →
     **non-terminal soft hold**. Wait `CAPTURE_DEFERRED_RETRY_POLL_MS` (1500 ms,
     `main.js:1397`) and **re-post the identical `begin_capture`**. This is
     how VERIFY's `on_apply` wait resolves — see §8.
   - `phase=="capture_set_exhausted"` → session over (attempt budget spent
     while waiting).
   - 20 s with no matching event → terminal timeout
     (`main.js:1744`,`1817-1821`).
2. **Once authorized:** start the mic recording, then POST event
   `{armed: true, degraded: false, device: {...}, begin_capture: {index,
   attempt}, setup: <same setup payload>, acknowledgement: <ack or
   omitted>}` (`main.js:2043-2053`). `noise_floor`/`ambient_stats` are
   **optional** (`isinstance(..., dict)` checks default to `None` on
   `jasper/capture_relay/session.py:415-419`, `:852-855`) — `e0_capture.py`
   omits them.
3. **While recording:** poll `GET /phone-status` for
   `host_event.phase` transitions `ambient_started → sweep_started →
   sweep_complete` purely for operator-visible progress
   (`waitForSweepComplete`, `main.js:1277-1352`); `sweep_failed` /
   `sweep_cancelled` are host-driven abort signals.
4. **Stop recording**, wait `spec.post_roll_ms`, encode WAV, encrypt, then
   `PUT /blob?index={attempt-1}` with `X-Plaintext-Length` /
   `X-Plaintext-Sha256` (§4, §9).
5. **Poll `GET /phone-status`** for the verdict
   (`waitForCaptureResult`, `main.js:1830-1892`), timeout
   `max(30000, spec.duration_ms)` ms:
   - `phase=="capture_result"` matching `index`/`attempt` →
     `{accepted: bool, error?: str}`.
   - `phase=="capture_set_complete"` → whole set done (`{accepted,
     capture_target}`).
   - `phase=="capture_set_exhausted"` → attempt budget spent, set not
     complete (`{accepted, capture_target, attempts}`).
6. **Route:** accepted + `index >= target` (or `set_complete`) → done.
   `set_exhausted` → terminal. accepted and more captures remain → advance
   to `(index+1, attempt+1)`, per the **next** entry's
   `screen.auto_advance` policy (`tap`/`countdown`/`on_apply` — presentation
   only, doesn't change the wire loop). Not accepted → retry
   `(index, attempt+1)` (same index, budget permitting).

`begin_capture` is validated strictly server-side: **exactly** the two keys
`index`/`attempt`, both ints, `1 <= index <= capture_target`, `1 <= attempt
<= max_attempts`, `index <= attempt`
(`jasper/capture_relay/session.py:962-1007`).

---

## 8. VERIFY is same-session deferred, NOT a separate session

For the crossover v2 flow specifically: one relay session
(`crossover_v2:session` label, `jasper/web/correction_crossover_v2.py:70`)
spans CHECK (index 1) → MEASURE (index 2) → VERIFY (index 3). Evidence:

- `run_capture_plan`'s docstring: *"the v2 crossover conductor's
  heterogeneous plan parked between MEASURE and VERIFY while its own
  auto-apply is in flight ... no household tap involved"*
  (`jasper/capture_relay/session.py:1098-1109`).
- The VERIFY entry's `screen.auto_advance == "on_apply"`
  (`AUTO_ADVANCE_ON_APPLY`, `jasper/capture_relay/session.py:82`,
  mirrored in JS at `main.js:1559`). When the phone finishes MEASURE and
  advances, `advanceAfterAccepted` sees the upcoming (VERIFY) entry's
  policy is `on_apply` and calls `renderPlanDeferred` +
  `scheduleAutoBegin` (`main.js:1643-1657`, `1585-1593`) — i.e. it
  **immediately re-posts `begin_capture {index:3, attempt:N}` itself**, no
  tap.
- Server-side, `authorize_begin` for that index raises
  `CaptureBeginDeferred` (not `CaptureBeginRefused`) while the conductor's
  own background auto-apply transaction is in flight; the phone sees
  `capture_deferred` and keeps re-posting the identical `begin_capture`
  (§7 step 1) until the apply completes and admission succeeds — budget
  untouched throughout (`jasper/capture_relay/session.py:166-189`,
  `1098-1121`, module docstring "APPLYING" step in
  `docs/handoff-v2.md` lines ~102-113).
- `/crossover/v2/verify` (§2b) is a structurally different, later-use
  endpoint for *re-running* VERIFY from durable post-apply state — its own
  1-entry plan, own relay session, own conductor rebuild in "verify-only
  mode" (`jasper/web/correction_crossover_v2.py:2333-2371`). It is not part
  of a first end-to-end CHECK→MEASURE→VERIFY run and `e0_capture.py` never
  calls it.

`e0_capture.py`'s plan loop therefore needs no special-casing for VERIFY: it
is entry index 2 (0-based) / wire index 3, reached by the *same*
begin/poll/deferred-retry loop as CHECK and MEASURE. The only operator-
visible difference is that admission may sit in `capture_deferred` for
however long the Pi's own apply transaction takes (budget:
`REVIEW_HOLD_BUDGET_S = 30.0`, `jasper/capture_relay/session.py:75`) before
authorizing.

---

## 9. Encryption + integrity (verbatim reuse)

Wire format (`jasper/capture_relay/crypto.py:14-25`,
`capture-page/js/crypto.js:14-19`): `blob = IV(12 random bytes) ‖
AES-256-GCM(plaintext ‖ 16-byte tag)`. `e0_capture.py` builds this directly
with `cryptography.hazmat.primitives.ciphers.aead.AESGCM` (the Pi-side
`decrypt_and_verify` is the *decrypt* half, used only in the self-test
round-trip — §11):

```python
iv = os.urandom(12)
blob = iv + AESGCM(content_key).encrypt(iv, wav_bytes, None)
headers = {
    "X-Plaintext-Length": str(len(wav_bytes)),
    "X-Plaintext-Sha256": hashlib.sha256(wav_bytes).hexdigest(),
}
```

`content_key` comes from `crypto.content_key_from_b64url(fragment["k"])`
(`jasper/capture_relay/crypto.py:65-71`, padding-tolerant base64url decode,
must yield exactly 32 bytes).

---

## 10. WAV encoding the page produces — and the matching `sox` command

`float32ToWavBlob` (`deploy/assets/shared/js/measurement-audio.js:113-146`)
writes a canonical 44-byte-header PCM WAV: format tag `1` (PCM), 1 channel,
`sampleRate = spec.sample_rate_hz` (always 48000 — `validate()` rejects any
other value, `jasper/capture_relay/spec.py:819-823`), `bitsPerSample = 16`,
`blockAlign = 2`. This is byte-for-byte what

```sh
sox -t coreaudio "UMIK-2" -r 48000 -b 16 -c 1 -e signed-integer <out.wav> trim 0 <seconds>
```

produces. `e0_capture.py` uses exactly this command (mono is correct per
the task brief: the UMIK-2's two capsule channels are identical).

---

## 11. Setup / calibration / acknowledgement payloads

- **`setup` field** (piggybacked on every `begin_capture`/`armed` post —
  comment at `main.js:1902-1915`): mirrors `setupWirePayload()`
  (`main.js:137-140`) composed with `applyDefaultCalibrationHintSilently`
  (`main.js:659-668`, `636-643`) — since the crossover_sweep kind has no
  calibration-picker screen (comment at `main.js:645-658`). Logic:
  - if `spec.default_setup.calibration` is present, `mode` is `"serial"` or
    `"upload"`, `resolvable === true`, and `calibration_id` is non-empty →
    send `{"calibration": {"mode": "stored", "calibration_id": <id>,
    "model": <model>}}`.
  - otherwise send `{"calibration": {"mode": "none"}}`.
  Server-side resolution: `_relay_calibration_from_setup`
  (`jasper/web/correction_setup.py:3349-3488`), `mode=="stored"` branch at
  `:3423-3487`.
- **`device` field**: small non-secret metadata
  (`CaptureResult.device` docstring, `jasper/capture_relay/session.py:296-301`
  — example shape `{"label": "UMIK-1", "device_id": "..."}`). Only consulted
  in the `stored`-calibration branch, and only as a **mismatch check**: a
  positive match to a *different* registered mic's aliases skips applying
  the calibration (degrades to uncalibrated, never blocks the capture) —
  an *empty or non-matching* label is fine
  (`_stored_calibration_model_mismatch`, `jasper/web/correction_setup.py:1991-2031`).
  `e0_capture.py` sends `{"label": <mic name, e.g. "UMIK-2">}` so a
  correctly-registered UMIK-2 calibration matches its own model instead of
  risking a spurious cross-match.
- **`acknowledgement` field**: only sent if `spec.acknowledgement` is
  non-null. Mirrors `acceptedAcknowledgement`
  (`capture-page/js/render.js:162-174`): `{"schema_version": 1, "id":
  spec.acknowledgement.id, "binding_id": spec.acknowledgement.binding_id,
  "accepted": true}` — i.e. auto-confirm placement (the human operator
  confirms placement physically before running the tool; there is no
  checkbox to tick). Server-side check:
  `validate_capture_acknowledgement` (`jasper/capture_relay/session.py:500-529`)
  — `id`/`binding_id` must match the spec's values via
  `secrets.compare_digest`.

---

## 12. Ambiguities / things not fully resolved from static reading

1. **Exact recording-stop timing.** The real page starts `recorder.start()`
   before `armed`, then stops it after `waitForSweepComplete` observes
   `sweep_complete` **plus** `post_roll_ms` — i.e. the true recorded length
   is *whatever the Pi's actual playback took*, discovered by polling, not
   a value known in advance. `sox` needs an upfront duration. `e0_capture.py`
   instead records a **fixed** window sized generously
   (`max(entry.duration_ms, spec.duration_ms)/1000 + post_roll_ms/1000 +
   --record-margin-sec`) starting at/just-before the `armed` post. This is
   a deliberate simplification (not a protocol ambiguity — the actual
   choreography is unambiguous from the code), flagged because it means
   our recording is a fixed superset window rather than tracking the real
   sweep boundaries. If the Pi's actual CHECK/MEASURE/VERIFY acoustic
   window ever runs *longer* than `entry.duration_ms` by more than the
   margin, the capture will be truncated — worth confirming against a live
   run's actual `sweep_complete` timing before trusting the margin.
2. **`capture_page_build` freshness.** Hardcoded from
   `capture-page/version.json` at this commit (`"20260720.1"`). If the
   deployed capture page has since republished a newer build, the value is
   still schema-compatible (`validate_capture_page` only checks the
   `[0-9]{8}\.[0-9]+` shape and protocol-list membership, not the exact
   string) so this should not break admission — but it is worth a live
   confirmation since the Cloudflare Pages deploy is independent of the Pi.
3. **Whether `device`/`noise_floor` omission has any second-order effect**
   on CHECK's level-solver (`ambient_stats`) beyond the calibration-mismatch
   check covered in §11 — the code paths read are all defensively
   `isinstance(..., dict)`-gated to `None`/degrade, never a hard failure,
   but this was not verified against a live CHECK phase.

---

Last verified: 2026-07-21, against `origin/main` @ `856903ca1`, by static
reading only (no live requests were made).
