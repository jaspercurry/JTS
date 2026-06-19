# JTS open-source due-diligence review — 2026-06-12

> **Status: superseded (2026-06-18).** Point-in-time audit snapshot — several
> items here have since shipped (notably the daemon privilege separation and the
> OSS governance docs). **Do not drive work from this doc.** The current,
> verified launch-readiness backlog is
> [LAUNCH-READINESS.md](LAUNCH-READINESS.md). Preserved for archaeology (the
> audit reasoning), not current operational truth.

> **Status: review snapshot.** Staff-engineer-style due-diligence pass conducted
> 2026-06-12 against `main` @ `6772b81a`, framed as "would a major engineering
> org adopt/steward this codebase as OSS?" Findings reflect that commit; specific
> line numbers will drift. Companion to the 2026-06-04 review series.

## Methodology

56 specialized review agents ran across three orchestrated waves (~10M tokens of
analysis, ~1,600 tool calls), following current multi-agent code-review practice:
fan-out readers with bounded structured outputs (the orchestrator never reads raw
file dumps), per-area subsystem cartographers (13), cross-cutting quality lenses
(10), line-level deep dives on the 7 largest/riskiest files plus 10 hotspots
voted by wave-1 agents (17), **adversarial verification** of every high-severity
claim (10/10 confirmed real, one downgraded), a completeness critic, and a
follow-up wave (5) on the gaps it flagged — active-speaker hardware safety,
privacy surfaces, Bluetooth pairing, Google token handling, and the
audio-hardware reconciler. Every finding below is grounded in a file/function
citation, and the headline claims were independently re-derived by a skeptic
agent before inclusion. Docs were treated as maps, code as truth.

Limits: no hardware was exercised (no Pi, no DAC, no mic), no tests were
executed beyond `--collect-only`, no paid LLM evals were run. Estimated ~90% of
product LOC was in some reviewer's scope.

---

## 1. Verdict

**Overall: 8/10 — top-decile for an open-source launch candidate, with launch
debt concentrated in exactly four places: licensing, privacy disclosure, the
contributor front door, and a handful of god-files.**

This is not typical hobby-project code. It is an unusually disciplined
production embedded system: incident-grounded engineering (comments and tests
cite dated production failures and PR numbers), a 5,264-test hardware-free
suite with a guard/contract-test culture most professional teams lack, supply
chain rigor (SHA-256 on every download, provenance manifest gated in CI,
commit-pinned Actions) that exceeds most mature OSS projects, and a
documentation *system* (freshness CI, doc-impact routing, verified-claim
footers) rather than a doc pile. The strongest signal for OSS stewardship:
the project converted its 2026-06-04 internal review into ~314 commits of
landed fixes within one week, each pinned by regression tests.

The honest counterweights: the repo's two most safety/privacy-sensitive claims
needed this review's second wave to be examined at all; the contributor quick
start is broken as written (verified: `uv sync` never installs pytest, and the
macOS path it courts cannot build two Linux-only C deps); several vendored
firmware files are explicitly not cleared for redistribution; household voice
transcripts land verbatim in the persistent journal with no disclosure; and
four god-files (4,133 / 3,085 / 2,557 / 4,712 lines) concentrate the risk that
outside contributors can't safely touch the core.

---

## 2. Scorecard

