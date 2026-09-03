# C — the web component of the tuning side (bottom-up)

Read-only at `c0325038`. Sized from first principles; the prior top-down recon
(`recon/04-web-frontend.md`) covered `correction_*` only and I re-derived every
number rather than inheriting it (my counts agree with it where they overlap).

Two daemons serve this: `jasper-sound-web` (`sound_setup:main` → `/eq/`,
`/sound/setup/`) and `jasper-correction-web` (`correction_setup:main` → HTTPS
`/sound/room/`, `/sound/crossover/`, `/sound/bass/`, `/sound/measurements/`)
— `deploy/nginx-jasper.conf:167,175,431,440,449`; `pyproject.toml:202,238`.

---

## 1. What the web component consists of today

### 1a. Python (measured `wc -l`, prose = comment+docstring lines via `tokenize`)

| page / flow | files | lines | prose | prose % |
|---|---|---:|---:|---:|
| **Config wizard** `/sound/setup/` + `/eq/` | `jasper/web/sound_setup.py` | 6,147 | 527 | 8.6 |
| — its commissioning engine, web-only consumers | `active_speaker/web_commissioning.py` 2,917 · `commissioning_coordinator.py` 1,219 | 4,136 | 642 | 15.5 |
| **Measurement-walk** `/sound/crossover/` (crossover v2) | `correction_crossover_v2.py` 7,832 · `_backend` 1,749 · `_v2_wired` 1,187 · `_v2_relay` 1,100 · `_v2_status` 859 · `_v2_republish` 375 · `_flow` 522 · `_context` 64 · `active_speaker_flow.py` 91 | 13,779 | 5,486 | 39.8 |
| **Room-correction (PEQ) wizard** + the shared host | `correction_setup.py` 7,481 · `correction_room_flow` 437 · `_measurements` 99 · `_bass_flow` 137 · `_report` 100 · `_hub` 37 · `_tuning` 335 | 8,626 | 1,840 | 21.3 |
| tuning-web subtotal | | **32,688** | **8,495** | **26.0** |
| *(excluded, not tuning)* | `balance_*` 1,293 · `sync_flow` 528 · `_common.py` 1,385 | 3,206 | | |
| *(excluded, real shared substrate)* | `active_speaker/setup_status.py` — also consumed by `control/`, `cli/doctor/`, `multiroom/` | 1,347 | | |

Templates: there is no template engine. All HTML is inline f-strings —
~450 lines total across `sound_setup._index_html`, `correction_room_flow`,
`correction_crossover_flow`, `_bass_flow`, `_measurements`. This part is lean
and needs no change.

### 1b. Browser (`deploy/assets/`, JS + CSS)

| bundle | lines | what |
|---|---:|---|
| `sound-profile/js/main.js` | 7,649 | one IIFE, 352 functions. Split by function name: **commissioning ladder 3,246** · output/topology setup 1,549 · manual-crossover + component settings ~1,813 · EQ editor 791 · volume floor 81 |
| `sound-profile/js/active-speaker-ui.js` | 678 | step vocabulary + policy |
| `sound-profile/sound.css` · `eq-math.js` | 1,012 · 91 | |
| **`correction/js/crossover/` (the walk UI)** | **1,713** | `main.js` 1,137 · `cloud.js` 350 · `frequency-chart.js` 169 · `chart.js` 57 |
| `correction/crossover.css` | 431 | |
| `correction/js/main.js` (**room wizard**) | 3,647 | mic capture+level 772 · walk/render 734 · calibration 182 · chart 176 · relay 138 · other 1,215 |
| `correction/js/measurements.js` · `bass/main.js` · css | 291 · 246 · 676 | |
| `shared/js/` | 2,438 | `qr.js` **1,440** (relay-only) · `measurement-audio.js` 393 (room-only) · http/dialog/dom/escape 605 |
| **total** | **18,872** | (`app.css` 682 excluded — design system for all 20 wizards) |

**The measurement-walk UI is 1,713 of 18,872 JS.** Everything else is the
config wizard (9,430), the room wizard (4,860), and relay QR (1,440).

