# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Binary resolution and bounded-subprocess offline render invocation.

Implements R5 (binary identity, resolved from the running unit), R8
(per-shape determinism receipts), and R9 (the execution contract: isolation,
ordering, bounds) of
``docs/bass-extension-waves/limiter-tap-realization.md``.

This module owns its own binary resolution and subprocess shape. It does
**not** import :mod:`jasper.dsp_apply` — that module's ``_camilladsp_binary``
prefers ``JASPER_CAMILLADSP_BIN`` (R5 explicitly forbids that for renders: a
set-but-different override must refuse, not be silently preferred), and its
``--check`` validation invocation is a different operation from a real render.
The bounded-subprocess *shape* (explicit argv, timeout, captured
stdout/stderr, typed result) mirrors ``dsp_apply.py``'s pattern per R5's own
text, tightened with the R9 rlimits.

Upstream citations verified against the pinned CamillaDSP v4.1.3 source
(https://github.com/HEnquist/camilladsp/tree/v4.1.3):

* **No websocket / statefile without the flags.** ``src/bin.rs``: the
  websocket server only starts ``if let Some(port) = matches.get_one::<usize>
  ("port")`` — omitting ``-p``/``--port`` (and therefore ``-a``/``--address``,
  which ``.requires("port")``) starts no server. ``--statefile`` is a
  separate, independent flag. Passing only a bare positional config path
  loads it, processes until the ``WavFile`` capture reaches EOF, and (since
  ``--wait`` is not passed) the outer loop's ``!wait && !has_commands`` branch
  returns ``EXIT_OK`` — a clean one-shot batch run.
* **``--version`` reports "4.1.3".** ``Cargo.toml`` pins ``version = "4.1.3"``
  with the ``cargo`` clap feature enabled, so clap's derived ``--version``
  output contains that exact string.
* **``--gain`` reproduces the recorded fader gain, immediately, no ramp.**
  ``src/bin.rs``: ``Arg::new("gain").short('g').long("gain")`` takes one
  numeric value (``parse_gain_value``, range [-120, 20]) and sets
  ``initial_volumes[0]`` — index 0, the exact index
  ``Pipeline::from_config`` reads via ``processing_params.current_volume(0)``
  for ``self.volume``. ``filters/basicfilters.rs``'s ``Volume::new`` sets
  ``ramp_start: current_volume`` and starts with ``ramp_step: 0`` ("not in a
  ramp") — the first processed chunk already applies the ``--gain`` value
  directly, no startup fade.
"""

from __future__ import annotations

import hashlib
import os
import re
import resource
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from jasper.audio_measurement.evidence_identity import json_fingerprint

PINNED_CAMILLADSP_VERSION = "v4.1.3"
_VERSION_SUBSTRING = "4.1.3"
# The deployed release binary (deploy/install.sh's camilladsp-linux-aarch64
# tarball for CAMILLA_VERSION=v4.1.3) is built by upstream's `linux_aarch64`
# release job as plain `cargo build --release` (no --features), so it carries
# no `32bit` Cargo feature. `src/lib.rs` then pins `PrcFmt = f64` for that
# build: the deployed processing precision is 64-bit float. "F64_LE" is the
# CORRECT CamillaDSP v4.1.3 device-format spelling for that precision
# (src/config/mod.rs `FileSampleFormat`/`BinarySampleFormat`, both
# `{..., S24_4_RJ_LE, S24_4_LJ_LE, S24_3_LE, S32_LE, F32_LE, F64_LE}`) —
# "FLOAT64LE" is a retired v1/v2 spelling and is REJECTED by this pinned
# build (`deny_unknown_fields`). See derivation.py's module docstring for
# the full citation chain.
DEPLOYED_PROCESSING_PRECISION = "F64_LE"
# `derive_truncated_config`'s algorithm identity — bump when its truncation,
# device-swap, or allowlist LOGIC changes (not on comment-only edits), so a
# `tap_implementation_id` recomputation is forced whenever the derivation
# behavior it fingerprints actually changes.
DERIVATION_FUNCTION_VERSION = "1"

_DEFAULT_UNIT_NAME = "jasper-camilla.service"
_EXEC_START_PATH_RE = re.compile(r"path=(\S+)")
_FORBIDDEN_ARGV_TOKENS = frozenset({"--statefile", "-p", "--port", "-a", "--address"})


class RenderError(RuntimeError):
    """A binary resolution, render invocation, or determinism check failed."""


@dataclass(frozen=True, slots=True)
class RenderBounds:
    """R9's process-local bounds — recorded campaign-manifest inputs, never
    unstated constants."""

    timeout_s: float
    rlimit_as_bytes: int
    rlimit_cpu_s: int
    nice: int


@dataclass(frozen=True, slots=True)
class BinaryIdentity:
    """R5's resolved, recorded render-binary identity."""

    path: str
    version_output: str
    sha256: str
    camilladsp_build_id: str

    @property
    def tap_implementation_id(self) -> str:
        """The frozen protocol's string ``tap_implementation_id`` fingerprint.

        The canonical lowercase SHA-256 ``json_fingerprint`` over
        ``{camilladsp_version, binary_sha256, derivation_function_version}``.
        ``binary_path`` is deliberately excluded (a filesystem path is not a
        stable identity input); it lives only in the retained identity
        artifact alongside the fingerprinted fields.
        """

        return json_fingerprint(
            {
                "camilladsp_version": PINNED_CAMILLADSP_VERSION,
                "binary_sha256": self.sha256,
                "derivation_function_version": DERIVATION_FUNCTION_VERSION,
            },
            field_name="tap_implementation_id",
        )

    def identity_artifact(self) -> dict[str, object]:
        """The retained identity artifact: the fingerprinted fields plus the
        (non-fingerprinted) resolved path, so the resolution stays replayable."""

        return {
            "binary_path": self.path,
            "camilladsp_version": PINNED_CAMILLADSP_VERSION,
            "version_output": self.version_output,
            "binary_sha256": self.sha256,
            "derivation_function_version": DERIVATION_FUNCTION_VERSION,
            "tap_implementation_id": self.tap_implementation_id,
            "camilladsp_build_id": self.camilladsp_build_id,
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _running_unit_exec_start_path(unit_name: str) -> str:
    """R5: resolve the binary path from the running unit's ``ExecStart``.

    Uses ``systemctl show <unit> --property=ExecStart --value``: systemd's own
    resolved view (handles drop-ins / relative-to-absolute resolution), rather
    than hand-parsing the unit file text (line continuations, comments). The
    property value is systemd's structured
    ``{ path=... ; argv[]=... ; ... }`` form for a single ``ExecStart=``; the
    ``path=`` field is the exact executable path systemd invokes. This is the
    simplest robust read available without a D-Bus client dependency; verify
    the parse holds on-device before relying on it (there is no CI host with
    a real systemd + jasper-camilla.service to exercise this against).
    """

    try:
        result = subprocess.run(
            ["systemctl", "show", unit_name, "--property=ExecStart", "--value"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError(
            f"could not query systemctl for {unit_name}'s ExecStart: {exc}"
        ) from exc
    if result.returncode != 0:
        raise RenderError(
            f"systemctl show {unit_name} failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    match = _EXEC_START_PATH_RE.search(result.stdout)
    if match is None:
        raise RenderError(
            f"could not parse a path= field from {unit_name}'s ExecStart: "
            f"{result.stdout!r}"
        )
    return match.group(1)


def resolve_render_binary(
    *, unit_name: str = _DEFAULT_UNIT_NAME, env: dict[str, str] | None = None
) -> BinaryIdentity:
    """R5: resolve, verify, and record the render binary's identity.

    ``JASPER_CAMILLADSP_BIN`` is ignored for renders; if it is set AND differs
    from the unit-resolved path, the campaign refuses rather than silently
    preferring the override. The recorded version must equal
    :data:`PINNED_CAMILLADSP_VERSION` or the campaign refuses.
    """

    environ = os.environ if env is None else env
    resolved_path = _running_unit_exec_start_path(unit_name)
    override = environ.get("JASPER_CAMILLADSP_BIN")
    if override and override != resolved_path:
        raise RenderError(
            "JASPER_CAMILLADSP_BIN is set to "
            f"{override!r} but the running unit resolves to {resolved_path!r} — "
            "refusing rather than silently preferring the override (R5)"
        )
    binary_path = Path(resolved_path)
    if not binary_path.is_file():
        raise RenderError(f"resolved render binary does not exist: {binary_path}")
    resolved = shutil.which(str(binary_path)) or (
        str(binary_path) if os.access(binary_path, os.X_OK) else None
    )
    if resolved is None:
        raise RenderError(f"resolved render binary is not executable: {binary_path}")

    try:
        version_result = subprocess.run(
            [str(binary_path), "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError(f"could not run {binary_path} --version: {exc}") from exc
    version_output = (version_result.stdout or "") + (version_result.stderr or "")
    if _VERSION_SUBSTRING not in version_output:
        raise RenderError(
            f"render binary {binary_path} --version did not report "
            f"{PINNED_CAMILLADSP_VERSION}: {version_output!r}"
        )

    sha256 = _sha256_file(binary_path)
    build_id = f"camilladsp-{PINNED_CAMILLADSP_VERSION}-{sha256[:12]}"
    return BinaryIdentity(
        path=str(binary_path),
        version_output=version_output.strip(),
        sha256=sha256,
        camilladsp_build_id=build_id,
    )


@dataclass(frozen=True, slots=True)
class RenderInvocation:
    """One completed render's recorded argv, outcome, and timing."""

    argv: tuple[str, ...]
    returncode: int
    duration_s: float
    stdout_tail: str
    stderr_tail: str
    output_sha256: str
    output_byte_size: int


def _tail(text: str, limit: int = 2000) -> str:
    return text[-limit:] if len(text) > limit else text


def _rlimit_preexec(bounds: RenderBounds):
    def _apply() -> None:
        resource.setrlimit(
            resource.RLIMIT_AS, (bounds.rlimit_as_bytes, bounds.rlimit_as_bytes)
        )
        resource.setrlimit(
            resource.RLIMIT_CPU, (bounds.rlimit_cpu_s, bounds.rlimit_cpu_s)
        )
        os.nice(bounds.nice)

    return _apply


def render_config(
    binary_path: str,
    config_path: Path,
    *,
    output_path: Path,
    bounds: RenderBounds,
    fader_db: float,
) -> RenderInvocation:
    """R9: invoke the pinned binary on ``config_path``, bounded, isolated.

    argv is exactly ``[binary_path, "--gain", str(fader_db), str(config_path)]``
    — no ``--statefile``, no ``-p``/``-a``. ``fader_db`` is R4(a)'s bracketed
    (before == after, proved stable) main-volume reading, reproduced here
    because R4(c) always resolves to "precedes the limiter" for the pinned
    build (see this module's docstring): every render's ``self.volume`` stage
    must apply the identical gain the live pass's fader applied, or the
    rendered peak is systematically off by the fader's dB. Bounds are
    process-local (``RLIMIT_AS``, ``RLIMIT_CPU``, ``os.nice``) applied in the
    child via ``preexec_fn``, on top of the same bounded-subprocess shape
    ``jasper/dsp_apply.py`` uses for ``--check`` (explicit argv, timeout,
    captured stdout/stderr, typed result).
    """

    argv = (binary_path, "--gain", str(fader_db), str(config_path))
    if any(token in _FORBIDDEN_ARGV_TOKENS for token in argv):
        raise RenderError(
            "render argv carries a statefile or websocket-bind token — refusing "
            f"before start: {argv!r}"
        )
    if not output_path.parent.exists():
        raise RenderError(f"render output directory does not exist: {output_path.parent}")
    if output_path.exists():
        output_path.unlink()

    start = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=bounds.timeout_s,
            check=False,
            preexec_fn=_rlimit_preexec(bounds),
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(
            f"render timed out after {bounds.timeout_s}s: argv={argv!r}"
        ) from exc
    except OSError as exc:
        raise RenderError(f"could not start render subprocess: {exc}") from exc
    duration_s = time.monotonic() - start

    stdout_tail = _tail((result.stdout or b"").decode("utf-8", errors="replace"))
    stderr_tail = _tail((result.stderr or b"").decode("utf-8", errors="replace"))
    if result.returncode != 0:
        raise RenderError(
            f"render exited {result.returncode}: argv={argv!r} "
            f"stdout={stdout_tail!r} stderr={stderr_tail!r}"
        )
    if not output_path.is_file():
        raise RenderError(
            f"render exited 0 but produced no output file: {output_path}"
        )
    return RenderInvocation(
        argv=argv,
        returncode=result.returncode,
        duration_s=duration_s,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        output_sha256=_sha256_file(output_path),
        output_byte_size=output_path.stat().st_size,
    )


@dataclass(frozen=True, slots=True)
class DeterminismReceipt:
    """R8: one config shape, rendered twice, proved SHA-identical."""

    config_sha256: str
    first: RenderInvocation
    second: RenderInvocation

    @property
    def deterministic(self) -> bool:
        return self.first.output_sha256 == self.second.output_sha256


def config_shape_sha256(yaml_text: str) -> str:
    """R8's "shape" identity: the exact byte content of a derived config."""

    return hashlib.sha256(yaml_text.encode("utf-8")).hexdigest()


def render_with_determinism_receipt(
    binary_path: str,
    config_path: Path,
    *,
    yaml_text: str,
    first_output_path: Path,
    second_output_path: Path,
    bounds: RenderBounds,
    fader_db: float,
) -> DeterminismReceipt:
    """R8: render ``config_path`` twice; refuse on any byte mismatch.

    ``fader_db`` is threaded to both renders via ``--gain`` (R4(c)/R9) — see
    :func:`render_config`. Establishes determinism only for the shape
    rendered here — it makes no claim about untested shapes. Callers key
    their receipt cache by :func:`config_shape_sha256` so a shape already
    receipted this campaign is never re-rendered twice more.
    """

    first = render_config(
        binary_path, config_path, output_path=first_output_path, bounds=bounds, fader_db=fader_db
    )
    second = render_config(
        binary_path, config_path, output_path=second_output_path, bounds=bounds, fader_db=fader_db
    )
    receipt = DeterminismReceipt(
        config_sha256=config_shape_sha256(yaml_text), first=first, second=second
    )
    if not receipt.deterministic:
        raise RenderError(
            "render is non-deterministic for this config shape "
            f"({receipt.config_sha256}): {first.output_sha256} != "
            f"{second.output_sha256} — the determinism assumption underlying "
            "the tap artifacts has been falsified on this host"
        )
    return receipt


def estimate_render_bytes(sample_rate_hz: int, channels: int, duration_s: float) -> int:
    """A conservative per-render byte estimate for the free-space guard.

    F64_LE (8 bytes/sample) at the full pipeline channel width — the
    playback file's actual precision (see
    :data:`DEPLOYED_PROCESSING_PRECISION`) — with a 2x safety margin for the
    WavFile capture artifact plus filesystem overhead.
    """

    bytes_per_frame = 8 * max(1, channels)
    return int(sample_rate_hz * bytes_per_frame * max(0.0, duration_s) * 2)


def check_free_space(
    bundle_dir: Path, *, per_render_estimate_bytes: int, renders_outstanding: int
) -> None:
    """R9: refuse before a render if free space is below the campaign estimate.

    Checked before EACH render against renders still outstanding for the
    WHOLE campaign, not just the current target.
    """

    if renders_outstanding <= 0:
        return
    required = per_render_estimate_bytes * renders_outstanding
    usage = shutil.disk_usage(bundle_dir)
    if usage.free < required:
        raise RenderError(
            f"free space {usage.free} bytes is below the campaign estimate "
            f"{required} bytes ({renders_outstanding} renders outstanding at "
            f"~{per_render_estimate_bytes} bytes each)"
        )


def extract_channel(
    raw_path: Path, *, channel_index: int, channel_count: int, bytes_per_sample: int
) -> bytes:
    """Extract one channel's samples from an interleaved raw-float render output.

    The playback file carries the full pipeline channel width (R3); the owner
    channel is extracted OFFLINE from the interleaved frame — no mixer or
    pipeline step is ever inserted for this. Operates on the raw
    ``File``-device payload (no WAV header — the derived playback device
    writes a headerless raw stream; ``wav_header`` is never set).
    """

    if channel_index < 0 or channel_index >= channel_count:
        raise RenderError(
            f"channel_index {channel_index} is outside [0, {channel_count})"
        )
    data = raw_path.read_bytes()
    frame_bytes = bytes_per_sample * channel_count
    if len(data) % frame_bytes != 0:
        raise RenderError(
            f"render output {raw_path} is not a whole number of "
            f"{channel_count}-channel frames"
        )
    offset = channel_index * bytes_per_sample
    out = bytearray()
    for frame_start in range(0, len(data), frame_bytes):
        start = frame_start + offset
        out += data[start : start + bytes_per_sample]
    return bytes(out)


# The pinned build's soft-clip transform (src/filters/limiter.rs):
#   let mut scaled = *val / self.clip_limit;
#   scaled = scaled.clamp(-1.5, 1.5);
#   scaled -= CUBEFACTOR * scaled.powi(3);
#   *val = scaled * self.clip_limit;
# with `const CUBEFACTOR: PrcFmt = 1.0 / 6.75;` and `clip_limit` converted
# from dB via `db_to_linear` (src/utils/decibels.rs: `10.0.powf(value/20.0)`,
# computed in PrcFmt = f64 for this deployed build).
_SOFT_CLIP_CUBEFACTOR = 1.0 / 6.75


def reference_soft_clip(samples: np.ndarray, *, clip_limit_dbfs: float) -> np.ndarray:
    """The digital_transfer_probe's "reference post-limiter stream produced
    by the pinned implementation" — a direct, float64 Python port of
    CamillaDSP v4.1.3's ``Limiter::apply_soft_clip`` (``soft_clip`` is always
    true per the frozen protocol, so only that branch is reproduced; the hard
    clip branch is never reached).

    The frozen transfer verdict is a byte-exact compare between this and the
    deployed binary's own render (R3) — this function's numerics must be
    exact, not merely close, hence the direct line-for-line port rather than
    a reimplementation from the general soft-clip concept.
    """

    clip_limit = 10.0 ** (clip_limit_dbfs / 20.0)
    scaled = np.asarray(samples, dtype=np.float64) / clip_limit
    scaled = np.clip(scaled, -1.5, 1.5)
    scaled = scaled - _SOFT_CLIP_CUBEFACTOR * np.power(scaled, 3)
    return scaled * clip_limit


__all__ = [
    "BinaryIdentity",
    "DEPLOYED_PROCESSING_PRECISION",
    "DERIVATION_FUNCTION_VERSION",
    "DeterminismReceipt",
    "PINNED_CAMILLADSP_VERSION",
    "RenderBounds",
    "RenderError",
    "RenderInvocation",
    "check_free_space",
    "config_shape_sha256",
    "estimate_render_bytes",
    "extract_channel",
    "reference_soft_clip",
    "render_config",
    "render_with_determinism_receipt",
    "resolve_render_binary",
]
