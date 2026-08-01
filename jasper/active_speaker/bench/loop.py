# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The offline emit loop: emit ×2 → derive ×2 → render → extract → grade.

**Stage: VERIFY (offline orchestration).** This module is the only place the
pieces meet. It owns the stimulus, the work-directory layout, the order of
operations, and the typed report; it owns no config semantics (that is
:mod:`~jasper.active_speaker.bench.derivation`), no signal math (that is
:mod:`~jasper.active_speaker.bench.compare`), no binary identity or subprocess
shape (that is :mod:`jasper.bass_extension.bench.render`), and no verdict
vocabulary (that is :mod:`jasper.active_speaker.delta_probe`).

**Units.** Frequencies in hertz; levels in decibels (peaks in dBFS relative to
1.0 full scale); durations in seconds; channel indices zero-based.

The run, in order
-----------------
1. Emit the preset twice through the REAL emitter — once carrying the
   linearization under test, once carrying none. Everything else is identical,
   so the two configs differ only in the filters being graded.
2. Generate one stimulus: silence, a synchronized swept sine, silence. Written
   once and captured by both arms, so the excitation cancels out of the A/B
   along with everything else the two configs share.
3. Derive both into offline-renderable configs and refuse on any admission
   failure before a render starts.
4. Render each through the pinned binary with a determinism receipt — the A/B
   difference is only a measurement if each render is reproducible, and that is
   an assumption worth proving rather than assuming.
5. Per branch: extract its channel from each arm's interleaved output, bound the
   always-on soft clip's contribution, deconvolve both against one window taken
   from the control arm, and grade the difference against the fit's own claim.

:func:`plan_emit_loop` is steps 1 and 3 on their own — everything reachable
without a binary — so the CLI's ``--dry-run`` proves the same emit and
derivation refusals on a laptop that the full run would hit on the Pi.

**No live graph is touched.** Nothing here loads, applies, or reloads a config
on a running CamillaDSP, plays audio through a device, or writes outside the
caller's work directory. Every render is a one-shot batch pass over files.

**This process is not bounded; the renders are.** The rlimits apply to the
render children. The deconvolution and its FFTs run here, in the caller's own
process: a production-length run measures **221 MiB parent-only peak RSS**
(``getrusage(RUSAGE_SELF)``, 10 s sweep, 2-way mono; an independent measurement
during review put the same run at 235 MiB). That is a real fraction of a 1 GB
Pi, which is why the documented Pi invocation goes through
``scripts/pi-run-diagnostic.sh`` rather than being run bare. The dominant term
is the deconvolution's zero-padded FFT, so it scales with sweep length, not with
the number of branches.