### 1c. Net of the relay-deletion chain (do not double-count)

PR **#3724** (read via `pull_request_read get_files`) is step 1 of nine: 22
files, net **−304** in my Python (`correction_crossover_v2.py` +84/−296,
`_v2_wired` +46/−79, `correction_setup` +2/−54, `correction_room_flow` +4/−11).
Issue **#3661**'s later steps claim, measured at HEAD:
`correction_crossover_v2_relay.py` 1,099; relay-named top-level defs in
`correction_setup.py` **2,306 / 41 defs** (`_run_relay_level_match:4481` alone
is 562); ~138 relay functions + call sites in `correction/js/main.js`; the relay
panel in `crossover/js/main.js` (~150). **`shared/js/qr.js` (1,440) is orphaned
by it and is not on #3661's list** — its only consumers are the two
`renderRelayQr` call sites (`correction/js/main.js:22,353,360,364`,
`crossover/js/main.js:6,742,799`). Flag it to the chain.

**Already claimed: ≈3,700 Python, ≈1,790 JS.** Everything below is net of that:
**28,988 Python / 17,082 JS+CSS.**

---

## 2. From first principles: what the owner's model needs

### Unit rows

| unit | one line | current (Py / JS+CSS) | clean (Py / JS+CSS) | top reason for the gap |
|---|---|---:|---:|---|
| **Config wizard** — declare hardware, topology, drivers, protection, sensitivity; save a profile | form + validate + save, over `output_topology.py` | 6,147 / 9,430 | **1,450 / 3,000+700css** | 3,247 Py + 3,246 JS are a hardware bring-up state machine (tone ramps, summed test, baseline apply) living in the page instead of the engine |
| **Measurement-walk page** — show the position prompt, tap to advance, show progress + verdict, hand back | poll `/status`, POST `/position-ready`, render the closed envelope | 13,779 / 2,144 | **400 / 800+250css** | 13,300 of the 13,779 is the crossover engine parked under `jasper/web/`; the walk contract itself is 2 routes |
| *(preference EQ `/eq/`)* — not tuning, ships in the same file | band editor + live draft | ~600 / ~900 | 450 / 800 | already about right; only needs its own module |
| *(room-correction PEQ wizard)* — **separate product, size separately** | browser-mic sweep + PEQ apply | 8,626 / 4,860 | 700 / 1,200+400css | a second, independent measurement walk with a second capture architecture |

**Tuning web component, clean: ~1,900 Python + ~3,800 JS + ~950 CSS.**
(+ ~700 Python and ~1,200 JS if the room wizard is kept as a product.)

### The evidence that 400 lines is the right number for the walk page

`jasper/active_speaker/arm_walk.py` (1,036 lines) drives the **identical** walk
with the turntable instead of a human, and it needs exactly two endpoints:
poll `GET /correction/crossover/status` for `relay.position_pending` →
`{index, attempt, degrees, role, action}`, and `POST
/correction/crossover/v2/position-ready`. The buttons are server-supplied too:
`crossover/js/main.js:975` posts `action.endpoint` straight out of the envelope
minted by `active_speaker/crossover_envelope_v2.py`. The thin-front-end model is
already proven in-tree for the arm; the human page just never adopted it.

### The assumptions behind the clean numbers

- **Stays shared substrate (not charged to web):** `output_topology.py` (2,460),
  `crossover_v2_flow.py` (7,839), `crossover_envelope_v2.py` (4,417),
  `setup_status.py` (1,347), `jasper/correction/` (16,883), `_common.py`,
  `app.css`. The CLI tools (`measure`, `jasper-round`, `jasper-basic-profile`)
  need all of it identically.
- **Legitimate complexity in the web layer:** CSRF/host guards, a mutating-request
  mutex, the follower/bonded-pair redirect, the frequency chart (~250 JS), and
  the getUserMedia + AudioWorklet meter *if* the room wizard survives (~400 JS).
- **Prose bar:** AGENTS.md — constraints and `why`-pointers only. Target ≤12%.

---

## 3. The gap, with evidence

