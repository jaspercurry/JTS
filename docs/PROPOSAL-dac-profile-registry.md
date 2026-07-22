# Proposal: DAC Profile Registry

> **Status: proposal / implementation handoff, updated 2026-06-11.** The
> initial IO-free registry scaffold exists in
> [`jasper/audio_hardware/dac.py`](../jasper/audio_hardware/dac.py);
> `jasper.output_hardware` derives static output metadata and card-label
> matching from it, classifies registered single-device DACs through it, and
> `jasper.output_topology` consumes it for known DAC labels, physical output
> counts, clock-domain labels, and clock-domain contracts. Composite output
> profiles still need explicit live-observation and runtime-activation design;
> `kind="composite"` is not generic aggregate-output permission. This supersedes
> the narrower 2026-06-04 sketch that modeled only a single Apple dongle and a
> HiFiBerry DAC8x. Current operational truth for
> output ownership lives in
> [HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md),
> [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md), and
> [audio-paths.md](audio-paths.md).

## Summary

JTS is moving from "the Apple USB-C dongle is the DAC" toward explicit output
hardware profiles:

- `apple_usb_c_dongle`: one Apple USB-C adapter, two physical outputs.
- `hifiberry_dac8x` / DAC8x-family: one coherent multichannel DAC, eight
  physical outputs.
- `dual_apple_usb_c_dac_4ch`: two Apple USB-C adapters treated as one
  four-output composite profile for active crossover work.

The right product shape is a small data-driven DAC registry that describes
hardware identity, physical output shape, mixer/headphone policy, validation
expectations, and runtime constraints. Adding an ordinary single-device DAC
should normally add one profile plus detection/contract tests and any genuinely
new deploy artifact it needs, not scatter device-specific branches through
doctor, topology, ALSA rendering, outputd, commissioning, and docs.
Composite or aggregate output profiles are intentionally a higher bar: they
need explicit child identity/order rules, clock-domain evidence, runtime
activation gates, fail-closed partial states, and operator-visible diagnostics.

This registry must build on the current boundaries rather than replacing
them:

- `jasper.output_hardware` observes live hardware and can identify composite
  states such as dual Apple.
- `jasper.output_topology` owns user-facing speaker groups, physical output
  assignment, role identity, and safety evidence.
- `jasper-audio-hardware-reconcile` owns observed-hardware-to-runtime
  activation, including fail-closed parking and outputd env writes.
- `jasper-outputd` owns the final DAC write loop and speaker monitor/reference.

## Why This Is Needed

The repo still has many Apple-dongle-specific references: USB IDs, card names,
headphone mixer checks, udev rules, doctor messages, validation assumptions,
and topology defaults. That was acceptable when the Apple dongle was the only
supported output path. It does not scale once JTS supports DAC8x, DAC8x Studio,
dual Apple, and later other DACs or subwoofer-oriented output devices.

The failure mode is not only maintenance cost. It is safety and observability:
a DAC8x or dual-Apple system should not show false Apple-dongle failures, skip
the wrong mixer guard, render the wrong output alias, or imply a physical
output shape that does not match the hardware actually present.

## Current Building Blocks

Use these instead of creating a parallel hardware stack:

- `jasper.audio_hardware.dac` records static DAC profile metadata and pure
  lookup/output-count helpers. It does not probe hardware or mutate runtime
  state.
- `JASPER_AUDIO_DAC_ID` and `JASPER_AUDIO_DAC_CARD` are reconciler-owned runtime
  facts for the active final-output role.
- `/run/jasper-output-hardware/output_hardware.json` records observed output hardware,
  including composite dual-Apple readiness and partial states.
- `/var/lib/jasper/output_topology.json` records the operator's physical output
  mapping, speaker groups, role identity, and safety state.
- `/var/lib/jasper/outputd.env` selects outputd runtime mode, including
  `JASPER_OUTPUTD_SINK=dual_apple` and pinned child PCMs only after the active
  four-channel Camilla graph is loaded.