What only a real run can prove
------------------------------
The renderer is the subject. A stand-in renderer in a test can prove this
module wires the legs together and that the comparison catches a mis-realized
filter, but it cannot prove that **CamillaDSP** realizes an emitted biquad the
way the fit models it — that is precisely the question, and only the pinned
binary answers it. On a host with no ``jasper-camilla.service`` the CLI's
``--dry-run`` produces the emitted and derived configs and stops.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.active_speaker.camilla_yaml import emit_active_speaker_baseline_config
from jasper.active_speaker.linearization_fit import (
    LinearizationFilter,
    complex_correction_response,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset, required_driver_roles
from jasper.audio_measurement.deconv import DEFAULT_MAX_CAPTURE_SECONDS
from jasper.audio_measurement.sweep import (
    SweepMeta,
    synchronized_sweep_metadata,
    synchronized_swept_sine,
)
from jasper.bass_extension.bench.derivation import ArtifactHeader
from jasper.bass_extension.bench.render import (
    DEPLOYED_PROCESSING_PRECISION,
    BinaryIdentity,
    RenderBounds,
    RenderError,
    check_free_space,
    estimate_render_bytes,
    render_with_determinism_receipt,
)
from jasper.log_event import log_event

from .compare import (
    SOFT_CLIP_BUDGET_DB,
    BranchComparison,
    EmitComparisonError,
    analysis_band_hz,
    compare_branch,
    decode_render_channel,
    deconvolved_ir,
    magnitude_fft_length,
    shared_arrival_window,
    soft_clip_error_bound_db,
    windowed_magnitude_db,
)
from .derivation import (
    DerivedRenderConfig,
    DeviceGeometry,
    EmitDerivationError,
    derive_offline_render_config,
    device_geometry,
)

logger = logging.getLogger(__name__)

#: Peak level of the offline stimulus, dBFS.
#:
#: Chosen against :data:`~jasper.active_speaker.bench.compare.
#: SOFT_CLIP_BUDGET_DB`, not by habit. CamillaDSP's soft clip compresses the
#: fundamental by ``20·log10(1 − a²/9)`` where ``a`` is the peak relative to the
#: branch limiter's ``clip_limit`` (0.05 dB of compression is reached at
#: ``a ≈ 0.227``). At the emitter's own baseline clip limit of −1 dBFS, a
#: −24 dBFS stimulus puts a branch at ``a ≤ 0.071`` — about 0.005 dB of
#: compression, an order of magnitude inside the budget, with the crossover and
#: the split mixer only ever lowering it further. The measurement's usual
#: −12 dBFS
#: (:data:`jasper.audio_measurement.excitation.
#: AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS`) would sit essentially AT the
#: budget, which is why this bench does not inherit it. Nothing acoustic is at
#: stake in the choice: no device is opened and the deconvolution's per-bin
#: processing gain leaves 16-bit quantization far below the residuals of
#: interest.
OFFLINE_STIMULUS_PEAK_DBFS: float = -24.0

#: Silence before the sweep, seconds. Puts the recovered impulse response at a
#: positive delay so the analysis window's pre-arrival span
#: (:data:`~jasper.active_speaker.bench.compare.ARRIVAL_PRE_MS`) has somewhere
#: to sit instead of clamping at index 0 and putting a step in the window. It
#: must stay wider than that span; a test pins the relationship.
STIMULUS_LEAD_IN_S: float = 0.3

#: Silence after the sweep, seconds. Two jobs: it gives the chain's own decay
#: somewhere to land, and it guarantees the render is at least as long as the
#: sweep — CamillaDSP processes in whole ``chunksize`` blocks, so a stimulus
#: that ended exactly at the sweep could come back a fraction of a chunk short
#: and the deconvolution would refuse it.
STIMULUS_TAIL_S: float = 1.0

#: Nominal sweep length, seconds. The generator rounds it to Novak's
#: synchronization condition.
STIMULUS_SWEEP_SECONDS: float = 10.0

#: Longest whole stimulus (lead-in + sweep + tail) this bench will build.
#:
#: The deconvolution caps its FFT input at
#: :data:`jasper.audio_measurement.deconv.DEFAULT_MAX_CAPTURE_SECONDS` to bound
#: the working set on a 1 GB Pi, and past that cap it TRUNCATES — with a
#: warning, but it still returns a curve. A truncated arm would be graded as a
#: measurement, so this refuses instead. The default stimulus sits at about
#: 11.3 s, a third of the way to the bound.
MAX_STIMULUS_SECONDS: float = DEFAULT_MAX_CAPTURE_SECONDS

#: The ``--gain`` every render carries, dB.
#:
#: A candidate config is not a running graph, so unlike the sibling bench there
#: is no recorded fader reading to reproduce — 0 dB is the neutral value. What
#: makes the choice safe rather than merely conventional is that it is
#: IDENTICAL across the A/B: whatever the fader does, it does to both arms and
#: cancels out of their difference.
RENDER_FADER_DB: float = 0.0

#: Process-local bounds for each render.
#:
#: A bound, not a measurement. A batch pass over a ~11 s file is small and
#: quick, and these are set well clear of that so a legitimate render never
#: trips them while a runaway still stops. The address-space figure is the one
#: to revisit if the first on-device run refuses: a Rust binary's reserved
#: address space is not its resident set, and this has not been observed
#: against the real binary.
DEFAULT_RENDER_BOUNDS = RenderBounds(
    timeout_s=300.0,
    rlimit_as_bytes=768 * 1024 * 1024,
    rlimit_cpu_s=300,
    nice=10,
)

#: Determinism PAIRS a run performs — two arms, each rendered twice.
#:
#: The free-space guard is sized in pairs, not renders, because
#: :func:`~jasper.bass_extension.bench.render.estimate_render_bytes` already
#: carries a x2 for exactly one such pair being on disk at once. 2 pairs x 2 =
#: the four ``.raw`` files a completed run retains (both arms' first render AND
#: both repeats, all four kept as evidence — the repeats' SHA-256 is what the
#: determinism receipt asserts against, so a reviewer can re-verify it).
RENDER_PAIRS_PER_RUN: int = 2


class EmitLoopError(RuntimeError):
    """The emit loop refused. Every failure here is a refusal, never a grade."""


@dataclass(frozen=True, slots=True)
class RenderArm:
    """One arm of the A/B: its config text, its derivation, and its render."""

    name: str
    config_path: Path
    output_path: Path
    derived: DerivedRenderConfig
    determinism_receipt: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "config_path": str(self.config_path),
            "output_path": str(self.output_path),
            "derivation_receipt": self.derived.receipt,
            "determinism": self.determinism_receipt,
        }