| Dimension | Score | One-line justification |
|---|---|---|
| Architecture & modularity | **8** | Documented 3-pattern config ownership, real plugin registries, no circular imports; dragged by 2 god-daemons + a web→control layering inversion |
| Python craftsmanship | **8** | 96% return-annotation coverage, zero bare excepts, best-in-class why-comments; no type checker anywhere |
| Rust (fanin/outputd) | **9** | RT-safe loops (no locks/allocs), 2 `expect`s in ~10k LOC, 144 in-crate tests; ~2k LOC stranded dead module layer |
| Bash (installer/reconcilers) | **8** | Env-seam dependency injection, hermetic subprocess tests, atomic writes; duplication drift + two confirmed CLI bugs |
| JS / frontend | **6** | Clean shared escape/CSRF/dialog modules; one 4,712-line monolith with a real state-loss bug, no JS lint/syntax gate in CI |
| C++ / firmware | **7** | Hardware-empirical docs, exact pinning, sane ISR discipline; vendored-license blocker, byte-duplicated files, no CI compile |
| Testing | **8.5** | 5,264 tests, contract/meta-test culture, incident-derived pins; no coverage metrics, 2 prose-only safety promises, 1,300 LOC untested onboarding CLIs |
| Resilience / availability | **8.5** | 5-tier watchdog ladder w/ cross-boot circuit breaker, fail-closed daemons, hotplug self-heal — all code-verified; 3 real recovery bugs found |
| Security | **7.5** | One guarded CSRF/Host chokepoint + conventions tests, fail-closed supply chain; unauthenticated LAN control plane is a stated-nowhere trade-off |
| Privacy | **6** | Mic-mute honored deeply where wired, audio stays local, engineered retention; transcripts in journald, no PRIVACY.md, under-disclosed cloud egress |
| Performance (1 GB Pi) | **8** | Allocation-free Rust hot loops, OOM ladder, socket-activated wizards, RAM claims verified; fork-per-poll subsystems + per-frame churn in the AEC bridge |
| Documentation | **8** | Governance system (freshness CI, doc-map, 15/18 claims verified); drift clusters in duplicated regions, AGENTS.md is a 2,964-line wall |
| Deploy / operations | **8.5** | TOFU identity + direction guards, SHA-pinned everything, dry-run with drift-guard test; root-run daemons with uneven sandboxing |
| Contributor experience | **6.5** | Genuine no-hardware dev loop and honest cost discipline; the documented quick start fails on first contact, no lockfile/types/coverage |
| OSS launch readiness | **6.5** | LICENSE/NOTICE/SECURITY/CoC/templates all present and honest; licensing verification, privacy statement, releases/versioning still open |

---

## 3. Where it shines

1. **The test estate is the moat (9/10).** 5,264 hardware-free tests (111k LOC
   against 140k product LOC) with a culture rare even in professional teams:
   *meta-tests that enforce policy* (`test_tools_have_regression_scenarios.py`
   fails any LLM tool lacking an eval scenario, with a shrink-only allowlist),
   *cross-language contract pins* (`test_wire_contracts.py` pins Rust JSON keys
   to Python consumers both ways; a panic-freedom ratchet bans `unwrap` in Rust
   runtime code), and *incident-derived regressions* whose docstrings cite the
   production failure they close. CI gates pytest+ruff, shellcheck over the
   root-mutating installer, three cargo crates, and a bespoke provenance check.

2. **Incident-grounded engineering, written down.** Across every deep dive the
   same pattern: non-obvious decisions carry dated empirical rationale —
   `voice_daemon.py`'s VAD constants document an 83-event corpus sweep,
   `install.sh` explains the PR #118 socket-restart trap inline,
   `jasper-bootloop-guard`'s header reasons about which direction unvalidated
   input would bias. Multiple independent reviewers called the why-comment
   density the best they'd seen at this scale.

3. **Supply-chain rigor beyond most mature OSS.** Every network fetch in
   `install.sh` is SHA-256-pinned and fail-closed, cross-checked against
   `deploy/provenance.toml` by CI; GitHub Actions are commit-SHA-pinned;
   download sizes/timeouts bounded; 1,817-commit history clean of secrets.

4. **Resilience as a designed ladder, verified in code.** Per-unit watchdogs,
   OOM-score ladder, a T5.1 reboot escalation with a cross-boot circuit
   breaker (`jasper-bootloop-guard` parks permanently-sick units instead of
   reboot-looping the house speaker), T5.2 userspace-liveness supervisor whose
   24h rate-limit survives the reboot it issues, hotplug self-heal for DAC and
   mic, fail-soft `/state`, and a mechanically enforced "no silent failure"
   cue rule (a two-way static test cross-checks the cue registry against all
   play sites).

5. **Hardware-safety chain held under adversarial review.** The newest, riskiest
   subsystem (active crossover commissioning — wrong config = blown tweeter)
   re-enforces every gate server-side: tone playback needs triple env opt-in on
   the Pi, frequency/level caps are recomputed from code-owned policy ignoring
   client input, CamillaDSP startup loads only mute-first sha256-bound configs
   with rollback anchors — pinned by 222 tests. Verdict from the dedicated
   reviewer: *a confused user or buggy client cannot destroy a driver through
   this subsystem.* Same story for the LLM calibration agent: allowlisted
   actions, recursive prohibited-key scan, no commit executor wired — a
   hallucinated response cannot touch volume or persist config.

