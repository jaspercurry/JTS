# AI agent operational guide for JTS

> **This file is canonical.** Edit operational rules and
> per-subsystem guidance here, not in CLAUDE.md. CLAUDE.md is a
> thin Claude-Code-specific shim that imports this file via
> `@AGENTS.md`. Any operational content added to CLAUDE.md will
> be lost or ignored — make changes here instead.

## Contents

This file is long. Jump to a section:

**Conventions & quality bars**
- [Agent behavior baseline](#agent-behavior-baseline)
- [COAH quality bar](#coah-quality-bar)
- [Config ownership — which pattern for a new DAC / mic / provider / city](#config-ownership--which-pattern-for-a-new-dac--mic--provider--city)
- [Documentation paradigm](#documentation-paradigm)
- [Web wizard conventions](#web-wizard-conventions)

**Deploy, identity & laptop state**
- [Deploying code changes to the Pi](#deploying-code-changes-to-the-pi)
- [Speaker hostname — single source of truth](#speaker-hostname--single-source-of-truth)
- [Laptop-side state — `.env.local` and `CLAUDE.local.md`](#laptop-side-state--envlocal-and-claudelocalmd)

**Audio output & renderers**
- [Renderer architecture — file map](#renderer-architecture--file-map)
- [librespot — one-time OAuth claim for cold-start voice](#librespot--one-time-oauth-claim-for-cold-start-voice)
- [USB Audio Input (`jasper-usbsink`) — read first](#usb-audio-input-jasper-usbsink--read-first)

**Voice, wake & mic**
- [Voice provider switching — read first](#voice-provider-switching--read-first)
- [Voice prompting — read HANDOFF-prompting.md first](#voice-prompting--read-handoff-promptingmd-first)
- [Gemini model switching — read first](#gemini-model-switching--read-first)
- [Wake-word switching — read first](#wake-word-switching--read-first)
- [AEC bridge — input profile and reconciler](#aec-bridge--input-profile-and-reconciler)
- [Wake-event telemetry — capture + labeling](#wake-event-telemetry--capture--labeling)
- [Mic mute — persists across restarts](#mic-mute--persists-across-restarts)

**Integrations & connectivity**
- [Wi-Fi switching — read first](#wi-fi-switching--read-first)
- [Transit configuration — read first](#transit-configuration--read-first)
- [Home Assistant integration — read first](#home-assistant-integration--read-first)

**Resilience & recovery**
- [shairport-sync AP2 wedge — auto-recovers](#shairport-sync-ap2-wedge--auto-recovers)
- [T5.2 — userspace-liveness SystemSupervisor — read first](#t52--userspace-liveness-systemsupervisor--read-first)

**Hardware accessories**
- [Satellite devices — opt-in hardware](#satellite-devices--opt-in-hardware)

**Debugging, testing & PR workflow**
- [Debugging — fetch evidence before guessing](#debugging--fetch-evidence-before-guessing)
- [Testing](#testing)
- [Branch and remote](#branch-and-remote)
- [PR workflow on a fast-moving `main` — read before you push](#pr-workflow-on-a-fast-moving-main--read-before-you-push)

---

**Read [README.md](README.md) first** — it has the project context
(architecture, hardware, repo layout, subsystem overview,
deployment, debugging entry points). This file adds AI-specific
operational guidance on top of that. Don't expect this file to
restate README; it doesn't.

What goes here:
- Things easy to get wrong (model gotchas, file ownership lines,
  brick hazards)
- Operational shortcuts (specific scripts, env-var formats)
- AI behavioral rules specific to this codebase
- How docs are organized (see "Documentation paradigm" below)

---

## Agent behavior baseline

These rules mirror the user's global Claude Code ruleset
(`jaspercurry/claude-rules`, last reviewed at commit
`7f8e9b3` on 2026-05-25) and apply to every AI agent working in
this repo, including Codex. Project-specific instructions below add
detail; they do not replace this baseline.

1. **Think before coding.** Surface assumptions, ambiguity, and
   trade-offs before making non-trivial changes. If the request is
   genuinely unclear, ask instead of silently choosing.

2. **Check prior art first.** For non-trivial work, look for existing
   in-repo patterns, shipped subsystem designs, libraries, standards,
   and relevant research before inventing a new abstraction.

3. **Diagnose before solving.** When fixing a bug, locate the cause
   with evidence before writing the patch. A plausible fix without a
   located failure mode is not enough.

4. **Prefer simplicity.** Solve the requested problem with the
   smallest clear design that fits the codebase. Avoid speculative
   flexibility, single-use abstractions, and broad refactors.

5. **Make surgical changes.** Touch only what the task requires.
   Match local style. Clean up obvious orphans created by your own
   edits. If you notice a significant unrelated problem, finish the
   current task and then surface it rather than casually widening
   scope.

6. **Work toward verifiable goals.** For multi-step work, define
   what success looks like and loop until the evidence matches it.

7. **Close the loop.** Run the code, tests, browser, CLI, endpoint, or
   operational probe that proves the change. If the environment cannot
   run a relevant check, say exactly what blocked it.

### Project-specific reinforcements

- **Bug work starts with evidence.** Fetch the logs, probe the
  affected daemon/user surface, and name the specific failure line or
  state transition before proposing a fix.
- **Use existing integration helpers.** Existing helpers —
  `pycamilladsp`, `openwakeword`, `google-genai`, `spotipy`, the
  transit/provider registries, and the reconcilers — handle most of
  the integration. Do not reinvent them.
- **Surgical changes — file ownership.** Our files live under
  `/opt/jasper/`, `/etc/camilladsp/`, `/etc/jasper/`,
  `/etc/modprobe.d/snd-aloop.conf`, `/etc/asound.conf`,
  `/etc/shairport-sync.conf`, `/etc/nginx/sites-enabled/jasper.conf`,
  and `/etc/systemd/system/{jasper-*,librespot,shairport-sync,nqptp,bt-agent}.service`.
  Touch only what you must when modifying these.
- **Renderer ALSA device names must resolve as the renderer's
  runtime user.** When changing a renderer's ALSA device (the
  `--device` in librespot.service, `output_device` in
  shairport-sync.conf, `--pcm` in bluealsa-aplay's drop-in), the
  new name MUST be openable via `sudo -u $USER aplay -D $DEVICE
  -c 2 -r 48000 -f S16_LE -d 0.1 /dev/zero`. This catches the
  PR #214 bug class: a user-space ALSA PCM defined only in a
  root-readable `~/.asoundrc` fails to resolve under non-root
  renderer users (shairport-sync, pi). System-wide PCM defs go
  in `/etc/asound.conf` (mode 0644). `jasper-doctor`'s
  `check_renderer_device_resolvable` runs this probe on every
  install, but verify by hand before relying on it.
- **No silent failure paths.** Any new code path that would
  prevent the speaker from responding to a wake event MUST also
  trigger an audio cue (so the user hears why nothing happened).
  Add cues by appending a `CueDef` to
  [`jasper/cues/registry.py`](jasper/cues/registry.py) and calling
  `cues.play("<slug>")` from the failure handler; see
  [docs/HANDOFF-audible-feedback.md](docs/HANDOFF-audible-feedback.md).
  Cue text must stay provider-agnostic (no "Google" / "Gemini" —
  voice backend is replaceable).
- **Codify, don't memorise.** If a runtime value matters for the
  speaker to behave correctly, it MUST be set somewhere the next
  fresh Pi will pick up automatically — either as a code default
  in [`jasper/config.py`](jasper/config.py), a seeded value in
  [`.env.example`](.env.example) (with a prose comment explaining
  what it does and why this default), or an explicit step in
  [`install.sh`](deploy/install.sh). Setting a value live on a Pi
  and not codifying it is a hidden runtime dependency. Same
  principle applies to wizards: the wizard writes a file to
  `/var/lib/jasper/`, but the wizard itself is the codification,
  and absence of the file MUST fail loudly. For env-var changes
  specifically: every `JASPER_*` line added to `.env.example`
  ships with a prose comment block above it (what, why this
  default, recommended ranges if tunable).
- **Pin promises with tests.** A documented behavior, invariant,
  or safety claim that no test asserts is where bugs hide. When a
  comment, docstring, or doc says "X disables Y" / "A keeps B
  safe" / "this never runs", that sentence gets a test in the
  same PR. The reusable guard-pattern catalog lives in
  [docs/testing-tooling.md](docs/testing-tooling.md) "Guard &
  contract test patterns".
- **Fix what you notice — tier the response.** Trivial + obvious
  stale prose, typos, dead links, and 1-3 line fixes should be fixed
  inline. Significant bugs, design judgments, or behavior changes
  should be surfaced after finishing the requested task. When in
  doubt, flag it.
- **Verify at the user's surface, not at upstream config.** When
  the user references what they observe (a wizard, dashboard,
  HTTP response), verify by hitting that URL or reading the
  rendering code. JTS systemd units chain multiple
  `EnvironmentFile=` directives (`/var/lib/jasper/*.env`
  overrides `/etc/jasper/jasper.env`), so the runtime answer
  lives in whichever file is loaded last. For daemon state,
  prefer the daemon's own surface (`/state`, MPRIS, websocket)
  over `/etc/*` files when both exist.
- **JTS is a production speaker — design for resilience.**
  Reasonable physical actions (unplugging speakers, power
  cycling, briefly losing WiFi, removing a satellite) must not
  put the speaker in a state it can't self-recover from. When
  touching systemd units, daemon startup paths, or any code near
  hardware, ask whether the resource can disappear and later
  recover without operator intervention. No silent restart loops:
  either the system recovers or someone hears/sees the issue via a
  cue, log, dashboard, or dial LED.
- **Scope fixes to the observed-broken path, not symmetric ones.**
  When fixing a bug in one provider's adapter (for example,
  OpenAI session), don't preemptively mirror the change into
  sibling adapters (Gemini, Grok) just because they share a
  protocol. Shared interfaces don't mean shared bugs.
- **Mic capture is consumed by ML, not humans.** The downstream
  consumers are openWakeWord (16 kHz ONNX) and the real-time
  speech LLMs — never a human listener. Optimize for wake-word
  reliability and ASR accuracy, not naturalness. Aggressive
  band-limiting to roughly 100 Hz-7 kHz is often a win; human-
  perceptual tunings can make ML consumers worse. See
  [`docs/HANDOFF-aec.md`](docs/HANDOFF-aec.md).

---

## COAH quality bar

When the user asks whether work meets "COAH standards", treat that as
the JTS staff-maintainer review bar:

- **Clean.** Boundaries are crisp, ownership is local, names explain the
  domain, and the design is the smallest durable shape that fits the
  existing system. Prefer simple composition over speculative
  abstractions; leave the codebase easier to reason about.
- **Observable.** Failures and important state transitions are visible
  through stable `event=` logs, `/state` or doctor surfaces where
  appropriate, and user-facing UI hints when a household needs to act.
  Avoid journal spam; make diagnostics specific enough to fix the
  problem.
- **Available and resilient.** The speaker should keep working for
  months on a 1 GB Pi. Availability is the user-visible promise;
  resilience is how the system keeps that promise under crashes,
  missing hardware, network stalls, memory pressure, and deploy churn.
  Changes should have bounded CPU, memory, I/O, subprocess, and network
  behavior; degrade gracefully where possible and fail fast where a
  missing dependency would crash a critical runtime path.
- **Hardware-safe.** Audio output, DAC/mic topology, AEC assumptions,
  firmware interactions, secrets, and deploy/rollback behavior must not
  surprise the operator or risk loud output, bricked hardware, broken
  connectivity, or leaked credentials.

COAH is not a substitute for evidence. A COAH review should inspect the
actual diff and relevant callers, run targeted tests or hardware checks
when feasible, scan mapped docs for behavior drift, and call out any
remaining validation gap explicitly.

---

## Config ownership — which pattern for a new DAC / mic / provider / city

When you add a pluggable subsystem (a DAC, a mic array, an LLM voice
provider, a transit city/mode), the first decision is **who owns its
config and who parses it**. JTS has three established patterns. Pick by
the *shape* of the thing, not by habit — the wrong one is how the core
grows a per-plugin edit it shouldn't have.

| If the new thing is… | Pattern | Owns + parses config |
|---|---|---|
| read widely by the core / hot path, stable, small | **1. Central typed `Config`** | [`jasper/config.py`](jasper/config.py) — one `from_env` parse point |
| one more of an open-ended set of self-similar plugins | **2. Self-contained module + registry** | the plugin module parses its own env from a plain `Mapping` |
| a hardware variant selected at runtime (presence is dynamic) | **3. Pure-data registry + reconciler** | a reconciler is the *single* env writer; daemons read the resolved env |

**1 — Central typed `Config`.** For cross-cutting, stable config the
daemon reads on the hot path (`hostname`, AEC knobs, volume headroom).
Typed access, one parse point ([`jasper/config.py`](jasper/config.py),
`@dataclass(frozen=True)` + `from_env`). Cost: every field is a central
edit. *Don't* put per-plugin config here — N plugins would mean N core
edits and a bloated config object. (Transit config was pulled out of
`Config` for exactly this reason.)

**2 — Self-contained module + registry (the transit pattern).** For an
open-ended set of structurally-similar plugins. Canonical example:
[`jasper/transit/`](jasper/transit/__init__.py) — each provider satisfies
the `TransitProvider` Protocol ([`jasper/transit/base.py`](jasper/transit/base.py)),
declares its own `env_keys`, and parses them itself in `build_client(env)`
from a plain `Mapping[str, str]` (the daemon passes `os.environ`). A
registry (`CITY_PACKS` → derived `REGISTRY`) groups them; the daemon calls
one entry point (`active_transit(env)`) and iterates with **zero
per-provider knowledge** — adding a provider/city needs no `config.py` and
no `voice_daemon.py` edit. The next LLM voice provider's *runtime* is the
same shape (the `LiveConnection` interface in
[`jasper/voice/session.py`](jasper/voice/session.py) + the `PROVIDERS`
registry in [`jasper/voice/catalog.py`](jasper/voice/catalog.py) —
interchangeable implementations behind one interface). It's a partial hybrid,
though: a provider's API key + model ride **typed `Config`** (`gemini_api_key`,
… — pattern 1), *not* a per-provider `build_client(env)`, so unlike transit,
adding one DOES touch [`jasper/config.py`](jasper/config.py). The price: the
plugin owns ALL its surfaces (wizard card, tool factory, client,
validation) — see the 7-item checklist in
[`jasper/transit/__init__.py`](jasper/transit/__init__.py)'s module
docstring.

**3 — Pure-data registry + reconciler owns env I/O (the wake-model / AEC /
DAC pattern).** For hardware variants chosen at runtime where the
hardware may or may not be present. A pure-data registry *describes* the
variants ([`jasper/wake_models.py`](jasper/wake_models.py)'s `REGISTRY` of
`WakeModelEntry`; the `JASPER_AUDIO_INPUT_PROFILE` profiles); a
**reconciler** owns writing the concrete device env — `jasper-aec-reconcile`
is the *single writer* of `JASPER_MIC_DEVICE_*`, mapping "selected profile
+ hardware actually present" → resolved devices, and self-heals as hardware
comes and goes. Daemons **read** the resolved env; they never choose. The
DAC registry already follows this —
[`jasper/audio_hardware/dac.py`](jasper/audio_hardware/dac.py)'s pure-data
`DacProfile` `REGISTRY`, with `jasper-audio-hardware-reconcile` /
`jasper-dac-init` writing the ALSA / CamillaDSP device config (**not** typed
`Config` fields per DAC). An ordinary single-device DAC should be one new
`DacProfile` plus detection/contract tests; `jasper.output_hardware` classifies
registered single-device profiles through the registry and should not grow
per-DAC branches. Composite/aggregate output is different:
`kind="composite"` is profile vocabulary, not permission to play. A new
composite shape needs explicit design for child identity/order, clock-domain
contract, runtime activation gates, fail-closed partial states, and `/state` /
doctor observability before reconcile/outputd can route it. Why a reconciler
and not the wizard alone:
hardware presence is dynamic, so resolution must re-run on boot / hotplug,
not once at save time.

**Doctor checks stay flat, one `CheckResult` per function.** `jasper-doctor`
is a static registry of `@doctor_check`-decorated functions that each
return a single `CheckResult` ([`jasper/cli/doctor/_registry.py`](jasper/cli/doctor/_registry.py)).
A new subsystem that needs a health probe adds one more decorated function
in the matching domain module — that's the scaling pattern, consistent
across every domain. **Do not** add a `health_check()` method to the
`TransitProvider` Protocol (or any plugin Protocol) to "generically"
iterate plugins in the doctor: today only Citi Bike has a transit health
probe (`check_citibike`), and the registry has no fan-out — a per-provider
iterator would either collapse providers into one result (losing the
per-station drift detail Citi Bike reports) or need framework surgery.
Revisit only when ≥2 plugins genuinely need runtime probes *and* you're
ready to give the registry a list-returning shape; until then a bespoke
`check_<plugin>` is cheaper and clearer.

---

## Documentation paradigm

How docs in this repo are structured, so additions land in the
right place. Read this before adding or restructuring docs.

1. **Single source of truth.** Each concept (hardware, voice-provider
   switching, AEC tuning, etc.) lives in exactly one file. Others
   link to it; they don't restate it. Drift between files is a bug.

2. **HANDOFF shape: current state first, history below.** Every
   `docs/HANDOFF-*.md` opens with the current operational truth —
   what works today, what to touch, what not to touch — in <400
   lines. Investigation narrative (dated entries, decision
   archaeology, "how we got here") sits below as an appendix.

3. **Date every load-bearing claim. `Last verified:` footer.**
   Every HANDOFF ends with `Last verified: YYYY-MM-DD`. Bump it
   when you re-verify (re-read the doc against the current code
   and confirm claims still hold), not just on edit.
   `scripts/doc-freshness.sh` reads this footer and reports docs
   overdue for re-verification.

4. **Memory = user-private. Repo = everyone.** Rules that apply to
   every contributor go in [CONTRIBUTING.md](CONTRIBUTING.md) or
   this file. Memory (Claude Code's `~/.claude/.../memory/`) stays
   user-private — personal preferences, household composition,
   in-progress hunches. If a memory entry should apply to anyone
   touching this repo, externalize it.

5. **Code references use function names, not line numbers.** A
   reference like `jasper/voice_daemon.py` + `build_cue_tts_backend`
   survives refactors; `:172` doesn't. Use line numbers only when
   the line itself is the point (a magic number, a specific bug
   location). When a line number is the right call, treat the
   number as load-bearing — a verification pass on
   `HANDOFF-correction.md` (2026-05-23) found four stale `:N`
   refs that misled readers.

6. **Touched-subsystem rule.** If your PR touches `jasper/voice/*`,
   scan `docs/HANDOFF-voice-providers.md` (and similarly for other
   subsystems). The PR template's documentation-impact section asks
   for the docs scanned and the evidence/rationale. If you found
   anything stale while scanning, fix it inline in the same PR.

7. **Doc impact map is the routing layer.**
   [`docs/doc-map.toml`](docs/doc-map.toml) maps high-risk code globs
   to the canonical docs that should be scanned when those files
   change. Treat the bot output as a starting point, not a verdict:
   update the mapped doc if behavior, commands, paths, invariants, or
   safety rules changed; otherwise leave a short no-doc-impact note in
   the PR. Keep map entries coarse and canonical. Do not copy
   architecture prose into the map.

8. **README is the doc atlas.** Every shipped doc gets listed in
   README's documentation map, or is explicitly tagged elsewhere
   (session-artifact / archived / research). No orphan docs.

9. **One canonical file per agent convention.** AGENTS.md (this
   file) is canonical. CLAUDE.md is a thin Claude Code shim: a
   canonical-file banner, `@AGENTS.md`, and optional
   `@CLAUDE.local.md` per-checkout context. No operational content
   lives in CLAUDE.md. This follows the
   [agents.md](https://agents.md) cross-tool convention adopted
   by Codex, Cursor, GitHub Copilot, Gemini, Aider, and others.

10. **Historical handoffs are tagged at the top.** Most
   `docs/HANDOFF-*.md` files are living operational references
   (rules 2 and 3). A small minority are frozen-in-time
   session-pickup narratives ("you're picking up X, here's the
   state of the world, your job is Y"). These age fast — env
   defaults change, files move, the work they describe gets
   completed and superseded. They're still useful as
   primary-source archaeology for "why did we make this
   decision," but they're not current operational truth.

   Tag such docs with a `> **Status: historical**` callout
   immediately under the H1 title. The tag tells readers (a) not
   to trust specific facts (env defaults, line numbers, "what's
   working" snapshots), (b) where to look for current operational
   truth, and (c) what the snapshot date / context was. The
   touched-subsystem rule (#6) does NOT apply — these docs are
   intentionally not kept in sync with code.

   Template (markdown blockquote, GitHub-rendered admonition):

   ```markdown
   # Handoff: <title>

   > **Status: historical.** Snapshot from <YYYY-MM-DD> when
   > <one-sentence context>. Preserved for primary-source
   > archaeology — specific facts (env defaults, file paths, line
   > numbers, "what's working" lists) will drift over time. Read
   > this for the narrative, not for current state. Current
   > operational truth lives in [<linked doc>](<path>).
   ```

   When in doubt about whether a doc is "operational" vs
   "historical": if you'd reach for it to answer "what does this
   subsystem currently do?", it's operational. If you'd reach for
   it to answer "why did we end up here?", it's historical.

---

## Web wizard conventions

The setup pages under `jasper/web/` are intentionally small stdlib
servers, not a frontend framework. Keep that architecture, but do not
copy older one-off markup patterns when adding a new wizard or touching
an existing one.

Use [`jasper/web/_common.py`](jasper/web/_common.py) as the shared
primitive layer:

- Render state-changing forms with `csrf_field_html()`.
- Render `fetch()` pages with `csrf_meta_html()` plus
  `csrf_fetch_helpers_js()`.
- Send mutating JSON POSTs with `jsonHeaders()`.
- Send mutating non-JSON POSTs, such as `audio/wav`, with
  `csrfHeaders({...})`.
- Route-check unknown GET paths before `guard_read_request()` so bogus
  paths return 404 without revealing Host / Fetch Metadata guard state.
  Call `guard_read_request()` before rendering or returning page data on
  recognized GET routes. It permits valid-host top-level document
  navigations (needed for OAuth redirect-follow and ordinary links) while
  still rejecting cross-site `fetch()` / subresource reads; state-changing
  GET routes must pass `allow_cross_site_navigation=False` or become POSTs.
- Route-check unknown POST paths before `guard_mutating_request()` so bogus
  paths return 404 without revealing CSRF state.
- Use `send_html_response()` / `send_see_other()` rather than
  hand-rolled response helpers.
- Hand page data to an ES module with `json_island(element_id, payload)`
  (a typed `application/json` data island), never a hand-built
  `<script>` + `json.dumps` — the helper owns the `<`/`>`/`&` escaping
  that keeps an untrusted string from closing the inline element early
  (the /ha/ stored-XSS bug class). A conventions test in
  [`tests/test_web_json_island.py`](tests/test_web_json_island.py)
  fails any page that hand-rolls an island.
- Confirm/alert with `jtsConfirm(msg, {danger})` / `jtsAlert(msg)` from
  [`/assets/shared/js/dialog.js`](deploy/assets/shared/js/dialog.js), never
  native `confirm()`/`alert()` — the browser can suppress those, which
  silently broke the action guards. `await` the confirm; pass `{danger:true}`
  for destructive actions.
  `onsubmit="return confirm(...)"` becomes
  `onsubmit="return jtsConfirmSubmit(this, '...', {danger:true})"`.

Switch controls must use the shared checkbox-based toggle: `toggle_html()`
for server-rendered markup plus the canonical `.toggle` rules in
[`deploy/assets/app.css`](deploy/assets/app.css). Avoid clickable
`<div class="switch">` controls. Native checkboxes give keyboard interaction,
focus state, and accessibility semantics for free.

The confirm/alert dialog ships automatically on every wizard: each one
now renders through `canonical_page()` (migration complete — see
"Canonical design system" below), which loads the shared
[`/assets/shared/js/dialog.js`](deploy/assets/shared/js/dialog.js)
module. No wizard hand-rolls its own `<!doctype html>` shell anymore, so
none should manually embed dialog CSS or inline dialog JavaScript. A regression test in
[`tests/test_web_wizard_conventions.py`](tests/test_web_wizard_conventions.py)
keeps native `confirm()`/`alert()`/`prompt()` out of the canonical ES
modules.

Treat device names, SSIDs, USB descriptors, Bluetooth MAC-adjacent
metadata, and browser-provided labels as untrusted. Escape before
assigning to `innerHTML`, or use DOM/text APIs where practical — on the
ES-module pages that means `escapeHtml` from
[`/assets/shared/js/escape.js`](deploy/assets/shared/js/escape.js), the
shared module promoted from per-page copies; the conventions test fails
any page module that re-declares its own escaper. Do not
put untrusted strings into generated inline JavaScript such as
`onclick="handler('...')"`. Prefer escaped `data-*` attributes with a
delegated click handler.

Browser phone-mic measurement pages should use
[`/assets/shared/js/measurement-audio.js`](deploy/assets/shared/js/measurement-audio.js)
for mono mic constraints, inline AudioWorklet loading, graph cleanup,
RMS-to-dBFS conversion, mono WAV encoding, and the invariant that mic
nodes never feed browser speaker output. Keep feature policy in the
owning page/module: `/balance/` owns the one-speaker ramp threshold,
`/sync/` owns marker timing and upload, `/correction/` owns calibrated
mic/device selection and capture-quality evidence. Do not migrate
`/correction/` onto the shared helper without an on-device browser pass;
its existing capture path is larger and was moved verbatim for
hardware-verified behavior.

Static regression coverage for these conventions lives in
[`tests/test_web_wizard_conventions.py`](tests/test_web_wizard_conventions.py).
If that test catches a new page, change the page to use the shared
primitive unless there is a documented, reviewed reason not to.

### Canonical design system (new look)

The management UI has migrated to the redesigned look first shipped on
the landing page ([`deploy/index.html`](deploy/index.html)): an oklch
sage/beige palette with Figtree + Outfit. **The migration is complete —
every wizard under `jasper/web/*_setup.py` renders through
[`canonical_page()`](jasper/web/_common.py) and ships its page behaviour
as a static ES module** (no `<!doctype>` hand-rolled shells, no inline
`<script>` on a migrated page). The shared design layer is a single
static stylesheet, [`deploy/assets/app.css`](deploy/assets/app.css)
— tokens, base reset, `@font-face`, and shared component primitives
(`.page`, `.eyebrow`, `.segmented`, `.btn`, `.ico`, focus/reduced-motion).
nginx serves it from `/assets/` (the same path as the fonts); `install.sh`
installs it.

A page renders with
[`canonical_page()`](jasper/web/_common.py).
`canonical_page(title, body, *, csrf_token, page_css)`
emits the document shell — doctype, the cache-busted
`/assets/app.css?v=<build-sha>` link, the CSRF meta tag, the shared inline
icon sprite (`CANONICAL_ICON_SPRITE`), and the body. **Page-specific CSS
goes in `page_css`; never add single-page rules to `app.css`.** Each page
stays self-contained (no shared-CSS single point of failure beyond nginx,
which every page already depends on for fonts); `jasper-doctor`'s
`check_web_design_assets` warns if `app.css` is missing.

[`jasper/web/sound_setup.py`](jasper/web/sound_setup.py) (`/sound/`) and
[`jasper/web/system_setup.py`](jasper/web/system_setup.py) (`/system/`)
are the reference wizards — mirror their shape when adding a new one.
The old wrapper/style/nav primitives have been deleted from
[`jasper/web/_common.py`](jasper/web/_common.py); use `canonical_page()`,
`canonical_header()`, `canonical_banner()`, and `toggle_html()` for migrated
pages. The design tokens currently live
in both `deploy/index.html` and `app.css` until the landing page is
migrated to link the stylesheet — a test
([`tests/test_web_design_system.py`](tests/test_web_design_system.py))
guards the two token blocks against drift.

The shared layer grows by promotion: a component used by more than one
page (the sticky `.app-header`/`.icon-button`, the `.info-card`/`.deflist`/
`.badge` settings vocabulary, the `.btn--*` variants) lives in `app.css`;
genuinely single-page visuals (the `/system/` stat tiles, sparklines, and
CPU bars) stay in that page's `page_css`. Status colour is one knob: a
component sets `--tone: var(--status-ok|warn|danger|idle)` on its root and
the CSS reads it.

The jts.local management UI intentionally does not show browser focus rings.
`app.css` suppresses native outlines; active/selected state must be represented
by component state (`.active`, `[aria-pressed]`, checked radio/toggle styling),
not by `:focus-visible`/`:focus-within` rings. Do not add page-level focus
outlines or box-shadow rings; the static design system tests should fail if
those selectors return.

**Page behaviour ships as static ES modules, not inline `<script>`.** A
migrated page's JavaScript lives in `deploy/assets/<page>/js/*.js` (today:
`system-status/`, `sound-profile/`), imports its siblings by relative
path, and is loaded from the body as a `type="module"` script whose `src`
is `/assets/<page>/js/main.js`. nginx
serves these from `/assets/` but — unlike the immutable `app.css`/fonts —
with `Cache-Control: no-cache` (a scoped `location ~ \.js$` block in
[`deploy/nginx-jasper.conf`](deploy/nginx-jasper.conf)), because a
relative-import module graph can't be URL-cache-busted the way `app.css`
is (`?v=<sha>`); ETag revalidation keeps it correct across deploys at the
cost of one conditional GET per module on a page open. `install.sh` copies
the per-page `js/` dirs alongside `app.css`. The module reads the CSRF
token from the `<meta name="jts-csrf">` tag (so the cached file carries no
secret) and uses the same `jsonHeaders()` / `X-CSRF-Token` contract as the
inline wizards. This is why no inline JS remains on a migrated page —
`system-status/` is split into `dom`/`format`/`charts`/`components`/
`views`/`main`; `sound-profile/` is the EQ editor relocated as a single
module (its interactions need CamillaDSP hardware to re-verify, so it was
moved verbatim rather than split — splitting it finely is a good follow-up
done on-device).

**Confirm/alert use a shared `<dialog>`, never `window.confirm`/`alert`.**
The first cross-page shared module,
[`deploy/assets/shared/js/dialog.js`](deploy/assets/shared/js/dialog.js),
exports Promise-based `jtsConfirm(message, {danger})` and
`jtsAlert(message)`, styled by the `.jts-dialog` block in `app.css`. A
migrated page imports it by absolute path (`/assets/shared/js/dialog.js`) and
`await`s the answer. Why it exists: the browser can suppress the native popups
("prevent this page from creating more dialogs"), which silently defeated
`/system/`'s restart/reboot guards — the click did nothing, with no feedback.
`<dialog>.showModal()` can't be suppressed and brings a focus trap,
ESC-to-cancel, and a backdrop for free; `danger:true` reddens the confirm
button and autofocuses Cancel. `install.sh` copies `shared/` like a page dir
and records every copied asset in `assets/.install-manifest`
([`deploy/lib/install/web-assets.sh`](deploy/lib/install/web-assets.sh)), which
`jasper-doctor`'s `check_web_design_assets` verifies file-by-file; a
regression test in
[`tests/test_web_wizard_conventions.py`](tests/test_web_wizard_conventions.py)
keeps native `confirm()`/`alert()`/`prompt()` out of the canonical ES modules.

---

## Deploying code changes to the Pi

From the laptop, one command:

```sh
bash scripts/deploy-to-pi.sh
```

This is the **only** supported deploy path. It does, in order:

1. `git rev-parse` → captures local SHA + branch (writes `-dirty`
   suffix if working tree has uncommitted changes)
2. Preflight SSH + sudo before upload. Pubkey SSH is required.
   Passwordless sudo (`sudo -n true`) is the unattended path; if the
   deploy is attached to an interactive terminal, it can fall back to
   `ssh -tt ... sudo` prompts without storing the password. Do not add
   broad sudoers rules from the installer. **If a deploy fails the
   `sudo -n` preflight on a Pi, that Pi just needs passwordless sudo
   enabled once — the recommended posture and the exact one-liner are in
   [BRINGUP.md](BRINGUP.md) "Phase 2.5 — Enable passwordless sudo". It is
   per-Pi, so a working speaker and a freshly-imaged one can differ.**
3. `rsync` to the remote user's `${HOME}/jts/` (for the default
   beginner path this is `pi@jts.local:/home/pi/jts/`; excludes `.git/`,
   `.venv/`, `captures/`, `wake-events/`, `*.egg-info`, etc.)
4. `ssh ... sudo bash install.sh` with `JASPER_DEPLOY_SHA*` env vars
   set — rsyncs the Python source from the remote checkout into
   `/opt/jasper/`, then `pip install -e`'s `/opt/jasper` into
   `/opt/jasper/.venv` (the runtime). Also writes
   `/var/lib/jasper/build.txt`, migrates units to socket
   activation, conditionally enables AEC on 6-ch firmware. See
   "Runtime Python lives in /opt/jasper" below.
5. `systemctl restart jasper-control` + `systemctl start
   jasper-aec-reconcile` — picks up Python control code and lets the
   mic/AEC reconciler restart or park `jasper-voice` according to the
   hardware actually present. `jasper-camilla` is the Rust camilladsp
   binary (not restarted).
6. Verifies the management surface: probes `/system/data.json` through
   loopback nginx with `Host: <speaker hostname>` (bounded retries) and
   **fails the deploy** if it doesn't answer 200. This exercises the
   browser path — nginx → socket-activated wizard → jasper-control
   behind its management-host guard — so a deploy can't silently ship
   a 403ing dashboard (the 2026-06-11 `Host: 0.0.0.0` regression
   class). `jasper-doctor`'s `check_management_surface` runs the same
   probe on-Pi. Skipped under `SKIP_RESTART=1` (no restart, nothing
   new to verify).

**Do NOT hand-roll `rsync + sudo bash install.sh + systemctl restart`.**
That flow exists historically but misses:
- the laptop-side SHA capture (dashboard's "Software" card shows
  "unknown")
- the post-install daemon restart on subsequent deploys (install.sh
  only conditionally restarts `jasper-voice` when the AEC default
  flips — a one-time event)

**Deploy-target identity guard:** the preflight also verifies WHICH
speaker `PI_HOST` resolves to. The first deploy records the target's
stable peer_id (`/var/lib/jasper/peer_id`) as `PI_PEER_ID=` in
`.env.local`; later deploys abort before rsync on a mismatch — an mDNS
collision rename or a re-image can silently repoint a hostname at a
different Pi. After a deliberate re-image, accept the new identity
with `JTS_ACCEPT_NEW_IDENTITY=1`. Both this guard and the direction
guard below require **passwordless sudo**: under the interactive-sudo
fallback, `ssh -tt` merges the password prompt into captured output,
so each guard skips with a printed notice rather than mis-verifying.
Details: [docs/HANDOFF-identity.md](docs/HANDOFF-identity.md).

**Deploy direction guard:** the preflight also compares the local
commit against the Pi's installed build manifest
(`/var/lib/jasper/build.txt`) and refuses to move the Pi's code
**backwards**. Multiple checkouts/worktrees (Claude *and* Codex
sessions) deploy to the same Pi; on 2026-06-11 a stale parallel
checkout deployed four minutes after a bugfix build and silently
reverted it — the hardware retest then ran the old code and the fix
looked broken. When the local commit is an ancestor of the installed
one, the deploy aborts before rsync; a deliberate rollback/bisect uses
`JASPER_DEPLOY_ALLOW_DOWNGRADE=1`. Diverged sibling branches warn and
proceed, naming the branch being replaced — if the other session's
work matters, coordinate before redeploying. Helpers
(`classify_deploy_direction`, `build_manifest_value`) live in
[`scripts/_lib.sh`](scripts/_lib.sh) and are pinned by
`tests/test_lib_deploy_direction.py`. `SKIP_INSTALL=1` (rsync-only)
deploys skip the guard: they never touch the `/opt/jasper` runtime.

**Skip / opt-in flags:** `SKIP_INSTALL=1` (rsync only),
`SKIP_RESTART=1` (install but don't restart/reconcile),
`JTS_ACCEPT_NEW_IDENTITY=1` (accept a changed deploy-target peer_id),
`JASPER_DEPLOY_ALLOW_DOWNGRADE=1` (deploy an older commit deliberately),
`JASPER_BUILD_OPTIONAL_FIRMWARE=1` (explicitly rebuild optional
ESP32 dial/satellite firmware during install), `PI_HOST=...`,
`PI_USER=...`, `JASPER_HOSTNAME=...` (speaker identity/cert hostname
when the SSH target is an IP), `REMOTE_REPO_DIR=...` (rare override
for nonstandard remote homes).

**Previewing install blast radius:** `bash deploy/install.sh --dry-run`
prints the apt package groups, downloads/source builds, runtime file
writes, env migrations, systemd actions, restarts, and post-install
checks without requiring root or mutating the host. Use it when
reviewing installer changes. It is a planning surface only — deploy
and hardware validation still go through `bash scripts/deploy-to-pi.sh`.

**Adding a wizard port to `jasper-web.socket`?** `install.sh`'s
wizard-socket loop uses `systemctl restart` (not `start`) so a new
`ListenStream=` line actually re-binds the live socket on deploy. A
bare `start` is a no-op when the socket is already active, which
silently leaves the old port set live and 502s on the new wizard
until the next reboot. Verified failure mode + fix landed in PR #118
when /sources/ on port 8773 went out without the restart.

**Verify the deploy landed:**
- `http://jts.local/system/` → Software card shows the matching
  short-SHA and recent install timestamp
- Or `ssh pi@jts.local 'sudo cat /var/lib/jasper/build.txt'`

A **fresh Pi** doing first-time setup still uses the laptop-side
onboard/deploy path once the Pi is reachable: clone JTS on the laptop,
then run `bash scripts/onboard.sh <hostname> --adopt` or
`bash scripts/deploy-to-pi.sh` (see [QUICKSTART.md](QUICKSTART.md) and
[BRINGUP.md](BRINGUP.md)). A Pi-local `git clone` + native
`sudo JASPER_HOSTNAME=<hostname>.local bash deploy/install.sh` is now
an advanced/developer fallback only, not the normal public install
path.

### Running ad-hoc diagnostics on the Pi

For memory-heavy, open-ended, or experimental Pi-side commands, use:

```sh
bash scripts/pi-run-diagnostic.sh -- <command...>
```

This wraps the command in a transient `systemd-run` unit with
`MemoryHigh=256M`, `MemoryMax=384M`, `MemorySwapMax=0`,
`RuntimeMaxSec=10min`, and `OOMScoreAdjust=500` by default. Override
with `JTS_DIAG_*` env vars only when you understand the blast radius.

Do not run raw `ssh pi@... 'sudo /opt/jasper/.venv/bin/python -'` for
large model loading, corpus scans, compiles, or other unbounded work.
Those commands can starve the 1 GB Pi. The bounded runner gives the
kernel an obvious diagnostic process to kill before it kills product
daemons.

### Runtime Python lives in `/opt/jasper`, not the rsync checkout

`install.sh` **copies** Python source into
`/opt/jasper/jasper/...` (it doesn't `pip install -e` from
the rsync checkout). The checkout lives at `${HOME}/jts` for the
SSH user (`/home/pi/jts` on the beginner `pi` path) unless
`REMOTE_REPO_DIR` overrides it; `/opt/jasper/.venv` is the runtime the
daemons actually execute. Edits to the rsync checkout don't go live
until install.sh re-copies — so a full deploy is the canonical path.

For one-off hot-patch testing without a full deploy:

```sh
scp jasper/cli/foo.py pi@jts.local:/tmp/foo.py
ssh pi@jts.local 'sudo install -m 644 /tmp/foo.py \
    /opt/jasper/jasper/cli/foo.py && sudo systemctl restart jasper-voice'
```

(Substitute the affected daemon.) For systemd units, ALSA confs,
nginx config, etc., the live location differs — check
[`deploy/install.sh`](deploy/install.sh) for the canonical
install target before patching.

---

## Speaker hostname — single source of truth

`JASPER_HOSTNAME` (default `jts.local`) is the canonical name other
devices type in to reach the speaker. Set in `/etc/jasper/jasper.env`.

What derives from it (so you only set it once):
- Python: `Config.hostname` plus `JASPER_MANAGEMENT_URL` and
  `JASPER_SPOTIFY_SETUP_URL` defaults (`http://${JASPER_HOSTNAME}` and
  `http://${JASPER_HOSTNAME}/spotify` respectively).
- Legacy bash helpers under `scripts/`: `_lib.sh` and several older
  scripts still let an unset `PI_HOST` fall back to
  `${JASPER_HOSTNAME:-jts.local}` for compatibility. New scripts and
  docs should treat `PI_HOST` as the SSH transport target and
  `JASPER_HOSTNAME` as speaker identity/cert hostname; set both when
  they differ (for example, SSH by IP but advertise `jts.local`).

What does NOT derive (intentionally):
- The Pi's actual mDNS hostname (set with `hostnamectl set-hostname`
  + Avahi). Setting `JASPER_HOSTNAME` doesn't change what the Pi
  advertises — that's a separate, OS-level concern. Run hostnamectl
  first; then point `JASPER_HOSTNAME` at it.
- The Spotify OAuth bounce page at
  `https://jaspercurry.github.io/spotify-oauth-callback/` — separate
  public repo (`jaspercurry/spotify-oauth-callback`). It's hostname-
  agnostic: the local target is passed in as `?host=<JASPER_HOSTNAME>`
  on the redirect URI registered with Spotify, validated against an
  mDNS regex, and used as the redirect target. So changing
  `JASPER_HOSTNAME` here Just Works against the same hosted page —
  no fork-and-redeploy.

**Identity drift is reconciled, renames are scripted.** The OS
hostname, Avahi's *effective* mDNS name (which silently changes to
`<name>-2.local` when another device claims the same hostname), and
`JASPER_HOSTNAME` can disagree. `jasper-identity-reconcile` (boot +
5-min timer, pure observer) snapshots all three into
`/var/lib/jasper/identity.env`; `jasper.http_security` reads that file
plus an avahi-suffix rule so a collision-renamed speaker's management
UI stays reachable instead of 403ing; `/state.resilience.identity` and
`jasper-doctor` surface collision/drift with remediation. **To rename
a speaker, use `bash scripts/rename-speaker.sh <new-name>`** — it
converges hostnamectl, `jasper.env`, avahi, the laptop state, and the
TLS cert (via a full deploy) in one operation; a bare `hostnamectl` by
hand leaves the derived surfaces drifted. Canonical doc:
[docs/HANDOFF-identity.md](docs/HANDOFF-identity.md).

---

## Laptop-side state — `.env.local` and `CLAUDE.local.md`

The Pi-side single-source-of-truth above is `JASPER_HOSTNAME`. The
laptop-side single source of truth for "which Pi does this checkout
talk to?" is **`.env.local`** at the repo root. Gitignored. Recognized
keys:

```sh
PI_HOST=jts.local       # SSH target; may be an IP
PI_USER=pi
JASPER_HOSTNAME=jts.local  # speaker hostname/cert identity
```

Keep `PI_HOST` and `JASPER_HOSTNAME` conceptually separate. If a user
connects by IP, the IP is only the SSH target; onboarding records an
explicit `JASPER_HOSTNAME` by querying the Pi hostname or by accepting
`--speaker-hostname <name>.local`.

It's auto-written by `bash scripts/onboard.sh <hostname> --adopt` (see
[QUICKSTART.md](QUICKSTART.md)) and sourced by
[`scripts/_lib.sh`](scripts/_lib.sh), which every laptop-side script
should source as its first non-`set` line. New scripts pick up the
state for free.

`CLAUDE.local.md` (same root, also gitignored, also written by
`onboard.sh`) is the Claude-Code-facing companion: it's `@`-imported
from [CLAUDE.md](CLAUDE.md) so every session lands with the active
Pi in its context window. Missing file is a graceful no-op — fresh
clones work; `onboard.sh` populates it on first run.

**Multi-Pi pattern**: one checkout (or worktree) per Pi. Each has
its own `.env.local` and `CLAUDE.local.md`. The wrapper scripts
(`deploy-to-pi.sh`, `fetch-pi-logs.sh`, etc.) honor `PI_HOST`
from `.env.local`; `_lib.sh` keeps the old `JASPER_HOSTNAME`
fallback only for compatibility, so flipping between Pis is just `cd`
into the right checkout.

**Adopting an already-deployed Pi** (password auth only): run
`bash scripts/onboard.sh <hostname> --adopt`. The `--adopt` flag
runs `ssh-copy-id` first so subsequent commands use pubkey auth. It
does not change sudoers; deploy preflights sudo separately and prompts
interactively when possible.

**Custom user boundary:** the beginner/fresh-appliance path uses
username `pi`. `--user` / `PI_USER` is advanced and currently supported
for onboarding/deploy only; some diagnostics and operator scripts still
assume `pi` or `/home/pi`. Do not imply full custom-user coverage unless
those scripts have been audited.

**Switching active target without re-onboarding**: `bash scripts/use
<hostname> [user] [speaker-hostname]`. Rewrites `.env.local` +
`CLAUDE.local.md` in one shot;
no ssh, no install. Use this to flip a checkout between Pis that
have both already been onboarded (the SSH alias from a prior
`onboard.sh` run persists in `~/.ssh/config` so `ssh jts` /
`ssh jts2` continue to work).

**Driving onboarding for an end user**: invoke the `/onboard-pi` skill
([`.claude/commands/onboard-pi.md`](.claude/commands/onboard-pi.md)).
It auto-triggers when a user says any natural-language variant of "set
up a Pi" / "install JTS" / "I just got a new speaker." The skill
covers the full flow (hardware sanity → Pi Imager → flash → boot →
`scripts/onboard.sh` → post-install wizards), drives it interactively
one question per turn, and proactively probes the LAN for existing
speakers to suggest a non-colliding hostname before the user picks
one in Imager. **This is the canonical entry point for the human-
facing setup story.** `QUICKSTART.md` is the same flow in human-
readable form for users who'd rather read than be guided.

---

## Renderer architecture — file map

`install.sh` source-builds shairport-sync (AirPlay 2) + nqptp,
drops in librespot (rust, via raspotify .deb) + bluez-alsa + the
JTS no-code `bt-agent.service`, installs the optional `jasper-usbsink`,
and owns the full systemd unit per renderer.

`jasper/renderer.py:RendererClient` reads renderer state from each
daemon's own surface:
- librespot → `/run/librespot/state.json` (written by the
  `--onevent` hook `/usr/local/bin/jasper-librespot-event`)
- shairport-sync → MPRIS PlaybackStatus over busctl
- bluez-alsa → `bluealsa-cli list-pcms`
- jasper-usbsink → `/run/jasper-usbsink/state.json`

`jasper-mux.service` owns renderer source policy. In auto mode it does
latest-source-wins preemption: when a new source transitions to playing
while another is already active, it pauses the older one. The landing
page's Source selector can switch mux into manual mode; mux then asks
`jasper-fanin` to pass one renderer lane without turning any source on
or off. Before moving the fan-in gate, mux asks `VolumeCoordinator` to
prepare the target source's volume carrier; after the gate moves, it
finalizes the steady-state carrier. This is the source-switch transient
guard. While no source has a guarded winner, mux keeps fan-in in `NONE`
so a renderer cannot leak through between polls. The `/sources/`
wizard remains the on/off surface.

All music/content sources enter the fan-in topology through a private
snd-aloop lane. Before adding another playback source, read
[`docs/audio-paths.md`](docs/audio-paths.md#adding-a-new-music-source);
that checklist is the single source of truth for `jasper/music_sources.py`
source metadata, lane assignment, fan-in config, mux, volume,
`/sources/`, doctor, and correction measurement-window updates,
including `/source/select` landing-page selection wiring.

### Final output — `jasper-outputd`

`jasper-outputd.service` is the **final-output owner**: it sits after
fan-in and CamillaDSP in the chain and owns "what the speaker actually
emits." `jasper-voice` declares it as a hard `After=`/`Wants=`
dependency; `jasper-camilla` integrates with it through a shared
CamillaDSP statefile (`outputd-statefile.yml`, seeded from
`outputd-cutover.yml`) rather than a systemd dependency. In solo mode,
assistant TTS/cues route to fan-in's outputd-compatible local socket
(`JASPER_TTS_TRANSPORT=outputd`,
`JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-fanin/tts.sock`,
`JASPER_DUCK_TRANSPORT=fanin`) so they enter before CamillaDSP and keep
the crossover/correction/protection path. While a speaker is bonded in
multi-room mode, the grouping reconciler may layer
`/var/lib/jasper/grouping-voice.env` to point voice at
`/run/jasper-outputd/tts.sock` and arm outputd's local TTS lane for
member-local responses. Source-of-truth code: `jasper/config.py`
(`Config.from_env`), `deploy/systemd/jasper-voice.service`, and
`jasper/multiroom/reconcile.py`. The topology contract lives in
[`jasper/output_topology.py`](jasper/output_topology.py). This is coarse
on purpose — the canonical design (output/reference/TTS/barge-in signal
flow, rollback behavior) is
[`docs/HANDOFF-speaker-output-reference.md`](docs/HANDOFF-speaker-output-reference.md).

Spotify volume control goes via the Spotify Web API (the multi-
account `spotify_router`) since librespot has no local control
HTTP — see [`docs/HANDOFF-volume.md`](docs/HANDOFF-volume.md).

CamillaDSP configs must keep the project safety ceiling in place:
`devices.volume_limit` is `0.0` in the base config and generated
correction/sound configs, and `CamillaController.set_volume_db`
clamps positive writes to 0 dB as runtime defense in depth.
`jasper-doctor` checks the active config; do not remove this floor
when adding new DSP config generators.

---

## Voice provider switching — read first

The voice loop runs against any of three real-time speech-to-speech
APIs. Architecture and per-provider trade-offs are in
[`docs/HANDOFF-voice-providers.md`](docs/HANDOFF-voice-providers.md);
this section is the operational summary.

### Single source of truth: `/var/lib/jasper/voice_provider.env`

**`JASPER_VOICE_PROVIDER` lives in exactly one file**:
[`/var/lib/jasper/voice_provider.env`](deploy/systemd/jasper-voice.service),
written by the `/voice` wizard. **Never set it in
`/etc/jasper/jasper.env`** — `install.sh` migrates any stale value
out of there into the wizard file on every run, since having a
default in BOTH led to stale-vs-runtime confusion (the wizard wrote
one value, the install template still had another, and reading
either file in isolation gave a wrong answer about "what's the
active provider").

There is **no fallback default**. Fresh installs land with the
variable unset; `jasper-voice` refuses to start with a clear error
("visit `http://jts.local/voice` and pick one") until the wizard
writes the file. The doctor and the `/system/` dashboard surface
this state. Same pattern applies to the per-provider keys
(`GEMINI_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`) and model /
voice selectors — all wizard-owned per
[`jasper/web/voice_setup.py`](jasper/web/voice_setup.py).

**Reading the active provider in code — one reader, never `os.environ`.**
Surfaces that display or aggregate the active provider but are *not*
`jasper-voice` (chiefly `jasper-control`'s `/state` and the `/system/`
dashboard) MUST resolve it through
[`jasper/voice/provider_state.py`](jasper/voice/provider_state.py)
(`read_active_provider()` / `read_active_provider_and_model()`), which
re-read the SSOT file fresh on every call. They must **not** read
`JASPER_VOICE_PROVIDER` from `os.environ`: those long-lived daemons load
the env file once at start and are not restarted on a switch, so
`os.environ` goes stale — that was the "`/system/` still shows the old
provider after switching" bug. Only `jasper-voice` is restarted on a
switch, so `Config.from_env` there is always fresh. The reader returns
`""` (unconfigured) for an unset/invalid value — never a guessed
default.

**To override without using the wizard** (CI, headless imaging,
operator preference): write `JASPER_VOICE_PROVIDER=<id>` to
`/var/lib/jasper/voice_provider.env` directly. systemd loads it on
the next jasper-voice start.

### Two ways to switch the active provider

**Web UI (preferred, end-user friendly)** — visit
`http://jts.local/voice/` from any device on the LAN. The page
shows one card per provider for pasting API keys, picks model and
voice from curated dropdowns, and has a single radio group at the
top for "use this provider". Saving writes
`/var/lib/jasper/voice_provider.env` and restarts `jasper-voice`.
Source: [`jasper/web/voice_setup.py`](jasper/web/voice_setup.py).

**Laptop-side script (operator-friendly, scriptable)**:

```sh
bash scripts/switch-voice-provider.sh           # show current
bash scripts/switch-voice-provider.sh gemini    # gemini-3.1-flash-live-preview
bash scripts/switch-voice-provider.sh openai    # gpt-realtime-2 (released 2026-05-07)
bash scripts/switch-voice-provider.sh grok      # grok-voice-think-fast-1.0
```

The script refuses to switch if the destination provider's API key
isn't already in `/etc/jasper/jasper.env` (`GEMINI_API_KEY`,
`OPENAI_API_KEY`, or `XAI_API_KEY`) or in the wizard-written
`/var/lib/jasper/voice_provider.env`. Set the key first via
either path; the script sets the provider and restarts
`jasper-voice` in one shot.

**Per-provider model env var** is independent of the provider switch
— `JASPER_GEMINI_MODEL`, `JASPER_OPENAI_MODEL`, `JASPER_GROK_MODEL`.
The `switch-gemini-model.sh` script (below, "Gemini model switching")
flips the *Gemini* model alias for within-Gemini fallback (3.1 ↔ 2.5)
and is independent of cross-provider switching.

**Pricing trade-off** (early 2026):

| Provider | Cost / minute | Notes |
|---|---|---|
| `gemini` | ~$0.025 | cheapest; 15-min audio cap with 2-h resumption handle |
| `openai` | ~$0.30 | reasoning levels, 128K context, 60-min hard cap, no resumption |
| `grok` | ~$0.05 | flat $3/hour, metered by connection uptime (`ConnectionUptimeMeter`), not tokens |

Spend accounting (`jasper/usage.py`): the stored `cost_usd` is a true
estimate at built-in list rates (overridable via
`/var/lib/jasper/pricing.json` / `JASPER_PRICING_FILE`); the spend cap
pads it at read time via `JASPER_DAILY_SPEND_CAP_SAFETY_MULTIPLIER`
(default 1.25) rather than inflating the displayed number. Gemini's
session-cumulative token counter is normalised to per-turn deltas so
`SUM()` across rows doesn't multi-count.

**Cue regeneration**: cue WAVs (static failure cues +
dynamic-content cues like timer fire announcements) are baked from
the **active provider's TTS endpoint** — Gemini 3.1 Flash TTS,
OpenAI gpt-4o-mini-tts, or xAI Grok TTS — picked by the factory at
[`build_cue_tts_backend`](jasper/cues/factory.py) (re-exported
through `jasper.cues` and called from `jasper/voice_daemon.py`).
Cues sound in the same voice the assistant uses for live replies.
Switching providers (env or web wizard) auto-invalidates baked
WAVs via the cache key (model + voice change → new hash).
Per-provider model defaults are pinned in
[`jasper/cues/generator.py`](jasper/cues/generator.py) and
overridable for Gemini via `JASPER_GEMINI_TTS_MODEL`. If the
active provider's key is missing, the factory falls back to any
other configured key with a warning so cues still play; with no
keys at all, regen is disabled and the daemon plays whatever WAVs
already exist on disk.

**Adding a fourth provider**: see the "Adding a fourth provider"
checklist in
[`docs/HANDOFF-voice-providers.md`](docs/HANDOFF-voice-providers.md).
The interface is `LiveConnection` + `LiveTurn` at
[`jasper/voice/session.py`](jasper/voice/session.py); shared
supervisor helpers (backoff, fingerprint, escalation cue) live at
[`jasper/voice/_supervisor.py`](jasper/voice/_supervisor.py).

---

## Voice prompting — read HANDOFF-prompting.md first

Before editing `SYSTEM_INSTRUCTION` in
[`jasper/voice/prompt.py`](jasper/voice/prompt.py), any tool
description in [`jasper/tools/`](jasper/tools/), or any LLM-facing
prompt surface, read
[`docs/HANDOFF-prompting.md`](docs/HANDOFF-prompting.md). It's the
canonical playbook — cross-provider principles, provider deltas,
the JTS `SYSTEM_INSTRUCTION` walk-through, a tool-prompt cookbook,
and a pitfalls catalog. Refreshed against the provider docs
2026-05-23.

The rules most often violated without it:

- **Conditional over absolute.** OpenAI's docs say "remove
  `always`/`never`/`only`/`must` rules unless truly required."
  Absolute preamble bans get ~33% compliance on gpt-realtime
  per a public community thread. Phrase rules as "When X, do
  Y" and enumerate X — the model doesn't generalize unstated
  scopes. The Gemini story is muddier (forum evidence of 3.1
  audio ignoring conditionals 2.5 honored) — documented in the
  playbook.
- **POSITIVE framing for tool calls.** "Call X when Y," not
  "Don't guess." A negative-heavy version of our prompt made
  gpt-realtime-2 skip tools across five voice-eval scenarios —
  rationale in the comment block above `SYSTEM_INSTRUCTION` in
  [jasper/voice/prompt.py](jasper/voice/prompt.py).
- **Preamble suppression is a conditional skip-list, never a
  ban.** Live version in the `Tools — preambles` section of
  `SYSTEM_INSTRUCTION`; mirrors OpenAI's documented pattern.
- **Per-tool conditional rules belong in the tool's docstring,
  not `SYSTEM_INSTRUCTION`.** `build_tool()` at
  [jasper/tools/__init__.py](jasper/tools/__init__.py) sends
  the full cleaned docstring to the LLM. When-to-call,
  voice-answer style, and response-shape handling live in each
  tool's docstring. `SYSTEM_INSTRUCTION` keeps only cross-tool
  meta-rules (`error` / `confirm` field handling, preamble
  policy, verbosity, unclear-audio handling, the small set of
  cross-tool routing rules where two similar tools need
  disambiguation).

Canonical provider sources (full list in HANDOFF-prompting.md):
- OpenAI Realtime — [Realtime Prompting Guide](https://cookbook.openai.com/examples/realtime_prompting_guide)
  + [Using realtime models](https://developers.openai.com/api/docs/guides/realtime-models-prompting)
- Gemini Live — [3.1 Flash Live Preview docs](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview)
  + [Live API best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices)
- xAI Grok Voice — [Voice agent guide](https://docs.x.ai/docs/guides/voice/agent)

---

## Wake-word switching — read first

The wake phrase the speaker listens for is one of a curated set of
openWakeWord models. As of 2026-05-16 the default is **"Jarvis"**
(the fwartner community model in `/var/lib/jasper/wake/jarvis_v2.onnx`,
which also still triggers on "Hey Jarvis"). The registry of available
models is the single source of truth at
[`jasper/wake_models.py`](jasper/wake_models.py); install.sh reads it
to know which non-bundled `.onnx` files and openWakeWord
package-resource assets to fetch and hash-check.

**Two ways to switch.** Either works.

**Web UI (preferred)** — visit `http://jts.local/wake/` from any LAN
device. One row per registered model with pronunciation + description
+ author-reported false-fire rate. Pick one, hit Save — writes
`/var/lib/jasper/wake_model.env` at mode 0644 and restarts
`jasper-voice`. A sensitivity slider underneath the picker tunes
`JASPER_WAKE_THRESHOLD` (0.05–0.95, default 0.30 — lower wakes more
easily, higher requires a more confident match), persists into the
same env file, and has its own Save control. Source:
[`jasper/web/wake_setup.py`](jasper/web/wake_setup.py).

**Laptop-side script:**

```sh
bash scripts/switch-wake-word.sh             # show current + options
bash scripts/switch-wake-word.sh jarvis_v2   # community Jarvis (default)
bash scripts/switch-wake-word.sh hey_jarvis  # stock Hey Jarvis
bash scripts/switch-wake-word.sh alexa       # stock Alexa
bash scripts/switch-wake-word.sh hey_mycroft # stock Hey Mycroft
```

The script resolves the key via the Pi-side registry, refuses to
flip to a model whose `.onnx` is missing on disk (rare — install.sh
fetches them every deploy), and restarts `jasper-voice`.

**Adding a new model**: edit `REGISTRY` in
[`jasper/wake_models.py`](jasper/wake_models.py) with one
`WakeModelEntry(...)`. Bundled openWakeWord names (e.g. `alexa`) need
no `download_url`, but their exact ONNX file must be listed in
`OPENWAKEWORD_ASSETS` so install.sh can stage it with bounded retries,
a byte cap, and SHA-256 verification. External `.onnx` files set
`download_url` to a raw URL and `model` to an absolute path under
`/var/lib/jasper/wake/`. Re-run `bash scripts/deploy-to-pi.sh` and
the new model appears in `/wake/` and `switch-wake-word.sh`
automatically.

**Hand-rolled custom models** are still supported: set
`JASPER_WAKE_MODEL=/abs/path/to/foo.onnx` in `/etc/jasper/jasper.env`
directly. The wizard surfaces this as a "Custom: …" row and won't
overwrite it unless the household picks a registered alternative.

---

## Wi-Fi switching — read first

The household-facing way to change the speaker's Wi-Fi network is
the wizard at `http://jts.local/wifi/`. Current network at the top,
Scan button + tap-to-connect for nearby networks in the middle,
manual "Join by name" fallback for hidden or scan-suppressed networks,
and Saved networks (Forget anything) in a collapse section at the
bottom. All backed by `nmcli` subprocess calls; no new dependency.

**Lockout safety is the part to read before editing this page.**
Three layers, all in [`jasper/web/wifi_setup.py`](jasper/web/wifi_setup.py):

1. **Connect rollback.** `nmcli --wait 30 dev wifi connect …` — on
   non-zero exit we explicitly `nmcli --wait 20 connection up
   <previous-profile>` to put the user back on the network they were
   on. If that connect created a brand-new (broken) profile, we
   delete it so the saved list doesn't accumulate dead entries.
   Don't rely on NM's auto-rollback alone — it's not reliable across
   all failure modes.

2. **Forget guard.** If the user tries to forget the currently-active
   network, an extra warning fires in the inline confirm panel —
   stronger when no Ethernet is plugged in.

3. **Radio kill warning.** Toggling the Wi-Fi radio off when the Pi
   has no Ethernet path fires a confirm() dialog with stark caps-lock
   copy: "TURNING WI-FI OFF WILL DISCONNECT THIS PI". The page can't
   reach the Pi after the radio goes down, so this dialog is the
   user's only chance to bail out.

Lockout classification is driven by `_has_ethernet()` (the `lockoutRisk`
field on `/state`). If the Pi has both Wi-Fi and Ethernet, the
warnings soften — Ethernet is the fallback path.

**Operational reach:** there's no laptop-side script wrapper for this
(unlike `switch-voice-provider.sh` / `switch-wake-word.sh`). Manual
nmcli still works for SSH-driven changes:

```sh
nmcli dev wifi list
nmcli dev wifi connect "<SSID>" password "<PSK>"
nmcli connection delete "<NAME>"
```

The wizard polls `/state` every 7 s so SSH-driven changes show up in
the UI without a manual reload.

The Available networks list deliberately hides the currently-connected
SSID. That network is already represented by the current-network card;
keeping it out of the connectable list avoids a pointless "connect to
what I am already using" action. Scan diagnostics still count the raw
current-network row for suppression detection.

**Manual SSID join is supported.** The "Join by name" form posts SSID
and optional PSK to the same rollback-protected `/connect` path used
by tap-to-connect. Its Hidden network checkbox passes `hidden yes` to
`nmcli`; even without the checkbox, the backend retries with
`hidden yes` when NetworkManager reports that the SSID is absent from
the scan cache. That retry is intentional for true hidden SSIDs and
for Pi 5 radios whose scan cache is broken by brcmfmac suppression.

**Scanning returns only the connected SSID? Known Pi 5 brcmfmac
firmware bug.** When the kernel logs `brcmf_cfg80211_scan:
Scanning suppressed: status (4)` continuously, that's the
`BRCMF_SCAN_STATUS_SUPPRESS` bit getting stuck after a DHCP exchange
or Bluetooth-coexistence event. The driver returns `-EAGAIN` to every
scan request until the bit clears, but the closed-source chip firmware
on the Pi 5 doesn't always clear it.

Do not confuse scan suppression with `iw reg get` showing
`phy#0 country 99: DFS-UNSET`. Linux uses the `99` alpha2 for a
driver-built regulatory domain whose specific ISO country cannot be
determined. On Pi 5 brcmfmac that per-phy value can remain `99` even
when the real, actionable cfg80211 global country is set correctly by
Pi Imager or `raspi-config nonint do_wifi_country`. We verified this
on hardware: cmdline has `cfg80211.ieee80211_regdom=US`, global
regdom = US, phy0 = 99, and scanning can still work after the
suppression repair.

`jasper-doctor`'s `check_wifi_regdom` warns only when the global
regdom is unset (`00` / `99`) or cannot be parsed. A per-phy `99`
with a valid global country is reported as OK detail, not a warning.
Diagnostic:

```sh
sudo iw reg get
# Fine as long as the global section is a real country:
# global
# country US: DFS-FCC
# phy#0
# country 99: DFS-UNSET
```

The `/wifi/scan` backend productionizes the least disruptive repair we
validated on hardware: after a scan is classified as
`driver_scan_suppressed`, [`jasper/wifi_scan_repair.py`](jasper/wifi_scan_repair.py)
sends `NL80211_CMD_CRIT_PROTOCOL_STOP` to `wlan0`, waits briefly, and
retries the scan. This does not intentionally drop WiFi and is
rate-limited by `/var/lib/jasper/wifi_scan_repair.json` so page reloads
or repeated button taps do not spam the radio. Structured logs use
`event=wifi_scan_repair.*`.

The repair is intentionally narrow: it only runs for the brcmfmac
driver, only after suppression evidence, and never reloads kernel
modules. If it cannot repair the scan, the UI keeps "Join by name"
available with rollback protection.

Remaining workarounds with real trade-offs: reload brcmfmac (drops WiFi;
[OpenWrt #23069](https://github.com/openwrt/openwrt/issues/23069)
documents the chip wedging after repeated reloads on Pi 5),
`sudo rpi-update` (newer firmware may help, may regress other
things), external USB WiFi dongle (100% works, hardware change).

**WPA-Enterprise (802.1X) not supported.** Home networks only. The
scan-list filter shows "WPA-Enterprise" as the security label so the
user knows why connecting won't work, but the Connect panel doesn't
expose cert/identity fields.

### Profile guardian — self-heal after filesystem loss

The 2026-05-23 incident: a USB-C power yank during a power-splitter
swap left the Pi's root ext4 partition with an in-flight write to
`/etc/NetworkManager/system-connections/<SSID>.nmconnection`. Journal
recovery discarded the file. The Pi rebooted with no WiFi profile,
was unreachable on the LAN, and required HDMI + USB-keyboard recovery.

The behavioural fix (graceful shutdown via the `/system/` Power Off
button) is being adopted separately. The **WiFi profile guardian** is
the software floor under it: if the NM keyfile ever disappears for
any reason — this incident, filesystem corruption, an accidental
`rm`, a botched migration — the Pi self-heals on next boot rather
than bricking.

**Architecture** mirrors `jasper-aec-reconcile`. Wizard-owned env
file at `/var/lib/jasper/wifi_guardian.env` (mode 0600, three keys:
`JASPER_WIFI_SSID` / `JASPER_WIFI_PSK` / `JASPER_WIFI_KEY_MGMT`).
Pure-bash policy script at
[`deploy/bin/jasper-wifi-guardian`](deploy/bin/jasper-wifi-guardian)
run by `jasper-wifi-guardian.service` (`Type=oneshot`, after
`NetworkManager-wait-online.service`, gated by
`ConditionPathExists=`).

Zero resident RAM. ~3-5 ms at boot in the steady-state path. Full
design in [`docs/HANDOFF-resilience.md`](docs/HANDOFF-resilience.md)
"WiFi profile recovery" section.

**Lifecycle.** The wizard writes the stash on every successful
`/wifi/` connect (`connect_new` sees the PSK on the wire; `connect_saved`
re-reads it from NM via `nmcli -s`). `install.sh`'s
`migrate_wifi_guardian` seeds the stash from the currently-active
profile on every deploy so SSH-driven setups also arm the recovery.
`forget` clears the stash only when the forgotten SSID matches the
stashed one — forgetting a guest network doesn't invalidate recovery
for the household network.

**What the guardian does at boot:**

```
if active WiFi matches stash SSID    -> no-op (steady_state)
if active WiFi differs from stash    -> no-op (stash_stale)
                                        (operator manually switched
                                         via SSH; don't disconnect them)
if no WiFi, profile EXISTS in NM     -> `nmcli connection up SSID`
if no WiFi, profile MISSING          -> THE INCIDENT: recreate via
                                        `nmcli dev wifi connect`
```

The stash-stale path is the most important defensive behaviour: a
wrong action here would disconnect a working network mid-operator-
session. Mirrors AEC reconciler's "custom JASPER_MIC_DEVICE → leave
voice config untouched" idiom.

**Observability:**

```sh
# Per-event structured lines (one per guardian run):
journalctl -u jasper-wifi-guardian | grep event=wifi_guardian

# Live state from jasper-control:
curl -s http://jts.local:8780/state | jq .resilience.wifi_guardian

# Doctor surface (warn on stash absence + drift):
sudo /opt/jasper/.venv/bin/jasper-doctor | grep "WiFi profile guardian"

# Manual trigger (operator decided to retry after a known-bad boot):
sudo /usr/local/sbin/jasper-wifi-guardian --reason manual
```

**PSK redaction.** The PSK lives in the stash file (mode 0600, root
only — mirrors NM's own `/etc/NetworkManager/system-connections/`
posture). It does NOT appear in any log line: the bash script scrubs
both literal-PSK and `password \S+` patterns from nmcli stderr
before re-emission; the Python wizard hook logs only SSID +
key_mgmt; the doctor + `/state` blocks read the stash for SSID but
never expose the PSK in any output.

**Out of scope (deferred unless observed need):**
- **NM dispatcher script** on the `up` event. The wizard hook
  covers the canonical path; SSH-driven changes get caught by
  `install.sh`'s migration on the next deploy. A dispatcher would
  race the wizard's own connect and add debugging-during-incident
  confusion.
- **Multi-network stash.** Household speaker doesn't travel. If
  someone takes their JTS on the road, revisit.
- **WPA-Enterprise.** Same scope as the wizard itself — the
  guardian skips with `event=wifi_guardian.skip reason=enterprise`
  if it encounters one.

---

## Transit configuration — read first

The subway, bus, and Citi Bike tools (`get_subway_arrivals`,
`get_bus_arrivals`, `get_citibike_status`) are configured via the
wizard at `http://jts.local/transit/`. The wizard owns **every**
transit env var:

- `JASPER_TRANSIT_LAT`, `JASPER_TRANSIT_LON`,
  `JASPER_TRANSIT_DISPLAY_NAME` (wizard scaffolding; user's geocoded
  home, ~110 m precision)
- `JASPER_SUBWAY_STATION_ID`, `JASPER_SUBWAY_DEFAULT_DIRECTION`
  (empty = both directions)
- `JASPER_MTA_BUSTIME_KEY`, `JASPER_BUS_STOPS` (multi-stop list
  formatted as `id|label,id|label`, parsed by `jasper.bus.parse_bus_stops`)
- `JASPER_CITIBIKE_STATIONS` (multi-station, same `id|label,id|label`
  shape as bus, parsed by `jasper.citibike.parse_saved_stations`),
  `JASPER_CITIBIKE_EBIKE_ONLY` (`"1"` to suppress classic-bike
  counts in voice answers; empty / `"0"` reports both kinds)
- `JASPER_TRANSIT_CITIES` (comma-separated `CityPack` ids, e.g. `nyc`)
  — the household's enabled city packs, toggled in the `/transit/`
  wizard's **Transit cities** section. A pack being on makes its
  providers *eligible*; each still self-gates on its own config above,
  and the wizard only renders a city's provider cards while that city is
  on. Absent vs present is load-bearing in
  `jasper.transit.enabled_pack_ids`: **key absent → all packs** (the
  non-breaking default for installs predating the toggle), but **key
  present → exactly the listed packs, even when empty** — so unchecking
  every city writes an explicit empty value that means "no transit," not
  a fall-back to all. `install.sh` seeds `nyc` for households that
  already use NYC transit. Which packs are enabled is surfaced at
  `/state.transit` (`{packs:[{id,label,enabled}]}`, read fresh from
  transit.env by [`jasper/transit/state.py`](jasper/transit/state.py) —
  never `os.environ`, since jasper-control isn't restarted on a save).

All live in **`/var/lib/jasper/transit.env`** at mode 0640 — same
single-source-of-truth pattern as `voice_provider.env`. Never put
them in `/etc/jasper/jasper.env`. `install.sh`'s
`migrate_transit_config` moves any stale operator-set values into
the wizard file on every deploy. `jasper-voice.service` sources
both files with the wizard file last so it wins on conflicts.

**Subway behavior.** "Next train" returns every line stopping
at the station — including trains rerouted from other lines during
service changes. This works because Subway Now's `/api/stops/{id}`
endpoint aggregates across all 7 MTA GTFS-RT feeds server-side
(an N rerouted onto D tracks at a D station appears in the same
response as the regular Ds). The nyct-gtfs fallback can only see
the station's CSV-documented lines (no reroutes during fallback —
documented degradation; Subway Now outages are rare). See
[`jasper/subway.py`](jasper/subway.py) docstring for the full
prior-art chain.

**Two-step bus flow.** MTA BusTime requires a free API key
(register at the wiki — link in the wizard). The bus card is
**locked** until a key is pasted: nothing else renders, because
the stops-lookup endpoint itself needs that key. Saved → re-render
unlocks the picker. The unlocked card shows nearby stops grouped
by intersection (opposing-direction stops at one corner cluster);
**check multiple stops** if you want voice answers covering both
directions. Each arrival in the voice answer names its
`stop_label` so you can tell which bus is at which stop.

**Routes per stop come from SIRI, not OBA.** MTA's OBA
`stops-for-location` returns GTFS-static-scheduled routes only —
lagging real-world dispatch (e.g. B70 was rerouted via 4 Av/39 St
in 2023 but OBA still listed only B35 for that stop). The wizard
SIRI-probes each candidate stop in parallel during render to
enumerate the routes actually dispatching there. OBA's `routes`
field is the fallback when SIRI is silent (off-peak quiet stop).

**External-config soft-unlock.** If `JASPER_MTA_BUSTIME_KEY` is in
`os.environ` (from a hand-edited `/etc/jasper/jasper.env`) but not
yet in `transit.env`, the wizard renders the bus card unlocked
with a yellow notice — values are visible, save moves them into
the wizard's owned file.

**Citi Bike flow.** Keyless (GBFS is public CDN at
`gbfs.citibikenyc.com`). Card unlocks as soon as the user's
coordinates are inside the bbox (NYC + Jersey City + Hoboken).
A household-wide "Only mention e-bikes" toggle sits above the
multi-station picker; check it when the household only rides
e-bikes and the classic-bike count is noise. Each picker row
shows a live snapshot (`{classic} classic, {ebikes} e-bikes,
{docks} docks`) so users pick informed; the voice tool re-fetches
every time so the snapshot is informational only. **GBFS feeds
are cached in-process** at [`jasper/citibike.py`](jasper/citibike.py)
(30 s for `station_status`, 1 h for `station_information`) with
**stale-on-error**: a transient GBFS outage serves the last cached
copy at WARN log level — the voice answer degrades to "as of a
few minutes ago…" rather than going silent. Per-station response
includes `last_reported_age_seconds` so the LLM can disclose
staleness. **Station drift** (a saved station retired by Lyft)
surfaces as `status="missing"` in the tool response and is logged
at WARN by `CitiBikeClient.get_status`; `jasper-doctor`'s
`check_citibike` flags drift at boot/probe time. Full design
in [`docs/HANDOFF-transit-citibike.md`](docs/HANDOFF-transit-citibike.md).

**Address geocoding** runs against OSM Nominatim (Photon as
fallback). No API key, but the policy requires a descriptive
User-Agent + 1 req/sec throttle — both enforced in
[`jasper/transit/geocode.py`](jasper/transit/geocode.py). The
address is never persisted; only the resulting coords (3 decimals)
land in `transit.env`. The wizard discloses this inline next to
the address field.

**Modular provider registry + city packs.** Providers are
**self-contained**: each module under
[`jasper/transit/providers/`](jasper/transit/providers/) owns both its
wizard surface (`bbox`, `find_stops_near`, `validate_credentials`) AND its
voice runtime (`build_client(env)` → client-or-None, `make_tools(client)`
→ LLM tools, both lazy-importing heavy deps so the socket-activated wizard
process stays light). `build_client` parses the provider's OWN env keys
(the same ones it declares in `env_keys`), so adding a provider/city needs
**no `jasper/config.py` edit** — the boundary that keeps it self-contained.
Providers are grouped into `CityPack`s — one household-facing on/off per
city via `JASPER_TRANSIT_CITIES` (wizard-owned; unset = all packs,
non-breaking). The flat `REGISTRY` is **derived** from `CITY_PACKS`, so they
never drift, and `jasper-voice` calls `active_transit(env)` once — it walks
the enabled packs and builds tools with **zero per-provider knowledge in the
daemon**, each provider guarded so one broken provider can't crash startup.

So adding transit is now **no `voice_daemon.py` edit**. Two shapes: a new
*mode in an existing city* appends a provider to that `CityPack`'s
`providers` tuple; a new *city* adds one `CityPack` to `CITY_PACKS`. The
remaining edits are genuinely per-provider (bespoke UI + the live
tool/client surface):
  1. New provider module under [`jasper/transit/providers/`](jasper/transit/providers/)
     — discovery + runtime surfaces (mirror `nyc_subway.py` keyless or
     `nyc_bus.py` credentialed)
  2. Add it to a `CityPack` in `CITY_PACKS` (new pack for a new city;
     append to an existing pack's `providers` for a new mode). `REGISTRY`
     derives automatically — no separate registry edit
  3. One `elif p.id == "<slug>":` dispatch branch in `_index_html`
     at [`jasper/web/transit_setup.py`](jasper/web/transit_setup.py)
  4. A bespoke `_<slug>_card_html(p, state)` renderer in that same
     file — there is no generic card (subway has a direction radio,
     bus has the locked-until-keyed flow, Citi Bike has the live
     dock/bike snapshot); this is the biggest chunk of new code
  5. A `make_<slug>_tools(client)` factory under
     [`jasper/tools/`](jasper/tools/) — what the provider's `make_tools`
     lazy-imports
  6. A `<Slug>Client` runtime class (mirror `jasper/subway.py`,
     `jasper/bus.py`, `jasper/citibike.py`) that `build_client`
     constructs. If it owns a connection pool, give it `aclose()` — the
     managed `ActiveTransit` result `active_transit` returns closes every
     built transit client on shutdown (duck-typed), so a pool is reclaimed
     with no daemon edit
  7. The `keys=(...)` bash array in `migrate_transit_config` in
     [`deploy/install.sh`](deploy/install.sh) — duplicates
     `transit.all_env_keys()` because install.sh runs before Python
     is available (`JASPER_TRANSIT_CITIES` is a pack-level toggle, not a
     provider env key, so it is NOT in that array — `migrate_transit_config`
     moves an operator-set value out of `jasper.env` AND seeds `nyc` for
     existing NYC households, both in their own dedicated step)

See `nyc_subway.py` (keyless, CSV-backed) and `nyc_bus.py`
(credentialed, REST-backed) for the two provider shapes. The
registry's own module docstring at `jasper/transit/__init__.py`
enumerates all of these in more detail.

**Refreshing subway data.** The bundled CSV at
[`jasper/data/mta_stations.csv`](jasper/data/mta_stations.csv) is
regenerated by `bash scripts/refresh-mta-stations.sh` from
data.ny.gov dataset `39hk-dx4f`. The refresh **preserves
hand-curated direction labels** when the existing CSV has them —
MTA's official labels are sometimes bland ("Southbound") where a
destination-anchored label ("Coney Island") makes voice answers
materially better. Edit by hand to override; the next refresh
keeps your edits.

**Transit nudge.** When the daemon boots with neither subway,
bus, nor Citi Bike configured, `_build_system_instruction` in
[`jasper/voice/prompt.py`](jasper/voice/prompt.py) appends a
conditional instruction redirecting transit questions to
`jts.local/transit`. Conditional ("if the user asks about the
next train, say X") not absolute ("never answer transit
questions"), per the provider-prompt-guide rule. Partial
configurations (e.g. subway set, Citi Bike not) don't fire the
nudge — the registered tools answer what's configured; the
absent tools just aren't visible to the model.

---

## Home Assistant integration — read first

The speaker delegates smart-home control ("turn on the bedroom
lights", "good night", "bedroom medium" → custom automation) to
whatever Home Assistant the household has on the LAN. JTS is a
relay: captures the utterance, hands it to HA's conversation
pipeline, speaks back what HA returns. HA owns NLU, entity
resolution, sentence triggers, automation dispatch — everything
that makes household-specific phrases work. Full architecture in
[`docs/HANDOFF-homeassistant.md`](docs/HANDOFF-homeassistant.md).

### Configure

Wizard at `http://jts.local/ha/`. Three states (mirrors
`/spotify/`'s shape):

1. No URL → "Find Home Assistant" mDNS scan or manual URL entry
2. URL set, no token → paste a Long-Lived Access Token from
   `<HA URL>/profile/security`
3. Connected → status card + test button + agent picker + disconnect

Persists to `/var/lib/jasper/home_assistant.env`:

```sh
JASPER_HA_URL=http://homeassistant.local:8123
JASPER_HA_TOKEN=eyJ0eXAiOi…
JASPER_HA_AGENT_ID=         # optional, empty = HA's default agent
JASPER_HA_VERIFY_SSL=0      # optional, only written when user accepts
                            # a self-signed cert in state 2. Wizard
                            # renders the checkbox only for https://
                            # URLs.
```

Both URL and token must be set for `home_assistant` to register as a
voice tool. When either is missing, the tool isn't visible to the
model and smart-home requests get answered conversationally
("smart-home isn't set up — visit jts.local/ha").

### Why the REST conversation API, not MCP

Verified against HA core source: HA's MCP server cannot trigger
automations (no `automation.trigger` tool in its catalogue;
`HassTurnOn` against an `automation.*` entity *enables* the
automation rather than running it). Sentence triggers (the
`trigger: conversation` automation pattern — HA's documented
mechanism for household phrases) only fire through
`default_agent._async_handle_message`, never through MCP. So MCP
loses two critical surfaces a household has set up. See
[`docs/HANDOFF-homeassistant.md`](docs/HANDOFF-homeassistant.md)
"Why the conversation API, not MCP" for the full case.

### Debug

```sh
# Tool registration at daemon startup:
journalctl -u jasper-voice | grep "home_assistant:"
# →  home_assistant: enabled url=http://... agent_id=(default)
# OR home_assistant: disabled (set JASPER_HA_URL + JASPER_HA_TOKEN...)

# Per-call structured events — six outcome buckets
# (ok / network / timeout / auth / agent_error / intent_miss / parse_error):
journalctl -u jasper-voice | grep "event=ha\.call"

# Live state from jasper-control:
curl -s http://jts.local:8780/state | jq .home_assistant

# Diagnostic check (skip-if-not-configured):
sudo /opt/jasper/.venv/bin/jasper-doctor | grep "Home Assistant"

# Dashboard card on http://jts.local/system/ shows:
#   ✓ Connected to <name> (<version>) — green
#   ✗ Unreachable + error detail — red
#   Not configured
```

### Common gotchas

- **`conversation_id` TTL ≈ 5 min idle**, observed not contractual.
  HAClient caches with a 4-min safety margin; HA may rotate the ID
  silently on each response, we treat what comes back as canonical.
  After daemon restart the conversation context resets — fine, HA
  mints a fresh ID.
- **`agent_id` parameter is undocumented in HA's REST API surface**
  but functional. A future HA schema-tightening could break us.
  Regression test asserts the field is accepted.
- **Footgun:** POST to `/api/conversation/process`, NOT
  `/api/services/conversation/process`. The latter returns no
  response body (HA core issues #93754, #104122 — live in 2026).
- **HA's `response_type=error` returns HTTP 200**. Caller must
  inspect the body, not just the status code. Covered by
  `HAClient._parse` — outcome bucket is `intent_miss`.
- **`no_valid_targets` is NOT a hard error**. In multi-satellite
  homes, another device may have answered the same utterance. HA's
  speech text is still useful to surface; we speak it.
- **LLM-backed HA agents add 1-3 s latency**. If the household has
  HA's default conversation agent set to OpenAI Conversation /
  Anthropic / Google, every smart-home command pays for two LLM
  hops (ours + theirs). To bypass: set `JASPER_HA_AGENT_ID=
  conversation.home_assistant` in the wizard's Advanced disclosure
  → JTS routes to HA's rule-based agent directly.

### Switch / disconnect from the laptop

No `switch-home-assistant.sh` helper yet (planned v1.1+). Manual
paths for now:

```sh
# Re-test without opening the wizard:
ssh pi@jts.local 'sudo /opt/jasper/.venv/bin/jasper-doctor' | grep "Home Assistant"

# Disable (preserves recent URLs for one-tap reconnect):
ssh pi@jts.local 'sudo rm -f /var/lib/jasper/home_assistant.env \
  && sudo systemctl restart jasper-voice'
```

---

## Mic mute — persists across restarts

User-driven mic mute is a privacy promise. When on, the wake loop
drains mic frames without feeding wake detection or any session.
State persists to `/var/lib/jasper/mic_mute.env`
(`JASPER_MIC_MUTED=0|1`, mode 0644, atomic tempfile+rename) so it
survives every daemon restart — deploys, web-wizard saves, watchdog
timeouts, AEC reconciler events, full Pi reboots. Before
[PR #119](https://github.com/jaspercurry/JTS/pull/119) the flag was
in-memory only and silently un-muted on any of those events.

Two ways to toggle (no voice tool — see footnote):

- **Dashboard** — `http://jts.local/system/`, mic chip on the top
  card. Reads the persisted state via `/state`, so it reflects the
  truth immediately after a restart.
- **HTTP** on `jasper-control` (port 8780):

  ```sh
  curl -s http://jts.local:8780/mic                          # read
  curl -s -X POST http://jts.local:8780/mic/mute \
       -H 'Content-Type: application/json' \
       -d '{"muted":true}'                                   # mute
  curl -s -X POST http://jts.local:8780/mic/mute \
       -H 'Content-Type: application/json' \
       -d '{"muted":false}'                                  # unmute
  ```

**Fail-safe direction**: a missing, unreadable, or malformed
`mic_mute.env` resolves to **unmuted** at boot. Better the speaker
respond than be silently deaf because of one bad byte on disk.

**On boot when restored as muted**, jasper-voice logs a single
`mic mute: restored from /var/lib/jasper/mic_mute.env (mic is muted
at startup)` line. If wake stops responding after a deploy/reboot,
check this first.

**No voice tool by design.** "Hey Jarvis, mute the mic" would
create a one-way trap — once muted, wake detection is off, so the
user couldn't say "Hey Jarvis, unmute" to get back. Toggle via the
dashboard or HTTP endpoint, never via the assistant itself.

---

## Gemini model switching — read first

**Preferred model: `gemini-3.1-flash-live-preview`** (latest Live
API model). Do NOT use the plain `gemini-2.5-flash` (it's not a
Live model — `Live API: Not supported` per
https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash).

**Acceptable fallback: `gemini-2.5-flash-native-audio-preview-12-2025`**
— Google's docs explicitly position 3.1 Flash Live as the
*successor* of 2.5 native-audio (see "Migrating from Gemini 2.5
Flash Live" section at
https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview).
Same Live API, same `client.aio.live.connect()` SDK path, same
prebuilt voice catalog, same `send_realtime_input(audio=Blob)`
shape. Use it when 3.1 Live Preview is silently failing for the
project (a real Google-side condition we've hit — server accepts
the WebSocket, accepts audio, sends nothing back; not surfaced
as an error in the SDK).

**Switch command** (laptop-side wrapper, SSHs to the Pi):

```sh
bash scripts/switch-gemini-model.sh        # show current model
bash scripts/switch-gemini-model.sh 3.1    # → gemini-3.1-flash-live-preview
bash scripts/switch-gemini-model.sh 2.5    # → gemini-2.5-flash-native-audio-preview-12-2025
```

The script flips `JASPER_GEMINI_MODEL` in `/etc/jasper/jasper.env`
and restarts `jasper-voice`. No code changes needed because the
daemon treats the model as opaque-string config.

**Symptoms that mean "Gemini Live is silently broken, switch to 2.5"**:

- Sessions repeatedly end with `0 input_tokens / 0 output_tokens`
  AND the daemon's `SILENT FAILURE: sent N bytes... received 0
  chunks back` warning is firing.
- Direct probe (text turn via `send_client_content`) returns no
  responses within 15s and no exception.
- Same-key non-Live `client.models.generate_content(...)` works
  (rules out auth/key issue).

When 3.1 Live unsticks, run `switch-gemini-model.sh 3.1` to flip
back.

---

## librespot — one-time OAuth claim for cold-start voice

`spotify_play "X"` from silence (no AirPlay carrying Spotify) needs
the Pi's librespot to be authenticated to a Spotify account, because
the voice tool calls `start_playback(device=JTS)` via the Web API and
JTS only appears in an account's `sp.devices()` list once that
account has logged in to it.

Two ways to authenticate librespot:

1. **Phone tap** — open Spotify on any device on the LAN, tap the
   device picker, select JTS once. The credential is then cached at
   `/var/cache/librespot` (via `--system-cache` in the systemd unit)
   and survives librespot restarts.
2. **Laptop-side OAuth script** — no phone needed:

   ```sh
   bash scripts/claim-librespot.sh
   ```

   SSH-tunnels librespot's hardcoded `127.0.0.1:8091` OAuth callback
   port to your laptop, runs `librespot --enable-oauth`, opens the
   Spotify auth page in your browser, writes credentials to the same
   `--system-cache` path. Same end state as the phone tap, just no
   phone involved.

Either path is one-time per librespot identity. After that, voice
cold-starts work indefinitely until the cache is cleared.

**Multi-user caveat**: librespot can only be logged in as one user
at a time. The household member whose account is currently cached
is the one voice cold-starts will play through. Other members can
still use their phone's Spotify Connect to claim JTS ad-hoc — that
overwrites the cache for that session, and they can also claim it
back when they want voice to play through their account. Per-user
librespot instances ("JTS-Jasper" / "JTS-Brittany") OAuth-locked
to each account is the deeper fix; deferred until the friction
actually bites.

---

## AEC bridge — input profile and reconciler

Input selection is **profile-first** and managed by the reconciler.
`/var/lib/jasper/aec_mode.env` carries
`JASPER_AUDIO_INPUT_PROFILE`, plus rollback-safe legacy
`JASPER_AEC_MODE` / `JASPER_WAKE_LEG_*` keys. Fresh installs seed:

```
JASPER_AUDIO_INPUT_PROFILE=auto
JASPER_AEC_MODE=auto
JASPER_WAKE_LEG_RAW=1
JASPER_WAKE_LEG_DTLN=0
JASPER_WAKE_LEG_CHIP_AEC=0
```

`auto` resolves to the XVF3800 chip-AEC profile when the configured
AEC mic is present with 6-channel firmware. In that profile the
bridge forwards the chip-AEC beam to `:9876`, emits fixed
150°/210° chip beams on `:9887`/`:9888`, and does **not** stack
software raw/DTLN wake legs. When chip-AEC is unavailable, `auto`
falls back to `xvf_software_aec3` (AEC3 on `:9876`, raw wake
fallback on `:9877`, DTLN off). `direct_mic` disables the bridge.
`custom` preserves the low-level leg booleans exactly for corpus
tests and nonstandard hardware.

### Wake-detection legs — custom sub-toggles

The `/wake/` page exposes profile choices first. Its advanced layer
toggles (`raw`, `dtln`, `chip_aec`, and the AEC master) are custom
controls: changing one stamps `JASPER_AUDIO_INPUT_PROFILE=custom`.
In custom software-AEC mode the bridge can fan out AEC3 (`:9876`),
raw chip-direct (`:9877`), and DTLN neural AEC (`:9878`). In custom
chip-AEC mode it emits the chip beams and clears raw/DTLN because the
single chip cannot do both modes at once.

> **Corpus-only 4th UDP leg (`:9879`) since PR #323.** The bridge
> also always emits chip channel 2 (truly raw — no chip OR software
> DSP) on `:9879`. **Not consumed by wake detection** — only by the
> wake-corpus recorder when an operator opts into "Also capture raw
> mic 0" in the http://jts.local/wake-corpus/ Begin-a-session form.
> Always-on cost ~0.25% of one core. Used for mic-agnostic wake-
> word training data — see [docs/HANDOFF-wake-training-experiment.md](docs/HANDOFF-wake-training-experiment.md)
> Phase 0b. Do NOT add it as a 4th wake-detection leg without
> first training a `raw0`-specific model — the unconditioned raw
> signal is acoustically different from what the production legs'
> models were trained on, and would degrade OR-gate precision.

The reconciler ([`deploy/bin/jasper-aec-reconcile`](deploy/bin/jasper-aec-reconcile))
is the single writer of the underlying env vars the daemons read.
It maps the selected profile / custom booleans to
`JASPER_MIC_DEVICE_RAW`, `JASPER_MIC_DEVICE_DTLN`,
`JASPER_AEC_DTLN_ENABLED`, `JASPER_MIC_DEVICE_CHIP_AEC_150`,
`JASPER_MIC_DEVICE_CHIP_AEC_210`, `JASPER_AEC_CHIP_AEC_ENABLED`, and
the outputd USB-IN reference producer, then restarts the bridge and
voice as needed. The reconciler clears underlying vars when a profile
does not use them so stale UDP devices never leave voice listening on
ports nobody feeds.

**Wake-word sensitivity slider** — lives on the same /wake/ Wake
detection card as the model picker and the leg toggles (they share a
restart cycle). The slider writes `JASPER_WAKE_THRESHOLD` into
`/var/lib/jasper/wake_model.env` (same file as the wake-word model
picker, which preserves the threshold on model save). Edit point is
`_write_wake_threshold` in
[`jasper/control/server.py`](jasper/control/server.py).

**HTTP API** ([`jasper/control/server.py`](jasper/control/server.py)):
- `GET /aec` → profile selection, resolved/active audio profile,
  bridge state, effective legs, raw legacy intent, threshold, mic
  status, and validation summary.
- `POST /aec/profile` body `{profile: "auto"|"xvf_chip_aec"|"xvf_software_aec3"|"direct_mic"}` → set canonical profile.
- `POST /aec/toggle` → custom AEC master flip
- `POST /aec/leg` body `{leg: "raw"|"dtln"|"chip_aec", enabled: bool}` → flip one custom leg
- `POST /aec/threshold` body `{threshold: float}` (0.0..1.0) → set sensitivity

**Migration on upgrade**: `migrate_wake_legs_config` in
[`deploy/install.sh`](deploy/install.sh) moves any hand-set
`JASPER_MIC_DEVICE_RAW` / `_DTLN` / `JASPER_AEC_DTLN_ENABLED` /
chip-AEC device vars from `/etc/jasper/jasper.env` into
`aec_mode.env`, infers the nearest profile (`xvf_chip_aec`,
`xvf_software_aec3`, or `custom`), then strips the underlying vars.
Existing pre-profile `aec_mode.env` files keep behavior: software
defaults infer `xvf_software_aec3`, chip-AEC booleans infer
`xvf_chip_aec`, unusual leg mixes infer `custom`. Only a truly fresh
file seeds `auto`.

**Restart blast radius** per profile/toggle:
- Profile change → reconciler decides bridge + voice + outputd restart
  needs; chip-AEC profile changes can restart outputd for USB-IN
  reference fanout.
- AEC master flip → custom bridge + voice restart
- DTLN flip → bridge + voice restart (DTLN model loads at startup
  in the bridge; voice opens `:9878` socket at startup)
- RAW flip → voice restart only (bridge already emits to `:9877`
  unconditionally — see [`aec_bridge.py`](jasper/cli/aec_bridge.py)
  around `OUT_PORT_RAW`)
- Sensitivity slider → voice restart only (openWakeWord reads
  threshold at startup; bridge unaffected)

In all cases the reconciler is the synchronization point — the
HTTP endpoints write the config file and kick
`jasper-aec-reconcile.service`. Restart latency: ~3-5 s bridge,
~10-15 s voice, run in parallel.

### Engine architecture — do not re-architect

 README's
"Acoustic echo cancellation" section covers the engines: XVF3800
chip-AEC for the recommended profile, and WebRTC AEC3 via the
`jasper_aec3` pybind11 binding as fallback/custom software profile
(BEST_A tuning since 2026-05, with prior REF_GAIN/MIC_GAIN
measurements showing −15 to −18 dB on music). The WebRTC path costs
~+85 MB Pss and ~+3% of one Pi 5 core (per the resource table at the
bottom of the README). The full investigation is in
[`docs/HANDOFF-aec.md`](docs/HANDOFF-aec.md); the chip-side
canonical reference (firmware variants, mixer state, failure
modes, diagnostic cookbook) is
[`docs/HANDOFF-xvf3800.md`](docs/HANDOFF-xvf3800.md).

**Architecture is fixed; swap the engine/profile, not the topology.** When
working on software AEC, default to engine-internal changes inside
[`jasper/cli/aec_bridge.py`](jasper/cli/aec_bridge.py) and the
`jasper_aec3` binding. Do **not** propose PipeWire
`module-echo-cancel`, replacing snd-aloop with PipeWire fanout,
dual-USB-sink hardware-AEC retry, or custom XVF firmware — the
current architecture (outputd/reference fanout or dsnoop tap → selected
AEC profile → UDP → voice daemon) is the result of a deliberate
decision rejecting those paths (rationale in
[`docs/HANDOFF-aec.md`](docs/HANDOFF-aec.md)).
Targeted single-knob OS-layer fixes (a specific ALSA
`rate_converter` setting, a kernel module parameter) ARE
acceptable when measurement has localized the root cause to that
layer — what's rejected is speculative re-architecture.

**One scoped carve-out: chip-AEC with USB-IN reference (Option D) —
now being promoted to a wake leg.** The "no chip AEC" rejection above
was for the variants we *tested* — none of which fed music to the
chip's USB-IN as the AEC reference. That specific variant (mono music →
chip USB-IN → chip HW AEC → mic via chip USB-OUT, with mic and reference
clocks sharing the chip's USB Adaptive Mode PLL) was measured in a
2026-05-29 lab pass and works. Its infrastructure lives at
[`docs/CHIP-AEC-EXPERIMENT.md`](docs/CHIP-AEC-EXPERIMENT.md) +
`scripts/chip-aec-*.sh` + `jasper/chip_aec_experiment.py`. **The chip's
fixed 150°/210° ASR beams (`chip_aec_150`/`chip_aec_210`) are now the
recommended 6-channel XVF3800 production profile and remain
hardware-conditional scored wake legs** — see
[`docs/HANDOFF-mic-fusion-architecture.md`](docs/HANDOFF-mic-fusion-architecture.md)
§2.4 and the `JASPER_AUDIO_INPUT_PROFILE` policy above. The leg
registry/config/telemetry, production `jasper-aec-init` chip profile,
bridge Option-A `:9876` repoint, and profile selector have landed.
The carve-out
stays **narrow**: it does not re-open PipeWire, dual-USB-sink, or custom
firmware, and the "architecture is fixed; swap the engine, not the
topology" rule still binds everywhere else — the chip-AEC profile rides
the existing reference→engine→UDP→voice topology (it adds scored beam legs and
forwards the chip beam into the existing `:9876` carrier; it does not
re-architect the bridge).

**Three layered bridge bugs were fixed on 2026-05-19.** Together
they had been silently corrupting AEC's reference signal since
the bridge shipped: (1) ALSA's linear resampler dropping HF
content in the 44.1→48 plug; (2) `_aec_loop` falling back to
`silence` when `ref_q` was empty (50 % of frames); (3) drain-
newest discarding the older frame in each ALSA burst, producing
byte-identical duplicate frames. All fixed in PRs #150, #154,
#157. Wake-rate baseline data from before 2026-05-19 is invalid
for evaluating AEC's contribution (the AEC-OFF / chip-direct
legs remain valid). See [docs/HANDOFF-aec.md](docs/HANDOFF-aec.md)
"Bridge ref starvation bug — fixed (2026-05-19)" for the full
diagnosis. The verification script
`scripts/verify-ref-no-silence-bug.sh` confirms the fixes are
active on any deployed build.

**NS=low + AGC1 — 2026-05-20 production tuning.** Post-ref-fix
wake-rate sweep surfaced two more knobs that moved the needle.
Both are now production defaults:
- `JASPER_AEC_NS_LEVEL=low` (was `moderate`) — less aggressive
  noise suppression. The wake model relies on HF speech
  consonants and aggressive NS strips them. Sweep on 2026-05-20:
  NS=low 5/20 vs prev NS=moderate 4/20 in the same data.
- `JASPER_AEC_AGC1_ENABLED=1` — WebRTC AGC1 in `kAdaptiveDigital`
  mode replaces static `MIC_GAIN_DB` for level normalization.
  Same wake rate as static +12 dB, but uniform output across
  utterances (fixes "some Jarvises overblown, some too quiet").

Knobs (env-controllable, `/etc/jasper/jasper.env`):
- `JASPER_AEC_NS_ENABLED` (default 1)
- `JASPER_AEC_NS_LEVEL` (default `low`; one of `low / moderate /
  high / very_high` — Trixie's libwebrtc doesn't expose
  `kVeryLow`)
- `JASPER_AEC_AGC1_ENABLED` (default 0 in binding; production
  has 1 in env)
- `JASPER_AEC_AGC1_TARGET_DBFS` (default 9)
- `JASPER_AEC_AGC1_MAX_GAIN_DB` (default 18)
- `JASPER_AEC_AGC2` documented as no-op on this libwebrtc;
  recommended off

Bridge startup log confirms the live config:
`engine=aec3 ns=on/low agc1=on(target=9,max=18dB) agc2=off ...`

**Prerequisite**: the XVF chip must be on the 6-channel firmware
variant — the bridge opens the 6-channel USB capture endpoint and
reads the ASR beam on channel 1. The known-good
filename + repo hash are tracked in
[`jasper/mics/xvf3800.py`](jasper/mics/xvf3800.py); as of
2026-05-15 that's `respeaker_xvf3800_usb_dfu_firmware_6chl_v2.0.8.bin`,
the only 6-channel variant in upstream `master`. Check the
[upstream firmware directory](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/xmos_firmwares/usb)
before flashing in case a newer one has shipped. If unsure
whether the chip is currently on 6-ch, check with:

```sh
# Pin to the Capture: section — Playback (Channels: 2) comes first
# in the file, so a naive `grep Channels:` returns the wrong value.
awk '/^Capture:/{c=1} c && /Channels:/{print; exit}' /proc/asound/Array/stream0
# Expect "Channels: 6"
```

DFU flash procedure is in
[`BRINGUP.md` "XVF firmware: switch to 6-channel variant via DFU"](BRINGUP.md#xvf-firmware-switch-to-6-channel-variant-via-dfu).
The reconciler also self-heals the post-flash ALSA mixer mute trap
(2-ch → 6-ch firmware can leave kernel-side ch2-5 muted across
reboot via `alsactl restore`); `jasper-doctor` flags drift under
"XVF mixer state".

To enable on the Pi (assumes 6-ch firmware already flashed):

```sh
printf 'JASPER_AEC_MODE=auto\n' | sudo tee /var/lib/jasper/aec_mode.env
sudo systemctl start jasper-aec-reconcile
```

`install.sh` enables and runs `jasper-aec-reconcile` automatically.
The reconciler is the source of truth for AEC mode: in `auto`, it
selects `JASPER_MIC_DEVICE=udp:9876` only when the configured AEC mic
(`JASPER_AEC_MIC_DEVICE`, default `Array`) is present with 6-channel
firmware. If the Array is absent after a previous AEC-enabled boot, it
clears stale UDP back to a direct mic candidate and stops voice rather
than letting it watchdog-loop on an unfed socket. Future direct mics can
be added to `JASPER_MIC_DEVICE_CANDIDATES` without changing this logic.

The bridge→voice transport is UDP localhost (`udp:9876`) since
May 2026; the prior snd-aloop `LoopbackAEC` topology was retired
for resilience reasons — see
[`docs/HANDOFF-resilience.md`](docs/HANDOFF-resilience.md).

To disable:

```sh
printf 'JASPER_AEC_MODE=disabled\n' | sudo tee /var/lib/jasper/aec_mode.env
sudo systemctl start jasper-aec-reconcile
```

Verify with `sudo /opt/jasper/.venv/bin/jasper-doctor` either way.

These commands are duplicated in
[`BRINGUP.md` "Optional: Software AEC bridge"](BRINGUP.md#optional-software-aec-bridge)
— keep both in sync.

The chip control library (`jasper.xvf.xvf_host`) is useful for
diagnostics regardless of bridge state. **Never call
`SAVE_CONFIGURATION`** — brick hazard on certain firmware
versions (respeaker repo issue #8).

---

## Wake-event telemetry — capture + labeling

Every wake-word fire (and the funnel that follows) lands in a
SQLite DB + per-event audio clips at
`/var/lib/jasper/wake-events/`. Used for: knowing your actual
wake rate, which AEC leg is firing more, building a labeled
corpus for future model training.

Full design, schema, queries:
[`docs/HANDOFF-wake-telemetry.md`](docs/HANDOFF-wake-telemetry.md).

### Enable multi-leg wake OR-gate

Normal operator path: use `http://jts.local/wake/`. Pick a profile
(`auto`, `xvf_chip_aec`, `xvf_software_aec3`, `direct_mic`) first;
only use the advanced `raw`, `dtln`, and `chip_aec` layer switches for
custom/corpus work. The page stamps `JASPER_AUDIO_INPUT_PROFILE=custom`
when a layer switch changes, then calls jasper-control.

Automation path: call `POST /aec/leg` on jasper-control, for example:

```sh
curl -sS -X POST http://127.0.0.1:8780/aec/leg \
  -H 'Content-Type: application/json' \
  -d '{"leg":"dtln","enabled":true}'
```

Accepted `leg` values are `raw`, `dtln`, and `chip_aec`. The handler
writes the intent keys (`JASPER_WAKE_LEG_RAW`,
`JASPER_WAKE_LEG_DTLN`, `JASPER_WAKE_LEG_CHIP_AEC`) into
`/var/lib/jasper/aec_mode.env` and restarts
`jasper-aec-reconcile.service`. The reconciler is the single writer of
the daemon-facing devices (`JASPER_MIC_DEVICE_RAW`,
`JASPER_MIC_DEVICE_DTLN`, `JASPER_MIC_DEVICE_CHIP_AEC_150`,
`JASPER_MIC_DEVICE_CHIP_AEC_210`) and bridge flags. Do **not** hand-
append those lower-level vars to `/etc/jasper/jasper.env`; install
migrations strip them because stale UDP devices leave voice listening
on ports nobody feeds.

Verify through `GET /aec`, `/state.voice.wake_legs`, or
`journalctl -u jasper-voice | grep UdpMicCapture`. The bridge/reconciler
truth is in `deploy/bin/jasper-aec-reconcile` (`write_leg_env`) and
`jasper/control/server.py` (`_post_aec_leg`).

### Pull the corpus to laptop for review

```sh
bash scripts/fetch-wake-events.sh        # pops Finder open on macOS
```

Lands as `./wake-events/<UTC-timestamp>/`:
- `wake-events.sqlite3` — consistent snapshot via `.backup`
  (jasper-voice keeps writing; the snapshot is safe to read)
- `<event_id>.aec-on.wav` + `<event_id>.aec-off.wav` (+ `.aec-dtln.wav`
  on triple-stream events) — 6 s windows (4 s pre + 2 s post wake fire)
- `index.csv` — newest-first metadata table (open in Numbers /
  Excel / Sheets); includes per-leg peak scores, peak offsets,
  RMS levels, music context, bridge config, funnel outcome
- `index.tsv` — same content as TSV (grep-friendly)
- `wake-events/latest` symlink updated each run

Skip the Finder pop-up with `NO_OPEN=1 bash scripts/fetch-wake-events.sh`.

### Sanity-check the corpus

```sh
bash scripts/audit-wake-events.sh
```

Runs three checks on `wake-events/latest`:

1. WAV integrity — format, duration, near-silent detection
2. Per-event AEC ON vs AEC OFF parity — duration match, RMS
   comparison, cross-leg time-alignment via speech-band xcorr
   (typical ≈+14 ms reflects AEC3 processing latency)
3. DB column-by-column populated count — catches "field never
   written" bugs like the AEC OFF capture-ring fill bug shipped
   in the initial dual-stream integration

Re-run after every fetch; takes ~2 s.

### Label an event

```sh
sqlite3 wake-events/latest/wake-events.sqlite3 \
  "UPDATE wake_events SET label='real_attempt' WHERE event_id='...'"
```

The `label` column is free-text — these are conventions, not an
enforced enum. Empty by default; fill in as you listen during the
weekly review. `label_notes` for longer free-form commentary.

**Real attempts** (the wake was correct):
- `real_attempt` — clear "Jarvis" / "Hey Jarvis" utterance
- `unclear_attempt` — utterance present but quiet / mumbled /
  partially obscured by music or background noise. Wake firing
  was still arguably correct, but worth a separate bucket so
  threshold-tuning analysis doesn't treat marginal hits as
  unambiguous positives.

**False positives** (the wake fired but no real attempt) —
these are the ones worth categorizing so we can tell what's
actually triggering them and whether we can suppress that
class of trigger:
- `tts_bleed` — JTS's own TTS output bled into the mic and
  fired wake. Most common when AEC OFF leg is on and TTS is
  loud. Mitigation lever: keep AEC ON leg dominant via
  threshold differential.
- `music_vocals` — singing in music sounded like "Jarvis" /
  "Hey Jarvis". The classic openWakeWord music false-fire
  mode. Mitigation lever: music-aware threshold, or custom
  wake-word model trained against this household's music
  taste.
- `music_other` — non-vocal music transient (drum hit, hi-hat,
  vinyl pop) crossed threshold. Rarer than music_vocals.
  Mitigation lever: deeper AEC tuning or DTLN-leg-only on
  music context.
- `tv` — TV / podcast / streamed-show audio triggered wake.
  Distinguish from music_* because TV usually has speech
  energy (whereas music_vocals are sung). Different mitigation
  paths.
- `ambient` — environmental noise / conversation in the room /
  appliances. Often the hardest to suppress because it's
  unpredictable.

**Other**:
- `unclear` — listened twice, still can't tell what fired it.
  Keep separately so it doesn't pollute the real_attempt vs
  FP ratio.
- `mute_or_correction` — wake fired during mic-mute or
  correction window (outcome=`late_cancel`). Not a quality
  signal, just a session-flow artefact.

**Voice-flagged** (the user said "flag that" mid-use):
- `voice_flagged` — written by the `flag_recent_issue` voice
  tool. The user's complaint is in `label_notes` as
  `{iso_ts}|{reason}`. These are the events to triage first
  during weekly review — they're labeled by the household member
  who actually noticed the misbehavior in real time, so they
  cover failure modes that pure-corpus mining might miss.
- `flag_action` — the wake event of the "flag that" utterance
  itself (the one whose tool call wrote the `voice_flagged`
  label on the prior event). Always filter these out of
  "real interaction" rollups: `WHERE label != 'flag_action'`
  or equivalent. The tool keeps these distinct from real
  interactions on purpose.

Find voice-flagged events to review:

```sh
sqlite3 wake-events/latest/wake-events.sqlite3 "
SELECT event_id, ts_utc, label_notes, audio_on_path
FROM wake_events WHERE label='voice_flagged'
ORDER BY ts_utc DESC"
```

The flag-tool surface lives at [`jasper/tools/diagnostic.py`](jasper/tools/diagnostic.py);
the SQLite layer is `WakeEventStore.record_flag` in
[`jasper/wake_events.py`](jasper/wake_events.py). Trigger phrases
the LLM is taught to act on (in the tool's docstring): "flag that",
"you cut me off", "you fired incorrectly", "didn't let me finish",
"that was wrong", and close paraphrases.

### Quick funnel query

```sh
sqlite3 wake-events/latest/wake-events.sqlite3 "
SELECT date(ts_utc) day, COUNT(*) wakes,
       SUM(ts_turn_opened IS NOT NULL) opened,
       SUM(ts_speech_detected IS NOT NULL) had_speech,
       SUM(ts_turn_complete IS NOT NULL) completed
FROM wake_events WHERE trigger_kind LIKE 'fire%' GROUP BY day"
```

More queries (per-leg fire breakdown, false-positive proxy, etc.)
in the HANDOFF doc's "Useful queries" section.

### Weekly review of which legs are firing

Triple-stream-aware analyzer that summarizes the corpus by leg pattern:

```sh
bash scripts/analyze-three-leg.sh                  # analyzes wake-events/latest
bash scripts/analyze-three-leg.sh --top 10         # 10 events per category
```

Five sections in the output: Venn-style fire breakdown across the
canonical patterns (`'on'`, `'off'`, `'dtln'`, `'dtln,off'`,
`'dtln,off,on'`, ...) with mean per-leg score per pattern; per-leg
score distribution (P10/P50/P90/Max — catches a silently-dead leg);
solo-save counts (★ marks "Only DTLN fired", the headline metric
for evaluating the third leg's distinct value); listening playlist
with `afplay` paths; funnel by pattern (fired → turn → speech →
tool — flags a pattern that never reaches speech as a false-fire
indicator). Pure-stdlib Python, ~instant.

### Retention + privacy

- WAVs: 1 GB ring buffer by default (~5-7 weeks at 30-50 events/day
  with triple-stream's 3 WAVs per event), oldest-first deletion.
  Tunable via `JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES`. Pre-triple-stream
  the default was 500 MB.
- DB rows kept forever. When audio rolls off, the row's
  `audio_*_path` becomes the literal string `'rolled_off'` (not
  NULL — preserves the historical fact that audio existed).
- Mute mic privacy preserved: when `JASPER_MIC_MUTED=1`, the
  wake-event capture rings stop filling — nothing recorded. The
  wake-corpus recorder honors the same switch: it refuses to start
  while muted and stops (labeling the clip `mute_stopped`) if mute
  flips mid-recording.

### Reset the corpus to start a clean week of data

Run before a fresh data-collection window (e.g. immediately after
deploying a tuning change you want to evaluate against a clean
baseline). The script archives the current corpus rather than
deleting it, so nothing is lost:

```sh
bash scripts/reset-wake-events.sh             # archive + reset
DRY_RUN=1 bash scripts/reset-wake-events.sh   # print the plan only
```

What it does on the Pi: stops `jasper-voice`, moves
`/var/lib/jasper/wake-events/` to
`/var/lib/jasper/wake-events-archive-<UTC-timestamp>/`, recreates
the empty live dir with correct permissions, restarts
`jasper-voice` (schema migration runs on `open()` → fresh DB),
logs `event=wake_events.reset` to the journal for audit. Archives
stay on the Pi — `fetch-wake-events.sh` only pulls the live
corpus; to pull an archive, rsync explicitly from the archive path.

### Architecture in one paragraph

The AEC bridge leg vocabulary lives in
[`jasper/wake_legs.py`](jasper/wake_legs.py), with emit defaults in
[`jasper/cli/aec_bridge.py`](jasper/cli/aec_bridge.py). Production
wake inputs are `on` (`:9876`, software AEC3 or the chip-AEC primary
carrier), `off` (`:9877`, chip-direct), `dtln` (`:9878`), and the
hardware-AEC beam pair `chip_aec_150` / `chip_aec_210`
(`:9887` / `:9888`). Corpus-only legs include `raw0` (`:9879`),
`ref` (`:9880`), `usb_raw` (`:9881`), `usb_webrtc` (`:9882`),
`usb_dtln` (`:9883`), `xvf_raw0_webrtc_aec3` (`:9889`), and
`xvf_raw0_dtln` (`:9890`), plus parametric AEC3 sweep variants from
`jasper.aec_sweep`; outputd's final-speaker reference is an input to
the bridge on `:9891`, not a wake leg. jasper-voice opens the
configured `wake_input=True` legs, gives each its own
`WakeWordDetector`, and OR-gates fires with the shared refractory
window (`WAKE_REFRACTORY_SEC` in
[`jasper/voice_daemon.py`](jasper/voice_daemon.py)). `WakeEventStore`
([`jasper/wake_events.py`](jasper/wake_events.py)) writes SQLite rows
at wake-fire + each funnel transition, then snapshots active leg rings
into one WAV per leg after the post-fire window. The schema has
per-leg `peak_score_*`, `audio_*_path`, `mic_rms_dbfs_*` columns and a
`fired_legs` CSV recording which legs crossed threshold.
Telemetry is **fail-soft** everywhere — store failures log at WARN
and never block wake / session paths.

---

## shairport-sync AP2 wedge — auto-recovers

shairport-sync's AirPlay 2 control plane occasionally wedges with the
process alive but unable to accept new SETUPs (Pi shows in the picker
but "cannot connect"). Closest upstream report:
[shairport-sync#2024](https://github.com/mikebrady/shairport-sync/issues/2024).
No upstream code fix.

The Tier 3 supervisor at
[`jasper/control/shairport_supervisor.py`](jasper/control/shairport_supervisor.py)
probes RTSP `OPTIONS *` on `127.0.0.1:7000` every 30 s; after 3
consecutive failures, gated on no active session, it restarts
shairport-sync + nqptp. Detection latency ~90 s. The gate ensures
live sessions aren't disrupted; a 10-minute rate limit prevents
restart storms.

Manual fix (still works, faster than the 90 s window):

```sh
bash scripts/airplay-reset.sh
```

Observability:

```sh
curl -s http://jts.local:8780/state | jq .resilience.shairport
ssh pi@jts.local 'journalctl -u jasper-control | grep event=shairport'
```

Off switch: set `JASPER_SHAIRPORT_SUPERVISOR=disabled` in
`/etc/jasper/jasper.env` (exact match, case-insensitive), then
restart `jasper-control`. Other values like `off` or `0` log a
warning and stay enabled.

Design rationale: [`docs/HANDOFF-resilience.md`](docs/HANDOFF-resilience.md)
(Tier 3).

---

## T5.2 — userspace-liveness SystemSupervisor — read first

Closes the Tier 5 blind spot exposed by the 2026-05-23 incident.
Lives in [`jasper/control/system_supervisor.py`](jasper/control/system_supervisor.py),
runs inside `jasper-control`'s asyncio thread (no new daemon, no
new resident-RAM cost). Probes three layers every 30 s ± jitter:

1. **sshd banner exchange** on `127.0.0.1:22` (TCP accept + read
   the `SSH-` banner within 2 s — catches the 2026-05-23 shape
   where sshd accepts TCP but can't write the banner under
   userspace starvation)
2. **HTTP GET `/healthz`** on `127.0.0.1:8780` (jasper-control's
   own endpoint — yes, probes itself; catches "asyncio loop
   wedged but systemd sees us alive")
3. **`/proc/loadavg` read** within 1 s (kernel I/O stall detector;
   runs on `asyncio.to_thread` so the read can't block the loop)

After 3 consecutive failures (any probe), rate-limited at 1 reboot
per 24 hours, calls `systemctl --no-block reboot` (clean, NOT
`reboot-force` — zram dirty pages must sync).

Observability:

```sh
curl -s http://jts.local:8780/state | jq .resilience.system_supervisor
ssh pi@jts.local 'journalctl -u jasper-control | grep event=system_supervisor'
```

Off switch: set `JASPER_SYSTEM_SUPERVISOR=disabled` in
`/etc/jasper/jasper.env` (exact match, case-insensitive), then
restart `jasper-control`. Mirrors `JASPER_SHAIRPORT_SUPERVISOR` —
other values log a warning and stay enabled.

Design rationale: [`docs/HANDOFF-tier5-watchdog-liveness.md`](docs/HANDOFF-tier5-watchdog-liveness.md)
(T5.2).

---

## USB Audio Input (`jasper-usbsink`) — read first

Fourth music source. The user plugs a computer into the Pi's USB-C
port (via the 8086 Consultancy USB-C/PWR Splitter; the splitter
provides external power so the Pi stays alive even with a host
attached). JTS exposes itself to the host as a UAC2 audio output
device; the daemon bridges captured audio into `usbsink_substream`,
its private fan-in lane, so it joins the existing CamillaDSP chain.

Off by default. Toggle at `http://jts.local/sources/`. **Requires a
one-time install + reboot** for the `dtoverlay=dwc2,dr_mode=peripheral`
in `/boot/firmware/config.txt` (added by install.sh's
`set_usb_gadget_mode`). Without the dtoverlay, the wizard toggle
greys out and surfaces a "re-run install.sh and reboot" note.

Full design at [`docs/HANDOFF-usbsink.md`](docs/HANDOFF-usbsink.md).
Operational summary:

**RAM contract**:
- Off: ~50 KB (the dwc2 kernel module only — below noise)
- On: ~22 MB Pss (one Python daemon)

The on/off enforcement is in three places:
1. install.sh adds the dtoverlay but does NOT enable the service or
   load libcomposite at boot
2. `jasper-usbsink-init.service` modprobes libcomposite in
   `ExecStartPre` and rmmods it in `ExecStopPost`
3. `jasper-doctor` warns if libcomposite is loaded but the service
   is inactive (RAM drift catch)

**Volume model**: Mac slider drives JTS canonical `listening_level`
just like the dial. The host's slider is observed via ALSA mixer
events on `PCM Capture Volume` (polled at 4 Hz by `volume_bridge.py`)
and routed through `VolumeCoordinator.observe_source_volume()`.
Dial / voice "louder" / etc. do NOT write back to the gadget mixer
— the host slider is one-way input, mirroring AirPlay sender
behavior. See HANDOFF-usbsink.md §3.2 for the rationale.

**Source arbitration**: Auto mode is latest-source-wins via
`jasper-mux`. When another source starts while USB is playing in auto
mode, mux POSTs `silenced=true` to `http://127.0.0.1:8781/preempt`
and the daemon silences its output. In manual source-selection mode,
fanin's selected-input gate is the arbiter instead; mux releases any
USB preempt so choosing a source does not turn other sources on/off.
When auto mode resumes and all other sources go idle, mux releases the
preempt so a fresh host transition (pause-then-play on Mac) can
re-take the speaker.

**Debugging quick reference**:

```sh
# Wizard toggle off? Check dtoverlay first.
grep dwc2,dr_mode=peripheral /boot/firmware/config.txt
# (Re-run scripts/deploy-to-pi.sh + reboot if missing.)

# Service active but no audio?
curl -s http://jts.local:8780/state | jq '.renderers.usbsink'
# Expect: {playing, preempted, host_connected, rms_dbfs, updated_at}

# Direct daemon state file:
cat /run/jasper-usbsink/state.json | jq

# Test from the host side:
# 1. Plug computer into 8086 splitter's data leg
# 2. macOS: System Settings → Sound → output → JTS USB Audio
# 3. Play music → expect speaker output
# 4. Move Mac slider → expect JTS volume to follow within ~250 ms
```

**Common failure modes**:
- *Mac sees "Playback Inactive"*: cosmetic kernel bug in
  `f_uac2.c`; music still plays. Don't chase.
- *No volume response*: check `amixer -c UAC2Gadget controls` —
  the gadget descriptor must expose `PCM Capture Volume` (it does;
  driven by `c_volume_present=1` in `jasper-usbsink-gadget-up`).
- *RAM not returning to baseline after disable*: jasper-doctor's
  `usbsink state` check will flag this. `sudo rmmod u_audio
  libcomposite` or reboot to recover.

**No `/etc/jasper/usbsink.env` is required** — defaults work. To
override capture/playback device or HTTP ports, set
`JASPER_USBSINK_*` in `/etc/jasper/jasper.env`.

**The ALSA card has two names**, and we need both:
- `JASPER_USBSINK_CAPTURE_DEVICE` (default `UAC2_Gadget`) — what
  sounddevice/PortAudio substring-matches against
  `sd.query_devices()`. PortAudio formats the gadget as
  `"UAC2_Gadget: PCM (hw:N,0)"` — note the **underscore**.
- `JASPER_USBSINK_MIXER_CARD` (default `UAC2Gadget`) — the kernel
  "short" name used by `amixer -c <name>` and
  `/proc/asound/<name>/`. **No underscore.**

They look like the same card, but the u_audio kernel driver
registers itself with the underscore form (PortAudio sees that)
while the ConfigFS gadget descriptor sets the short name without
the underscore (the kernel uses that everywhere else). Don't set
them to the same value — the tools will both break.

**Escape hatch**: `JASPER_USBSINK_PREEMPT=disabled` in
`/etc/jasper/jasper.env` (case-insensitive, exact literal `disabled`)
turns off the mux's preempt-via-POST mechanism. USB then behaves like
Bluetooth — when a new source starts, audio briefly mixes until the
host stops on its own. Useful if the localhost HTTP POST is ever
found to be causing unexpected disruption; lets an operator degrade
to graceful-mix without a redeploy or daemon restart. Restart
`jasper-control` after editing for the change to take effect.
Mirrors `JASPER_AIRPLAY_METADATA_GATE` / `JASPER_MUX_SPOTIFY_PREEMPT_RESTART`
/ `JASPER_SHAIRPORT_SUPERVISOR`.

---

## Satellite devices — opt-in hardware

The cross-cutting design home for ESP32 satellites (existing rotary
dial, AMOLED touchscreen mic satellite in progress, future devices)
lives in [`docs/satellites.md`](docs/satellites.md). It owns shared
protocols, multi-mic arbitration design, and per-device roadmap. Read
that first when working on satellite firmware or related Pi-side
daemons.

### Rotary dial

The CrowPanel 1.28" HMI ESP32-S3 rotary dial is a wireless physical
controller that talks to the Pi over WiFi. **Currently working
end-to-end on hardware:** volume control via encoder with an
on-screen volume gauge, transport toggle on short-press (play/pause),
hold-to-talk Gemini session on long-press. The other LVGL scenes
(clock face, listening orb, speaking waveform, now-playing card with
album art) have firmware scaffold but aren't yet validated on-device.

Pi side: `jasper-control` daemon binds `0.0.0.0:8780`, exposes
`POST /volume/adjust` (and `/volume/set`, `/healthz`). Volume
requests route through `VolumeCoordinator` (see
[`docs/HANDOFF-volume.md`](docs/HANDOFF-volume.md)), which dispatches
according to mux's effective source and
[`jasper/music_sources.py`](jasper/music_sources.py)'s `VolumeMode`:
AirPlay/USB use CamillaDSP as master; Spotify/Bluetooth push their
source-side volume and use Camilla only as a degraded-safe guard.
Persistence is explicit shared state in
`/var/lib/jasper/speaker_volume.json`, so dial-driven volume survives
restarts and converges with voice-daemon observers. Service file at
`deploy/systemd/jasper-control.service`.
No auth — home LAN only.

Dial side: PlatformIO project at `firmware/dial/`. ESP32-S3, native
USB-CDC, Improv-over-Serial provisioning. WS2812 LED 0 = status
indicator (magenta=boot, yellow=connecting, dim green=online,
red blink=HTTP error, solid red=WiFi down). Normal speaker installs
stage the firmware source but do **not** compile optional ESP32
firmware; most speakers do not have accessory hardware, and first-run
PlatformIO setup is a large accessory-specific download. Use
`JASPER_BUILD_OPTIONAL_FIRMWARE=1` for an intentional install-time
rebuild, or run `scripts/check-firmware-builds.sh` as a maintainer
check when touching firmware or PlatformIO pins.

To onboard a fresh dial, end-to-end:

```sh
# One-time, explicit accessory firmware build on the Pi:
bash /opt/jasper/firmware/dial/build.sh
# Stages bin to /opt/jasper/firmware/dial/jasper-dial.bin

# Plug the dial into a Pi USB-C port, then on the Pi:
sudo /opt/jasper/.venv/bin/jasper-dial-onboard
# → flashes via esptool, reads Pi's current WiFi creds from
#   NetworkManager (or wpa_supplicant), pushes via Improv,
#   waits for dial to appear at jasper-dial.local. ~30 s.

# Unplug from Pi and connect to USB power. Dial reconnects to
# WiFi from NVS flash on every subsequent boot.
```

To re-provision after a WiFi password change: same command, same
USB plug. The dial accepts `SUBMIT_SETTINGS` over Improv whenever
it's connected to USB.

If the dial is already flashed and you just need to update creds,
pass `--no-flash`. If auto-detection of WiFi creds fails (locked-down
NM secret store, etc.), pass `--ssid` and `--password` explicitly.

The control daemon is always installed and enabled by `install.sh`,
even if there's no dial — it costs <10 MB RAM idle and the volume
endpoints are useful for any LAN client (Home Assistant, shortcuts,
etc.).

### AMOLED satellite (Phases 0, 1.1, 1.2 done; 1.3+ in progress)

Waveshare ESP32-S3-Touch-AMOLED-1.8 — touchscreen + mic satellite.
Project at `firmware/satellite-amoled/`. Both ESP32 firmware projects
(dial + satellite) on **Arduino-ESP32 v3.x via pioarduino** — see
`docs/satellites.md` "Toolchain — Arduino-ESP32 v3.x via pioarduino"
for the rationale and v2.x→v3.x deltas.

Shipped:
- Phase 0 (2026-05-08) — ES8311 mic capture, 16 kHz mono PCM over
  USB-CDC. Validated against music playback. See
  `docs/satellites.md` "Audio init footguns" for the non-obvious
  ES8311 init quirks (I²S stereo + demux for slot alignment;
  REG02 pre_multi=3 for SCLK-derived MCLK).
- Phase 1.1 (2026-05-08) — WiFi join from NVS-stored creds,
  Improv-over-Serial provisioning, mDNS-SD discovery of
  `_jasper-control._tcp`, dlog over USB-CDC + UDP `:5514`.
- Phase 1.2 (2026-05-09) — on-screen connection-status indicator
  on the 368×448 SH8601 AMOLED via Arduino_GFX. Direct draws (no
  LVGL yet); colored circle + label keyed off the `Status` enum;
  `setStatus()` helper redraws inline so PROVISION→ONLINE
  transitions show up immediately. See "Display init footguns"
  in `docs/satellites.md` for the SH8601 + TCA9554 reset
  sequence and Arduino_GFX subclass gotchas.

Next milestone: Phase 1.3+ — capacitive touch (FT3168), LVGL "Tap
to Talk" surface, control-plane HTTP, I²S mic capture gated on
press, UDP audio stream to a new Pi-side `MicSource` endpoint.

**Onboarding flow:** plug the satellite into a Pi USB-C port, then
`sudo /opt/jasper/.venv/bin/jasper-satellite-onboard`. Mirrors
`jasper-dial-onboard`: USB CDC discovery → optional flash from
`/opt/jasper/firmware/satellite-amoled/jasper-satellite-amoled.bin`
(populated by `bash firmware/satellite-amoled/build.sh`) → push
WiFi creds via Improv → wait for `jasper-satellite-amoled.local`.
The flash itself wipes NVS (factory.bin pads 0x0–0x10000 with
0xFF, including the 0x9000–0xe000 NVS region) but the cred-push
that follows refills it — no manual provisioning step.

**Local PIO setup** for the v3.x toolchain (laptop-side):
pioarduino requires Python ≥ 3.10. The JTS project itself now floors
at Python 3.11 (`pyproject.toml`) and the Pi runtime is Python 3.13,
so installing PlatformIO into a normal JTS/dev venv is fine. The
separate-venv dance is only for hosts whose `python3`/current venv is
older (notably Apple's system Python 3.9) or for maintainers who want
to keep the large PlatformIO toolchain out of the repo venv:
`brew install python@3.11 && python3.11 -m venv /tmp/jts-pio-venv
&& /tmp/jts-pio-venv/bin/pip install platformio`. Prefix `pio`
invocations with `PATH="/opt/homebrew/bin:$PATH"` if PIO's subprocess
cannot find git for the Improv-WiFi library install. The Pi already has
Python 3.13 + PIO and builds cleanly without the dance.

To capture audio for testing or SNR comparisons:

```sh
bash scripts/capture-satellite-amoled.sh 10        # 10 s → captures/<ts>.wav
bash scripts/capture-chip-mic.sh 10                # same shape, from XVF3800
```

Capture scripts assume the satellite is plugged into the Pi via
USB-C and the Pi is at `jts.local`. WAVs land in `captures/` (which
is gitignored — large binaries, regenerate as needed).

---

## Debugging — fetch evidence before guessing

> **Before writing a new test or measurement script, check
> [docs/testing-tooling.md](docs/testing-tooling.md).** It's the
> index of every capture / scoring / forensic / diagnostic tool in
> the repo, organized by "if you want to X, use Y". Several rounds
> of duplication happened before this doc existed; the cost of
> reading it (~3 min) is far less than the cost of building a
> parallel tool. If you do add a new tool, add an entry there in
> the same PR.

When the user reports "it doesn't work" or asks about Pi-side
behaviour, **before guessing**, fetch the actual logs:

```sh
bash scripts/fetch-pi-logs.sh                # last hour, default Pi at jts.local
SINCE='10 minutes ago' bash scripts/fetch-pi-logs.sh
PI_HOST=192.168.1.42 bash scripts/fetch-pi-logs.sh
```

Output lands in `./logs/`. Read the `*-latest.*` symlinks:

- `logs/jasper-voice-latest.log` — voice daemon (wake events,
  tool calls, Gemini errors, idle timeouts, spend log)
- `logs/jasper-camilla-latest.log` — CamillaDSP (broken pipe,
  format mismatch, websocket connects)
- `logs/jasper-aec-bridge-latest.log` — software AEC bridge
  (only when enabled)
- `logs/combined-latest.log` — interleaved timeline
- `logs/alsa-devices-latest.txt` — `aplay -L` / `arecord -L`
  output. Always sanity-check actual ALSA card names against
  what the configs expect (`A` for Apple dongle standard builds,
  `sndrpihifiberry`/DAC8x on the JTS3 lab path, `Array` for
  ReSpeaker, `Loopback` for snd-aloop). The AEC bridge no longer
  has an ALSA output — it sends UDP to `127.0.0.1:9876` since
  May 2026; see [`docs/HANDOFF-resilience.md`](docs/HANDOFF-resilience.md)
- `logs/camilladsp-latest.yml` — current CamillaDSP config on
  the Pi
- `logs/asoundrc-latest.txt` — current `/etc/asound.conf`
  (legacy: `/root/.asoundrc`; migrated 2026-05-23 per PR #223)
- `logs/jasper.env-latest.txt` — current env (secrets redacted)
- `logs/sessions-latest.txt` — last 20 voice sessions with token
  counts and estimated cost
- `logs/systemctl-latest.txt` — `systemctl status` for all units

Live tail (interactive, Ctrl-C to stop):

```sh
bash scripts/tail-pi-logs.sh                # all jasper-* units + renderers
bash scripts/tail-pi-logs.sh jasper-voice   # just one
```

For just the cross-daemon "events" — duck transitions, source
preempts, dial volume routing, wake/turn boundaries — the
`jasper-trace.sh` wrapper filters the live tail down to the
high-signal lines:

```sh
bash scripts/jasper-trace.sh                # default: last 5 min, follow
SINCE='1 hour ago' bash scripts/jasper-trace.sh
```

For a single JSON snapshot of cross-daemon state (voice provider /
session / spend, main_volume_db / listening_level, renderer states,
dial heartbeat), hit jasper-control's `/state` aggregator:

```sh
curl -s http://jts.local:8780/state | jq
```

Each `/state` section fails soft — if a daemon is unreachable, that
section is null instead of the whole call erroring out.

For a one-shot full diagnostic dump (when something's badly
wrong), run on the Pi:

```sh
ssh pi@jts.local sudo bash /home/pi/jts/scripts/pi-bundle.sh
# prints the path to a tarball under /tmp/, scp it back to ./logs/
```

To turn up logging for one subsystem on the live Pi, or to get the
verbose DEBUG context automatically captured around a failure, see
the runtime debug toggle (`/system` Debug card) and the in-RAM log
flight recorder (dumped to the journal as `event=flightrec.dump`) in
[docs/HANDOFF-observability.md](docs/HANDOFF-observability.md).

### On the Pi itself

`jasper-doctor` codifies BRINGUP.md's smoke tests:

```sh
sudo /opt/jasper/.venv/bin/jasper-doctor
```

Returns 0 if all critical checks pass. First thing to ask the
user to run when something's broken. The doctor sources the same
env files `jasper-voice.service` does (`jasper.env` plus the
wizard-owned `/var/lib/jasper/*.env` — transit, weather, Home
Assistant, wake, etc.) itself via `jasper.env_load.ENV_FILES`, so
its checks see the running config without you sourcing anything
into the calling shell. That list mirrors the unit and is
drift-guarded by `tests/test_env_load_mirrors_unit.py` — if it
falls behind, the doctor silently misreports configured subsystems
as "not configured."

### "The speaker restarted on its own" — hardware watchdog + cross-boot journal

The Pi has the kernel hardware watchdog enabled by Raspberry Pi
OS Trixie's `/usr/lib/systemd/system.conf.d/40-rpi-enable-watchdog.conf`
(`RuntimeWatchdogSec=1m`). When userspace wedges hard enough that
PID 1 can't ping `/dev/watchdog0` for ~60 s, `bcm2835-wdt`
hard-resets the board. This is **Tier 5** of the resilience
ladder (see [`docs/HANDOFF-resilience.md`](docs/HANDOFF-resilience.md)),
intentional for unattended recovery, and the user will perceive
it as "the speaker restarted for no reason."

The boot fingerprint of a watchdog (or any unclean) reset, on
the *recovery* boot:

```sh
sudo dmesg -T | grep "orphan cleanup"
# EXT4-fs (mmcblk0p2): orphan cleanup on readonly fs
```

To find the cause, read the **previous** boot's journal —
persistent journal was enabled in PR #160 specifically so this
works:

```sh
ssh pi@jts.local 'sudo journalctl --list-boots'
# index 0 = current boot, -1 = previous, etc. If only one boot is
# listed and PR #160 has been deployed (check
# `/etc/systemd/journald.conf.d/50-jts-persistent-storage.conf`),
# the Pi only has the post-reset boot history — wait for the next
# event, or read /run/log/journal directly if still up.

ssh pi@jts.local 'sudo journalctl -b -1 -p warning --since "-2min"'
# The 2 minutes before the wedge. Common signatures: OOM-kill log
# lines, hung-task warnings (kernel.hung_task_timeout_secs default
# 120 s), runaway jasper-* daemon, zram thrash.
```

**Self-inflicted wedges from heavy offline analysis.** Running
something like `for i in range(100): Model()` (e.g.
`openwakeword.Model()`) on the Pi can OOM the RAM, fill `zram0`,
and peg every core on compression. Pi 5 is sized for production
daemons, not analysis bursts. For wake-rate sweeps and similar,
do it on the laptop (`pip install openwakeword onnxruntime`) and
rsync the captures.

**What catches this if it happens anyway** (post-2026-05-24):
- **Stage 1 memory resilience** (OOMScoreAdjust ladder + MGLRU
  `min_ttl_ms=1000` + RAM-aware `vm.min_free_kbytes`) makes the
  kernel OOM-killer fire fast on the offending process before
  userspace wedges. The 2-min wedge in the 2026-05-23 incident
  becomes a ~20 s kernel-OOM-kill under Stage 1.
- **T5.2 `SystemSupervisor`** ([jasper/control/system_supervisor.py](jasper/control/system_supervisor.py))
  catches the "PID 1 alive enough to pat `/dev/watchdog0` but
  userspace dead" shape that the kernel hardware watchdog
  (Tier 5) structurally cannot catch. The original "starve PID 1
  and trip the watchdog" claim in pre-T5.2 versions of this doc
  was wrong — PID 1 stayed alive through the 2026-05-23 wedge
  while sshd's banner exchange timed out. T5.2 closes that gap.
- See [docs/HANDOFF-resilience.md](docs/HANDOFF-resilience.md)
  + [docs/HANDOFF-tier5-watchdog-liveness.md](docs/HANDOFF-tier5-watchdog-liveness.md).

---

## Testing

Hardware-free tests (run locally, no SDK auth needed):

```sh
.venv/bin/pytest
```

Anything Pi-specific (audio I/O, websocket, Gemini Live) needs
to run on the actual hardware via `jasper-doctor` or by tailing
logs during use.

### Test discipline — required, not optional

The repo has >1000 hardware-free pytest functions. This is a
*production embedded system* and the test suite is what lets us
swap models, change prompts, or refactor without regressions.

**Rules — apply to every PR:**

- **Every new tool** the LLM can call (anything registered via
  `make_*_tools` in [`jasper/tools/`](jasper/tools/)) ships with
  a regression scenario under
  [`tests/voice_eval/regression/`](tests/voice_eval/regression/).
  No exceptions. A tool with no scenario can't be reasoned about
  across model swaps.
- **Every reported behavioural bug** (model hallucinates / skips a
  tool / misroutes / etc.) becomes a regression scenario *before*
  the fix lands. The scenario reproduces the bug; the fix turns it
  green. This is the only way bugs stay fixed across the live
  rebuilds of prompts, model versions, and provider switches.
- **Every new subsystem** ships with hardware-free pytest coverage
  under `tests/test_*.py` — see existing `test_camilla_ducker.py`,
  `test_tools_spotify.py` etc. for the shape. Network calls and
  device I/O are mocked.

What the voice-eval harness is and how to run it:
[`tests/voice_eval/README.md`](tests/voice_eval/README.md). TL;DR
`.venv/bin/pytest tests/voice_eval/regression/` — runs each
scenario 3× (pass^3) against the currently-active voice provider.

### Voice-eval cost discipline — non-negotiable

The voice-eval harness opens **paid** real-time LLM sessions. Cost
ballpark per pass^3 scenario: ~$0.075 (Gemini), ~$0.15 (Grok),
~$0.60 (OpenAI Realtime). Full V1 suite (4 scenarios × 3 trials)
against OpenAI is ~$2.40 per run.

**Rules — apply to every PR and every session that runs the harness:**

- **Never wrap `harness.ask()` in retry loops or `while True`.**
  Failure means investigate the transcript, not re-run.
- **Never auto-rerun on flake.** Same reason. The trace tells you
  why; re-running burns money to learn nothing new.
- **Never use `pytest-repeat` / `--count=N` with N > the per-scenario
  `PASS_K`** without explicit human approval and a stated dollar
  ceiling.
- **Never add the eval suite to CI on every commit.** Nightly at
  most, after the team has reviewed cost in their context.
- **If you're an LLM agent** and the human asks you to "investigate"
  or "loop until passing", **refuse and ask for explicit scope** —
  e.g. "I'll run one trial of one scenario, ~$0.05, and report
  back" rather than open-ended budgets.
- **Announce estimated cost + read-only vs side-effecting status**
  before running anything. The Spotify scenario starts playback;
  the others are read-only.
- **Skip playback-affecting scenarios** when the household is using
  the speaker: `JASPER_VOICE_EVAL_SKIP_PLAYBACK=1`.

---

## Branch and remote

Active branch: `main`. The user's GitHub remote is
`jaspercurry/JTS`. Use the `gh` CLI for GitHub operations (PRs,
issues, API) — it is authenticated locally as the `jaspercurry`
account, and this applies whether you're on Claude Code or Codex.
(A GitHub MCP may also be available in some sessions, but it is not
reliably loaded; `gh` is the dependable path. The earlier
"`mcp__github__*`, not `gh`" guidance here was a Codex-era artifact.)

---

## PR workflow on a fast-moving `main` — read before you push

`main` moves fast (multiple PRs land per hour; Claude *and* Codex both work
this repo). The slow part of shipping is almost never the CI gate itself —
it's a branch going **stale** under you. These habits keep velocity high
without merging breakage. They were distilled from a real incident where a
branch sat while `main` advanced 23 commits and silently went un-mergeable.

1. **Local preflight before every push.** Run `ruff check .` plus a fast
   test subset for what you touched (`pytest tests/test_<area>.py`) before
   you push. This catches undefined names / dead imports / obvious breaks in
   seconds instead of a ~4-minute CI round-trip. This is the single biggest
   velocity win. Do *not* lean on a local full-suite run to gate — on macOS
   the `test_wifi_guardian_script.py` / `test_aec_reconcile.py` subprocess
   tests flake under load (posix_spawn `EMFILE`/`EAGAIN`); CI on Linux is the
   source of truth for the full suite.

2. **Short-lived branches; rebase before you push/merge.** `git fetch origin`
   at the start, and rebase onto `origin/main` right before pushing and again
   before merging. Don't let a branch sit. A large, long-lived branch (e.g. a
   multi-day refactor of a 3,000+ line file) is the worst staleness profile —
   decompose big work into small, independently-mergeable steps.

3. **`main` is branch-protected.** The `pytest` check (which also runs
   `ruff check .`) — and the `rust` check (cargo build of the audio
   daemons) — **must pass before any PR merges**, enforced for admins too;
   force-pushes and branch deletion are blocked. You cannot merge a red
   `main`; wait for green. (Emergency override + the exact rule live in
   [CONTRIBUTING.md](CONTRIBUTING.md#branch-protection).)

4. **A conflicted (DIRTY) PR cannot run CI at all.** GitHub builds checks
   against a merge ref that does not exist when there's a conflict, so the
   checks simply never register. If your checks "never appear" after a push,
   **suspect a conflict first** (`gh pr view <n> --json mergeable`), not a
   trigger glitch — rebase onto `main` to resolve.

5. **What the CI gate covers — and does NOT.** It runs: hardware-free
   `pytest` (voice_eval is **excluded** — paid LLM suite, never CI), `ruff`,
   the supply-chain provenance check, a `shell` job (`bash -n` over every
   shell entry point + `shellcheck --severity=error`), and a
   `cargo build --release --locked` plus `cargo test --locked` of
   `rust/jasper-fanin`, `rust/jasper-outputd`, and `rust/jasper-dual-dac-lab`.
   It does **not** exercise real audio/mic/voice hardware or the Pi-side
   install — those still need a deploy + `jasper-doctor` / on-device check.
   "Green CI" means "safe to merge," not "validated on hardware."

6. **Workflow-file PRs: try the `gh` merge before assuming you can't.**
   An earlier version of this rule said a `gh` OAuth token can never
   merge a PR touching `.github/workflows/*`; that's empirically false —
   `gh pr merge --squash` of a workflow-touching dependabot PR succeeded
   on 2026-06-11 (#523). The `workflow`-scope refusal ("refusing to allow
   an OAuth App to create or update workflow … without `workflow` scope")
   applies to *pushing* workflow-file changes from your token, not to
   server-side merging of someone else's. If the merge does fail with
   that scope error, hand it to a human via the web UI — and note a
   stale-required-checks failure ("base branch policy prohibits the
   merge") looks similar but just means the PR predates a newer required
   check: `@dependabot rebase` (or re-push) to get a fresh check set,
   then merge.

7. **Don't re-run the default ruff cleanup — it's done.** `main` is clean
   under the committed `[tool.ruff]` config (default `E`/`F` rules; `E701`/
   `E731`/`E402` intentionally ignored; dbus `F821` per-file-ignored). A
   *broader* pass (`--select I,UP,B`, ~hundreds of mostly import-sort items
   plus a few real `B`-rule signals) is a separate, deliberate decision — if
   taken, it's one fast atomic autofix PR landed in a quiet `main` window,
   never a long-lived branch. `ruff check . --select I,UP,B --statistics`
   shows the current latent count.

8. **Coordinate across agents.** Because Claude and Codex both touch this
   repo, two sessions can independently make the same change (it has already
   caused conflicts). Always `git fetch` before starting *and* before
   pushing, and assume `main` moved while you were working.

9. **Check the weekly `hygiene` issue before doing repo-hygiene work.** A
   report-only scheduled sweep files a "Hygiene sweep YYYY-MM-DD" GitHub
   issue (label `hygiene`, Mondays) covering open-PR staleness + supersede
   triage, merged/stale branch candidates, doc freshness
   (`doc-freshness.sh 90 --all`), README-atlas orphans/broken links, and
   PLAN.md urgent-section age. Use the latest one as the starting map
   instead of re-deriving that state. The sweep never closes PRs, deletes
   branches, or pushes — acting on its recommendations is a
   human-supervised session's job.