#: Every graded branch matched, and at least one branch was graded.
OUTCOME_MATCHED = "matched"
#: At least one graded branch did not match. The finding.
OUTCOME_MISMATCHED = "mismatched"
#: No branch reached a verdict. **Not a pass**, and not a failure either.
OUTCOME_UNAVAILABLE = "unavailable"

#: Every outcome a run can report. Pinned by a test.
EMIT_LOOP_OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_MATCHED, OUTCOME_MISMATCHED, OUTCOME_UNAVAILABLE}
)


@dataclass(frozen=True)
class EmitLoopReport:
    """One offline emit-loop run: what was rendered, and how it graded.

    ``branches`` carries one :class:`~jasper.active_speaker.bench.compare.
    BranchComparison` per driver role, in preset order.

    **The outcome is three-state, because the evidence is.** A role the fit left
    alone commands nothing, so its comparison has nothing to verify and
    :func:`jasper.active_speaker.delta_probe.classify_delta_probe` returns
    ``unavailable`` for it — which that module is explicit is "not a pass … no
    evidence to refuse on, and no permission granted either". Folding those
    branches into a two-state pass/fail breaks that doctrine in whichever
    direction it picks: counted as failures, an ordinary tweeter-only
    correction reports a mismatch on a woofer measured across 10,000 clean bins
    at 0.000 dB of error; counted as passes, a run that graded nothing at all
    reports success. So:

    * :data:`OUTCOME_MISMATCHED` — a graded branch did not match. The finding.
    * :data:`OUTCOME_MATCHED` — every graded branch matched, and at least one
      was graded. Ungraded branches are disclosed, never silently counted.
    * :data:`OUTCOME_UNAVAILABLE` — nothing reached a verdict. No claim either
      way, and it must not read as a pass.

    :attr:`matched` is kept as the narrow "outcome is matched" predicate so a
    caller cannot accidentally read a two-state answer out of a three-state
    fact.
    """

    branches: tuple[BranchComparison, ...]
    treated: RenderArm
    control: RenderArm
    binary: dict[str, Any]
    stimulus: dict[str, Any]
    expected_offset_db: float

    @property
    def graded_branches(self) -> tuple[BranchComparison, ...]:
        """Branches a verdict was reached for."""

        return tuple(branch for branch in self.branches if branch.graded)

    @property
    def unavailable_branches(self) -> tuple[BranchComparison, ...]:
        """Branches no verdict was reached for — disclosed, never counted."""

        return tuple(branch for branch in self.branches if not branch.graded)

    @property
    def mismatched_branches(self) -> tuple[BranchComparison, ...]:
        return tuple(
            branch for branch in self.graded_branches if not branch.matched
        )

    @property
    def outcome(self) -> str:
        if self.mismatched_branches:
            return OUTCOME_MISMATCHED
        if self.graded_branches:
            return OUTCOME_MATCHED
        return OUTCOME_UNAVAILABLE

    @property
    def matched(self) -> bool:
        """Every graded branch matched and at least one was graded.

        NOT the negation of "something is wrong" — see :attr:`outcome`.
        """

        return self.outcome == OUTCOME_MATCHED

    @property
    def worst_branch(self) -> BranchComparison | None:
        finite = [b for b in self.branches if math.isfinite(b.band_max_error_db)]
        if not finite:
            return None
        return max(finite, key=lambda b: b.band_max_error_db)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "matched": self.matched,
            "graded_roles": [b.role for b in self.graded_branches],
            "unavailable_roles": [b.role for b in self.unavailable_branches],
            "mismatched_roles": [b.role for b in self.mismatched_branches],
            "expected_offset_db": self.expected_offset_db,
            "binary": self.binary,
            "stimulus": self.stimulus,
            "treated": self.treated.to_dict(),
            "control": self.control.to_dict(),
            "branches": [branch.to_dict() for branch in self.branches],
        }