- `jasper-doctor` and `/state` should report both observed hardware and active
  runtime role when they differ.

## Target Contract

The registry should be an IO-light Python module, likely under
`jasper/audio_hardware/dac.py` or a similar `audio_hardware` package. Keep it
small and data-first.

```python
@dataclass(frozen=True)
class DacProfile:
    id: str
    label: str
    kind: Literal["single", "composite"]
    physical_output_count: int
    coherent_clock_domain: bool
    clock_domain_label: str
    clock_domain_contract: Literal[
        "single_device",
        "independent",
        "measured_sync_required",
    ]
    outputd_sink: str
    supported_card_matches: tuple[str, ...]
    connection: Literal["usb", "i2s"] = "usb"
    usb_ids: tuple[str, ...] = ()
    child_profile_ids: tuple[str, ...] = ()
    requires_same_usb_bus: bool = False
    supports_active_outputd_lane: bool = False
    active_outputd_lane_channels: int | None = None
    supports_active_crossover_commissioning: bool = False
    mixer_controls: tuple[MixerControl, ...] = ()
    headphone_pinned_100: bool = False
    validation_profile: str | None = None
    udev_rule: str | None = None
    dtoverlay: str | None = None
```

Initial profiles should include at least:

- `APPLE_USB_C_DONGLE`
- `HIFIBERRY_DAC8X`
- `HIFIBERRY_DAC8X_STUDIO` if the runtime treats Studio distinctly
- `DUAL_APPLE_USB_C_DAC_4CH`

`connection` is consumed by the hardware USB-role resolver. An I²S profile
must declare its registered `dtoverlay`; a USB profile cannot. That keeps the
registry IO-free while letting a Zero reserve its single OTG port for USB
output unless explicit, durable I²S configuration proves the port is free.

The dual-Apple profile is not just "two dongles in a list." It needs explicit
metadata:

- `kind="composite"`
- four physical outputs
- `clock_domain_contract="measured_sync_required"` rather than claiming native
  single-device coherence
- two Apple child devices
- same USB controller/bus requirement for Pi 5
- stable child ordering via saved topology/serial evidence
- partial-state behavior: observed profile can exist while runtime remains
  parked or single-Apple fallback
- active-output requirement: outputd dual sink is allowed only after the active
  four-channel Camilla graph is loaded

## Boundary Rules

The registry owns static capabilities and detection hints. It should not own
runtime mutation.

`output_hardware` owns live observation: what is plugged in, card IDs, serials,
USB bus/controller, endpoint sync mode, partial/ready status, and child PCM
facts. Registered single-device DACs should classify through the registry; do
not add a new `if dac_x:` branch for each ordinary DAC.

`output_topology` owns operator intent: which physical output goes to which
speaker driver, which identities are verified, and which safety gates are
complete.

`jasper-audio-hardware-reconcile` owns runtime activation: write env files,
render ALSA aliases, enable or disable Apple helper services, park output on
unknown/partial states, and defer dual Apple until graph evidence exists.

`jasper-outputd` owns final samples: single ALSA or dual Apple sink selection,
xrun handling, delay divergence behavior, and the speaker monitor/reference.

Do not put speaker-role mapping, Camilla config loading, udev side effects, or
outputd process control inside the registry.

## Migration Plan

1. Add the registry module and tests, with no behavior change. It should expose
   `by_id`, `all_profiles`, and pure helpers for validating known IDs and
   output counts. **Initial scaffold landed:** `jasper.audio_hardware.dac`
   includes Apple USB-C, HiFiBerry DAC8x-family, and dual-Apple 4ch profiles.
2. Replace duplicated labels/output counts in `output_hardware`,
   `output_topology`, and doctor with registry lookups. **Runtime metadata
   consumers landed:** `jasper.output_hardware` derives its supported-output
   count, label, and clock-label maps from the registry, and uses
   registry-owned `supported_card_matches` for `aplay`/sysfs card
   classification. `jasper.output_topology` now derives known DAC labels,
   physical output counts, clock-domain labels, and clock-domain report
   branching from the same registry while retaining its no-audio authority
   boundary. It also reports composite-profile shape separately from
   aggregate-output runtime enablement. `jasper-doctor` now consumes output
   hardware state for Apple checks; a broader registry-only cleanup can remain
   incremental.
