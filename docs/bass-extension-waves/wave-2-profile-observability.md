# Wave 2 — profile, refusals, observability skeleton (Codex prompt)

Read `docs/bass-extension-waves/README.md` (binding charter) first,
then this file completely. Prereq: Wave 1 merged.

## Mission

Create the Bass Extension Profile artifact: schema, typed refusal
vocabulary, persistence, staleness evaluation, plus the read-only
observability skeleton (doctor check, `/state` section,
bass-management resolver field). **No runtime behavior, no CamillaDSP
imports, no HTTP handlers, no graph knowledge** — this wave makes the
profile a durable, evaluable fact and nothing more.

## Required reading (in order)

1. `docs/HANDOFF-bass-extension-plan.md` §4–5 (ownership, schema —
   read carefully; §5.1's JSON is the contract), §10.2–10.3.
2. `jasper/active_speaker/reconstruction_capability.py` — the typed
   fail-closed refusal pattern to mirror (small file, read fully).
3. `jasper/active_speaker/driver_safety.py` — ONLY the
   `evaluate_driver_safety_profile` status-ladder shape and the
   cabinet-block keys; skim the rest.
4. `jasper/active_speaker/baseline_profile.py` — ONLY
   `load_applied_baseline_profile_state` and
   `baseline_candidate_fingerprint` (what you bind to); skim.
5. `jasper/audio_measurement/evidence_identity.py` —
   `json_fingerprint`, `ArtifactIdentity` (reuse, do not reimplement).
6. `jasper/bass_management.py` (small; you add one additive field).
7. `jasper/control/state_aggregate.py` — one existing fail-soft
   section as the pattern (e.g. how `active_speaker_setup` is read).
8. `jasper/cli/doctor/audio.py` — one `@doctor_check` as exemplar,
   and `jasper/cli/doctor/_registry.py` for the decorator contract.

## Preflight facts

- Wave 1's `jasper.bass_extension.adapters.base.TargetSpec` and
  `jasper.bass_extension.targets.{MarginPolicy, MARGINS, AnchorPoint}`
  exist as specified in `wave-1-numerics.md`.
- `jasper.audio_measurement.evidence_identity.json_fingerprint` exists.
- `jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state`
  and `baseline_candidate_fingerprint` exist (names may have drifted —
  if so, STOP and report; do not guess a substitute).
- `jasper.bass_management.resolve_bass_management` returns a frozen
  `BassManagementState` dataclass.
- Locate the repo's atomic text-write helper (grep
  `atomic_write_text`; `design_draft.py` uses it). Use that helper;
  if it doesn't exist under that name, stop and report.

## File allowlist

Create:
- `jasper/bass_extension/profile.py`             (~350 lines)
- `tests/test_bass_extension_profile.py`
- `tests/test_bass_extension_state.py` — the `/state` section +
  doctor-check tests (repo convention is one small per-section state
  test file; there is no monolithic state-aggregate test file)

Modify (small, additive):
- `jasper/bass_management.py` — add `bass_extension` field to the
  state (default `None`), populated from `profile.py`'s summary
  reader; keep the resolver total/fail-soft.
- `jasper/control/state_aggregate.py` — add a `bass_extension`
  section (fail-soft null).
