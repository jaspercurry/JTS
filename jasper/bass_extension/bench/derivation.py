# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure config derivation for the offline pre/post-limiter tap render.

Implements R1, R2, R3, and R7 of
``docs/bass-extension-waves/limiter-tap-realization.md``: given the exact
``active_config_raw`` text proved by a pass's activation read-back receipt
(R1), derive a truncated, file-backed CamillaDSP config that isolates either
the owner step's pre-limiter or post-limiter signal (R2), swaps the
capture/playback devices to file-backed types under a closed normalization
allowlist (R3), and enforces the owner-path stage allowlist (R7).

Derivation operates on the **raw mapping** — ``yaml.safe_load`` of the proved
``active_config_raw``, edited in place — never on a
:class:`~jasper.active_speaker.graph_safety.GraphView` (R2): ``GraphView``
(via ``view_from_camilla_dict``) drops every non-``Filter`` pipeline step, the
per-step ``bypassed`` flag, the ``mixers`` block, and the ``devices`` block,
so it is used here ONLY to re-prove owner-step invariants against the
already-derived output, never as the object this module edits.

Upstream citations (recorded obligations, verified against the pinned
CamillaDSP v4.1.3 source tree at
https://github.com/HEnquist/camilladsp/tree/v4.1.3):

* **Wav capture semantics — a correction to the amendment's prose.** The
  amendment repeatedly calls the file capture device type "Wav"; the real
  config-schema type name is ``WavFile``
  (``src/config/mod.rs`` ``enum CaptureDevice { ... WavFile(CaptureDeviceWavFile), ... }``,
  no ``#[serde(alias=...)]``) and its struct
  (``struct CaptureDeviceWavFile { filename, extra_samples?, labels? }``,
  ``#[serde(deny_unknown_fields)]``) has **no ``channels`` field at all** — the
  channel count comes from the WAV file's own header
  (``CaptureDeviceWavFile::wav_info()``), confirmed by the README's own
  ``WavFile``/``File`` example (no ``channels:`` key under a ``WavFile``
  capture). This contradicts the amendment's literal
  "``devices.capture.channels`` is unchanged" — keeping that key would make
  CamillaDSP refuse the derived config outright (unknown field). This module
  implements the verified, working behavior: the ``channels`` key is DELETED
  from the derived capture block (like ``device``/``format``), and the live
  value is instead used as a pure validation input (compared against the
  artifact header before any render starts) and recorded in the derivation
  receipt. The playback side has no such issue: ``PlaybackDevice::File``
  *does* declare a required ``channels: usize`` field, matching R3's
  "``devices.playback.channels`` … at live pipeline width."
* **Processing precision.** The deployed binary
  (``deploy/install.sh``'s ``camilladsp-linux-aarch64.tar.gz`` for
  ``CAMILLA_VERSION=v4.1.3``) is built by the upstream release workflow's
  ``linux_aarch64`` / "Build with Alsa only" step as plain
  ``cargo build --release`` — no ``--features`` flag, so no ``32bit`` Cargo
  feature. ``src/lib.rs`` (`` #[cfg(feature = "32bit")] pub type PrcFmt = f32;
  #[cfg(not(feature = "32bit"))] pub type PrcFmt = f64;``) then pins
  ``PrcFmt = f64`` for this exact build — the deployed processing precision is
  64-bit float, spelled **``F64_LE``** in this pinned build's device-format
  enum (``src/config/mod.rs`` ``FileSampleFormat``/``BinarySampleFormat``:
  ``{..., S24_4_RJ_LE, S24_4_LJ_LE, S24_3_LE, S32_LE, F32_LE, F64_LE}``,
  ``deny_unknown_fields``) — "FLOAT64LE" is a retired v1/v2 spelling this
  build rejects outright. Callers pass this in (see ``render.py``, which owns
  the binary-identity verification); this module does not hardcode it so the
  two concerns — binary identity and config shape — stay decoupled.
* **Main-fader application point (R4(b)/R4(c), now a settled fact, not a
  recorded obligation pending citation).** ``src/pipeline.rs``
  ``Pipeline::process_chunk``: ``self.volume.process_chunk(&mut chunk)`` runs
  BEFORE the ``for step in &mut self.steps`` loop — i.e. before EVERY
  configured pipeline step, unconditionally — and ``Pipeline::from_config``
  builds that fader from ``processing_params.current_volume(0)``, the same
  index ``src/socketserver.rs``'s ``WsCommand::SetVolume``/``GetVolume``
  read/write. The main fader therefore ALWAYS precedes the owner limiter for
  this build: R4(c) permanently resolves to "reproduce the recorded fader
  gain" (never "apply no fader gain at all" — that branch is dead for this
  CamillaDSP version, not merely "currently unreachable"). Every render
  reproduces it via ``--gain`` (see ``render.py``).
* **``queuelimit`` / ``target_level``.** Both are declared optional on
  ``Devices`` (``src/config/mod.rs``, defaults 4 and ``chunksize``
  respectively) and the README documents no file-device-specific requirement
  — the guidance ("only changed if the capture device can provide data faster
  than playback can consume it, like the ALSA 'cdsp' plugin") is about
  real-time device pacing, meaningless for a batch ``WavFile``/``File`` pass
  with no live clock to steer. This module therefore leaves both keys
  untouched at their live values (they fall out of "everything else
  unchanged" below) rather than invent a specific value the source does not
  require.

R2's post-limiter re-proof reuses
:func:`jasper.bass_extension.bench.activation._prove_active_graph` verbatim
(the callable predicate derivation itself uses, per the amendment); the
pre-limiter re-proof is this module's own, distinct predicate
(:func:`_prove_pre_limiter_prefix`) because ``_prove_active_graph``
structurally requires the limiter name present and ordered, so it cannot
validate a truncation that omits the limiter by construction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import yaml

from jasper.active_speaker.camilla_yaml import (
    BASS_EXTENSION_LT_FILTER,
    BASS_EXTENSION_SUBSONIC_FILTER,
)
from jasper.active_speaker.graph_safety import GraphView, view_from_camilla_dict

from .activation import ActivationError, ActivationProof, _prove_active_graph

# R7's closed stage-type allowlist: derivation admits only filter types whose
# offline behavior is exactly reproducible from the config text alone.
ALLOWED_FILTER_TYPES: frozenset[str] = frozenset(
    {"Biquad", "BiquadCombo", "Conv", "Delay", "Gain", "Limiter"}
)

# S3: every derivation receipt carries these citations as DATA, not just
# module-docstring prose — receipts are bundle evidence a reviewer or the
# producer's own tests can inspect without reading source comments.
RECORDED_OBLIGATIONS: dict[str, Any] = {
    "wav_capture_semantics": {
        "file": "src/config/mod.rs",
        "symbol": "CaptureDevice::WavFile / CaptureDeviceWavFile",
        "fact": (
            "CaptureDeviceWavFile { filename, extra_samples?, labels? }, "
            "#[serde(deny_unknown_fields)] — no channels field; capture "
            "geometry comes from the WavFile's own header, not config"
        ),
    },
    "processing_precision": {
        "file": "src/lib.rs",
        "symbol": "PrcFmt (cfg(feature = \"32bit\"))",
        "fact": (
            "no 32bit Cargo feature on the deployed linux_aarch64 release "
            "build -> PrcFmt = f64 -> device format F64_LE "
            "(src/config/mod.rs FileSampleFormat/BinarySampleFormat)"
        ),
    },
    "fader_application_point": {
        "file": "src/pipeline.rs",
        "symbol": "Pipeline::process_chunk / Pipeline::from_config",
        "fact": (
            "self.volume.process_chunk(&mut chunk) runs before the pipeline "
            "steps loop, unconditionally; built from "
            "processing_params.current_volume(0) — the main fader ALWAYS "
            "precedes the owner limiter for this build"
        ),
        "offset_sign": (
            "R4(c) permanently resolves to 'reproduce the recorded fader "
            "gain' (render carries it via --gain); R10(b) offset is 0 dB, "
            "never -recorded_main_volume_db"
        ),
    },
    "queuelimit_target_level": {
        "file": "src/config/mod.rs",
        "symbol": "Devices.queuelimit / Devices.target_level",
        "fact": (
            "both Option<usize>, defaults 4 and chunksize respectively; no "
            "file-device-specific requirement documented — left unchanged "
            "from the live value"
        ),
    },
}

Boundary = Literal["pre_limiter", "post_limiter"]


class DerivationError(ValueError):
    """A derived config could not be constructed or proved against R1/R2/R3/R7."""


@dataclass(frozen=True, slots=True)
class ArtifactHeader:
    """The minimal WAV/PCM geometry facts R3 and R6a(iv) validate against."""

    sample_rate_hz: int
    channels: int
    bits_per_sample: int


@dataclass(frozen=True, slots=True)
class DerivedConfig:
    """One derived config: its exact YAML text plus its derivation receipt.

    ``receipt`` is a plain JSON-serializable mapping — a bundle FILE via the
    sink, never a field on any closed evidence schema object (the frozen
    protocol's ``measured_context`` / source-observation / candidate /
    sweep-sustain-transfer records are exhaustive "has exactly" lists).
    """

    yaml_text: str
    receipt: dict[str, Any]


def _parse_active_config(raw_text: str) -> dict[str, Any]:
    if type(raw_text) is not str or not raw_text.strip():
        raise DerivationError("active_config_raw is empty")
    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise DerivationError(f"active_config_raw did not parse: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DerivationError("active_config_raw is not a mapping")
    return parsed


def _step_channels(step: Mapping[str, Any]) -> frozenset[int] | None:
    """Normalize one Filter step's channel set (list or scalar sugar).

    Mirrors ``jasper.active_speaker.graph_safety._running_step_channels`` — the
    JTS-verified adapter for CamillaDSP's *active-config read-back* dialect,
    which may render a single-channel Filter step's channels as the scalar
    ``channel: N`` rather than the always-plural ``channels: [N]`` the
    ``PipelineStepFilter`` config-file struct declares. Returns ``None`` for a
    step with neither key (a Mixer/Processor step has no channel field at
    all).
    """

    chans = step.get("channels")
    if isinstance(chans, list):
        return frozenset(
            int(c) for c in chans if isinstance(c, int) and not isinstance(c, bool)
        )
    channel = step.get("channel")
    if isinstance(channel, int) and not isinstance(channel, bool):
        return frozenset({int(channel)})
    return None


def _find_owner_step(
    pipeline: Sequence[Any], owner_channels: frozenset[int]
) -> tuple[int, list[str]]:
    """Find the single Filter pipeline step whose channels equal ``owner_channels``.

    Mirrors ``_assert_bass_extension_safe``'s owner-step selection (the MODEL
    for this invariant), but over the raw mapping rather than a ``GraphView``.
    """

    if not isinstance(pipeline, list):
        raise DerivationError("live pipeline is not a list")
    matches: list[int] = []
    for index, step in enumerate(pipeline):
        if not isinstance(step, Mapping) or step.get("type") != "Filter":
            continue
        if _step_channels(step) == owner_channels:
            matches.append(index)
    if len(matches) != 1:
        raise DerivationError(
            "expected exactly one live owner pipeline step for channels "
            f"{sorted(owner_channels)}, found {len(matches)}"
        )
    index = matches[0]
    names = pipeline[index].get("names")
    if not isinstance(names, list) or not all(type(n) is str for n in names):
        raise DerivationError("live owner pipeline step names is not a list of strings")
    return index, list(names)


def boundary_index(names: Sequence[str], limiter_name: str) -> int:
    """R2: the index ``i`` of ``limiter_name`` within the live owner step's names."""

    try:
        return list(names).index(limiter_name)
    except ValueError as exc:
        raise DerivationError(
            f"limiter {limiter_name!r} is not present in the live owner step names"
        ) from exc


def validate_boundary(
    *,
    live_names: Sequence[str],
    retained_names: Sequence[str],
    index: int,
    boundary: Boundary,
) -> None:
    """R2's exact boundary math — the only two retained lengths derivation may emit.

    Post-limiter retains ``names[:i+1]``; pre-limiter retains ``names[:i]``.
    Any other retained length (e.g. ``names[:i-1]``) refuses. A pure, directly
    testable predicate: ``derive_truncated_config`` always calls it with its
    own correctly-computed ``retained_names``, but it exists so the boundary
    invariant is independently pinned regardless of that caller.
    """

    live = list(live_names)
    if boundary == "post_limiter":
        expected = live[: index + 1]
    elif boundary == "pre_limiter":
        expected = live[:index]
    else:
        raise DerivationError(f"unknown boundary {boundary!r}")
    if list(retained_names) != expected:
        raise DerivationError(
            "retained owner-step names do not match the exact R2 boundary "
            f"(boundary={boundary!r}, expected {expected!r}, got {list(retained_names)!r})"
        )


def _derive_devices_block(
    live_devices: Mapping[str, Any],
    *,
    capture_filename: str,
    capture_header: ArtifactHeader,
    playback_filename: str,
    processing_precision: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """R3: derive the ``devices`` block; return ``(derived, receipt_diff)``.

    Refuses before any render if the stimulus artifact's header does not
    match the live capture geometry (R3 / R6a(iv)).
    """

    if not isinstance(live_devices, Mapping):
        raise DerivationError("live devices block is not a mapping")
    live_capture = live_devices.get("capture")
    live_playback = live_devices.get("playback")
    if not isinstance(live_capture, Mapping):
        raise DerivationError("live devices.capture is not a mapping")
    if not isinstance(live_playback, Mapping):
        raise DerivationError("live devices.playback is not a mapping")

    live_samplerate = live_devices.get("samplerate")
    if type(live_samplerate) is not int or live_samplerate <= 0:
        raise DerivationError("live devices.samplerate is not a positive integer")

    live_capture_channels = live_capture.get("channels")
    if type(live_capture_channels) is not int or live_capture_channels <= 0:
        raise DerivationError(
            "live devices.capture.channels is not a positive integer"
        )
    live_playback_channels = live_playback.get("channels")
    if type(live_playback_channels) is not int or live_playback_channels <= 0:
        raise DerivationError(
            "live devices.playback.channels is not a positive integer"
        )

    # R3 / R6a(iv): the artifact header must match live geometry, and be
    # 16-bit PCM (the live capture `format: S32_LE` is an ALSA-plug widening
    # of fan-in's S16 mix, not independent geometry — a 16-bit artifact is the
    # bit-exact equivalent). Any mismatch refuses before the render starts.
    if capture_header.sample_rate_hz != live_samplerate:
        raise DerivationError(
            "stimulus artifact sample rate "
            f"({capture_header.sample_rate_hz}) does not match "
            f"devices.samplerate ({live_samplerate})"
        )
    if capture_header.channels != live_capture_channels:
        raise DerivationError(
            "stimulus artifact channel count "
            f"({capture_header.channels}) does not match "
            f"devices.capture.channels ({live_capture_channels})"
        )
    if capture_header.bits_per_sample != 16:
        raise DerivationError(
            "stimulus artifact is not 16-bit PCM "
            f"(got {capture_header.bits_per_sample}-bit)"
        )

    # Capture swap: type -> WavFile (see module docstring for the "Wav" naming
    # correction), filename set. `device` and `format` are deleted (WavFile
    # geometry comes from the artifact's own header); `channels` is ALSO
    # deleted — CaptureDeviceWavFile has no such field — the live value is
    # validated above and recorded, never emitted as a key here.
    derived_capture: dict[str, Any] = {"type": "WavFile", "filename": capture_filename}
    if "labels" in live_capture:
        derived_capture["labels"] = live_capture["labels"]

    # Playback swap: type -> File, filename set, format -> the recorded
    # processing precision (R3's added normalization-allowlist entry).
    # `channels` is KEPT at the live pipeline width — PlaybackDevice::File
    # declares a required `channels` field, unlike the capture side.
    derived_playback: dict[str, Any] = {
        "type": "File",
        "channels": live_playback_channels,
        "filename": playback_filename,
        "format": processing_precision,
    }

    derived_devices: dict[str, Any] = {}
    live_enable_rate_adjust = live_devices.get("enable_rate_adjust")
    for key, value in live_devices.items():
        if key == "capture":
            derived_devices[key] = derived_capture
        elif key == "playback":
            derived_devices[key] = derived_playback
        elif key == "enable_rate_adjust":
            derived_devices[key] = False
        else:
            derived_devices[key] = value
    if "enable_rate_adjust" not in derived_devices:
        derived_devices["enable_rate_adjust"] = False

    device_diff = {
        "capture_type_live": live_capture.get("type"),
        "capture_type_derived": "WavFile",
        "capture_device_key_live": live_capture.get("device"),
        "capture_format_key_live": live_capture.get("format"),
        "capture_channels_live_validated_only": live_capture_channels,
        "playback_type_live": live_playback.get("type"),
        "playback_type_derived": "File",
        "playback_device_key_live": live_playback.get("device"),
        "playback_format_live": live_playback.get("format"),
        "playback_format_derived": processing_precision,
        "playback_channels": live_playback_channels,
        "enable_rate_adjust_live": live_enable_rate_adjust,
        "enable_rate_adjust_derived": False,
        # S3: queuelimit/target_level are recorded EVEN THOUGH unchanged — a
        # deliberate, cited decision (see the module docstring), not an
        # oversight.
        "queuelimit_live": live_devices.get("queuelimit"),
        "queuelimit_derived": live_devices.get("queuelimit"),
        "target_level_live": live_devices.get("target_level"),
        "target_level_derived": live_devices.get("target_level"),
    }
    return derived_devices, device_diff


def _assert_filters_mixers_identical(
    live_active_config_raw: str, derived: Mapping[str, Any]
) -> None:
    """R2(ii): filters/mixers identical after canonical re-serialization.

    Takes the ORIGINAL SOURCE TEXT and re-parses it independently, rather
    than comparing ``derived`` against the ``live`` mapping
    ``derive_truncated_config`` already parsed: ``derived["filters"]`` /
    ``derived["mixers"]`` are the SAME dict objects copied by reference from
    that ``live`` mapping (derivation never edits them), so comparing against
    ``live`` directly would compare each object to itself — an ``==`` that
    can never be false, hence never catch anything. Re-parsing from the
    immutable source text is genuinely independent: it protects against a
    future in-place mutation of the shared objects (by this module or a
    caller) going undetected — exactly where an altered or dropped mixer
    source (e.g. the -6.02 dB split-mixer hazard) would be caught.
    """

    reparsed = _parse_active_config(live_active_config_raw)
    if reparsed.get("filters") != derived.get("filters"):
        raise DerivationError("derived filters block differs from the live filters block")
    if reparsed.get("mixers") != derived.get("mixers"):
        raise DerivationError("derived mixers block differs from the live mixers block")


def _derive_pipeline(
    live_pipeline: Sequence[Any],
    *,
    owner_step_index: int,
    retained_names: Sequence[str],
) -> list[Any]:
    """R2(iii): the live pipeline truncated at the owner step, names replaced."""

    derived: list[Any] = []
    for index, step in enumerate(live_pipeline[: owner_step_index + 1]):
        if index == owner_step_index:
            if not isinstance(step, Mapping):
                raise DerivationError("owner pipeline step is not a mapping")
            new_step = dict(step)
            new_step["names"] = list(retained_names)
            derived.append(new_step)
        else:
            derived.append(step)
    return derived


def _assert_owner_path_allowlist(
    retained_pipeline: Sequence[Any], filters: Mapping[str, Any]
) -> None:
    """R7: every RETAINED step must be Filter or Mixer, unbypassed, allowlisted.

    Enforced against every step in the retained (truncated) prefix — R7's
    enforcement sentence says "any retained step", not "any owner-path step";
    :func:`owner_path_stages` computes the narrower backward-walked subset
    separately, for R6's lead-in/lead-out delay math, not for this gate.
    """

    for step in retained_pipeline:
        if not isinstance(step, Mapping):
            raise DerivationError("retained pipeline step is not a mapping")
        step_type = step.get("type")
        if step_type not in ("Filter", "Mixer"):
            raise DerivationError(
                f"retained pipeline step type {step_type!r} is not Filter or Mixer"
            )
        if step.get("bypassed"):
            raise DerivationError("retained pipeline step is bypassed")
        if step_type != "Filter":
            continue
        names = step.get("names")
        if not isinstance(names, list):
            raise DerivationError("retained Filter step names is not a list")
        for name in names:
            spec = filters.get(name)
            ftype = spec.get("type") if isinstance(spec, Mapping) else None
            if ftype is None:
                raise DerivationError(f"retained filter {name!r} has no definition")
            if ftype not in ALLOWED_FILTER_TYPES:
                raise DerivationError(
                    f"retained filter {name!r} has type {ftype!r}, outside the "
                    "R7 stage allowlist"
                )
    # devices.resampler (any Async type) is a devices-block key, not a
    # pipeline step — R7 names it explicitly as a refusal trigger even though
    # it lives outside the pipeline list this loop walks.


def assert_no_async_resampler(devices: Mapping[str, Any]) -> None:
    """R7: an Async resampler block on the (pre-normalization) live devices refuses.

    ``enable_rate_adjust`` itself is NOT a refusal trigger (R3 normalizes it to
    ``false``) — only an actual ``devices.resampler`` block of Async type.
    """

    resampler = devices.get("resampler")
    if isinstance(resampler, Mapping) and str(resampler.get("type", "")).startswith(
        "Async"
    ):
        raise DerivationError(
            "live devices.resampler is an Async type, outside the R7 allowlist"
        )


def owner_path_stages(
    retained_pipeline: Sequence[Mapping[str, Any]],
    mixers: Mapping[str, Any],
    *,
    owner_channels: frozenset[int],
) -> list[Mapping[str, Any]]:
    """R7's "owner path": retained steps whose signal reaches ``owner_channels``.

    Walks the retained mixers' mappings backwards from the owner channel(s) to
    the capture channels. Used for R6's lead-in/lead-out group-delay and
    Conv-impulse-length computation — a Delay/Conv stage on an unrelated
    channel (e.g. a tweeter path preceding the woofer owner step in pipeline
    order) must not count toward the owner's group delay. This is narrower
    than, and independent of, :func:`_assert_owner_path_allowlist`'s
    every-retained-step admission gate.
    """

    of_interest = set(owner_channels)
    touched: list[Mapping[str, Any]] = []
    for step in reversed(list(retained_pipeline)):
        step_type = step.get("type")
        if step_type == "Filter":
            channels = _step_channels(step)
            if channels is not None and channels & of_interest:
                touched.append(step)
        elif step_type == "Mixer":
            mixer_name = step.get("name")
            mixer = mixers.get(mixer_name) if isinstance(mixer_name, str) else None
            if not isinstance(mixer, Mapping):
                continue
            mapping = mixer.get("mapping")
            if not isinstance(mapping, list):
                continue
            relevant = [
                m
                for m in mapping
                if isinstance(m, Mapping) and m.get("dest") in of_interest
            ]
            if not relevant:
                continue
            touched.append(step)
            new_interest: set[int] = set()
            for entry in relevant:
                for source in entry.get("sources") or ():
                    if isinstance(source, Mapping) and isinstance(
                        source.get("channel"), int
                    ):
                        new_interest.add(int(source["channel"]))
            of_interest = new_interest
    return list(reversed(touched))


def _prove_pre_limiter_prefix(
    view: GraphView, *, owner_channels: frozenset[int], limiter_name: str
) -> bool:
    """R2's DISTINCT pre-limiter re-proof, over the retained prefix.

    ``bass_ext_lt`` before ``bass_ext_subsonic``, both on exactly the owner
    channels, limiter name ABSENT. A separate predicate from
    ``activation._prove_active_graph``, which structurally requires the
    limiter name present and ordered — it cannot validate a truncation that
    omits the limiter by construction.
    """

    owner_steps = [step for step in view.pipeline_steps if step.channels == owner_channels]
    if len(owner_steps) != 1:
        return False
    names = owner_steps[0].names
    if limiter_name in names:
        return False
    required = (BASS_EXTENSION_LT_FILTER, BASS_EXTENSION_SUBSONIC_FILTER)
    if not all(name in names for name in required):
        return False
    return names.index(BASS_EXTENSION_LT_FILTER) < names.index(
        BASS_EXTENSION_SUBSONIC_FILTER
    )


def derive_truncated_config(
    live_active_config_raw: str,
    *,
    boundary: Boundary,
    limiter_name: str,
    owner_channels: Sequence[int],
    profile_summary: Mapping[str, Any],
    expected_clip_limit_dbfs: float,
    capture_header: ArtifactHeader,
    capture_filename: str,
    playback_filename: str,
    processing_precision: str,
) -> DerivedConfig:
    """Derive one pre- or post-limiter render config from a proved read-back.

    ``live_active_config_raw`` MUST be the exact text from the pass's own
    activation read-back receipt (R1) — this function never reads a config
    file from disk or re-fetches from a live daemon. ``profile_summary`` and
    ``expected_clip_limit_dbfs`` are the same activation-proof inputs the live
    pass itself used (``TargetPlan.profile_summary`` /
    the candidate's configured ``clip_limit``, or the baseline for discovery).
    Raises :class:`DerivationError` on any refusal.
    """

    live = _parse_active_config(live_active_config_raw)
    live_devices = live.get("devices")
    live_pipeline = live.get("pipeline")
    if not isinstance(live_devices, Mapping):
        raise DerivationError("live config has no devices mapping")
    if not isinstance(live_pipeline, list):
        raise DerivationError("live config has no pipeline list")
    assert_no_async_resampler(live_devices)

    owner_set = frozenset(int(channel) for channel in owner_channels)
    if not owner_set:
        raise DerivationError("owner_channels must be non-empty")
    owner_index, live_owner_names = _find_owner_step(live_pipeline, owner_set)
    index = boundary_index(live_owner_names, limiter_name)
    retained_names = (
        live_owner_names[: index + 1]
        if boundary == "post_limiter"
        else live_owner_names[:index]
    )
    validate_boundary(
        live_names=live_owner_names,
        retained_names=retained_names,
        index=index,
        boundary=boundary,
    )

    derived_devices, device_diff = _derive_devices_block(
        live_devices,
        capture_filename=capture_filename,
        capture_header=capture_header,
        playback_filename=playback_filename,
        processing_precision=processing_precision,
    )
    derived_pipeline = _derive_pipeline(
        live_pipeline, owner_step_index=owner_index, retained_names=retained_names
    )
    live_filters = live.get("filters")
    filters_for_allowlist = live_filters if isinstance(live_filters, Mapping) else {}
    _assert_owner_path_allowlist(derived_pipeline, filters_for_allowlist)

    derived: dict[str, Any] = {}
    for key, value in live.items():
        if key == "devices":
            derived[key] = derived_devices
        elif key == "pipeline":
            derived[key] = derived_pipeline
        else:
            derived[key] = value
    _assert_filters_mixers_identical(live_active_config_raw, derived)

    if boundary == "post_limiter":
        proof = ActivationProof(
            limiter_name=limiter_name,
            owner_channels=tuple(sorted(owner_set)),
            profile_summary=profile_summary,
            expected_clip_limit_dbfs=expected_clip_limit_dbfs,
        )
        try:
            _prove_active_graph(derived, proof)
        except ActivationError as exc:
            raise DerivationError(f"post-limiter re-proof failed: {exc}") from exc
    else:
        view = view_from_camilla_dict(derived)
        if not _prove_pre_limiter_prefix(
            view, owner_channels=owner_set, limiter_name=limiter_name
        ):
            raise DerivationError("pre-limiter re-proof failed")

    live_mixers = live.get("mixers")
    mixers_for_walk = live_mixers if isinstance(live_mixers, Mapping) else {}
    owner_path = owner_path_stages(
        derived_pipeline, mixers_for_walk, owner_channels=owner_set
    )

    yaml_text = yaml.safe_dump(derived, sort_keys=False)
    receipt: dict[str, Any] = {
        "boundary": boundary,
        "limiter_name": limiter_name,
        "owner_channels": sorted(owner_set),
        "owner_channel_frame_indices": sorted(owner_set),
        "boundary_index": index,
        "live_owner_step_names": list(live_owner_names),
        "retained_names": list(retained_names),
        "owner_path_step_count": len(owner_path),
        "owner_path_filter_names": [
            name
            for step in owner_path
            if step.get("type") == "Filter"
            for name in (step.get("names") or ())
        ],
        "device_diff": device_diff,
        "processing_precision": processing_precision,
        "recorded_obligations": RECORDED_OBLIGATIONS,
    }
    return DerivedConfig(yaml_text=yaml_text, receipt=receipt)


__all__ = [
    "ALLOWED_FILTER_TYPES",
    "RECORDED_OBLIGATIONS",
    "ArtifactHeader",
    "Boundary",
    "DerivationError",
    "DerivedConfig",
    "assert_no_async_resampler",
    "boundary_index",
    "derive_truncated_config",
    "owner_path_stages",
    "validate_boundary",
]