def _write_stimulus_wav(
    path: Path, sweep: np.ndarray, *, sample_rate: int, channels: int
) -> ArtifactHeader:
    """Write ``[lead-in, sweep, tail]`` replicated across ``channels`` as S16 PCM.

    The capture device's channel count comes from this file's own header
    (``CaptureDeviceWavFile`` declares no ``channels``), so the replication is
    what makes the file match the graph the emitter wrote. Identical channels
    are also what the split mixer's mono sum expects, and they carry the most
    signal into every branch.

    16-bit, matching the artifact geometry
    :class:`~jasper.bass_extension.bench.derivation.ArtifactHeader` admits.
    """

    from scipy.io import wavfile

    if channels < 1:
        raise EmitLoopError("capture channel count must be at least 1")
    lead = np.zeros(int(round(STIMULUS_LEAD_IN_S * sample_rate)), dtype=np.float64)
    tail = np.zeros(int(round(STIMULUS_TAIL_S * sample_rate)), dtype=np.float64)
    mono = np.concatenate([lead, np.asarray(sweep, dtype=np.float64), tail])
    frame = np.repeat(mono[:, None], channels, axis=1)
    wavfile.write(
        str(path), sample_rate, (np.clip(frame, -1.0, 1.0) * 32767.0).astype(np.int16)
    )
    return ArtifactHeader(
        sample_rate_hz=int(sample_rate), channels=int(channels), bits_per_sample=16
    )


def _claimed_delta_db(
    filters: Sequence[Mapping[str, Any]], freqs_hz: np.ndarray
) -> np.ndarray:
    """The magnitude, dB, the FIT claims its filters apply across ``freqs_hz``.

    Built from the fit's own records through
    :func:`~jasper.active_speaker.linearization_fit.complex_correction_response`
    — the same evaluator the conductor's prediction, the emitter's headroom
    charge, and the runtime contract's proof all share — so the claim graded
    here is the claim the rest of the flow made, not a restatement of it.

    An empty filter list claims unity (0 dB everywhere), which is the honest
    reading of "this role was not corrected".
    """

    if not filters:
        return np.zeros_like(np.asarray(freqs_hz, dtype=np.float64))
    records = [
        LinearizationFilter(
            biquad_type=str(entry["biquad_type"]),
            freq=float(entry["freq"]),
            q=float(entry["q"]),
            gain=float(entry["gain"]),
        )
        for entry in filters
    ]
    response = complex_correction_response(records, np.asarray(freqs_hz, dtype=np.float64))
    return 20.0 * np.log10(np.maximum(np.abs(response), 1e-12))


def _derive(
    name: str,
    emitted_text: str,
    *,
    roles: Sequence[str],
    stimulus_path: Path,
    capture_header: ArtifactHeader,
    playback_path: Path,
) -> DerivedRenderConfig:
    try:
        return derive_offline_render_config(
            emitted_text,
            roles=roles,
            capture_filename=str(stimulus_path),
            capture_header=capture_header,
            playback_filename=str(playback_path),
            processing_precision=DEPLOYED_PROCESSING_PRECISION,
        )
    except EmitDerivationError as exc:
        log_event(logger, "emit_loop.refused", arm=name, stage="derive", reason=str(exc))
        raise EmitLoopError(f"{name} arm could not be derived: {exc}") from exc


@dataclass(frozen=True)
class EmitLoopPlan:
    """Everything a run needs, built and proved WITHOUT rendering anything.

    The shared front half of :func:`run_emit_loop` and the CLI's ``--dry-run``.
    Both arms are emitted through the real emitter and derived through the real
    derivation, and both derived configs are written to ``work_dir`` — so every
    refusal that does not require a binary (an emitter validation refusal, a
    stage outside the offline allowlist, a hard-clip limiter, a stimulus past
    the FFT cap) surfaces here. That is what makes ``--dry-run`` a preflight
    rather than an echo of the arguments: it catches on a laptop what would
    otherwise be discovered on the Pi.

    ``stimulus_path`` is the file the real run WILL write. Planning does not
    write it — the derivation validates the header it is handed, never the
    file — so a dry run leaves no multi-megabyte WAV behind.
    """

    roles: tuple[str, ...]
    work_dir: Path
    treated: DerivedRenderConfig
    control: DerivedRenderConfig
    geometry: DeviceGeometry
    sweep_meta: SweepMeta
    capture_header: ArtifactHeader
    stimulus_path: Path
    stimulus_seconds: float
    expected_offset_db: float

    @property
    def config_paths(self) -> tuple[Path, Path]:
        """The two derived configs, written to disk by :func:`plan_emit_loop`."""

        return (self.work_dir / "control.yml", self.work_dir / "treated.yml")


