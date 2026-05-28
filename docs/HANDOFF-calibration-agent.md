# HANDOFF: LLM-driven calibration agent

> **Status: research + early substrate (2026-05-28).** This is the
> design-space document for the guided calibration/tuning arc. Phase
> 0a substrate has landed: calibrated mic registry/parser,
> Dayton/miniDSP serial lookup, manual upload fallback, input-device
> picker, bundle metadata, capture-quality checks, bounded correction
> strategies, design-audit reports, first-pass confidence reports,
> `position_analysis.json` artifacts, `runtime_integrity.json`
> evidence, `acoustic_quality.json` evidence, correction
> visualization/confidence UX, a deterministic
> `jasper.correction.evidence` packet, and a read-only
> `jasper-calibration-agent` intake CLI. The LLM agent itself is still
> not implemented.
>
> **What this proposes:** build toward a guided speaker-tuning system
> on top of the existing `/correction/` wizard. The first layer is
> deterministic: calibrated mic ingest, device selection, richer
> measurement bundles, and science-based visualizations. The later
> layer is an LLM "audio engineer" (best available Claude Opus /
> GPT-5 / Gemini Pro — whichever the user has a key for) that
> interprets the measurement, asks about room shape and listening
> position, critiques the auto-filter, suggests alternatives, and
> iterates across re-measurements. Restraint-first philosophy: the
> agent's job is to
> talk users *out* of over-EQ at least as often as into adjustments.
>
> **What this does NOT propose:** the LLM agent should not replace the
> measurement pipeline, the PEQ designer, the CamillaDSP hot-swap path,
> or any other shipped subsystem in
> [`HANDOFF-correction.md`](HANDOFF-correction.md). Deterministic
> substrate can evolve underneath it; the agent sits *above* that
> surface and calls into it via tools — same arms-length shape as the
> voice tools.

---

## TL;DR

1. The shipped `/correction/` substrate is good (Phase 0–2.5 in
   [`HANDOFF-correction.md`](HANDOFF-correction.md), with focused
   correction tests green on synthetic data). What it lacks is the
   **judgment layer**:
   today's UX is "auto-PEQ proposes ≤5 cuts, you tap Apply" with no
   coaching, no room-shape context, no critique of the proposal.
2. **The May 2026 research pass found no product filling that gap
   cleanly.** Sonos Trueplay, Dirac, Sonarworks, Genelec GLM,
   Audyssey, Neumann MA 1 — every commercial room-correction tool we
   reviewed is a closed-loop one-shot batch process. The closest
   architectural prior art is in *spectroscopy* — LUMIR / IR-Agent /
   EIS-LLM — where the same
   measure → interpret → propose → iterate pattern was built in 2025.