- `jasper/cli/doctor/audio.py` — add `check_bass_extension_profile`.
- `tests/test_bass_management.py` (exists — extend, don't fork).

## Frozen interface (`profile.py`)

```python
BASS_EXTENSION_PROFILE_KIND = "jts_bass_extension_profile"
BASS_EXTENSION_SCHEMA_VERSION = 1
BASS_EXTENSION_ALGORITHM_VERSION = "bass_extension_v1"
DEFAULT_PROFILE_PATH = Path("/var/lib/jasper/bass_extension_profile.json")
  # env override: JASPER_BASS_EXTENSION_PROFILE_STATE (mirror how
  # baseline_profile.py handles its override)

class BassExtensionRefusal(StrEnum):
    # exactly the plan §5.4 list, values prefixed "bass_extension_",
    # e.g. BASELINE_NOT_APPLIED = "bass_extension_baseline_not_applied"
    ...

@dataclass(frozen=True)
class BassExtensionProfile:
    # fields exactly mirroring plan §5.1 (profile_id, created_at,
    # algorithm_version, baseline_fingerprint, topology_id,
    # topology_fingerprint, bass_owner, enclosure, mic_calibration_id,
    # measurement_ids, natural, targets, anchors, margin,
    # digital_margin_db, clean_ceiling, sustain_test,
    # impedance_import, status)
    # targets/anchors use Wave 1's TargetSpec / AnchorPoint.
    # to_dict()/from_dict(): strict — reject unknown keys, wrong
    # kinds/versions, non-finite numbers; ValueError with a specific
    # message. profile_id = "bex-" + json_fingerprint(...)[:12] over
    # the content minus profile_id/created_at.

@dataclass(frozen=True)
class BassExtensionEvaluation:
    status: str          # missing | malformed | stale | accepted | bypassed
    refusals: tuple[BassExtensionRefusal, ...]
    profile: BassExtensionProfile | None
    detail: str

def save_bass_extension_profile(profile, path=None) -> None
    # atomic write, mode 0o640 (match baseline_profile's chmod/group
    # handling exactly — copy its approach, do not innovate)

def load_bass_extension_profile(path=None) -> BassExtensionProfile | None
    # None on absent; raises nothing — malformed returns None with
    # the malformed detail surfaced via evaluate (read file once)

def evaluate_bass_extension_profile(*, path=None, topology,
                                    applied_baseline_state) -> BassExtensionEvaluation
    # ladder: missing -> malformed -> stale -> accepted/bypassed.
    # stale iff any of: baseline fingerprint mismatch, topology
    # id/fingerprint mismatch, adapter_version != registered
    # adapter's, algorithm_version != current. Each mismatch appends
    # its typed refusal; status "stale" carries ALL applicable
    # refusals, not just the first.

def bass_extension_state_summary(path=None) -> dict | None
    # the small read-only dict for /state and bass_management:
    # {commissioned, status, profile_id, deepest_hz, natural_hz,
    #  margin, anchors: [{target_id, max_listening_level, evidence}]}
    # totally fail-soft: any exception -> None.
```

The evaluation takes `topology` and `applied_baseline_state` as
**arguments** — this module never loads them itself (host-mediated;
the doctor/state callers do their own reads exactly like they already
do for other checks).

## Doctor check contract

`check_bass_extension_profile` (one `CheckResult`): profile absent →
OK "bass extension: not commissioned"; malformed → FAIL naming the
parse detail; stale → WARN naming each mismatched binding; accepted →
OK with deepest/natural corners in the detail; bypassed → OK noting
bypass. File-level only — graph coherence checks arrive with Wave 3/5.

## `/state.bass_extension`

Wire `bass_extension_state_summary()` into the aggregate exactly like
the other fail-soft sections (null on any failure). No live camilla
reads in this wave.

## Tests (pinned coverage)

- Round-trip: build a full profile (use Wave 1 types for a sealed
  family), save/load/compare equal; `from_dict` rejects unknown key,
  wrong kind, wrong schema_version, NaN anchor, missing natural-last
  target (assert the §5.1 invariant: last target has empty filters
  and 0.0 boost).
- Staleness matrix: each binding mismatch independently yields
  `stale` + its specific refusal; multiple mismatches accumulate.
- Missing file → `missing`; garbage bytes → `malformed`; valid but
  `status: "bypassed"` → `bypassed`.
- `bass_extension_state_summary` returns None on unreadable path and
  a correct dict otherwise.
- Doctor check: one test per status → expected severity + message
  fragment.
- `/state` section: fail-soft null when the profile module raises
  (monkeypatch), populated dict otherwise.
- `bass_management`: state gains the field with default None; the
  existing resolver behavior is unchanged when no profile exists
  (extend the existing tests, don't rewrite them).

## Anti-overengineering fences

Do NOT build: profile migrations (schema v1 is the only version —
`from_dict` rejects others, that's the whole story); a generic
"artifact store"; history/versioning of profiles (recommissioning
overwrites; the evidence bundle — Wave 4 — is the archive); any
CamillaDSP/graph/HTTP/asyncio code; caching of loads (every caller
reads fresh — these are sub-millisecond files); new env vars beyond
the one path override; no changes to `reconstruction_capability.py`
or `driver_safety.py` (you mirror their patterns, you don't touch
them).

## Acceptance commands

```
.venv/bin/pytest tests/test_bass_extension_profile.py -q
.venv/bin/pytest tests/test_bass_management.py -q   # or the module's actual test file
scripts/test-fast
```