6. **The plugin boundaries are real, not aspirational.** Transit's
   CityPack/provider registry lets the daemon build all tools with zero
   provider knowledge; the AEC reconciler is verifiably the single writer of
   mic env (with a cross-language constants drift-guard test); the
   thin-Grok-adapter (117 lines reusing the OpenAI adapter) proves the voice
   provider Protocol earns its keep.

7. **Docs are a governed system.** `doc-map.toml` routes changed code to
   canonical docs via a PR bot; freshness and link CI; 100% `Last verified`
   footer coverage on the 43 HANDOFFs; 15 of 18 sampled load-bearing claims
   verified exactly against code — far above typical OSS doc accuracy.

8. **Review findings become landed, pinned fixes.** The 2026-06-04 internal
   review's bugs were all fixed within a week (~314 commits), two of six
   recommended god-file decompositions landed exactly as specified, and the
   fixes shipped with enforcement tests. That conversion rate is the single
   best predictor of how this project will handle outside contributions.

---

## 4. Launch blockers (verified)

Each of these was adversarially re-verified by an independent agent.

1. **Licensing: the repo already redistributes uncleared third-party code.**
   `firmware/THIRD_PARTY.md:13` itself marks the ELECROW CST816D touch driver
   (`firmware/dial/src/CST816D.{h,cpp}`) and four SquareLine `ui_img_*.c`
   assets "NOT cleared for redistribution"; `jasper/xvf/xvf_host.py` is
   vendored without its MIT notice; DTLN ONNX models are re-hosted on the
   project's releases without bundled license text. *Fix: replace the touch
   driver with a permissively licensed one (which also kills its confirmed
   infinite-loop-on-I2C-NACK bug), regenerate the gauge assets from owned
   artwork, check in upstream notices, attach the MIT text to the dtln-models
   release.*

