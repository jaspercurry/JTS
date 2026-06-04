# JTS Deep-Dive Review — OSS Release Readiness (2026-06-04)

> **Status: review snapshot.** Point-in-time assessment of `main` at
> `b4417b1`, produced by a 23-agent parallel code review covering every
> major subsystem plus an adversarial verification pass on high-severity
> bug claims. This supersedes the 2026-05-26
> [REVIEW-google-oss-readiness.md](REVIEW-google-oss-readiness.md), which
> predates the `active_speaker`/`output_topology`/DAC8x subsystem, the
> supply-chain/provenance rework, the `http_security` guard, and the
> canonical-UI migration. Not operational truth — for current behavior
> read the canonical HANDOFFs via [README's atlas](../README.md).
>
> Companion docs (this is the index):
> - **[REVIEW-2026-06-04-big-rocks.md](REVIEW-2026-06-04-big-rocks.md)** —
>   architecture / re-architecture / large refactors.
> - **[REVIEW-2026-06-04-small-wins.md](REVIEW-2026-06-04-small-wins.md)** —
>   bugs, quick fixes, doc-staleness, hygiene.

---

## TL;DR

This is a genuinely impressive codebase, and it has moved up materially
since the last review. The legal/OSS-plumbing "F" is gone (LICENSE,
CONTRIBUTING, SECURITY, NOTICE, CODE_OF_CONDUCT, third-party attribution,
pytest CI, a machine-readable provenance manifest enforced in CI). The
supply chain went from "1 of 7 pinned" to **every** binary/source/model
SHA-256-pinned and fail-closed. The control daemon gained a real
DNS-rebinding/CSRF guard. The correction subsystem's "misfiled domain
logic" was decomposed. The new `active_speaker` subsystem landed at an
**A** with defense-in-depth no-audio defaults and exhaustive tests.

**You are close to "publicly impressive."** The remaining work is no
longer "build the plumbing" — it's a focused pass of (1) a handful of
real correctness/resilience bugs, (2) one security gap that matters for a
home appliance (the nginx wizard tier doesn't validate `Host`, unlike the
control daemon), (3) hygiene-infrastructure that's present-in-name-only
(`ruff` declared but unconfigured, ~161 live lint findings, 610 `# noqa`
markers half of which suppress a deleted rule), and (4) decomposing four
or five god-files that are the parts a new contributor will fear touching.

Nothing here is a brick/loud-output hazard in shipping code. The biggest
*structural* risk to the "delightful to extend" goal is file size:
`doctor.py` (5,632 lines), `voice_daemon.py` (3,923), `wake_corpus_setup.py`
(3,862), and two ~2,000-line voice adapters that are ~36 methods of
copy-paste apart.

---

## Grades by dimension

Reviewed 20 areas across 106 findings (8 high · 33 medium · 65 low).

| Area | Grade | One-line |
|---|---|---|
| `active_speaker` + output topology + DAC8x | **A** | Defense-in-depth no-audio defaults; exhaustive validation + tests. New code, landed clean. |
| Renderers + mux + music sources | **A−** | Clean policy/gate/volume ownership split; leaked-audio-between-polls structurally prevented. |
| Room correction | **A−** | "Misfiled domain logic" now decomposed into ~24 modules; ear-safety model is exemplary. |
| Control server + supervisors + volume | **A−** | Textbook resilience supervisors; real `http_security` guard. Unbounded threading + unauth destructive endpoints drag it. |
| LLM tool system | **A−** | Smallest-durable registry, fail-closed factories, docstring-as-prompt. Schema gen still collapses `Literal`/`list`. |
| doctor + observability | **A−** | 73 checks, fail-soft `/state`, flight recorder. But doctor.py is a 5.6k-line monolith with a hand-ordered list. |
| install.sh + deploy + systemd | **A−** | Unusually disciplined; every download pinned. Bash reconcilers re-hardcode Python constants. |
| Test suite | **A−** | 228 files / >1000 fns; new subsystem fully covered. Regression-scenario rule only partly enforced. |
| Security posture | **A−** | Strong + self-aware for trusted-LAN. One real gap: wizard tier lacks the `Host` guard the control daemon has. |
| Documentation health | **A−** | `Last verified:` footers, 0 stale at 90d, historical tagging done right. AGENTS.md drift on `jasper-outputd` + design-system. |
| voice_daemon + wake loop | **B+** | Sophisticated, well-instrumented. `_end_turn` re-entrancy race + 3.9k-line file with uncut seams. |
| voice provider abstraction | **B+** | Clean Protocol seam (Grok = 112 lines). ~36 methods duplicated across the two heavy adapters. |
| web wizards + design system | **B+** | Excellent shared primitives + XSS discipline. Legacy `wrap_page` stack now dead; AGENTS.md describes a finished migration as in-progress. |
| transit extensibility | **B+** | Discovery layer is clean; "3 places to add a city" is really ~9–12, several in core `voice_daemon.py`. |
| config + env management | **B+** | Real `_validate`, fail-loud provider. `.env.example` frozen-seed permanently shadows code defaults. |
| ESP32 firmware | **B+** | Exceptional "why" comments. No OTA on either device; vendored assets lack license attribution. |
| supply chain + CI + provenance | **B+** | Artifact provenance is exemplary. Governance lags: public repo, no branch protection, mutable Action tags, no Dependabot. |
| Wake corpus + telemetry | **B+** | `wake_events.py` is a model store. `wake_corpus_setup.py` is a 3.8k-line god-module; telemetry HANDOFF cites stale 500 MB ring. |
| AEC bridge + jasper_aec3 | **B** | Ref-starvation fix intact, queues bounded. `_aec_loop` is ~960 lines mixing production with 4 corpus leg families. |
| Cross-cutting Python quality | **B** | ~75% return-typed, zero bare `except:`. But lint/type unenforced; god-methods; one live `NameError`. |

---

## Confirmed bugs (adversarially verified)

The three high-severity bug *claims* were each independently re-checked by
a skeptic agent reading the actual code. **All three confirmed real**; the
verifier downgraded severity on two after assessing trigger likelihood.
Full detail in [small-wins](REVIEW-2026-06-04-small-wins.md#confirmed-bugs).

| Bug | Verdict | Sev | Where |
|---|---|---|---|
| `_end_turn` has no re-entrancy guard — a dashboard/HTTP mic-mute landing during turn teardown re-enters `_end_turn` (state stays `SESSION` across all its awaits), hitting `assert _session_id is not None` after it was cleared → recoverable daemon crash + corrupted usage row. | Confirmed | **Med** (was High) | `voice_daemon.py:3146,3232` |
| `jasper-cues play <slug>` calls undefined `_env` → `NameError`. Latent since 2026-05-07; the only test exits early before the bug. Operator diagnostic CLI, not the live cue path. | Confirmed | **Med** (was High) | `cues/cli.py:166-167` |
| `JASPER_WAKE_THRESHOLD` disagrees 3 ways: code/AGENTS.md say 0.50, `.env.example` ships 0.30 and wins in production via the frozen-seed. Behavior is the *intended* 0.30; only engineers reading code/docs are misled. | Confirmed | **Low** (was High) | `config.py:451`, `.env.example:115` |

Other real `bug`-category findings (not in the high-verify set): Gemini
GoAway-mid-turn teardown ignores `time_left` (`gemini_session.py:1343`);
Grok billed per-hour but `ConnectionUptimeMeter` is never wired so the
daily spend cap is inoperative for Grok (`grok_session.py` /
`voice_daemon.py:754`); tool schema generator collapses
`Literal`/`list`/`dict` to bare `"string"`, now load-bearing for
`spotify_play(kind=...)` (`tools/__init__.py:190`).

---

## What's genuinely good (preserve these)

The review repeatedly flagged design choices worth *not* losing in any
refactor:

1. **Resilience-by-construction.** Bounded drop-on-full queues everywhere
   in the AEC bridge; the never-give-up reconnect with jittered backoff;
   the two complementary stall watchdogs (consecutive-empty +
   slow-drip); fail-safe-to-active supervisor gates; `reset_failed` before
   restart to avoid tripping the reboot escalation. This is real
   post-incident scar tissue.
2. **Fail-soft observability.** `/state` fans out slow probes in parallel
   and nulls a dead section instead of blanking the snapshot; every
   telemetry write is wrapped so a store bug can never silence the
   speaker; per-check crash isolation in doctor; layered secret redaction
   before any LAN-proxied output.
3. **Hardware-safety as policy.** The CamillaDSP `volume_limit=0.0` floor
   is intact and test-guarded; `active_speaker` blocks the main-lane
   loud-output footgun on both generate and inspect sides; autolevel's
   ear-safety bounds are computed from the user's own listening volume
   with a documented first-blast incident behind them.
4. **The provenance loop.** `provenance.toml` + `check-provenance.py` +
   `test_provenance.py` make supply-chain drift fail CI. This is rare at
   any scale and is the model to extend (to a Python lock).
5. **Documentation discipline.** `Last verified:` footers, honest
   "code paths not changed in this PR" scoping, correct historical
   tagging. The corpus is genuinely fresh.

---

## Recommended sequencing

Each phase is independently shippable. Effort is rough, single-author.

**Phase A — Correctness & safety quick wins (~2–3 days).** The verified
bugs + a few high-confidence resilience one-liners. Highest value/effort
ratio in the whole review.
- `_end_turn` re-entrancy guard (flip state at top / early-return idempotency).
- `jasper-cues play` `_env` → `os.environ.get` + a play-path test.
- Spotify Web API `requests_timeout` + `wait_for` so a hung pause can't
  freeze the mux tick (`spotify_router.py:197`, `mux.py:961`). **High
  confidence, quick — this one can stall source switching indefinitely.**
- `active_speaker` forbidden-lane guard on the audible `TEST_PCM` path.
- Reconcile `JASPER_WAKE_THRESHOLD` to one value (likely bump code+docs to 0.30).

**Phase B — Public-release security & governance (~3–4 days).** The
things a reviewer will check first on a public home-appliance repo.
- Apply the existing `http_security` `Host`/`Origin` guard to the wizard
  tier in `web/_common.py` (it's wiring, not new design) — closes the
  DNS-rebinding gap and makes SECURITY.md's claim true.
- Branch protection on `main` (required `pytest` check + review).
- Pin GitHub Actions to commit SHAs; add `.github/dependabot.yml`.
- Vendored-asset license attribution (Elecrow CST816D, SquareLine blobs, NOTICE).
- Decide + document the destructive-endpoint auth tradeoff (optional token).

**Phase C — Hygiene infrastructure (~3–5 days).** Make the linter real.
- Add `[tool.ruff]`, `ruff check --fix` the ~102 auto-fixable, per-file-
  ignore `voice_eval`, then wire `ruff check` into CI (non-blocking →
  blocking once green).
- Strip or re-activate the 462 vestigial `BLE001` `# noqa` markers.
- Fix `.env.example` frozen-seed shadowing (prune tunable-default keys, or
  add a migration that prunes template-equal literals from `jasper.env`).
- Commit a Python lock and have CI + install consume it (extend
  check-provenance to assert coverage, mirroring `Cargo.lock`).

**Phase D — Decompositions (~1.5–2.5 weeks).** The "fear to touch" files.
See [big-rocks](REVIEW-2026-06-04-big-rocks.md). Roughly in value order:
`doctor.py` → registry of per-domain check modules; `voice_daemon.py` →
extract `system_instruction`, the endpoint VAD, and a build-deps context;
`_BaseLiveConnection` to kill the adapter duplication; `wake_corpus_setup.py`
→ a `jasper/wake_corpus/` package; `_aec_loop` corpus-leg extraction;
`control/server.py` `do_POST` → route table.

**Phase E — Generalize & document (~1 week).** Extensibility honesty +
the doc-staleness cluster.
- Make the transit "add a city" story honest (it's ~9–12 sites, several in
  core), or add a per-provider runtime descriptor so it's really 3.
- Single-source the AEC port/mixer constants the bash reconcilers re-hardcode.
- Sweep the stale-doc cluster (AGENTS.md design-system + `jasper-outputd`,
  wake-telemetry 500 MB→1 GB, the orphan UI-migration doc, etc.).
- Roadmap-only: ESP32 OTA, a read-only `/metrics` derived from `/state`.

---

## Notes on scope

- **Do not rewrite.** Every weakness here is consolidation or a contained
  fix. The architecture's seams are in the right places.
- This review and its companions are session artifacts. Per the
  documentation paradigm (README rule #8), either add an atlas entry or
  tag them, and prune them once the work lands — they age fast.
- The old `REVIEW-google-oss-readiness.md` is already tagged historical; a
  forward-pointer to this review has been added to its banner.
