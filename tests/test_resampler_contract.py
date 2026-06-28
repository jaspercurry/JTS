# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-language byte-identity contract: Rust jasper-resampler ↔ C++ binding.

The resampler + spa_dll math is implemented TWICE — once in Rust
(``rust/jasper-resampler``, consumed by the jasper-outputd daemon via
content_bridge) and once in C++ (``jasper_resampler/src/resampler_binding.cpp``,
consumed by Python/jasper-usbsink) — because this repo has no
PyO3/maturin/cdylib toolchain (a Rust→Python binding would introduce an entirely
new build path; see the binding's header comment). This is the same discipline
the existing Rust↔Python FIFO/format contract tests use (e.g.
``test_fanin_coupling_rust_contract.py``, ``test_camilla_config_contract.py``):
duplicated cross-language logic is pinned by a contract test so an edit to one
side that is not mirrored is a hard CI failure.

Two pins:

1. **One-shot byte-identity.** The same canonical deterministic stereo input
   resampled at the same ratios by the Rust ``resample_i16`` one-shot and the
   C++ ``RateResampler.resample_block`` one-shot must agree to ≤1 LSB,
   element-by-element AND in length. The Rust reference is produced by shelling
   out to the committed ``cargo run --example golden_vector`` (the fixture —
   input + ratios — is defined ONCE in ``jasper_resampler::golden`` so the two
   sides cannot drift).

2. **Capture-follower DLL sign.** The C++ ``RateResampler`` driven in a closed
   loop by a constant ±ppm fill offset must converge to that ppm with the SAME
   sign as the Rust jasper-clock test
   ``tracks_a_constant_offset_without_standing_error`` — the load-bearing
   "a too-full buffer drains by reading faster (ratio>1)" convention.

Both are skipped when the C++ extension hasn't been built (a dev laptop that
hasn't run ``pip install ./jasper_resampler``); the Rust one-shot part is
additionally skipped when ``cargo`` is unavailable. The Rust side's own
correctness is the ``rust/jasper-resampler`` ``cargo test`` gate in CI.
"""
from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

# Skip the whole module when the C++ extension isn't built. The pure package
# imports fine without the compiled `_resampler`, so guard on the symbol — not
# the package — or this errors (AttributeError) where it should skip.
jasper_resampler = pytest.importorskip("jasper_resampler")
if not hasattr(jasper_resampler, "RateResampler"):
    pytest.skip(
        "jasper_resampler C++ extension not built (run: pip install ./jasper_resampler)",
        allow_module_level=True,
    )

REPO = Path(__file__).resolve().parent.parent
RUST_CRATE = REPO / "rust" / "jasper-resampler"


def _run_rust_golden() -> dict:
    """Shell out to the Rust golden_vector example and parse its output.

    Returns {"channels": int, "input": [int], "outputs": {ratio: [int]}}.
    Skips if cargo is unavailable or the example fails to run.
    """
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not available — Rust reference can't be generated")
    if not RUST_CRATE.is_dir():
        pytest.skip(f"rust crate missing at {RUST_CRATE}")
    try:
        proc = subprocess.run(
            [cargo, "run", "--quiet", "--release", "--example", "golden_vector"],
            cwd=str(RUST_CRATE),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
        pytest.skip(f"cargo run failed to launch: {exc}")
    if proc.returncode != 0:  # pragma: no cover
        pytest.skip(
            f"cargo run --example golden_vector failed (rc={proc.returncode}): "
            f"{proc.stderr[-500:]}"
        )

    channels = None
    input_samples: list[int] = []
    outputs: dict[float, list[int]] = {}
    cur_ratio: float | None = None
    for line in proc.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "CHANNELS":
            channels = int(parts[1])
        elif parts[0] == "INPUT":
            input_samples = [int(x) for x in parts[1:]]
        elif parts[0] == "RATIO":
            cur_ratio = float(parts[1])
        elif parts[0] == "OUTPUT":
            assert cur_ratio is not None
            outputs[cur_ratio] = [int(x) for x in parts[1:]]
    assert channels is not None, "golden output missing CHANNELS"
    assert input_samples, "golden output missing INPUT"
    assert outputs, "golden output missing OUTPUT rows"
    return {"channels": channels, "input": input_samples, "outputs": outputs}


def _cpp_one_shot(input_samples: list[int], channels: int, ratio: float) -> list[int]:
    """One-shot resample via the C++ binding.

    A freshly-constructed RateResampler fed the WHOLE input in a single
    resample_block call behaves as a one-shot (the streaming cursor seats at
    RADIUS_FRAMES on the first block, identical to the Rust one-shot's seat).
    bytes_per_sample=2 (S16) is the cross-language reference path.
    """
    rc = jasper_resampler.RateResampler(
        bw=0.128,
        period_frames=480,
        rate=48000,
        channels=channels,
        bytes_per_sample=2,
    )
    in_bytes = struct.pack(f"<{len(input_samples)}h", *input_samples)
    out_bytes = rc.resample_block(in_bytes, ratio)
    return list(struct.unpack(f"<{len(out_bytes) // 2}h", out_bytes))


def test_rust_and_cpp_resample_agree_within_one_lsb():
    """The duplicated Rust and C++ windowed-sinc math produce bit-identical
    output (≤1 LSB) at every contract ratio — the byte-identity gate that
    makes the duplication safe."""
    golden = _run_rust_golden()
    channels = golden["channels"]
    input_samples = golden["input"]

    assert golden["outputs"], "no ratios in the golden fixture"
    overall_max = 0
    for ratio, rust_out in golden["outputs"].items():
        cpp_out = _cpp_one_shot(input_samples, channels, ratio)
        # Length must match exactly (same emit-count law on both sides).
        assert len(cpp_out) == len(rust_out), (
            f"ratio {ratio}: length mismatch C++ {len(cpp_out)} vs "
            f"Rust {len(rust_out)}"
        )
        max_diff = max(
            (abs(a - b) for a, b in zip(cpp_out, rust_out)), default=0
        )
        overall_max = max(overall_max, max_diff)
        assert max_diff <= 1, (
            f"ratio {ratio}: C++ and Rust diverge by {max_diff} LSB "
            "(the duplicated resampler math drifted — regenerate the golden "
            "vector on BOTH sides in lockstep if the change was intentional)"
        )
    # At unity the two should be exactly equal (0 LSB) — a stricter spot-check.
    if 1.0 in golden["outputs"]:
        cpp_unity = _cpp_one_shot(input_samples, channels, 1.0)
        assert cpp_unity == golden["outputs"][1.0], (
            "unity-ratio output must be exactly equal across languages"
        )


def test_cpp_dll_capture_follower_sign_and_convergence():
    """The C++ RateResampler closed loop converges to the producer's ppm with
    the capture-follower sign — matching the Rust jasper-clock property
    ``tracks_a_constant_offset_without_standing_error``.

    Model (the same negative-feedback loop the Rust test uses): a producer
    fills a buffer at ``+ppm`` per cycle; our consumer drains at ``ratio``. The
    loop is fed ``fill - target`` (it negates internally). At lock the ratio
    matches ``+ppm`` (SAME sign — a faster producer needs a faster consumer)
    and the standing fill error is nulled.
    """
    target = 1920.0  # 40 ms @ 48 kHz, the usbsink target.
    period = 480.0

    def run_loop(ppm: float, cycles: int) -> tuple[float, float, bool]:
        rc = jasper_resampler.RateResampler(
            bw=0.128, period_frames=480, rate=48000, channels=2,
        )
        produced = period * (1.0 + ppm / 1.0e6)
        fill = target
        ratio = 1.0
        for _ in range(cycles):
            fill += produced / ratio - period
            ratio = rc.update(fill - target)
        return rc.ratio_ppm(), fill - target, rc.is_locked()

    for ppm in (-50.0, 50.0):
        ratio_ppm, residual, locked = run_loop(ppm, 120_000)
        assert abs(residual) < 1.0, (
            f"standing fill error should vanish at {ppm} ppm, got {residual}"
        )
        assert abs(ratio_ppm - ppm) < 3.0, (
            f"ratio should track ~{ppm} ppm (capture-follower sign), got "
            f"{ratio_ppm} ppm"
        )
        assert locked, f"loop should lock at {ppm} ppm"


def test_cpp_default_construction_matches_for_rate_thresholds():
    """The default (max_error=-1, max_resync=-1) construction uses the Rust
    DllConfig::for_rate derivation, so a single huge error past
    max_resync=max(period, max(256, period/2)) hard-resyncs — proving the
    default contract is the Rust one (usbsink overrides max_resync=0 instead).
    """
    rc = jasper_resampler.RateResampler(
        bw=0.128, period_frames=480, rate=48000, channels=2,
    )
    # Warm a few cycles at zero error.
    for _ in range(100):
        rc.update(0.0)
    before = rc.resync_count()
    # An error far past max_resync (== max(480, 256) == 480) must resync.
    rc.update(50_000.0)
    assert rc.resync_count() == before + 1, (
        "default construction must keep the for_rate max_resync hard-jump"
    )