def plan_emit_loop(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    linearization: Mapping[str, Sequence[Mapping[str, Any]]],
    work_dir: Path,
    sweep_seconds: float = STIMULUS_SWEEP_SECONDS,
    emit_kwargs: Mapping[str, Any] | None = None,
) -> EmitLoopPlan:
    """Emit both arms, derive both, write both configs. No binary, no render.

    Raises:
      EmitLoopError: on any refusal reachable without a binary.
    """

    extra = dict(emit_kwargs or {})
    if "linearization" in extra:
        raise EmitLoopError(
            "emit_kwargs must not carry 'linearization' — the loop sets it per "
            "arm, and a caller-supplied one would make the two arms differ by "
            "something other than the filters under test"
        )
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    roles = required_driver_roles(preset.way_count)

    treated_text = emit_active_speaker_baseline_config(
        preset,
        playback_device=playback_device,
        linearization=dict(linearization),
        **extra,
    )
    control_text = emit_active_speaker_baseline_config(
        preset, playback_device=playback_device, linearization=None, **extra
    )

    geometry = device_geometry(control_text)
    meta = synchronized_sweep_metadata(
        duration_approx_s=float(sweep_seconds),
        sample_rate=geometry.sample_rate_hz,
        amplitude_dbfs=OFFLINE_STIMULUS_PEAK_DBFS,
    )
    stimulus_seconds = STIMULUS_LEAD_IN_S + meta.duration_s + STIMULUS_TAIL_S
    if stimulus_seconds > MAX_STIMULUS_SECONDS:
        raise EmitLoopError(
            f"a {stimulus_seconds:.1f} s stimulus is past the deconvolution's "
            f"{MAX_STIMULUS_SECONDS:.0f} s FFT cap, which truncates rather than "
            "refuses — a truncated arm would be graded as a measurement. Shorten "
            "the sweep."
        )

    stimulus_path = work_dir / "stimulus.wav"
    capture_header = ArtifactHeader(
        sample_rate_hz=geometry.sample_rate_hz,
        channels=geometry.capture_channels,
        bits_per_sample=16,
    )
    arms: dict[str, DerivedRenderConfig] = {}
    for name, text in (("control", control_text), ("treated", treated_text)):
        derived = _derive(
            name,
            text,
            roles=roles,
            stimulus_path=stimulus_path,
            capture_header=capture_header,
            playback_path=work_dir / f"{name}.raw",
        )
        (work_dir / f"{name}.yml").write_text(derived.yaml_text, encoding="utf-8")
        arms[name] = derived

    return EmitLoopPlan(
        roles=tuple(roles),
        work_dir=work_dir,
        treated=arms["treated"],
        control=arms["control"],
        geometry=geometry,
        sweep_meta=meta,
        capture_header=capture_header,
        stimulus_path=stimulus_path,
        stimulus_seconds=stimulus_seconds,
        expected_offset_db=(
            arms["treated"].program_headroom_db - arms["control"].program_headroom_db
        ),
    )


