# 2026-09-23 platform cleanup review

Frozen report of one audit run (ADR-0284). Audited SHA: `bdba966e4` (origin/main
at 03:12 UTC, 2026-09-23). Written once; the disposition of every finding lives in
the batch PRs named in §5 and on the tracking issue named in `docs/audits/README.md`.

## 1. Headline

The 2026-09-22 refresh and a peer session's toolbox review had already cut most
dead code and split the worst god files. This run covered everything outside the
tuning zone (which the peer session held) and found the remaining debt is mostly
**second copies of facts that already have an owner**, **tests and prose that pin
or narrate history**, and **a few quiet behaviour faults**:

- **One owner per fact, not yet everywhere.** Truthy parsing (≈12 spellings in two
  vocabularies), env-number parsing, read-side `systemctl` probes, unit names, the
  voice socket path, the USB gadget identity, DAC ids, volume dead bands and the TTS
  wire rate each had an owner and several copies; Spotify and Google each kept
  their own OAuth pending-flow store. All converged.
- **Provider adapters copied the base's lifecycle.** OpenAI and Gemini each carried a
  drifted `acquire_turn` (different event names) and the billing meter was written
  twice; the base now owns both.
- **Quiet faults found by reading, one confirmed on hardware:**
  `/run/jasper/volume_policy.json` never reached `/state` (jts.local: the file does not
  exist, and jasper-control could not read it anyway); three tuning CLIs and the
  long-lived `jasper-correction-web` logged around the redacting filter; a malformed
  Home Assistant token could reach DEBUG logs through httpx header errors; the landing
  page's 5 s poll compiled tuning diagnostics nobody read; the outputd boolean env
  reader silently turned unknown values into "off".
- **Tests pinned internals or repeated the base.** History ratchets, source-text
  scans, detail-prose asserts, tautological tests of test-only constants, three
  Gemini test modules that skipped silently on a renamed import, and four provider
  suites that each re-tested about 14 behaviours the base now owns.
- **Structure.** outputd's chip-reference writer moved out of `main.rs` (2281 → 1577
  lines); fan-in's program-ring publisher and log writer left `mixer.rs`
  (3826 → 2551); the installer lost unreachable branches (adversarial review: sound).
- **Second-vendor review earned its keep.** Two investigator premises were wrong and
  were caught before landing: IPv6 bracketing in Home Assistant discovery is needed
  (zeroconf returns AAAA records over an IPv4 transport — a Codex review caught the
  deletion), and a web-helper claim was narrowed. A Codex review of the provider-test
  consolidation found two weakened pins (proven with planted faults); both were fixed
  before merge.

## 2. Method

Six read-only Claude investigators (Opus 5.5 ×4, Sonnet 5 ×2), one tile each, from a
pinned checkout; then three read-only Codex (GPT) investigators for the areas the first
pass covered lightly. Same preamble for all: evidence before judgment; verify callers
through registries, entry points, units, udev, CI, JS fetches and `getattr`; name the
larger issue; no lints, gates or guard tests as fixes; held zones reported, not worked.

| tile | scope |
|---|---|
| I1 | voice + assistant stack, tools, cues, integrations, wake corpus |
| I2 | audio platform modules and reconcilers (hearing files report-only) |
| I3 | control plane, web wizards, system services, web assets, nginx |
| I4 | CLI and doctor |
| I5 | deploy, laptop scripts, CI, Rust, C |
| I6 | tests and docs cartography |
| X1 | Rust crates and C not covered by I5 |
| X2 | web assets outside the tuning zone |
| X3 | scripts, wake_training, release, side trees |