2. **Privacy: verbatim household utterances persist in the system journal.**
   With the OpenAI provider, input STT is always on and user + assistant
   transcripts are logged at INFO (`openai_session.py:1701-1707`); journald is
   persistent (PR #160) and `fetch-pi-logs.sh` copies it to laptops. Nothing
   discloses this. Adjacent gaps: no PRIVACY.md anywhere (README has zero
   privacy mentions); Gmail tool payloads (sender/subject/body-prefix) land in
   the journal via the generic tool-dispatch log; email bodies are sent to the
   active voice LLM with only "read-only Gmail" disclosed; `jasper-wake-enroll`
   records mic legs without checking the mic-mute flag the rest of the system
   treats as a promise. *Fix: demote transcript/tool-payload logs to DEBUG or a
   flag; ship PRIVACY.md (egress, local stores, retention, mute scope, LAN
   boundary); mute-gate wake_enroll.*

3. **The contributor front door fails on first contact.** CONTRIBUTING's
   recommended `uv sync` never installs pytest (dev deps live only in an
   extra; no `[dependency-groups]`, no lockfile), and the macOS path it
   explicitly courts cannot install `pyalsaaudio`/`evdev` (Linux-only C
   extensions, no `sys_platform` markers). Test counts are ~5x stale in two
   places; the branch-protection section omits the required `rust` check and
   its emergency-override JSON would drop it. For a project whose README
   promises a no-hardware dev loop, this is the highest-leverage credibility
   fix available. *Also: the shipped `jasper-spotify-auth` console script has
   crashed on import since 2026-05-05 (`spotify_auth.py:25` imports
   `SPOTIFY_SCOPE` from a module that no longer exports it) — no entry-point
   smoke test exists.*

4. **Install depends on artifacts that don't exist publicly yet.**
   `install.sh`'s DTLN fetch 404s while the repo is private, and
   `provenance.toml`'s own TODO says GitHub commit archives (not byte-stable)
   must be mirrored as release assets before public launch. Both are already
   self-documented — they just have to actually happen before announcement.
   **Resolved 2026-06-16:** the repo is now public and both `dtln-models-v1`
   ONNX assets download anonymously (HTTP 200); the source-build archives
   (nqptp, shairport-sync, webrtc-audio-processing) now consume byte-exact JTS
   release-asset mirrors under `build-deps-v1`, with upstream commit-archive
   URLs retained only as provenance (`provenance.toml` `source-archive-mirroring`).
   `stage_dtln_models()`'s now-stale "repo is still private" recovery message
   was corrected in the same pass.

5. **The LAN-trust security model is real but stated nowhere.** Any LAN device
   can, without auth: unmute the privacy mic switch, reboot/power off the
   speaker (port 8780), rewire multiroom bonds (`/grouping/set`), and read
   masked-secret wizard pages cross-origin (no Host guard on wizard GETs —
   a DNS-rebinding read leak; SECURITY.md already admits this one). Bluetooth
   auto-accept pairing is deliberate and Echo/Sonos-normal, but
   `configure-bluez.sh` never pins `Pairable=false` at rest, so
   non-pairability depends on a best-effort runtime call. None of this is
   indefensible for a home appliance — but an OSS launch will be judged on
   whether the trade-off is documented and the floor pinned. *Fix: a
   threat-model section in SECURITY.md, `Pairable=false` in the bluez config,
   and (optionally) a shared token for power/mic mutations.*

---

## 5. Where it needs work (themes)

### 5.1 God-files concentrate untouchability (the #1 structural risk for OSS)

| File | Size | State |
|---|---|---|
| `jasper/voice_daemon.py` | 4,133 | WakeLoop ~2,200 lines, 15-param ctor, ~60 state attrs; grew since the 06-04 review flagged it; tests bypass `__init__` via `__new__`, forcing `getattr`-on-self defensiveness in prod code |
| `deploy/assets/sound-profile/js/main.js` | 4,712 | EQ half excellent; ~3k-line active-crossover wizard grew inside it with 38 copy-pasted state rebuilds — **three drop the `rehearsal` field (real state-loss bug)** — no concurrency guards, zero harness coverage |
| `jasper/control/server.py` | 3,085 | Five separable concerns; 1,213-line handler closure; ~30 duplicated Spotify-router-construction lines; one lock-over-unbounded-D-Bus wedge risk |
| `jasper/correction/session.py` | 2,954 | ~70-method MeasurementSession |
| `deploy/install.sh` | 2,557 | Clean 22-step main, but two ~520-line functions and triplicated un-testable model-download heredocs |
| `jasper/cli/aec_bridge.py` `_aec_loop` | 975-line fn | Seven inline duplicates of its own `emit_packet` helper; production path interleaved with corpus-experiment legs |

The repo has already proven it can do these splits well (doctor → package,
wake-corpus → package, both exactly as the prior review recommended). The
playbook exists; it just hasn't reached the four biggest files.

### 5.2 Dead code misleads navigation

~2,066 LOC (28%) of `rust/jasper-outputd` models a retired TTS ingress with no
daemon caller (near-verbatim twin of live fanin modules); ~360 lines (27%) of
`jasper/web/_common.py` is the dead legacy design system — still test-pinned,
with `TOGGLE_CSS` dimensionally drifted from its live `app.css` twin; the
`VoiceSession`/`GeminiLiveSession` legacy layer (~330 lines) has zero
production consumers; 495 vestigial `# noqa: BLE001` markers suppress a rule
that isn't enabled. All cheap deletions with outsized navigation payoff.

### 5.3 Duplication with confirmed drift

The clone-then-drift pattern recurs at every scale: `satellite_onboard.py` is a
~550-line byte-level clone of `dial_onboard.py` with zero tests and
already-shipped drift (tells touchscreen users to "turn the knob"); the two
reconcilers' terminal blocks drifted (one clears stale mic state, one doesn't);
firmware `discovery.cpp` is byte-identical across both projects;
`README`/`AGENTS.md` contradict their own subsystem sections (transit cost,
AEC leg topology, TTS socket path — the stale path is actively migrated away
by install because it breaks voice). The repo's own drift-guard-test idiom is
the cure; it just needs aiming at these seams.

### 5.4 Tooling gaps an OSS project will be judged on

No type checker (no mypy/pyright, no `py.typed` — 3,880 annotated functions
never verified), no JS lint/syntax gate for 13k LOC, no clippy/rustfmt gate, no
coverage measurement, no lockfile, no pre-commit, no `permissions:`/
`timeout-minutes`/`concurrency:` in the merge-gate workflow, single-version
(3.13) CI against a 3.11 floor, no release tags/changelog/CODEOWNERS.

### 5.5 Confirmed runtime defects worth filing now