3. Replace hardcoded Apple/DAC8x identity checks in `audio_validation` and
   `jasper-doctor` with profile-derived expectations.
4. Move mixer/headphone policy into profile data, but keep mutation in
   `jasper-dac-init` and `jasper-headphone-monitor`.
5. Teach `output_hardware` to emit profile IDs from the registry, including
   composite dual-Apple states. **Single-device path landed:** `output_hardware`
   still owns live probing and composite classification, but its static
   vocabulary, single-device card matching, and registered single-device
   classification now derive from `jasper.audio_hardware.dac`. Composite
   profiles still require explicit observation/activation logic; adding
   `kind="composite"` to the registry alone must not route output.
6. Keep `jasper-audio-hardware-reconcile` as the runtime owner, but have it
   consume profile metadata rather than duplicating every device string. The
   remaining bash-side matching is intentionally small, covered by drift-guard
   tests, and should not grow new hardware vocabulary without a matching
   registry update.
7. Burn down remaining hardcoded Apple/DAC8x references only when each consumer
   moves to the profile boundary. Do not do a broad mechanical rewrite without
   tests.

Each phase should be independently shippable and hardware-free tests should
cover Apple, DAC8x, and dual-Apple shapes. Pi/hardware validation is still
required for deploy, ALSA, mixer, and outputd behavior.

## Design Principles

- Keep the registry simple: data plus pure lookup helpers.
- Do not model future DACs that do not exist yet.
- Treat composite devices as first-class profiles, not exceptions, but do not
  generalize them into implicit aggregate-output support.
- Preserve fail-closed behavior for partial, unknown, or unsafe states.
- Expose observed vs runtime role clearly. A system can observe dual Apple but
  safely run the single-Apple output path until the active graph is ready.
- Keep hardware safety in the profile data, but keep actual safety enforcement
  in the existing runtime owners.

## Response For Agent Review

This section is intentionally appended as review guidance for the next agent.

The original PR 454 proposal was directionally correct, but too narrow for the
current architecture. Do not implement the two-profile, env-only version
verbatim. The production-grade version must include dual Apple as a composite
profile and must respect the existing observed-hardware, topology, reconcile,
and outputd boundaries.

Specific follow-up from review:

- Keep the proposal in README/doc-map. That is already handled by this update
  in branches that include this file; do not leave a TODO saying to add it.
- Update any implementation plan that says only `APPLE_DONGLE` and
  `HIFIBERRY_DAC8X`. The minimum set now includes dual Apple, and may include
  DAC8x Studio if runtime behavior distinguishes it.
- A DAC profile registry is useful, but it is not the runtime graph owner.
  `jasper-audio-hardware-reconcile` still decides whether outputd may switch to
  dual Apple based on both live hardware and active Camilla graph evidence.
- Ordinary single-device DAC classification is now registry-driven. Future
  single-device DACs should add profile data plus tests, not another
  `output_hardware.classify_output_cards` branch. Future composite DACs still
  need deliberate child identity, clock-domain, activation, fail-closed, and
  observability design before runtime routing is safe.
- The first implementation PR should be boring: add profile data and pure tests,
  then move one low-risk consumer such as labels/output counts. Do not combine
  registry creation with outputd, Camilla, or udev behavior changes.
- The UI should eventually present dual Apple as a normal four-output DAC when
  observed and graph-ready, while still warning on bad physical topology or
  partial hardware states.

Last verified: 2026-07-15 (DAC8x-only active-crossover commissioning launch
capability rechecked; `connection`/`dtoverlay` role contract rechecked;
prior 2026-06-11 registered single-device classification, registry
consumers, and remaining bash drift guards rechecked against the dual-Apple
active-output architecture).