def _render_arm(
    name: str,
    plan: EmitLoopPlan,
    derived: DerivedRenderConfig,
    *,
    binary: BinaryIdentity,
    bounds: RenderBounds,
) -> RenderArm:
    """Render one arm twice, proving the render repeats byte-for-byte.

    ONE derivation — ``derived``, built by :func:`plan_emit_loop` and written to
    ``<name>.yml`` — rendered twice by
    :func:`~jasper.bass_extension.bench.render.render_with_determinism_receipt`,
    the same helper the sibling bass-extension campaign renders through, so the
    determinism proof has one implementation rather than two that can drift.

    **Why ONE config and not two.** R8 defines a *shape* as the exact byte
    content of a derived config, keyed by its SHA-256, and asks that each shape
    be rendered twice. An earlier version of this function derived the emitted
    text a second time so the repeat could write somewhere else — a CamillaDSP
    render's destination is not an argument, it is
    ``devices.playback.filename`` INSIDE the config, so a second destination
    meant a second config. Those two configs differed by that one line, which
    made them two distinct shapes with two distinct SHA-256s: the receipt
    recorded the first shape's hash and the first config's argv while the second
    render consumed the OTHER shape, so it asserted that shape X repeats on
    evidence that included a render of shape Y. Rendering one config twice is
    what actually satisfies R8 — and it, not the move-aside below, is the whole
    of the improvement.

    Both renders therefore write to the single ``<name>.raw`` that one config
    declares, and the helper moves each output aside — to ``<name>.first.raw``,
    then ``<name>.repeat.raw``. That move PRESERVES render 1's bytes so the two
    can be compared at all; it proves nothing on its own, because
    :func:`~jasper.bass_extension.bench.render.render_config` already unlinks
    its destination before every render, so the destination is absent when
    either render starts and was equally absent under the two-config design.
    Same binary, same stimulus, same graph, same config bytes: byte-identical
    output, or the instrument does not repeat and nothing measured with it is a
    measurement.

    :attr:`RenderArm.output_path` is therefore the preserved FIRST render,
    ``<name>.first.raw``; the declared ``<name>.raw`` has been moved away by the
    time this returns and does not exist on a completed run.

    The helper raises :class:`~jasper.bass_extension.bench.render.RenderError`
    for a guard failure, a failed render, AND a non-deterministic one, so all
    three refuse here as a single ``stage="render"`` refusal carrying the
    helper's own message — which names non-determinism explicitly when that is
    what happened.
    """

    work_dir = plan.work_dir
    config_path = work_dir / f"{name}.yml"
    first_output_path = work_dir / f"{name}.first.raw"
    repeat_output_path = work_dir / f"{name}.repeat.raw"

    # Clear OUR two preserved slots so a second run into the same work directory
    # is not refused by its own leftovers. This does not defeat the helper's
    # slot guard, it scopes it: that guard knows only that a bundle path was
    # used before, and it defends the bass-extension campaign, where MANY
    # shapes share one bundle under caller-chosen names — so there the caller
    # is the only one who can say whether a collision is reuse or two shapes
    # fighting over a name. This loop renders exactly two shapes and derives
    # both names from the arm, so here the answer is always reuse.
    # `render_config` already unlinks its own declared destination for the same
    # reason; this restores that parity for the two paths the determinism
    # helper adds on top of it.
    for slot in (first_output_path, repeat_output_path):
        try:
            slot.unlink(missing_ok=True)
        except OSError as exc:
            # `missing_ok` suppresses only FileNotFoundError; anything else
            # (a directory in the slot, a read-only mount) would otherwise
            # escape as a bare OSError, past run_emit_loop and past the CLI's
            # refusal handler — surfacing as exit 1, "a graded branch did not
            # match", for a run that never rendered anything. Refusing here
            # keeps it exit 2, "no verdict was reached", which is true.
            log_event(
                logger, "emit_loop.refused", arm=name, stage="prepare",
                slot=str(slot), reason=str(exc),
            )
            raise EmitLoopError(
                f"{name} arm's preserved output slot {slot} could not be "
                f"cleared: {exc}"
            ) from exc

    try:
        receipt = render_with_determinism_receipt(
            binary.path,
            config_path,
            declared_output_path=work_dir / f"{name}.raw",
            first_output_path=first_output_path,
            second_output_path=repeat_output_path,
            bounds=bounds,
            fader_db=RENDER_FADER_DB,
        )
    except RenderError as exc:
        log_event(logger, "emit_loop.refused", arm=name, stage="render", reason=str(exc))
        raise EmitLoopError(f"{name} arm could not be rendered: {exc}") from exc

    first, second = receipt.first, receipt.second
    log_event(
        logger,
        "emit_loop.render",
        arm=name,
        config_sha256=receipt.config_sha256[:12],
        output_sha256=first.output_sha256[:12],
        deterministic=receipt.deterministic,
        duration_s=round(first.duration_s, 3),
    )
    return RenderArm(
        name=name,
        config_path=config_path,
        output_path=first_output_path,
        derived=derived,
        determinism_receipt={
            "config_sha256": receipt.config_sha256,
            "deterministic": receipt.deterministic,
            "argv": list(first.argv),
            "output_sha256": first.output_sha256,
            "repeat_output_sha256": second.output_sha256,
            "output_byte_size": first.output_byte_size,
            "duration_s": first.duration_s,
        },
    )