1. `jasper-spotify-auth` crashes on import (broken since 2026-05-05).
2. Voice daemon killed mid-TTS leaves music ducked forever (fanin never
   releases duck on client disconnect; fresh daemon's restore() no-ops).
3. Shairport supervisor can't recover a fully dead shairport (MPRIS gate
   fail-safes to "session active" exactly when the process is gone).
4. Yanked DAC/3.5mm jack costs up to two full system reboots before the
   bootloop guard parks `jasper-camilla` (no device-presence gate; outputd's
   exit-78 park pattern exists to copy).
5. `SystemSupervisor` clean-reboots a healthy speaker daily if sshd is
   disabled/non-standard (hardcoded port-22 probe trips the threshold alone).
6. fanin epoch gate silently drops in-flight control commands — a flush racing
   `CONTENT_METER_RESUME` leaves the content meter paused (unfixed twin of the
   already-fixed duck-off trap).
7. sound-profile JS: three success paths rebuild `activeSpeaker` state without
   the `rehearsal` field, wiping commissioning evidence.
8. `jasper-bootloop-guard --reason` (no value) busy-loops a core; `${var@Q}`
   breaks its fail-open paths on bash 3.2 (same idiom in 4 sibling scripts).
9. Wake-arbitrate task is fire-and-forget with only a weak ref — GC can strand
   `_acquiring=True` and deafen the speaker until watchdog restart.
10. No output-side cap when a provider wedges mid-response with the socket
    open — SESSION sticks until mute or adapter ping.
11. `wake_model.env` has two daemon writers with no lock (wizard + control
    server read-modify-write race).
12. `Config.server_vad_enabled` parses empty/junk as **enabled** (fails open),
    unlike every other boolean in the file.

---

## 6. Prioritized roadmap

### Phase 0 — launch gate (do before flipping the repo public; ~1–2 focused weeks)

| # | Item | Why / benefit |
|---|---|---|
| 0.1 | Licensing sweep: replace CST816D driver + regen dial assets, add xvf_host MIT notice, bundle DTLN license, verify jarvis model terms | Legal exposure; the repo documents its own blocker. Driver swap also fixes a real bug |
| 0.2 | Publish dtln-models release + mirror source archives as release assets | Install is broken for the public until done |
| 0.3 | PRIVACY.md + demote transcript/tool-payload logs to DEBUG + disclose LLM egress in /google wizard + mute-gate `wake_enroll` | Privacy is what a smart-speaker launch gets judged on; all items are small |
| 0.4 | Fix CONTRIBUTING quick start (`uv sync --extra dev` or PEP 735 group + committed `uv.lock`), `sys_platform` markers for pyalsaaudio/evdev, correct counts, add `rust` to branch-protection docs/JSON | First-contact credibility; currently fails for every new contributor |
| 0.5 | Fix `jasper-spotify-auth` import + add a console-script import-smoke test | Shipped-broken CLI; the smoke test prevents the class |
| 0.6 | SECURITY.md threat-model section (8780 mutations, BT pairing, peering trust); pin `Pairable=false` in configure-bluez.sh | Converts "unexamined hole" optics into "documented trade-off" reality |
| 0.7 | Repo mechanics: tag v0.1.0 + minimal CHANGELOG, CODEOWNERS, `permissions:`/`timeout-minutes`/`concurrency:` in tests.yml | Table-stakes signals (OpenSSF-visible), each <1 hour |
| 0.8 | Fix the verified doc contradictions: AGENTS.md TTS socket path + multi-leg recipe, README "Known marginal items" + transit bullet, 7 dangling BRINGUP anchors | These are the docs newcomers will follow first |

### Phase 1 — first 90 days as an OSS project (contributor scale)