### 3.1 Engine logic in the web layer — 13,300 Python lines, quantified

`grep -cE "BaseHTTPRequestHandler|from \._common|<div|<section|canonical_page|
send_json_response|self\.send_response"` returns **0** for every one of:
`correction_crossover_v2.py` (7,832), `_backend` (1,749), `_v2_wired` (1,187),
`_v2_relay` (1,100), `_v2_status` (859), `_v2_republish` (375), `_context` (64),
`correction_tuning` (335), `correction_report` (100), `correction_hub` (37),
`active_speaker_flow` (91). **13,729 lines under `jasper/web/` that contain no
HTTP and no HTML.** Every route in the correction service registers in
`correction_setup.py` (`_POST_ROUTES:541`, `_make_handler:6132`).

Inside `sound_setup.py`, the `_active_speaker_*` payload builders span
**3,247 lines** (`_active_speaker_play_summed_commission_tone:3627` 294,
`_active_speaker_play_commission_tone:3326` 216,
`_active_speaker_summed_test_payload:4795` 186). `_play_commission_tone` claims
a fanin lane, holds a module-global tone session under a lock, and rolls back
CamillaDSP graphs — in a page handler module.

And the seam is not even drawn: `sound_setup.py:97-121` imports **sixteen
underscore-private names** from `jasper/active_speaker/web_commissioning.py`
(`_commission_tone_signal_plan`, `_commission_tone_select_fanin_lane`,
`_blocked_startup_anchor`, …). The commissioning engine is split across a web
module and an engine module with private internals as the API — the boundary
records where someone stopped moving code, not a design.

### 3.2 Two products in one file, twice over