def run_emit_loop(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    linearization: Mapping[str, Sequence[Mapping[str, Any]]],
    work_dir: Path,
    binary: BinaryIdentity,
    bounds: RenderBounds = DEFAULT_RENDER_BOUNDS,
    sweep_seconds: float = STIMULUS_SWEEP_SECONDS,
    emit_kwargs: Mapping[str, Any] | None = None,
) -> EmitLoopReport:
    """Render a predicted linearization and grade what the DSP actually emits.

    ``linearization`` is the emitter's reduced shape
    ``{role: [{biquad_type, freq, q, gain}, …]}`` —
    :func:`jasper.active_speaker.linearization_fit.linearization_filters_by_role`
    is what produces it from a persisted fit. It is passed to the emitter
    unchanged AND used, unchanged, to build the claimed curve, so a filter the
    emitter validates away or clamps shows up as a disagreement rather than
    disappearing from both sides at once.

    ``emit_kwargs`` are threaded to BOTH arms identically. Anything asymmetric
    would break the cancellation the whole method rests on, so ``linearization``
    is the one key this function sets itself and it refuses a caller that tries
    to pass it here.

    Raises:
      EmitLoopError: on any refusal — emit, derive, render, or an unattributable
        soft-clip contribution. A refusal is never downgraded to a verdict.
    """

    plan = plan_emit_loop(
        preset,
        playback_device=playback_device,
        linearization=linearization,
        work_dir=work_dir,
        sweep_seconds=sweep_seconds,
        emit_kwargs=emit_kwargs,
    )
    sample_rate = plan.geometry.sample_rate_hz
    sweep, meta = synchronized_swept_sine(
        duration_approx_s=float(sweep_seconds),
        sample_rate=sample_rate,
        amplitude_dbfs=OFFLINE_STIMULUS_PEAK_DBFS,
    )
    written_header = _write_stimulus_wav(
        plan.stimulus_path,
        sweep,
        sample_rate=sample_rate,
        channels=plan.geometry.capture_channels,
    )
    if written_header != plan.capture_header:
        raise EmitLoopError(
            f"the written stimulus header {written_header} is not the one both "
            f"arms were derived against ({plan.capture_header})"
        )

    try:
        # Sized at PLAYBACK width: the render writes the full pipeline frame,
        # which is wider than the capture on any multi-way preset. And in PAIRS,
        # because estimate_render_bytes already carries the x2 for a determinism
        # pair — see RENDER_PAIRS_PER_RUN.
        check_free_space(
            plan.work_dir,
            per_render_estimate_bytes=estimate_render_bytes(
                sample_rate, plan.geometry.playback_channels, plan.stimulus_seconds
            ),
            renders_outstanding=RENDER_PAIRS_PER_RUN,
        )
    except RenderError as exc:
        raise EmitLoopError(str(exc)) from exc

    control = _render_arm("control", plan, plan.control, binary=binary, bounds=bounds)
    treated = _render_arm("treated", plan, plan.treated, binary=binary, bounds=bounds)

    band_hz = analysis_band_hz(meta)
    branches = tuple(
        _grade_branch(
            role,
            treated=treated,
            control=control,
            sweep=sweep,
            sample_rate=sample_rate,
            band_hz=band_hz,
            expected_offset_db=plan.expected_offset_db,
            claimed_filters=linearization.get(role) or (),
        )
        for role in plan.roles
    )
    report = EmitLoopReport(
        branches=branches,
        treated=treated,
        control=control,
        binary=binary.identity_artifact(),
        stimulus=_stimulus_record(
            meta, band_hz, plan.geometry.capture_channels, plan.stimulus_path
        ),
        expected_offset_db=plan.expected_offset_db,
    )
    log_event(
        logger,
        "emit_loop.outcome",
        outcome=report.outcome,
        graded=len(report.graded_branches),
        mismatched=len(report.mismatched_branches),
        unavailable=len(report.unavailable_branches),
    )
    return report


def _stimulus_record(
    meta: SweepMeta,
    band_hz: tuple[float, float],
    capture_channels: int,
    stimulus_path: Path,
) -> dict[str, Any]:
    return {
        "path": str(stimulus_path),
        "sweep": meta.to_dict(),
        "lead_in_s": STIMULUS_LEAD_IN_S,
        "tail_s": STIMULUS_TAIL_S,
        "capture_channels": capture_channels,
        "analysis_band_hz": list(band_hz),
        "fader_db": RENDER_FADER_DB,
    }


