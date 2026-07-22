# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Click/capture route-latency measurement harness — supporting modules.

Produces the real per-impulse latency evidence
`jasper.cli.route_latency_artifact` needs to certify (or honestly fail) the
`usb_low_latency_48k` route's p95/p99 claims. See
`jasper.cli.route_latency_harness` for the CLI entry point
(`jasper-route-latency-harness`) and `docs/HANDOFF-usb-low-latency.md` for
the end-to-end quick/promotion usage.

Module map:

  - `click_track` — generates the WAV a human plays on the host, plus its
    JSON schedule, sized to clear the certification gates in
    `jasper.audio_validation` with margin.
  - `impulse_detect` — the shared peak/hysteresis/refractory detector
    algorithm, mirrored (not imported — different runtime) by the Rust
    ingress tap in `rust/jasper-fanin`.
  - `mic_readers` — the egress (mic) audio sources: the default UDP raw0
    leg on `:9879`, and an ALSA fallback for boxes without an XVF3800.
  - `pairing` — nearest-match pairing of tap/mic detections with ambiguity
    rejection and a match-rate floor.
  - `tap_client` — the fan-in control-UDS client for the Rust tap's
    arm/disarm surface plus its JSONL event-log reader.

This package intentionally has no audio-device or network I/O at import
time (readers/clients open sockets lazily in `__init__`), so importing it
is cheap and safe from any environment, including CI without hardware.
"""
from __future__ import annotations

__all__: list[str] = []
