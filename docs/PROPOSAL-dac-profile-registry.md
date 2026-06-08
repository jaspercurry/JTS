# Proposal: a DAC profile registry (make "add a DAC" one entry, not a 20-file edit)

> **Status: proposal / design hand-off (2026-06-04).** This is a scoped
> design for a *future* session to implement, not shipped behavior. It
> builds on the in-flight `dac_id` work (codex audio PRs #447–#452) —
> **coordinate with whoever is driving that** before starting; this doc
> deliberately extends their direction rather than forking a parallel one.
> Current operational truth for audio output lives in
> [HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md)
> and [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md).

## TL;DR

JTS hardcodes the **Apple USB-C dongle** as *the* DAC in ~151 places across
20 files. Supporting a second DAC (the HifiBerry **DAC8x** on the JTS3 lab
unit; future DACs) today means editing all of them, and getting it wrong is
silent (e.g. `jasper-doctor` reports two false "failures" on a DAC8x Pi
because its dongle checks don't apply). The fix is the same pattern the repo
already uses for transit providers and wake models: a **data-driven DAC
profile registry** where adding a DAC is *one `DacProfile` entry*, and every
audio surface reads the active profile instead of assuming the dongle.

The goal is **not** a grand abstraction. It is: pick the smallest durable
shape that lets "add the Nth DAC" be a registry entry, and migrate the
existing hardcoding into it **incrementally and non-breakingly**, keeping the
Apple dongle as just the first profile.

## Why now

- **Stated roadmap:** more DACs (and the identical story for mics — the
  XVF3800 is hardcoded the same way; this proposal is the template for a
  parallel `MicProfile` registry).
- **Concrete symptom:** deploying current `main` to the DAC8x unit (jts3)
  makes `jasper-doctor` emit `check_apple_dongle_audio` failures that are
  meaningless on that hardware. The doctor has *started* gating on `dac_id`
  (`doctor.py` ~`if dac_id != "apple_usb_c_dongle": ... ok`) — proof the seam
  is needed and emerging, but only partially threaded.
- **Tax:** ~151 dongle references (`grep -riE 'apple dongle|05ac:110a|usb-c to 3.5|dongle' jasper/ deploy/`)
  across `config.py`, `audio_io.py`, `output_topology.py`, `correction/*`,
  `doctor.py`, `cues/generator.py`, `install.sh`, the `jasper-dac-init`
  service + script, `udev/99-jasper-apple-dongle.rules`,
  `jasper-voice.service`, `shairport-sync.conf.template`, `camilladsp/v1.yml`.

## What already exists (build on this, don't replace it)

The codex audio-validation work has introduced the *beginning* of the
abstraction — credit it and extend it:

- A **`dac_id`** identity concept with real values: `apple_usb_c_dongle`,
  `hifiberry_dac8x` (`audio_validation.py:48` `DAC8X_DAC_ID`).
- **Per-DAC validation profiles** (`DAC8X_OUTPUTD_STABILITY_PROFILE`), a
  `_dac_identity_check(expected_id=...)`, and validation artifacts that carry
  `dac_id`.
- Overrides: **`JASPER_AUDIO_DAC_ID`** (`audio_validation.py:720`) and
  **`JASPER_AUDIO_DAC_CARD`** (`output_topology.py:608`).
- `jasper-doctor` gating its dongle check on `dac_id`.

What's missing: this `dac_id` is **threaded through validation + doctor only**,
there is **no single module that owns a DAC's full definition**, and the rest
of the stack (ALSA I/O, mixer init, deploy, correction) still assumes the
dongle. So `dac_id` is a string passed around, not yet a *profile* the system
resolves once and reads everywhere.

## Target contract

A `jasper/audio_hardware/dac.py` (name TBD) with a frozen-dataclass registry,
mirroring [`jasper/transit/__init__.py`](../jasper/transit/__init__.py)'s
`REGISTRY` and [`jasper/wake_models.py`](../jasper/wake_models.py)'s
`WakeModelEntry`:

```python
@dataclass(frozen=True)
class DacProfile:
    id: str                       # "apple_usb_c_dongle", "hifiberry_dac8x"
    label: str                    # human/UI name
    # Identity / detection — how to recognize this DAC at runtime:
    usb_id: str | None            # "05ac:110a" for the dongle; None for HAT DACs
    alsa_card_match: tuple[str, ...]  # card short-names ("A"; "sndrpihifiberry")
    # Output topology:
    output_pcm: str               # the ALSA pcm the renderer/outputd targets
    # Mixer init (jasper-dac-init owns this; some DACs have a fixed-gain
    # analog ceiling pinned at 100%, others expose a hardware volume):
    mixer_controls: tuple[MixerControl, ...]
    headphone_pinned_100: bool    # dongle=True (software never touches it)
    # Safety + calibration:
    safe_start_main_gain_db: float
    validation_profile: str | None   # the existing audio_validation profile id
    # Deploy:
    needs_dtoverlay: str | None      # HAT DACs need a config.txt dtoverlay
    udev_rule: str | None
```

Plus:

```python
DAC_PROFILES: tuple[DacProfile, ...] = (APPLE_DONGLE, HIFIBERRY_DAC8X)

def by_id(dac_id: str) -> DacProfile | None: ...
def resolve_active(env) -> DacProfile:
    """JASPER_AUDIO_DAC_ID override → else detect from present ALSA cards /
    USB ids → else the documented default. One resolver, used everywhere."""
```

**One resolver, read everywhere.** Every current hardcoded site becomes
"resolve the active `DacProfile`, read the field it needs." Adding a DAC =
one `DacProfile` literal + (rarely) one deploy artifact it references.

## Migration plan (incremental, each phase shippable, non-breaking)

The order matters: consolidate the *identity* first so nothing regresses,
then migrate consumers layer by layer. The Apple dongle stays the default
throughout, so existing single-DAC installs never change behavior.

- **Phase 0 — Registry + resolver, dongle-only.** Create the module with the
  two profiles and `resolve_active`. Re-express the existing `dac_id` /
  `JASPER_AUDIO_DAC_ID` / `JASPER_AUDIO_DAC_CARD` logic in terms of it. No
  consumer changes yet → pure addition, behavior-identical. Ship + test.
- **Phase 1 — Doctor.** Replace `check_apple_dongle_audio`'s hardcoding with
  "resolve profile; run the profile's identity/mixer checks." A DAC8x Pi gets
  *DAC8x* checks, not skipped dongle checks. (This finishes what the `dac_id`
  gating started.) This alone fixes the jts3 false-failures.
- **Phase 2 — Mixer init.** `jasper-dac-init` + `headphone-monitor` read
  `mixer_controls` / `headphone_pinned_100` from the profile instead of the
  dongle-specific `amixer -c A sget Headphone` path.
- **Phase 3 — Output I/O + topology.** `audio_io.py`, `output_topology.py`,
  renderer device resolution read `output_pcm` from the profile.
- **Phase 4 — Deploy.** `install.sh` / udev / systemd select the profile's
  `udev_rule` / `needs_dtoverlay`. Generalize
  `udev/99-jasper-apple-dongle.rules`.
- **Phase 5 — Correction + cues.** Anything assuming dongle gain/topology.

Each phase: convert a layer, delete the dongle-specific branch, add a test
asserting both profiles resolve correctly. `grep -c` of the dongle refs is the
burn-down metric (151 → 0).

## Design principles (avoid astronaut engineering)

- **Smallest durable shape.** Two real DACs justify the registry now (the
  review's bar: "does adding the Nth DAC mean one entry or many files?").
  Don't model DACs that don't exist; add fields when a second DAC needs them.
- **Mirror existing patterns.** `transit.REGISTRY` (data-driven, IO-free
  helpers) and `wake_models.WakeModelEntry` are the proven in-repo template —
  same frozen-dataclass + `by_id` + `resolve` shape. A contributor who has
  added a transit provider already knows this pattern.
- **The Apple dongle is just `DAC_PROFILES[0]`.** No special-casing survives;
  if the dongle needs a quirk, it's a field on its profile.
- **Hardware-safety stays first-class.** The dongle's "headphone pinned at
  100%, software volume only via CamillaDSP master_gain" invariant (see the
  memory + `jasper-dac-init`) becomes `headphone_pinned_100` — preserved, not
  lost, and other DACs declare their own ceiling.
- **Non-breaking.** Default resolves to the dongle; existing installs are
  byte-identical until they set `JASPER_AUDIO_DAC_ID`.

## Concrete first PR (for the implementing session)

1. Add `jasper/audio_hardware/dac.py` with `DacProfile`, the two literals
   (`apple_usb_c_dongle`, `hifiberry_dac8x` — copy values from the existing
   `audio_validation.py` constants + the dongle udev/init scripts), `by_id`,
   `resolve_active`.
2. Hardware-free tests: both ids resolve; `JASPER_AUDIO_DAC_ID` override wins;
   detection from a fake ALSA card list picks the right profile; unknown id →
   documented default.
3. Re-point `audio_validation.py`'s `dac_id` resolution at `resolve_active`
   (Phase 0 consumer swap — small, behavior-identical, proves the seam).
4. Leave Phases 1–5 as follow-ups; link this doc from the PR.

## Coordinate / open questions

- **Who owns the in-flight `dac_id` work?** Align on the module name + field
  set before Phase 0 so this lands *with* their direction.
- **Mic registry** is the identical shape for the XVF3800 → other mics; do DAC
  first as the template, then mirror it for mics.
- Add this doc to the README documentation atlas and `docs/doc-map.toml`
  (`aec-and-mic` / a new `audio-hardware` subsystem) when the first PR lands.

---
Last verified: 2026-06-04 (proposal; reflects `main` at the time of the
2026-06-04 staff review — `dac_id` is emerging in `audio_validation.py` +
`doctor.py`, not yet a consolidated registry).