Builds: Claude builders (Opus/Sonnet) in the first waves, then Codex (GPT-6 Astra /
GPT-5.6 Sol) per the owner's instruction; one worktree per job; the conductor
committed Codex work (its sandbox cannot take git's index lock), re-ran every suite
outside the sandbox (socket binds), ran `/simplify` (4 angles) and `/code-review` on
the batches, adversarial reviews on the non-negotiable tier (secrets, DSP emission,
installer), and a Codex read-only review of the later batches. Merges went out as
batch PRs because CI is the bottleneck.

## 3. Scorecard at the audited SHA

```
sha: bdba966e4  date: 2026-09-23T03:17:47Z
tracked files: 2745
root tracked files: 15  root dirs: 14
jasper/ py lines: 341718   files: 863   flat top-level modules: 125
tests/ py lines: 483697   files: 945
docs md lines: 59494   files: 371   top-level docs: 25
product files > 1500 lines: 34
product py files > 1000 lines: 84
test files > 2000 lines: 27
function-local first-party imports without # lazy (excl cli/doctor): 392
duplicate private helper defs (finite/positive_int/sha256/fingerprint/state_path/read_json/as_int/coerce): 58
top-level plan docs (docs/*plan*.md): 4
docs/historical lines: 18651   docs/research lines: 11319
largest 12 product py files:
    2437 jasper/active_speaker/runtime_contract.py
    2193 jasper/active_speaker/graph/active_verifier.py
    2095 jasper/active_speaker/linearization_fit.py
    2067 jasper/volume_coordinator.py
    1971 jasper/multiroom/reconcile.py
    1921 jasper/wake_corpus/recording_backend.py
    1892 jasper/active_speaker/crossover_v2/verification.py
    1889 jasper/audio_hardware/reconcile.py
    1866 jasper/active_speaker/baseline_profile.py
    1858 jasper/cli/doctor/aec.py
    1757 jasper/fanin/coupling_reconcile.py
    1748 jasper/mux.py
```

## 4. Findings

Severity uses the playbook taxonomy. "Disposition" is the state when this report was
frozen.

| id | area | finding | disposition |
|---|---|---|---|
| R5-08/16/19 | platform | truthy, env-number and read-side systemctl copies | #5631 |
| A-03/04/05/16 | platform | voice socket, USB gadget identity, unit names, DAC ids re-spelled | #5625, #5631 |
| A-06/07/08/23/24 | volume | one dead band two owners; re-exports; impossible fallbacks; twice-derived carrier target | #5631 |
| A-01 | volume | diagnostics file never reached `/state` (hardware-confirmed) | #5636 |
| A-02 | mux | closed test-gate owner allowlist re-spelled client tokens | #5631 |
| A-09..A-15 | platform | test-only symbols, source-text test, history prose | #5625 |
| A-17..A-20 | hearing / held | fader primitives in the tuning package; duplicate duck algebra; statefile paths | #5644 (hearing tier / held) |
| V-01..V-04 | voice | drifted acquire/billing copies in adapters | #5629 |
| V-05..V-22 | voice/tools/cues | INHERITS machinery, two music thresholds, dead facade, tool migrations, cue retry copies, stale prose | #5629, #5631 |
| V-23, V-26..V-28 | voice | capture convergence; debug WAV tee; context-reset knobs; old history key | owner calls, #5643 |
| V-24 | voice tests | `backoff_schedule` exists for tests (56 call sites) | #5646 |
| V-25 | voice tests | about 14 base behaviours tested once per provider | #5641 |
| C-01/02/04..08 | control/web | diagnostics per poll; mux via AirPlay snapshot; nginx route copies; route map; history ratchets; live pill; helper copies | #5624 |
| C-09 | web | `_bracket_ipv6` called a passthrough | premise false; restored in #5631 |
| C-10/11/13/17 | control/web | stale prose; unit literals; async probes; HA URL writes | #5631, #5636 |
| C-03, C-19, C-20 | control/web | AirPlay-health step 2; tier-gating owners; compat redirects | owner calls, #5643 (jts.local access logs: zero compat hits since about 09-11) |
| C-16 | web (secrets-adjacent) | two OAuth pending-flow stores | #5641 (adversarial review: no blockers) |
| L-01 | CLI (secrets) | tuning CLIs logged around redaction | #5625 |
| L-02, L-03 | doctor | duplicate coercion; detail-prose asserts | #5625, #5631, #5636 |
| D-01..D-05, D-07 | installer (deploy tier) | unreachable arch/group/source branches, an unused library source, history prose | #5631, #5636 |
| D-06, D-08 | deploy | one-consumer ALSA-card library; deploy knobs nothing sets | #5648 |
| D-09..D-14, D-23..D-26 | CI/deploy | false CI prose, dead Makefile target, stale unit pointers, dead diagnostic paths | #5623 |
| D-15 | CI | two owners of "what changed" | #5636 |
| D-16..D-22 | Rust | fail-silent env bool; chip-ref writer in `main.rs`; mixer god file; rate/watchdog/min-channel copies | #5623, #5636 |
| D-27..D-29 | scripts (D-29 secrets) | env reader copy; SSH options; API key copied to the laptop | #5631 |
| D-30, D-31 | scripts | AEC debug heredocs; journal-review location | #5645 (held by draft PRs) |
| D-32 | CI | single-entry matrix remnants | owner call, #5643 |
| T1..T9 | tests/docs | retired plan doc; test doubles; log-prose asserts; unit-text asserts; dead emitters | #5625 |
| X1-01..05 | Rust/C | TTS parser copy; unused resampler paths; C clamp; UDS tests; C prose | #5636 |
| X1-06 | C bench | live-ring bench cannot attach to the shipped geometry | #5644 (hearing tier) |
| X2-* | web assets | pollers, pack builder, HTTP contract, forwarding modules, previews, clipboard, buttons | #5636 |
| X3-02..05 | scripts | DTLN dir, placeholder negatives, AirPlay fit, VAD labels | #5636 |
| X3-01, X3-07 | scripts | wake-training helpers repeat the feature-bank owner; `pyproject.toml` prose | #5648, #5645 |
| contracts | platform | 173 private names imported across modules | 26 in #5636; 147 on #5646 |
| runtime | deploy | a running daemon lazily imported a replaced module mid-install | #5647 (seen on jts.local) |
| review | secrets | HA token could reach DEBUG logs via httpx header errors | #5636 |

## 5. Landing note (2026-09-23, written once)

Seven batch PRs, each merged on a green `ci`:

| PR | batch | merge | lines +/− | contents |
|---|---|---|---|---|
| #5623 | deploy, CI, native | `0d5768666` | +2223/−2113 | truthful CI docs; outputd chip-ref module; fan-in mixer split; fail-loud env bool |
| #5624 | control, web | `36d23a39b` | +414/−727 | lean `/system` snapshot; one nginx measurement snippet; shared web helpers |
| #5625 | platform 1 | `34837f2c1` | +376/−1777 | redacted tuning journals; one owner per fact; dead code and stale prose |
| #5629 | voice | `388c5dda5` | +664/−973 | provider base owns turn acquisition and billing; one music threshold; tool migrations |
| #5631 | platform 2 | `1394b1f48` | +1137/−1705 | one owner per primitive; installer dead branches; volume, mux, bluetooth, shell |
| #5636 | 3 | `616ea360b` | +1883/−3057 | CI Rust-lane owner; Rust/C single sources; HA token logging; doctor reasons; public contracts; web assets; scripts |
| #5641 | 4 | `04960a3d3` | +1104/−2020 | one OAuth pending-flow store; provider tests parametrized |

Net across the seven: +7801/−12372 lines (net −4571).

**Hardware (jts.local).** Each deploy used `scripts/deploy-to-pi.sh`:

- `36d23a39b`: identity match; doctor core 0 fail, 0 warn; `speaker_silent=false`;
  `nginx -t` ok; measurement routes answer 200 over HTTP and HTTPS;
  `outputd.chip_ref` active.
- `658827ed9`: doctor 0/0; group-owned directories `2775 root:jasper`; all units
  active. One `ImportError` in the old jasper-control during the install window
  (#5647).
- `616ea360b`: build manifest verified; doctor core 0 fail, 0 warn (21 rows);
  `speaker_silent=false`; no failed units and no restarts; fan-in
  `WatchdogUSec=30s`; `/run/jasper/volume_policy.json` absent (its writer is gone).
- `04960a3d3`: build manifest verified; doctor core 0 fail, 1 warn. The warn was one
  outputd DAC xrun 3 s after the restart, with none after it; the rule read one xrun
  as a high rate while uptime was short (PR #5649). A crawl of 16 pages loaded all 56
  assets and 44 JS modules with 200; `/spotify/` and `/assistant/google/` render.
  The journal also showed udev rejecting the Bluetooth adapter rule since 09-10, so
  an adapter's arrival never re-ran the source-intent reconcile (PR #5650).

**Not changed, noted:** `redact_secrets` does not mask `state=` or `code_verifier=`
values. Both are single-use and are spent before an exchange error can log them.

**Scorecard at `04960a3d3`** (same script as §3). The tuning session landed work in
the same window; its deletions account for most of the drop in product lines.

```
sha: 04960a3d3  date: 2026-09-23T13:56:30Z
tracked files: 2734
root tracked files: 15  root dirs: 14
jasper/ py lines: 321607   files: 854   flat top-level modules: 125
tests/ py lines: 458342   files: 942
docs md lines: 58336   files: 372   top-level docs: 25
product files > 1500 lines: 29
product py files > 1000 lines: 73
test files > 2000 lines: 24
function-local first-party imports without # lazy (excl cli/doctor): 364
duplicate private helper defs (finite/positive_int/sha256/fingerprint/state_path/read_json/as_int/coerce): 58
top-level plan docs (docs/*plan*.md): 4
docs/historical lines: 18651   docs/research lines: 11319
largest 12 product py files:
    2437 jasper/active_speaker/runtime_contract.py
    2193 jasper/active_speaker/graph/active_verifier.py
    2052 jasper/active_speaker/linearization_fit.py
    1968 jasper/volume_coordinator.py
    1966 jasper/wake_corpus/recording_backend.py
    1936 jasper/multiroom/reconcile.py
    1889 jasper/audio_hardware/reconcile.py
    1857 jasper/cli/doctor/aec.py
    1757 jasper/fanin/coupling_reconcile.py
    1744 jasper/mux.py
    1610 jasper/voice_daemon.py
    1533 jasper/audio_validation.py
```

Open work is on the tracking issue #5642 (leftovers #5643–#5648).

## 6. What only hardware or runtime can prove

- One wake per voice provider answers; `event=provider.turn_started` appears; Grok and
  OpenAI Live usage rows open and close once per turn (owner, audible).
- The Spotify and Google link flows complete end to end with the shared pending-flow
  store (owner; needs the accounts).
- The corpus buttons on `/wake-corpus/` render correctly (visual).
- Home Assistant discovery with an IPv6-only instance.
- No `ImportError` in the journal across the next deploys (#5647).