def _grade_branch(
    role: str,
    *,
    treated: RenderArm,
    control: RenderArm,
    sweep: np.ndarray,
    sample_rate: int,
    band_hz: tuple[float, float],
    expected_offset_db: float,
    claimed_filters: Sequence[Mapping[str, Any]],
) -> BranchComparison:
    """One role: extract both arms, bound the soft clip, deconvolve, grade."""

    control_branch = control.derived.branch(role)
    treated_branch = treated.derived.branch(role)
    if control_branch.channels != treated_branch.channels:
        raise EmitLoopError(
            f"branch {role!r} sits on different channels in the two arms "
            f"({control_branch.channels} vs {treated_branch.channels}) — the A/B "
            "would compare two different signals"
        )
    try:
        control_samples = decode_render_channel(
            control.output_path,
            channel_index=control_branch.extract_channel,
            channel_count=control.derived.playback_channels,
            precision=DEPLOYED_PROCESSING_PRECISION,
        )
        treated_samples = decode_render_channel(
            treated.output_path,
            channel_index=treated_branch.extract_channel,
            channel_count=treated.derived.playback_channels,
            precision=DEPLOYED_PROCESSING_PRECISION,
        )
    except EmitComparisonError as exc:
        raise EmitLoopError(f"branch {role!r} could not be read: {exc}") from exc

    soft_clip_bound_db = soft_clip_error_bound_db(
        float(np.max(np.abs(treated_samples))) if treated_samples.size else 0.0,
        float(np.max(np.abs(control_samples))) if control_samples.size else 0.0,
        clip_limit_dbfs=treated_branch.limiter_clip_limit_dbfs,
    )
    if soft_clip_bound_db > SOFT_CLIP_BUDGET_DB:
        log_event(
            logger,
            "emit_loop.refused",
            role=role,
            stage="soft_clip",
            bound_db=round(soft_clip_bound_db, 4),
            budget_db=SOFT_CLIP_BUDGET_DB,
        )
        raise EmitLoopError(
            f"branch {role!r} rendered hot enough that the always-on soft clip "
            f"could contribute {soft_clip_bound_db:.3f} dB to the A/B, above the "
            f"{SOFT_CLIP_BUDGET_DB} dB budget — the residual could not be "
            "attributed, so it is not graded. Lower the stimulus level."
        )

    try:
        control_ir = deconvolved_ir(control_samples, sweep, sample_rate)
        treated_ir = deconvolved_ir(treated_samples, sweep, sample_rate)
        window = shared_arrival_window(control_ir, sample_rate)
        n_fft = magnitude_fft_length(window)
        freqs, control_db = windowed_magnitude_db(
            control_ir, window, sample_rate, n_fft=n_fft
        )
        _, treated_db = windowed_magnitude_db(
            treated_ir, window, sample_rate, n_fft=n_fft
        )
    except EmitComparisonError as exc:
        raise EmitLoopError(f"branch {role!r} could not be analysed: {exc}") from exc

    comparison = compare_branch(
        freqs,
        treated_db,
        control_db,
        _claimed_delta_db(claimed_filters, freqs),
        role=role,
        band_hz=band_hz,
        expected_offset_db=expected_offset_db,
        soft_clip_bound_db=soft_clip_bound_db,
    )
    log_event(
        logger,
        "emit_loop.branch",
        role=role,
        verdict=comparison.verdict.verdict,
        max_error_db=round(comparison.band_max_error_db, 4),
        rms_error_db=round(comparison.band_rms_error_db, 4),
        worst_hz=round(comparison.band_worst_hz, 1),
        valid_band_hz=(
            f"{comparison.valid_band_hz[0]:.0f}-{comparison.valid_band_hz[1]:.0f}"
            if comparison.valid_band_hz is not None
            else "none"
        ),
        offset_db=(
            round(comparison.frame.fit.offset_db, 4)
            if comparison.frame.fit.offset_db is not None
            else None
        ),
        tilt_db_per_octave=(
            round(comparison.frame.fit.tilt_db_per_octave, 4)
            if comparison.frame.fit.tilt_db_per_octave is not None
            else None
        ),
        soft_clip_bound_db=round(soft_clip_bound_db, 4),
    )
    return comparison


__all__ = [
    "DEFAULT_RENDER_BOUNDS",
    "EMIT_LOOP_OUTCOMES",
    "MAX_STIMULUS_SECONDS",
    "OFFLINE_STIMULUS_PEAK_DBFS",
    "OUTCOME_MATCHED",
    "OUTCOME_MISMATCHED",
    "OUTCOME_UNAVAILABLE",
    "RENDER_FADER_DB",
    "RENDER_PAIRS_PER_RUN",
    "STIMULUS_LEAD_IN_S",
    "STIMULUS_SWEEP_SECONDS",
    "STIMULUS_TAIL_S",
    "EmitLoopError",
    "EmitLoopPlan",
    "EmitLoopReport",
    "RenderArm",
    "plan_emit_loop",
    "run_emit_loop",
]