- **`sound_setup.py` = preference EQ + hardware setup + bring-up ladder.** Its
  own docstring says so (`sound_setup.py:7`: "Shared backend for preference EQ
  at /eq/ and hardware setup at /sound/setup/"). Route census: **50 routes, 34
  of them `/active-speaker/*`.** The EQ half is ~600 Python / ~800 JS; the other
  ~5,500 / ~6,800 is the tuning wizard. They share a file because they share a
  daemon, not because they share a concern.
- **`correction_setup.py` = five products.** By top-level def span: room wizard
  2,594 · relay 2,306 · `_make_handler` (one nested `Handler` class) 1,119 ·
  mic calibration 414 · crossover route shims 330 · autolevel 309 · server
  plumbing 202 · balance/sync shims 91. Its docstring admits it
  (`correction_setup.py:25`: "this module now serves far more routes than fit a
  comment table").

### 3.3 The measurement walk is implemented twice, front to back

| | room wizard | crossover-v2 wizard |
|---|---|---|
| state | `jasper/correction/envelope.py:190` `needs_next_position` | `PositionGate` in `correction_crossover_v2.py` (322 lines) |
| routes | `/next-position` (`correction_setup.py:3074`), `/repeat-position` (:4037) | `/crossover/v2/position-ready` (:5165) |
| capture | **browser** getUserMedia + WAV upload (`/local-capture/setup`, `_handle_local_capture_setup:3898`) | **Pi's wired USB mic** (`correction_crossover_v2_wired.py`) |
| browser JS | `correction/js/main.js` 3,647 | `correction/js/crossover/main.js` 1,137 |

Two position walks, two capture architectures, two chart renderers, two session
stores, ~4,800 JS between them. Only one of the two is the tool the owner's
model names.

### 3.4 The inversion: the CLI is a client of the web page

`jasper/active_speaker/wizard_client.py` (408 lines) is *"one HTTP client for
every caller of the crossover wizard — the arm walk, `jasper-basic-profile`,
`jasper-round`, `scripts/run-crossover-round.py`"*; it POSTs
`/correction/crossover/v2/{session,verify,apply}`. `jasper/cli/basic_profile.py`
POSTs `/sound/setup/active-speaker/baseline-profile/save-and-apply`. In the
owner's model the engine is a library, the CLIs call it directly, and the web
page is one more client. Today the web daemon *is* the engine host and the tools
speak HTTP to a page. That is why 13,300 engine lines sit in `jasper/web/` and
why `tests/test_correction_crossover_v2_endpoints.py` is **9,309 lines** — an
endpoint suite is standing in for an engine suite.

### 3.5 Prose

**8,495 prose lines / 26.0%** in the unit. It is concentrated:
`correction_crossover_v2.py` 3,535 (45.1%), `correction_setup.py` 1,684,
`_v2_relay` 555 (50.5%), `_v2_status` 529 (**61.6%**), `_v2_wired` 498,
`commissioning_coordinator` 310 (25.4%), `active_speaker_flow.py` 60 over 31
lines of code (**65.9%**). `sound_setup.py` is fine at 8.6%.
At a 12% bar: **~4,800 recoverable, ~1,100 of it inside relay code already
claimed → ~3,700 net.** JS prose is *not* a problem (8–13%) except
`crossover/js/main.js` at 34%.

### 3.6 Dead and agent-only routes

- Dead in `correction_setup.py`: `/test-tone` (:3319, 23 lines, sole caller of
  `jasper/correction/playback.play_test_tone` ~40) and `/calibration/models`
  (:3350, 10) — no reference in `deploy/assets/**` or anywhere but tests.
- `sound_setup.py`: **16 of 50 routes have no browser caller.** Three have no
  caller anywhere outside tests: `/active-speaker/safe-playback`,
  `/active-speaker/staged-config`, `/active-speaker/commission-rollback`. The
  other 13 (`check-path-safety`, `startup-load`, `stage-config`,
  `bringup-preflight`, `load-startup-config`, `rollback-startup-config`, …) are
  an **HTTP API for the agent**, documented in `docs/tuning-operator-runbook.md`.
  Not dead — but in the owner's model those are CLI tools, not web routes.
- Orphan asset: `shared/js/qr.js` 1,440 (§1c).

### 3.7 The topology catalogue lives in the browser

`outputTemplateDefinition` (`sound-profile/js/main.js`, 124 lines) is the SSOT
for every supported speaker shape — `mono_passive`, `mono_active_2way`,
`mono_active_3way`, `stereo_*`, sub variants — with labels, hints, channel
maps and positions. `grep -rn "mono_active_2way" --include=*.py jasper/` returns
**zero**; Python only knows the *modes* (`output_topology.py:104-118`). The
config wizard's data model is authored in JavaScript and POSTed to a validator,
which is a large part of why that file is 7,649 lines and why the setup wizard
has no server-side template test.

---

## 4. Bottom-up total for the web component

Starting from **32,688 Python / 18,872 JS+CSS**:

| bucket | Python | JS+CSS |
|---|---:|---:|
| **legit** — the clean web layer (config wizard 1,450 + walk page 400 + EQ 450 + room 700) | **3,000** | **5,750** |
| relay / phone path (#3661 + #3724, already in flight; +qr.js 1,440) | 3,700 | 1,790 |
| prose over the AGENTS.md bar (net of relay) | 3,700 | ~250 |
| **engine misfiled in `jasper/web/`** — relocates, arrives ~35% smaller | 13,300 → ~8,600 relocated | — |
| engine misfiled in `sound_setup.py` (`_active_speaker_*` + tone sessions) | 3,247 → ~2,000 relocated | — |
| duplication — 4 volume owners, 2 position walks, 2 capture stacks, the `_status`↔host module-object cycle, the `_commission_tone_*` split-brain | ~2,000 | ~2,400 |
| room-correction wizard's own engine-shaped half (`correction_setup` room+calibration+autolevel) | ~2,900 | ~1,600 |
| dead / agent-only-route surface | ~350 | — |
| over-abstraction (`correction_crossover_context.py` 64 exists only to break an import cycle; `active_speaker_flow.py` 91 for 31 lines of code) | ~200 | — |
| feature surface that is real but browser-authored (topology templates, commissioning cards) | — | ~5,400 |

**The web component's own budget is ~1,900 Python + ~3,800 JS + ~950 CSS for
the tuning model** (~2,600 / ~5,000 / ~1,350 if the room wizard stays). Of the
32,688 Python it carries today, **~10,600 is engine that must exist somewhere**
and should be charged to the tools' budget, not deleted; **~11,600 is prose,
relay, duplication, dead surface and the second product**; and **~3,000 is
honest web.**

---

## 5. Three biggest single deltas

1. **`jasper/web/correction_crossover_v2.py` — 7,832 lines, zero HTTP, 45.1%
   prose.** `prepare_v2_session` at :6105 is **965 lines** and is the only thing
   in the tree that can fill `CrossoverV2Session`'s **44-keyword constructor**
   (`active_speaker/crossover_v2_flow.py:1516`); its own docstring says "Exactly
   nine of them read `verify_only`". The engine's session object cannot
   construct itself, so a web module owns its construction contract. Dissolving
   this file into `crossover_v2/` (assembly 2,089 · play 826 · volume 750 ·
   save/bank 1,055 · grading 390 · apply 644) is the single largest move in the
   area: **−2,400 prose, ~5,400 relocated, 0 left in `jasper/web/`.**

2. **`jasper/web/sound_setup.py:2233-5300` — 3,247 lines of `_active_speaker_*`
   commissioning**, plus 3,246 lines of the same ladder in
   `sound-profile/js/main.js`, plus the 16 private imports at
   `sound_setup.py:97-121` from `web_commissioning.py`. One commissioning engine
   split three ways. Consolidate into `jasper/active_speaker/commissioning/`,
   have the page render `commissioning_coordinator.build_commissioning_view`'s
   action list and POST `action.endpoint` (already the pattern for 6 of the 34
   routes — `commissioning_coordinator.py:633,646,969,976,1002,1020`):
   **−2,000 Python net, −2,300 JS.**

3. **The second measurement walk.** `correction/js/main.js` 3,647 +
   `correction_setup.py`'s room region 2,594 + `jasper/correction/` implement a
   position walk, a capture stack (browser getUserMedia + upload,
   `_handle_local_capture_setup:3898`) and a chart that the crossover walk
   implements again with the Pi's wired mic. After #3661 the house is meant to
   have exactly one capture path (issue text: *"the wired microphone on
   jts.local is the only capture path"*) — the room wizard is the one place
   that still doesn't. Either converge it onto `PositionGate` + wired capture
   (**−2,000 Python, −1,500 JS**) or declare it a separate product and size it
   in its own budget. It cannot stay a silent third of the "web component".

---

## 6. Is the room-correction (PEQ) wizard part of the tuning model?

**No — size it separately.** Evidence: it has **no CLI**. The generated tool
menu (`scripts/generate-tuning-tool-menu.py:55-73`, `TUNING_TOOL_MODULES`)
lists 18 tools — `basic_profile`, `seat_level`, `angle_capture`, `arm_walk`,
`measure`, `crossover_prescriber`, `round*`, `classify_features`,
`read_distortion`, `delay_sweep`, `forward_model`, `gate_sweep`,
`close_reference`, `null_door`, `audition`, `declare_geometry` — and not one of
them touches room PEQ. The room wizard is a self-contained
measure→compute→apply product reachable only from its own page, with its own
engine (`jasper/correction/`, 16,883 lines), its own capture architecture and
its own session store. It is the correct shape for a *wizard-only* feature; it
is simply not the thing the agent's toolbox drives.

It shares `correction_setup.py` with the crossover wizard purely because both
need an HTTPS daemon for a mic. That accident is worth ~1,100 lines of
`_make_handler` and 330 lines of crossover route shims.

## 7. Uncertainty

- The 35% shrink-in-transit on relocated engine assumes the prose pass lands
  first. If it doesn't, relocation diffs are ~2× and the number is optimistic.
- I costed `_run_relay_level_match` (562) entirely to the relay; some of it is
  likely wired level-match that survives. Relay claim range: 3,300–3,700 Python.
- The clean-JS numbers assume the server starts owning the topology template
  catalogue (§3.7). If it stays in the browser, add ~500 JS back.
- `correction_tuning.py` (335, the spend-capped LLM advisor) is arguably a tool,
  not a page; I left it in the room-wizard bucket and did not cost a move.