| # | Item | Why / benefit |
|---|---|---|
| 1.1 | Decompose `control/server.py` (aec_endpoints/uds/state/volume/dial) and land the three recommended `voice_daemon.py` seam extractions; give WakeLoop a test-constructor and delete the `__new__` fixture idiom | Biggest unlock for outside contribution; the doctor/wake-corpus splits prove the playbook |
| 1.2 | Route all AEC bridge legs through `emit_packet` + leg-emitter table; derive port defaults from `wake_legs` (or add the lockstep test) | ~100 lines deleted, kills a third unguarded copy of the leg map |
| 1.3 | sound-profile JS: `patchActiveSpeaker()` helper (fixes the rehearsal bug), seq-token guards on wizard actions, split per its own header plan, extend the node harness; add `node --check` + eslint to CI | Converts the weakest shipped surface (6/10) into house style |
| 1.4 | Delete dead code: outputd stranded TTS layer (or extract shared crate), `_common.py` legacy block, `VoiceSession` layer, vestigial noqa | ~3k LOC of navigation traps gone |
| 1.5 | mypy lenient baseline + `py.typed`; clippy+rustfmt gates; rust workspace + cargo cache; coverage artifact; 3.11 CI leg (or raise floor) | Verification for 3,880 unverified annotations; standard OSS hygiene |
| 1.6 | File + fix the §5.5 defect list (each is small; several have the fix pattern in-repo) | Real availability/UX wins; duck-stuck and DAC-reboot are household-visible |
| 1.7 | Extend freshness footers + drift tests to top-level docs (README/CONTRIBUTING/AGENTS); AGENTS.md ToC + split per its own charter | Stops the one doc class where this repo still drifts |
| 1.8 | Shared `jasper/cli/_improv.py` for dial/satellite onboarding + parametrized tests; `--auto` requires positive probe before pushing PSK | Removes the worst clone (5/10) and a credential-handling wart |

### Phase 2 — structural (3–6 months, as contributor base grows)

- Naming migration `JASPER_OUTPUTD_*`→fanin with deprecated aliases (pre-1.0 is
  the cheap window); document the snd-aloop lane-table successor design (it's
  at 8/8 kernel capacity and the add-a-source checklist dead-ends).
- Volume dispatch table-driven off `VolumeMode`; extract Spotify/Google/HA
  config clusters from `config.py` to pattern-2 modules.
- Satellite protocol auth + version byte before Phase 1.3 mic streaming;
  HMAC option for peering; prompt-injection fencing strategy for
  email/HA-content reaching the tool-calling LLM.
- Multiroom TTS PR-2 (the standing doctor warn); `/metrics`; correction-bundle
  retention cap; coverage-driven test backfill (satellite CLIs, `aec_tune`
  safety clamp, XVF `SAVE_CONFIGURATION` static ban).
- Release cadence + supported-hardware matrix; move OAuth bounce pages to a
  project org.

---

## 7. Per-area ratings appendix

**Subsystem maps:** audio-pipeline 8 · voice-stack 8 · AEC 8 · web-ui 8 ·
control/resilience 8.5 · integrations 8 · deploy/install 8.5 · DSP/correction
8.5 · multiroom 8 · tests-harness 9 · docs-system 8.5 · prior-review
follow-through 9 · firmware/satellites 7.

**Quality lenses:** modularity 8 · Python 8 · polyglot (Rust 9 / bash 8 / JS 6
/ C++ 7) · testing 8 · resilience 8.5 · security 8 · performance 8 ·
docs-accuracy 8 · OSS-readiness 7 · contributor DevEx 7.

**Deep dives:** voice_daemon.py 8 · control/server.py 7 · install.sh 8 ·
aec_bridge.py 7 · web/_common.py 8 · jasper-outputd 8 · voice_eval 8 ·
bootloop-guard 8 · config.py 8 · fanin/tts.rs 8 · tests.yml 7.5 · AGENTS.md 8 ·
CONTRIBUTING.md **5** · README.md 8 · sound-profile JS **6** ·
aec-reconcile 8 · satellite_onboard.py **5**.

**Gap wave:** active-speaker safety 8 (cannot destroy a driver via this
subsystem) · privacy surfaces **6** · bluetooth pairing 7 · google tokens 7 ·
hw-reconcile + calibration agent 8 (agent containment ~9).

Notable per-area headlines not covered above: Gemini/OpenAI adapters remain
~2k lines each with a duplicated supervisor state machine (4th provider still
costs ~2k lines); the voice_eval cost table understates the grown suite ~6x and
the timeout path discards the transcript it tells you to read; mux's 1 Hz tick
forks busctl/bluealsa subprocesses (and usbsink 8 forks/s) against a "~0% idle"
README claim; `/state`'s CamillaDSP "parallel" gather serializes behind a
per-request websocket; weather/transit/HA clients are model external-API
citizens (stale-on-error caches, throttles, secret-scrubbed logs).

---

*Review artifacts: full structured findings from all 56 agents live in the
session transcript; this document is the curated synthesis. Generated with
multi-agent orchestration (Claude Code workflows), adversarial verification
pass included.*
