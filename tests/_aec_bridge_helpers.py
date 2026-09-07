# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The stats identity every AEC-bridge suite stamps its `_BridgeStats` with."""
from __future__ import annotations

from jasper.aec.bridge_engines import FRAME_SAMPLES, SAMPLE_RATE
from jasper.aec.bridge_telemetry import StatsIdentity

IDENTITY = StatsIdentity(
    sample_rate_hz=SAMPLE_RATE,
    frame_samples=FRAME_SAMPLES,
    reference_source="outputd_udp",
    reference_endpoint="127.0.0.1:9891",
)


def _rms_log_line(ref: int, mic: int, aec: int, attn_db: float) -> str:
    """Synthesize one bridge `rms over` log line in the journal `--output=cat`
    format the parser sees."""
    return (
        f"2026-05-16 17:00:00,000 aec-bridge INFO "
        f"rms over 5.0s: ref={ref} mic={mic} aec={aec} → "
        f"attenuation={attn_db:.1f} dB (frames=1 ref_q=0 mic_q=0 "
        f"ref_clip=0.00% out_clip=0.00%)"
    )