3. Substrate to graft: calibrated microphone ingest, richer
   measurement artifacts, ~30–50 pages of distilled audio-engineering
   knowledge as markdown, a provider-abstracted agent harness (mirror
   [`jasper/voice/`](../jasper/voice/)'s `LiveConnection` shape), a
   small tool registry that calls into the existing
   [`MeasurementSession`](../jasper/correction/session.py), and a chat
   panel in the existing wizard.
4. The first supported measurement mics should be **Dayton Audio**
   iMM-6 / iMM-6C / UMM-6 and **miniDSP** UMIK-1 / UMIK-2. For these,
   the UX should be "enter mic model + serial number, JTS fetches the
   calibration file for you" whenever the vendor endpoint allows it.
   Users should not have to download a file into their phone and then
   re-upload it if the speaker can fetch it server-side. A manual
   calibration-file upload path still matters for unsupported mics and
   vendor lookup failures.
5. Calibrated input is a prerequisite for the agent's recommendations
   to be trustworthy — bad inputs + confident model = bad advice
   delivered with authority. Calibration-mic ingest should land
   *before* or *with* the agent, not after.
6. Provider posture: **add Anthropic Claude as a first-class option**
   alongside the existing Gemini / OpenAI keys reused from
   `/var/lib/jasper/voice_provider.env`. Opus-class Claude models are
   genuinely well-suited to expert-reasoning + tool-use tasks like
   this. xAI Grok stays voice-only for now (its strongest model
   lineage isn't matched in the chat surface).

---

## Why this is worth building

The honest answer: because the auto-correction UX has a measurable
ceiling, and judgment is what gets you above it.

The shipped greedy peak-fit PEQ designer
([`jasper/correction/peq.py`](../jasper/correction/peq.py)) is good
at the easy cases — a single dominant modal peak in the bass — and
applies the right constraints (cuts-only, 20–350 Hz, Q ≤ 8,
≤5 filters, max -10 dB cut). But it can't tell you:

- *Why* the 80 Hz peak is there (modal? SBIR? a sub crossover quirk?)
- Whether the -14 dB notch at 240 Hz is a real cancellation (don't
  EQ) or a measurement artefact (move the mic and re-sweep)
- Whether the user should move the speaker before reaching for EQ
- How much of the response above the Schroeder frequency
  (~100–300 Hz domestically) is the speaker, the room, or the
  measurement mic — and which of those EQ can fix
- What a *good* in-room target slope looks like for *this* room
  (Harman / Olive-Welti is roughly -1 dB/octave from 20 Hz to
  20 kHz, but the exact slope depends on speaker directivity and
  room liveness)
- Whether two measurements taken at the same listening position 30
  minutes apart agree well enough to trust the resulting filter

Every one of those is a judgment call. An LLM with the right knowledge
base and the right tools is well-suited to making them out loud, with
the user, in a way that the user can push back on.

## North star: assisted tuning, not just auto-correction

The long-term product vision is bigger than "generate a PEQ from a
sweep." The system should help people make the speaker sound good to
them, while keeping the technical layers honest and separate:

1. **Room correction** — compensate for repeatable speaker + room
   behavior where EQ is physically useful. This starts with today's
   restrained bass-region PEQ and can later grow into FIR / mixed-phase
   work when the measurement artifacts and safety rails support it.
2. **Target / house curve selection** — decide what "good" means for a
   room and listener. A Harman / Olive-Welti-style downward slope,
   B&K-style target, `neutral` / `warm` / `bright`, or a user-created
   target are taste decisions layered on top of measurement quality.
3. **Preference EQ / voice tuning** — let the user give qualitative
   feedback after listening to music they know: "I wish it had more
   bass," "the vocals feel recessed," "make it a little brighter."
   The system can translate that feedback into a safe, reversible
   preference layer without pretending it is correcting the room.
4. **Active speaker commissioning** — for JTS speakers where the
   woofer and tweeter are on separate amplifier/DSP channels,
   CamillaDSP also owns crossover filters, per-driver gain, delay,
   polarity, limiter behavior, and phase alignment. This is not room
   correction; it is the speaker's baseline acoustic design. The
   measurement tools should eventually help tune the crossover and
   driver integration before room correction or preference EQ runs.
   Current planning lives in
   [`HANDOFF-active-speaker-dsp.md`](HANDOFF-active-speaker-dsp.md).

Future UX sketch: the user says, "Jarvis, help me tune my speaker."
JTS opens the tuning flow, asks the user to play a track they know,
listens to qualitative feedback, shows what it plans to change, and
lets the user A/B the result. The voice loop can be the entry point,
but the actual calibration/tuning surface should remain visual because
plots, filter diffs, target curves, and measurement quality warnings
are part of the trust model.

The knowledge corpus should preserve all of these layers:
science-based room-correction guidance, active speaker DSP guidance,
and human-language preference vocabulary. That gives a future agent
enough context to say, for example, "that sounds like a preference for
a warmer target, not evidence that the 80 Hz room mode needs another
cut," or "that 2 kHz cancellation might be crossover/driver
integration, not the room." Persistent household taste and room notes
should live in a runtime/user-private store under `/var/lib/jasper/...`,
not in repo docs; the repo should contain the schema, concepts, and
public guidance.

The deeper point is **restraint**. Toole's central thesis (*Sound
Reproduction*, 3rd ed., 2017) is that good loudspeakers in reasonable
rooms need very little correction above the Schroeder frequency, and
aggressive auto-EQ *degrades* perceived sound by flattening
peaks-and-dips that are spaciousness cues from lateral reflections.
Every commercial auto-correction product violates this principle by
default because "more correction" is a better sales pitch than "less".
A self-hosted, hobbyist-priorities speaker has no such constraint —
the agent's first instinct can and should be "actually, your room
sounds fine, let's not touch anything below 150 Hz."

---

## What we already have (the substrate)

Re-read [`HANDOFF-correction.md`](HANDOFF-correction.md) for the
full picture. The bits that matter for this proposal:

### Web wizard — `jasper-correction-web`

- **Socket-activated** (zero RAM idle; spawns on first request to
  `https://jts.local/correction/`, 10-min idle timeout). Unit files
  at [`deploy/jasper-correction-web.service`](../deploy/jasper-correction-web.service)
  and [`.socket`](../deploy/jasper-correction-web.socket).
- stdlib [`ThreadingHTTPServer`](../jasper/web/correction_setup.py) on
  127.0.0.1:8770 behind nginx → `https://jts.local/correction/`.
  HTTPS is mandatory because `getUserMedia` requires a secure
  context; the page documents the iOS trust dance.
- Routes (all in [`jasper/web/correction_setup.py`](../jasper/web/correction_setup.py)):
  `GET /`, `GET /healthz`, `GET /status`, `GET /sessions`,
  `POST /start`, `POST /next-position`, `POST /repeat-position`,
  `POST /verify`, `POST /upload-noise`, `POST /upload-capture`,
  `POST /apply`, `POST /reset`,
  `POST /test-tone`, `POST /autolevel/start`, `POST /autolevel/lock`,
  `POST /autolevel/cancel`.
- Frontend is an inline HTML / vanilla JS page emitted from the
  `_PAGE_HTML` template with `__HOSTNAME__` / `__REQUIRED_SR__` /
  navigation substitutions in `_render_page()`. It uses a canvas
  chart and an AudioWorklet for mic capture at 48 kHz
  (constraint-pinned; the page hard-rejects sample-rate / EC / NS /
  AGC overrides per WebKit Bug 179411 mitigation).
- Browser polls `GET /status` every 500 ms for state snapshots; no
  SSE today.

### DSP pipeline — [`jasper/correction/`](../jasper/correction/)

| Module | Responsibility |
|---|---|
| `sweep.py` | Novak 2015 synchronized ESS, 20 Hz – 20 kHz, 10 s, -12 dBFS, 48 kHz, deterministic + on-disk cache under `/var/lib/jasper/correction/sweeps/`. |
| `playback.py` | `aplay -D correction_substream` (dedicated fan-in lane; sweep traverses the same pipeline real music does); 1 kHz test-tone cache under `/var/lib/jasper/correction/tones/`. |
| `deconv.py` | FFT + Tikhonov-regularized inversion (`H(f) = Y(f) conj(X(f)) / (\|X(f)\|² + ε)`), IR window 5 ms pre / 500 ms post direct arrival. |
| `analysis.py` | 1/48-octave power-mean smoothing, log-spaced 480-point resampling, multi-position power-mean spatial averaging. |
| `peq.py` | Greedy peak-fit: residual = measured − target → find max peak → estimate Q from -3 dB bandwidth → add peaking biquad → repeat. Cuts-only by default, 20–350 Hz, ≤5 filters, Q ∈ [1.0, 8.0], max -10 dB. |
| `target.py` | Named targets: `flat` / `neutral` / `warm` / `bright` (interpolations over a Harman-shaped base). |
| `camilla_yaml.py` | YAML emission by string concatenation (no pyyaml dep, keeps output reviewable). Preserves `master_gain` mixer; inserts `peq_1 … peq_N` biquads in front of the existing `flat` filter. |
| `coordinator.py` | `measurement_window()` async context manager — preconditions (no active voice session), pauses renderers via `systemctl stop`, sends UDS `MEASURE_PAUSE` to `jasper-voice`, restores in `finally`. |
| `session.py` | `MeasurementSession` + `SessionState` enum + `AutolevelStatus` + bundle writers. The big one. |

### Critical correctness property: `/start` always resets to flat

[`_handle_start()`](../jasper/web/correction_setup.py) hard-resets
the CamillaDSP config to `/etc/camilladsp/v1.yml` (identity) *before*
playing the sweep. This means every measurement captures the raw room,
never the corrected pipeline. The agent must understand this — its
"compare verify against measured" reasoning only works because both
were captured against the same flat baseline.

### Storage layout

```
/var/lib/jasper/correction/
├── sweeps/              # Deterministic sweep cache (one per param tuple)
├── tones/               # 1 kHz test-tone cache (for auto-level)
├── captures/            # Fallback per-position WAVs (when bundle save fails)
└── sessions/<id>/       # Per-session debug bundle
    ├── info.json        # state, params, peqs, sweep_meta, autolevel snapshot
    ├── result.json      # measured / target / predicted / verify curves
    ├── captures/        # p0.wav, p1.wav, …, p4.wav (per-position mono WAVs)
    ├── verify.wav       # post-Apply re-measurement
    └── applied.yml      # copy of the CamillaDSP config that was applied

/var/lib/camilladsp/
├── configs/correction_<id>_<unixtime>.yml   # never deleted; history
└── statefile.yml                            # current config_path:
```

Bundles are **useful but not yet complete** for offline agent or FIR
replay. `info.json` + `result.json` + WAV captures are enough for a
first PEQ critique, but the research pass calls for richer artifacts
before treating a bundle as a full analysis packet: raw float capture
where possible, raw/deconvolved impulse responses before smoothing,
complex transfer functions, per-position IRs, spatial-average method,
window/gate/FDW settings, target data, headroom, active speaker
profile, measurement environment/noise notes, and an audit log. This
will matter: the simplest possible v0 of the agent is still "feed a
session bundle to a model, get a written critique back, no UI," but it
should label missing artifacts rather than pretending the packet is
FIR-ready.

### Constraints already baked in

The agent should treat these as *floors*, not ceilings — its
recommendations should keep or tighten them, never loosen:

- cuts-only (no boosts in Phase 1–2.2)
- 20–350 Hz band only (modal region)
- ≤5 filters
- max -10 dB per filter
- Q ∈ [1.0, 8.0]
- `master_gain` mixer preserved (it's the ducking knob — see
  [`HANDOFF-volume.md`](HANDOFF-volume.md))

### Tests

121 hardware-free correction pytest functions are green on synthetic
data as of 2026-05-25. **Hardware verification — actually running a
sweep, taking the mic to multiple positions, applying the filter,
listening — is the gating step before this agent makes sense.** The
agent's recommendations are bounded by the measurement quality.
If the measurement pipeline is silently wrong end-to-end on real
hardware, the agent will confidently tell you to apply a filter that
makes the room sound worse.

---

## Measurement hardware — supported and bring-your-own mics

The primary supported measurement mics should be:

- **Dayton Audio iMM-6 / iMM-6C / UMM-6.** Dayton's public
  calibration tool accepts a microphone model and serial number. The
  iMM-6C product instructions explicitly say to select `iMM-6`, enter
  the iMM-6C serial from the case, then download the file.
- **miniDSP UMIK-1 / UMIK-2.** miniDSP documents the serial lookup
  flow on the UMIK product pages. UMIK-1 uses a seven-digit serial in
  `XXX-XXXX` form; UMIK-2 downloads two files named like
  `800xxxx.txt` and `800xxxx_90deg.txt`.
- **Unsupported / advanced mics.** The fallback UX is manual upload:
  pick "Other calibrated mic," upload a REW/HouseCurve-style text
  file, choose orientation/sign convention if needed, and store it
  under the same calibration registry.

The happy-path UX should be server-side serial lookup, not phone file
management:

```
User opens /correction/ on phone
  → chooses "miniDSP UMIK-1" or "Dayton iMM-6C"
  → enters serial number
  → JTS fetches the calibration file from the vendor endpoint
  → JTS previews the parsed curve and stores it for future sessions
```

This avoids the painful mobile flow where the user downloads a random
`.txt` into iOS/Android storage and then has to find it again in a
file picker. The manual upload path is still essential, but it should
be the fallback, not the expected path for known mics.

Implemented shape:

1. Added a small calibration registry under `jasper/correction/`, with
   one provider adapter per lookup family:
   `dayton_audio`, `minidsp`, and `manual_upload`.
2. Store calibration files and metadata under
   `/var/lib/jasper/correction/calibration_mics/`, including model,
   serial hash, redacted public source marker, fetch timestamp, raw
   file hash, parsed point count, orientation, and normalized sign
   convention.
3. Parse calibration files into one internal shape:
   `frequency_hz[]`, `correction_db[]`, optional `phase_deg[]`, and
   explicit sign convention. Provider adapters own vendor quirks; the
   DSP code only sees the normalized additive correction.
4. Apply the correction before target normalization / PEQ design so
   every downstream visualization, bundle, and agent tool is looking
   at calibrated measurement data.
5. Record a hash of the selected browser `deviceId`,
   browser-reported label, mic model, orientation, and calibration
   hash in every session bundle so the measurement can be replayed or
   challenged later without leaking raw device identifiers.

Calibration files are not a formal universal standard, but there is
a practical quasi-standard: frequency in Hz plus gain/correction in
dB, space/tab/comma delimited, with comments and sometimes a third
phase column. The parser should accept this broad family, reject
ambiguous files loudly, and normalize sign convention at import time.

**Status:** calibration-mic ingest is now the **Phase 0a substrate**.
It remains prerequisite work for the future LLM agent because the
agent's quality is bounded by the measurement; shipping the agent on
top of uncalibrated input would be a recipe for confidently bad
advice. The shipped substrate is intentionally small and modular:
calibration registry, vendor fetchers, parser, device picker, manual
upload fallback, and one additive correction hook in the analysis path.

---

## The four agent layers

```
[ Browser chat panel inside /correction/ ]
        │ POST {user_message, session_id}
        ▼
[ jasper-correction-web — existing wizard ]
        │ delegates to
        ▼
[ CalibrationAgent — provider-abstracted, mirrors LiveConnection/LiveTurn ]
        ├─ system prompt: assembled from docs/calibration-agent/*.md
        ├─ tools: get_measurement / propose_peq / apply_peq /
        │         request_remeasurement / compute_schroeder /
        │         analyze_peaks_nulls / look_up
        └─ provider adapters: anthropic_agent.py / openai_agent.py /
                              gemini_agent.py
                ▼
[ Frontier text model — user-chosen ]
```

### Layer 1 — Knowledge base (markdown files)

Live in `docs/calibration-agent/`. The initial corpus exists; keep
filling it with short, source-backed concept files as research is
distilled.

```
docs/calibration-agent/
├── README.md                    # how the loader assembles the prompt
├── concepts/
│   ├── measurement-quality.md   # clipping, SNR, repeatability, mic cal
│   ├── room-correction-limits.md # peaks, nulls, SBIR, transition region
│   └── spatial-averaging.md     # RMS vs vector averages, multi-position
├── targets/
│   └── house-curves.md          # B&K/Harman-like families + taste
├── filter-design/
│   ├── fir-room-correction.md   # minimum / linear / mixed phase ladder
│   └── preference-eq.md         # taste layer, not room correction
└── jts-specific/
    ├── implementation-ladder.md      # staged PEQ → FIR → LLM path
    └── runtime-context-schema.md     # public schema for private runtime notes
```

The corpus can grow from here, but prefer fewer high-signal files over
a sprawling tree. Total corpus target is still 30–50 pages / ~25–40K
tokens, which fits comfortably in any frontier model's context window.
**No RAG / retrieval — concatenate the whole corpus into the system
prompt at agent startup.** Revisit retrieval only if the corpus grows
past ~150K tokens, which it shouldn't.

The `jts-specific/` directory is the bit that makes the agent's
advice grounded in *our* pipeline rather than general audio theory.
[`implementation-ladder.md`](calibration-agent/jts-specific/implementation-ladder.md)
is particularly important: it prevents the agent from confidently
suggesting FIR phase correction that the implementation can't
actually deliver.

Household-specific context is **not** stored in repo docs. Gear, room
dimensions, taste preferences, "the speaker sits 60 cm from the back
wall behind the desk," and prior user feedback belong in a
user-private runtime file such as
`/var/lib/jasper/correction/runtime_context.json` (exact path TBD).
The repo corpus defines the public schema and interpretation rules;
the install owns the private values.

**Source bibliography** (each cited in the markdown corpus, not
loaded verbatim):

- Toole, *Sound Reproduction: The Acoustics and Psychoacoustics of
  Loudspeakers and Rooms*, 3rd ed., Routledge / AES Presents (2017)
- Linkwitz Lab — first-principles speaker/room design
- Genelec GLM 4.x whitepapers, Neumann MA 1 documentation
- Dirac Research, "On Room Correction and Equalization of Sound
  Systems" (PDF)
- REW Help — "Why Can't I Fix All my Acoustic Problems with EQ?"
- Welti & Devantier, AES papers on bass / multi-sub optimization
- GIK Acoustics + Acoustic Frontiers SBIR explainers
- Sean Olive / Todd Welti, AES papers on in-room target curves
  (the "Harman target")

### Layer 2 — Agent harness

Mirror the [`jasper/voice/`](../jasper/voice/) pattern — protocol +
adapters + shared helpers:

```
jasper/calibration_agent/
├── __init__.py
├── agent.py            # CalibrationAgent protocol: send_message, tools
├── prompt.py           # assemble_system_prompt() reads the markdowns
├── tools.py            # tool registry + per-tool implementations
├── anthropic_agent.py  # best available Claude Opus snapshot
├── openai_agent.py     # best available GPT-5 snapshot
├── gemini_agent.py     # Gemini 2.5 / 3.x Pro (text)
└── session.py          # ChatSession — history, tool-call loop, budget cap
```

Reuse the existing `voice_provider.env` keys for OpenAI / Gemini.
Add **`ANTHROPIC_API_KEY` as a net-new variable** in the same file
(`/var/lib/jasper/voice_provider.env`) — there's no Anthropic key in
the project today (`grep -r anthropic jasper/` returns nothing). The
existing `/voice/` wizard at
[`jasper/web/voice_setup.py`](../jasper/web/voice_setup.py) gains a
fourth card for the Anthropic key, but Anthropic is not selectable
as a *voice* provider (there's no Claude Realtime Audio API).

`xAI Grok` stays voice-only — its strongest model lineage
(`grok-voice-think-fast-1.0`) is realtime; the chat-surface models
are weaker than the others for expert-reasoning tasks. Worth
revisiting later, not on day one.

### Layer 3 — Tools

Read-mostly, with side-effecting tools always gated on explicit user
confirmation. All tools are thin wrappers around existing
`MeasurementSession` / `CamillaController` methods:

| Tool | Side-effecting? | What it does |
|---|---|---|
| `get_measurement_summary()` | no | Returns FR peaks/nulls list, RT60 by octave band (computed from windowed IR), Schroeder freq estimate, target curve choice, current PEQ proposal, applied-yet bool |
| `get_measurement_plot(format='png')` | no | Renders the FR chart as a PNG byte stream for multimodal models |
| `analyze_peaks_nulls(f_low, f_high, threshold_db)` | no | Filtered peak list with frequency, Q estimate, gain dB |
| `compute_schroeder(rt60_seconds, volume_m3=None)` | no | Returns fs estimate; volume can be user-supplied or estimated |
| `propose_alternative_peq(filters)` | no | Simulate user/agent-suggested filter set; returns predicted post-correction curve overlay |
| `look_up(topic)` | no | Keyword lookup into the markdown corpus; lets the agent cite specific guidance back at the user |
| `apply_peq(filters)` | YES (with confirm) | Write YAML + `set_config_file_path` + `reload`. Requires explicit `confirm: true` in the tool args + `confirmed_at` timestamp; daemon re-confirms via the UI |
| `request_remeasurement(reason, position_hint)` | indirect | Posts a "the agent wants another measurement" message into the wizard; user has to act on it; **does not** itself trigger a sweep |

The `apply_peq` confirmation gate matters: the agent should never
apply a filter without the user clicking "Yes, apply this." Same
posture as the voice tools' `confirm` field (see
[`HANDOFF-prompting.md`](HANDOFF-prompting.md)). Agency lives with
the user.

`request_remeasurement` is intentionally indirect — the agent can
*ask*, but the user has to walk to the listening position with the
phone and tap `Start`. No "the agent took over your speaker" energy.

### Layer 4 — Chat panel in the existing wizard

A new panel added to
[`jasper/web/correction_setup.py`](../jasper/web/correction_setup.py)
that appears once `state == READY` (measurement done, PEQ computed,
not yet applied). The panel shows:

- The agent's **opening read** of the measurement, with citations
  ("the 8 dB peak at ~92 Hz looks like a length-mode resonance for a
  ~6 m room dimension; the -14 dB notch at 240 Hz looks like SBIR
  from a wall ~70 cm behind the speaker — let's check that")
- Conversational input box (markdown-rendered output)
- A "**context the agent has**" disclosure: room shape, listening
  position, taste preferences — pre-filled from the private runtime
  context file if present, editable inline
- **Filter-diff visualization** when the agent proposes alternatives:
  predicted-curve overlay alongside the auto-PEQ's, side-by-side
  filter table
- **"Apply this filter set"** button when the user is ready;
  surfaces both the auto-PEQ and any agent-proposed alternatives as
  selectable options

Frontend stays inline HTML / vanilla JS + stdlib HTTP unless the
correction UI is separately migrated. Chat is naturally polling-
friendly; the existing 500 ms `/status` poll can grow a `chat`
sub-section. No SSE / WebSocket needed for v1 — revisit only if turn
latency feels bad.

New routes on `jasper-correction-web`:

- `POST /chat` — body: `{message: str}`. Posts a user turn into the
  active session's `ChatSession`, returns the agent's reply
  (synchronous; ~3–10 s latency for a frontier text model).
- `GET /chat` — returns the chat history for the active session
  (folded into `/status` if simpler).
- `POST /chat/reset` — clear the chat history (start a fresh
  conversation against the same measurement).
- `POST /chat/context` — update the private runtime context from the
  UI disclosure.

---

## Architectural fit

Where the agent slots into the existing flow (additions in bold):

```
IDLE
  ↓ POST /start
PREPARING → SWEEPING → AWAITING_CAPTURE
  ↓ multi-position iteration
  ↓ all positions captured
ANALYZING
  ↓ smoothing + spatial-avg + auto-PEQ design
READY                              ← agent kicks in here
  │
  │ [NEW] POST /chat → ChatSession opens, agent reads
  │       the session bundle, posts opening analysis
  │ [NEW] user converses; agent calls tools, proposes
  │       alternative PEQs, requests re-measurements
  │
  ↓ POST /apply (auto-PEQ OR agent-proposed set)
APPLIED
  ↓ optional POST /verify
VERIFYING → VERIFIED               ← agent re-reads verify curve,
                                     comments on whether the filter
                                     achieved what it claimed
```

Two key constraints from existing architecture:

1. **`/start` always resets to flat** ([`_handle_start`](../jasper/web/correction_setup.py)).
   The agent must never bypass this — fresh measurements always
   capture raw room. If the agent wants to know how the *corrected*
   pipeline measures, it goes through `/verify` (which deliberately
   doesn't reset).
2. **`measurement_window()` precondition: no active voice session.**
   ([`jasper/correction/coordinator.py`](../jasper/correction/coordinator.py))
   The agent cannot trigger a re-measurement while "Jarvis" is in a
   live session. `request_remeasurement` surfaces a "user should
   trigger another sweep" message — it doesn't itself open a
   measurement window.

### What the agent does NOT touch

- The PEQ designer (`peq.py`) — agent proposes filter *values*, not
  algorithm changes
- The CamillaDSP pipeline topology — `master_gain` mixer stays where
  it is; PEQs slot in front of `flat` as today
- The renderer pause/resume sequence — `measurement_window()` is the
  one true gate
- Voice-loop tool routing — the calibration agent is its own surface,
  not a voice tool

This keeps the blast radius narrow. If the agent is wrong, the worst
outcome is the user applies a bad filter, which they can revert with
`POST /reset` in two seconds. No other subsystem can be corrupted
because the agent has no other reach.

---

## Provider selection

### The shape of the choice

Today's voice loop runs against one of three real-time speech-to-speech
APIs (`gemini` / `openai` / `grok`) via the `LiveConnection` /
`LiveTurn` abstraction in
[`jasper/voice/`](../jasper/voice/). The calibration agent is a
**different surface** — it needs a text/chat model with tool use,
not a realtime audio one. So it gets its own provider abstraction +
its own model picker, even when it reuses the same API key.

### Recommended: add Anthropic Claude as a first-class option

Use the best available Claude Opus-class snapshot at implementation
time, pinned to a concrete model ID after checking current Anthropic
docs. Claude is a strong fit for the kind of structured-reasoning +
tool-use task this agent does. The user has explicitly said: *open
to allowing users to add their Anthropic key and use that frontier
model if we thought that would get them a meaningfully better
result.* The judgement here is: yes, meaningfully better, **because:**

- This task is reasoning-heavy, not throughput-heavy. A single
  calibration session is maybe 15–30 turns. The unit cost difference
  (~$0.30–1.00 with Opus vs ~$0.05–0.15 with Gemini Pro) is real but
  small in absolute terms.
- Anthropic's tool-use semantics are clean and well-documented; the
  agent's tool-call loop is simpler against the Anthropic SDK than
  against either OpenAI Assistants or Gemini Function Calling.
- Opus is particularly strong at "I don't have enough information,
  let me ask the user a clarifying question" — the *right* mode for
  this agent, vs the "confidently assert and apply" failure mode
  every commercial auto-correction product falls into.

So the order of operations is:

1. **Day 1 of Phase A: write the Anthropic adapter first.** It's the
   reference implementation; the other adapters port to its shape.
2. Add OpenAI (`responses` API + tool use) and Gemini (text-side
   function calling) adapters in Phase B for users who don't want a
   fourth API account.
3. xAI Grok: defer indefinitely. Re-evaluate when Grok's chat-surface
   model lineage catches up to its voice one.

### Selection logic at runtime

`MeasurementSession` consults the configured keys + an explicit user
preference (settable in the `/correction/` wizard):

```python
# Effective model resolver
def pick_model() -> tuple[str, str]:  # (provider, model)
    pref = config.get("JASPER_CALIBRATION_AGENT_PROVIDER")
    if pref and key_present(pref):
        return pref, default_model(pref)
    # Fallback: smartest-available, in this priority order
    for p in ("anthropic", "openai", "gemini"):
        if key_present(p):
            return p, default_model(p)
    raise NoProviderConfigured(
        "Add a key at https://jts.local/voice/ to enable the "
        "calibration agent."
    )
```

`/correction/` wizard shows the effective provider/model in the
chat-panel header. Picker is optional; default to smartest-available
so the household doesn't have to think about it.

### Cost discipline

Realistic per-session cost ranges (15–30 turns, ~5K–20K context):

| Provider | Model | Range |
|---|---|---|
| Anthropic | best available Claude Opus | $0.30 – $1.00 |
| OpenAI | GPT-5 (assumed text-side flagship) | $0.20 – $0.60 |
| Google | Gemini 2.5 / 3.x Pro | $0.05 – $0.15 |

Add a `JASPER_CALIBRATION_AGENT_MAX_USD_PER_SESSION` cap (default
~$2.00, soft warning at $1.00). The agent's tool-call loop checks
the running session cost before each model call; refuses to continue
when capped, posts an audible cue / chat message ("we've spent $X on
this calibration session, raise the cap to continue"). Same pattern
as the voice-eval harness, just per-session instead of per-test-run.

There's no provider lock-in: switch provider mid-session is
**permitted but starts a fresh chat** (different model = different
tokenizer + different system-prompt cache). The chat history doesn't
transfer.

---

## Design decisions to settle before building

1. **Multimodal: send the FR plot as PNG, JSON sidecar, or both?**
   **Recommend both.** OpenAI cookbook + the spectroscopy-agent
   literature converge here: images for gestalt pattern recognition
   ("is there a deep narrow notch around 80 Hz?"), JSON for precise
   quantitative reasoning. Token-cheap to include both. The PNG
   render path needs a new tool (`get_measurement_plot`) that
   produces a clean matplotlib chart sized for the model's image
   pipeline.
2. **Where does the agent kick in?** **Recommend after auto-PEQ is
   computed, before user applies.** That's where human judgment adds
   the most value — auto-filter is the starting point, agent's job
   is critique + refinement. Pre-measurement (walk a first-timer
   through positioning) is a nice-to-have for later phases.
3. **Agent autonomy.** **Recommend propose-only.** Agent never
   applies a filter without explicit user confirmation. Matches the
   voice tools' `confirm` posture, matches the project's restraint
   philosophy, keeps blast radius tiny.
4. **Chain-of-thought visibility.** **Recommend yes — show the
   reasoning, marked clearly as such.** Audio engineering is one of
   those domains where the *reasoning* is half the value. The user
   should be able to read "I think this is SBIR because the dip
   frequency matches a path-length of ~70 cm and you mentioned the
   speaker is on a desk near a wall" and either agree or push back.
   Don't hide the chain — surface it in a collapsed expander.
5. **Voice-loop integration.** **Recommend defer.** The natural
   "Jarvis, help me tune my speakers" handoff is appealing but adds
   significant complexity (voice tool that opens a chat session,
   bridging two LLM surfaces). Ship the chat UI first; revisit voice
   handoff as Phase E once chat is proven out.
6. **History across sessions.** Multiple calibration sessions over
   weeks/months — should the agent remember? **Recommend a
   conservative yes:** a user-private runtime context file is the
   persistent memory slot, edited by user or by
   agent-with-confirmation. Individual chat transcripts persist in
   the session bundle (`agent_transcript.json`) but aren't loaded
   into context for future sessions unless explicitly referenced.
   Same shape as repo docs → private memory split.

---

## Proposed phased build

Each phase is independently shippable, each ends in a measurable
user-visible improvement, each is small enough that a stall doesn't
strand the work.

### Phase 0a — Calibration mic + device picker (DONE)

Before any LLM work:

- Add a browser input-device picker and persist the selected
  `deviceId` for the session. The page should make it obvious which
  microphone the browser actually granted, because "USB mic plugged
  in" does not guarantee "browser selected USB mic."
- Add server-side vendor lookup for known mics:
  Dayton Audio iMM-6 / iMM-6C / UMM-6 and miniDSP UMIK-1 / UMIK-2.
  The user enters model + serial; JTS fetches, parses, previews, and
  stores the calibration file.
- Add manual upload as the fallback for unsupported mics, offline
  installs, vendor lookup failures, and advanced calibration files
  from third-party labs.
- Parse → normalize → resample the calibration curve to the analysis
  log grid → apply as an additive correction before target
  normalization and PEQ design.
- Make any built-in phone-mic compensation a **fallback** rather than
  the default when an external calibrated mic is selected.

**Status:** implemented 2026-05-25 in the room-correction substrate.
Keep future changes inside the provider/parser boundary rather than
letting vendor quirks leak into measurement math.

### Phase 0b — Bundle contract + measurement confidence (PARTIAL)

Before any LLM work:

- Upgrade the session bundle so it is replayable and agent-ready:
  raw captures, derived impulse responses, complex transfer functions
  when available, smoothed magnitude variants, phase/group-delay
  outputs where meaningful, clipping/SNR/repeatability flags,
  selected mic/device metadata, calibration-file hash, target curve,
  generated PEQ, predicted response, applied config, and verify
  measurement.
- Add first-class confidence reporting: mic calibration status,
  capture-quality status, position count, per-position variance,
  repeatability, browser/device confidence, and whether the selected
  correction strategy is justified by the evidence. A first pass now
  exists for the fields JTS already collects and is shown in the
  `/correction/` UI. Completed designs also write
  `position_analysis.json` for replayable seat-variance analysis.
  Browser-reported pre-sweep noise floors now produce a bounded
  `estimated_snr_db` warning in capture reports and an
  `acoustic_quality.json` summary. The browser flow also records
  native pre-sweep noise WAVs per position plus an optional main-seat
  repeat capture, giving the agent packet real SNR and repeatability
  evidence without treating repeats as extra listening positions.
  Calibrated acoustic SPL and research-tuned thresholds are still
  future work.
- Keep the current `info.json` / `result.json` shape compatible, with
  explicit versioning so future FIR and agent tooling can detect what
  artifacts are present instead of guessing from filenames.
- Durable-evidence substrate now exists: bundle schema v3 writes
  `artifact_manifest.json` so raw captures are named as canonical
  private evidence and derived artifacts declare their inputs,
  checksums, sensitivity, and recomputability. Bundles also write
  `runtime_integrity.json` with system/runtime snapshots, capture
  sample-count sanity, fan-in xrun deltas, and CamillaDSP runtime
  counters that feed the confidence report alongside capture quality.
  `acoustic_quality.json` records the current SNR/acoustic-trust
  verdict, and `jasper.correction.evidence` combines bundle,
  confidence, runtime, acoustic, and optional same-position
  repeatability facts into one read-only packet for the calibration
  agent.
  `jasper-correction-bundle` now provides the operator/replay surface
  for this contract: inspect + checksum validation, optional raw-capture
  replay into derived curves, and REW-friendly `.frd` / `.txt` / IR WAV
  export for external analysis.
- **Actually run the full Phase 0–2.2 pipeline on a real room** with
  the calibrated mic. This is the N10 hardware verification the user
  flagged as missing. Document what you find in
  [`HANDOFF-correction.md`](HANDOFF-correction.md) — known
  failure modes, surprises, "the auto-PEQ usually wants X, but in
  reality you want Y."

**Sequencing rationale:** the agent's recommendations inherit the
measurement quality. Shipping the agent on top of low-confidence input
is a confidence-amplifier on bad data. Phase 0a has landed; Phase 0b
and several real calibration sessions should come before Phase A so
the author has lived experience of what the agent should be saying.

### Phase 0c — FIR + tuning corpus research (INITIAL PASS DONE)

Initial deep-research intake was distilled on 2026-05-25 from three
user-provided reports (Google, Anthropic, OpenAI). The consensus
reinforced the staged plan: keep conservative bass PEQ, improve
bundle reproducibility, add calibrated mic + multi-position
measurement, separate target/preference layers, introduce FIR first
as runtime/export substrate, and keep LLM guidance advisory and
parameter-bounded.

The raw reports are preserved for re-review under
[`docs/research/2026-05-25-calibration-agent/`](research/2026-05-25-calibration-agent/README.md):
[`room-correction-science-and-agent-foundation.md`](research/2026-05-25-calibration-agent/raw/room-correction-science-and-agent-foundation.md),
[`fir-target-curves-and-preference-eq.md`](research/2026-05-25-calibration-agent/raw/fir-target-curves-and-preference-eq.md),
and [`fir-room-correction-implementation-blueprint.md`](research/2026-05-25-calibration-agent/raw/fir-room-correction-implementation-blueprint.md).

Before asking an LLM to opine about FIR, phase, target curves, or
preference tuning, keep extending the source corpus and cite it:

- Distill REW, Toole/Olive/Welti, Dirac, CamillaDSP convolution
  workflows, CamillaFIR if verified as a concrete useful reference,
  HouseCurve, Genelec/Neumann, and other high-quality open or
  publicly readable sources into short markdown concept files.
- Separate **facts and constraints** ("narrow nulls are not fixed by
  EQ", "minimum-phase correction is different from linear-phase
  convolution") from **taste guidance** ("warmer", "brighter",
  "more bass", "less forward vocals").
- Document FIR design choices before implementation: minimum-phase vs
  linear-phase vs mixed-phase, windowing / FDW, tap count and latency
  budgets on Raspberry Pi 5, pre-ringing risk, boost/headroom limits,
  and CamillaDSP coefficient loading.
- Include a parallel glossary that maps user language to technical
  levers. Example: "more bass" might mean target curve tilt,
  low-shelf preference EQ, or undoing an over-aggressive modal cut;
  the agent should ask clarifying questions before changing filters.
- Version the corpus and record the corpus git SHA in every future
  agent transcript.

Current distilled corpus files:

- [`docs/calibration-agent/concepts/measurement-quality.md`](calibration-agent/concepts/measurement-quality.md)
- [`docs/calibration-agent/concepts/room-correction-limits.md`](calibration-agent/concepts/room-correction-limits.md)
- [`docs/calibration-agent/concepts/spatial-averaging.md`](calibration-agent/concepts/spatial-averaging.md)
- [`docs/calibration-agent/filter-design/fir-room-correction.md`](calibration-agent/filter-design/fir-room-correction.md)
- [`docs/calibration-agent/filter-design/preference-eq.md`](calibration-agent/filter-design/preference-eq.md)
- [`docs/calibration-agent/targets/house-curves.md`](calibration-agent/targets/house-curves.md)
- [`docs/calibration-agent/jts-specific/implementation-ladder.md`](calibration-agent/jts-specific/implementation-ladder.md)

### Phase A — Corpus loader + agent scaffold (CLI-testable, no UI)

- ✅ **Read-only deterministic intake substrate.** Implemented
  2026-05-25 as `jasper-calibration-agent`. The CLI loads a
  correction session bundle, summarizes measurement/device/mic
  provenance, surfaces bundle + capture-quality + runtime-integrity
  + acoustic-quality issues, renders evidence readiness, finds
  bass-band peaks/nulls vs target, notes that Schroeder estimation is
  unavailable until room/RT60 context exists, and pulls short guidance
  snippets from `docs/calibration-agent/`. It performs no side effects
  and does not call an LLM.
- Extend the markdown corpus under `docs/calibration-agent/` as
  needed, then build a deterministic loader that assembles it into
  the agent prompt. The current intake CLI already includes a small
  corpus lookup tool; a future prompt assembler can reuse it.
- Build `jasper/calibration_agent/` with the Anthropic adapter as
  reference.
- Implement the read-only tools first
  (`get_measurement_summary`, `analyze_peaks_nulls`,
  `compute_schroeder`, `look_up`, and the read-only evidence packet).
  The first deterministic versions are in `jasper.calibration_agent.tools`
  and `jasper.correction.evidence`.
- CLI tool: `sudo /opt/jasper/.venv/bin/jasper-calibration-agent
  <session_id>` → loads the session bundle from
  `/var/lib/jasper/correction/sessions/<id>/` and prints the
  deterministic intake report. The LLM-backed single-shot
  "interpret this session" output is still future work.
- Tests against synthetic + recorded bundle fixtures. Don't burn API
  budget in CI; the harness gets `pytest.mark.requires_api_key` so
  CI skips it by default and a human runs it during PR review.
- Validates the knowledge base + the tool design + the prompt
  template before any UI investment.

### Phase B — Multi-provider + tool-call loop

- Add OpenAI (Responses + tool use) and Gemini (function calling)
  adapters.
- Implement `propose_alternative_peq` (simulate a filter set,
  return predicted curve) and `get_measurement_plot` (PNG render).
- Provider picker in
  [`jasper/web/voice_setup.py`](../jasper/web/voice_setup.py) gains
  the Anthropic key card + the calibration-agent provider selector.
- Wire `apply_peq` with explicit `confirm` gating.

### Phase C — Chat panel in `/correction/`

- New `POST /chat` + `GET /chat` + `POST /chat/reset` +
  `POST /chat/context` routes.
- Chat-panel UI inside the existing correction page — appears on
  `state == READY`, persists through `APPLIED` and `VERIFIED`.
- Filter-diff visualization: predicted-curve overlay, side-by-side
  filter table.
- Runtime-context disclosure (read/write the private
  `/var/lib/jasper/...` context file inline, with confirmation for
  agent-authored changes).
- Bundle gets `agent_transcript.json` alongside `info.json`.

### Phase D — Iterative re-measurement loop

- `request_remeasurement` end-to-end (agent asks → user prompted in
  UI → user taps `Start` → session continues with new data folded in).
- Multi-position guidance from the agent ("now move 30 cm to the
  left of the listening position"); agent reads each new bundle as
  it lands.
- "Tune until we agree it's done" closes-the-loop UX.

### Phase E — Optional voice handoff (DEFERRED)

- `start_calibration_session` voice tool: "Jarvis, help me tune my
  speakers" opens the wizard on the user's phone (push notification
  / pre-built URL).
- Re-evaluate after Phase D ships. Voice doesn't add much here;
  calibration is inherently visual.

---

## Open questions

Things that are still worth tracking after the Phase 0a substrate:

1. **Anthropic SDK as a net-new dependency.** Today's
   [`pyproject.toml`](../pyproject.toml) doesn't list `anthropic`.
   Adding it is fine but it's a real ~10 MB RAM cost at agent
   startup (lazy-imported, so zero idle). Worth confirming the
   household is happy with that cost in exchange for Claude Opus
   access.
2. **Vendor endpoint drift.** Phase 0a ships Dayton + miniDSP serial
   lookup plus manual upload fallback. Keep the provider boundary in
   [`jasper/correction/calibration.py`](../jasper/correction/calibration.py)
   narrow so future serial schemes do not touch measurement math. If a
   vendor blocks or changes lookup, the correct UX is a clear lookup
   error plus manual upload, not a hidden fallback to uncalibrated
   measurement.
3. **Calibration-file storage/privacy.** Phase 0a stores records under
   `/var/lib/jasper/correction/calibration_mics/<provider>/<model>/`,
   hashes serials, redacts raw browser device ids, and copies the
   selected calibration file/curve into each bundle. Future exported
   bundles should keep that posture: enough replay provenance for the
   agent, no raw serial/device identifiers in public metadata.
4. **Calibration-file sign convention UX.** Phase 0a normalizes to
   "add this dB correction to the measured response" and exposes a
   manual upload sign selector. Future provider adapters should keep
   that invariant and improve preview/plotting before asking users to
   trust obscure files.
5. **Per-room vs per-listening-position calibrations.** The agent
   probably wants to know "the lounge calibration" vs "the kitchen
   calibration" — same speaker, different rooms. Today's session
   bundles are flat under `sessions/<id>/`. Worth a folder
   hierarchy? My instinct: defer, let the private runtime context
   carry the context for now.
6. **Rationale visibility in chat.** The UI should show concise,
   source-cited, user-facing rationale: what evidence the agent used,
   what it is uncertain about, and why it recommends restraint or a
   change. Do not expose hidden model chain-of-thought.
7. **Knowledge-base versioning.** The markdown corpus will evolve.
   How does the bundle record *which version of the corpus* the
   agent was running against? My instinct: include the corpus
   git-SHA in `agent_transcript.json`, so a later reader knows what
   the agent was reading when it gave the advice.
8. **Cost cap UX.** When the cap is hit mid-session, do we (a)
   refuse to continue, (b) auto-fall-back to a cheaper provider, or
   (c) just warn? My instinct: (a) refuse + post a chat message
   ("we've spent $X, the cap is $Y, raise it at
   `JASPER_CALIBRATION_AGENT_MAX_USD_PER_SESSION` to continue").
   No silent provider switch — the user picked Opus for a reason.
9. **Preference tuning scope.** How much should "make it brighter" or
   "more bass" live inside `/correction/` versus a separate
   `/tuning/` surface? My instinct: same underlying artifacts and
   target-curve engine, separate user-facing mode, because preference
   EQ is reversible taste shaping while room correction is
   measurement-driven compensation.

---

## What we are NOT doing

Explicitly out of scope, to keep this from sprawling:

- **No new voice provider for calibration.** Voice and chat surfaces
  are intentionally separate.
- **No FIR / phase correction in the first agent implementation.**
  FIR is a parallel research and substrate workstream, not something
  the initial read-only agent should improvise. Phase 5 of
  [`HANDOFF-correction.md`](HANDOFF-correction.md) covers FIR; this
  handoff now adds the corpus/bundle prerequisites needed to approach
  it carefully.
- **No preference-EQ voice loop in the first implementation.** The
  "play music you love and tell JTS what you hear" flow is an
  explicit north-star feature, but it should build on the same
  target-curve and bundle substrate after room measurement is solid.
- **No active-speaker commissioning inside the initial `/correction/`
  chat loop.** Active crossover commissioning is a separate tool
  family with safety gates for channel maps, tweeter protection,
  timing references, and speaker-baseline profiles. Do not inherit
  room-correction assumptions like "reset to identity," "single mic
  magnitude trace is enough," or "apply PEQ only." Current planning:
  [`HANDOFF-active-speaker-dsp.md`](HANDOFF-active-speaker-dsp.md).
- **No new measurement methodology** (e.g. MLS, log chirp variants,
  multitone). The Novak 2015 ESS substrate stays.
- **No third-party room-correction engine integration** (REW headless,
  Dirac Live, etc.). The REW interop path in
  [`HANDOFF-correction.md`](HANDOFF-correction.md) Phase 4 is
  separate and orthogonal.
- **No autonomous "the agent ran a calibration overnight" mode.**
  The user is always in the loop. No "agent applied 5 filters at
  3am" energy.
- **No multi-user contention.** One calibration session at a time;
  the existing `MeasurementSession` is a singleton and the chat
  surface inherits that constraint.

---

## Prior art summary

Detailed survey is in the proposal-conversation log. Headlines:

- **The May 2026 research pass did not find an agentic /
  conversational room-correction product.** Every commercial system
  we reviewed
  (Sonos Trueplay, Dirac
  Live, Sonarworks SoundID, Genelec GLM, Neumann MA 1, Audyssey,
  IK ARC Studio, Apple HomePod auto-cal) is a closed-loop one-shot
  batch process with no per-decision rationale and no user dialogue.
- **Closest architectural prior art is in spectroscopy:** LUMIR
  ([Sci.Direct, 2025](https://www.sciencedirect.com/science/article/abs/pii/S0003267025012516)),
  IR-Agent ([arXiv 2508.16112](https://arxiv.org/html/2508.16112v1)),
  EIS-LLM ([Battery Design, 2025](https://www.batterydesign.net/how-well-can-an-llm-interpret-electrochemical-impedance-spectroscopy-eis-data/)).
  Same shape: measurement → interpret → propose action → iterate.
  These are barely a year old; the pattern is fresh.
- **REW API automation exists** ([AV NIRVANA project,
  2026](https://www.avnirvana.com/threads/i-made-a-free-easy-to-use-open-source-advanced-room-correction-software-that-leverages-rews-api.16552/))
  — pure scripting, no LLM. Useful as a "tool surface" reference
  for what an LLM could call into, but the project itself is
  agent-less.
- **Multimodal pattern (chart-as-image + JSON sidecar) is best
  practice.** OpenAI cookbook + GPT-4o vision benchmarks converge
  on "both, for different sub-tasks." Single-modality (JSON-only
  or image-only) measurably underperforms.

This is largely **greenfield in the LLM-room-correction space** —
opportunity, also risk. There are no proven prompting patterns to
copy; expect prompt-engineering iteration to be real work, especially
around restraint ("the agent shouldn't be eager to suggest more EQ,
which is its training-set instinct").

---

## References

Audio engineering:
- Toole, *Sound Reproduction*, 3rd ed., Routledge (2017)
- [Dirac — On Room Correction and Equalization (PDF)](https://www.dirac.com/wp-content/uploads/2021/09/On-equalization-filters.pdf)
- [REW Help — Why Can't I Fix All my Acoustic Problems with EQ?](https://www.roomeqwizard.com/help/help_en-GB/html/iseqtheanswer.html)
- [CamillaDSP](https://www.camilladsp.com/) — real-time IIR/FIR engine already in JTS
- [CamillaFIR](https://vilhovalittu.github.io/CamillaFIR/) — possible FIR workflow reference; verify current status before relying on it
- [HouseCurve — file formats](https://housecurve.com/docs/manual/file_formats) — practical curve/calibration text format
- [HouseCurve — target curves](https://housecurve.github.io/docs/tuning/target_curve.html) — house curves and taste guidance
- [Sonavyx — Schroeder Frequency Explained](https://sonavyx.com/en/insights/schroeder-frequency-explained)
- [GIK Acoustics — What is SBIR?](https://www.gikacoustics.com/blogs/knowledge-base/speaker-boundary-interference-response-sbir)
- [Audiosolace — Harman Target Curve Explained](https://audiosolace.com/harman-target-curve-explained/)
- [PS Audio — Using EQ With Speakers: Some Limitations](https://www.psaudio.com/blogs/copper/using-eq-with-speakers-some-limitations)

Microphone calibration:
- [Dayton Audio Microphone Calibration Tool](https://support.daytonaudio.com/MicrophoneCalibrationTool)
- [Dayton Audio iMM-6C product page](https://www.daytonaudio.com/product/1974/imm-6c-idevice-usb-c-calibrated-microphone)
- [Dayton Audio UMM-6 product page](https://www.daytonaudio.com/product/1116/umm-6-usb-measurement-microphone)
- [miniDSP — UMIK-1 calibration file download](https://www.minidsp.com/products/acoustic-measurement/umik-1?format=pdf&type=raw)
- [miniDSP — UMIK-2 user manual](https://www.minidsp.com/images/documents/miniDSP%20UMIK-2-User%20Manual.pdf)

LLM-agent prior art:
- [LUMIR — LLM agent for IR spectroscopy](https://www.sciencedirect.com/science/article/abs/pii/S0003267025012516)
- [IR-Agent — Expert-inspired LLM agents](https://arxiv.org/html/2508.16112v1)
- [EIS + LLM (Battery Design, 2025)](https://www.batterydesign.net/how-well-can-an-llm-interpret-electrochemical-impedance-spectroscopy-eis-data/)
- [Snakemake + LLM supervisor agent](https://arxiv.org/pdf/2510.14846)
- [OpenAI Cookbook — GPT-5 vision tips](https://developers.openai.com/cookbook/examples/multimodal/document_and_multimodal_understanding_tips)
- [Acoustic Room Compensation Using Local PCA (arXiv 2206.15356)](https://arxiv.org/pdf/2206.15356)
- [AV NIRVANA — Open-source REW API automation](https://www.avnirvana.com/threads/i-made-a-free-easy-to-use-open-source-advanced-room-correction-software-that-leverages-rews-api.16552/)

Codebase:
- [`HANDOFF-correction.md`](HANDOFF-correction.md) — the existing substrate this sits on
- [`jasper/correction/`](../jasper/correction/) — DSP pipeline
- [`jasper/web/correction_setup.py`](../jasper/web/correction_setup.py) — wizard
- [`jasper/voice/`](../jasper/voice/) — `LiveConnection`/`LiveTurn` pattern to mirror
- [`HANDOFF-voice-providers.md`](HANDOFF-voice-providers.md) — provider-abstraction precedent
- [`HANDOFF-prompting.md`](HANDOFF-prompting.md) — playbook for LLM prompts (cross-provider principles, conditional vs absolute rules, `confirm` field handling) that this agent should respect

---

Last verified: 2026-05-28
