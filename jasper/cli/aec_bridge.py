"""Software AEC bridge — `jasper-aec-bridge` (Python).

REPLACES the CamillaDSP-based aec-bridge. The XVF3800's on-chip
AEC turned out to be architecturally incompatible with our
"external USB DAC for the speaker" topology — the chip's AEC
pipeline assumes the chip's own audio output drives the speaker
(see XMOS XVF3800 user guide §3.5; all "Far end" categories are
defined as I²S sources). Even with USB-IN reference + correct
volume mirroring, the chip's adaptive filter doesn't reliably
attenuate echo in our config.

This bridge does the AEC in software, with chip-processed mic
(channel 1 of the chip's 6-channel USB capture — the ASR beam
with chip BF + NS + AGC + HPF applied, but with the chip's own
AEC stage disabled via SHF_BYPASS=1 in jasper-aec-init) as
near-end, and the host-side music chain as far-end. The engine
is WebRTC AEC3 via the `jasper_aec3` pybind11 binding around
Trixie's `libwebrtc-audio-processing-dev` (v1.3-3 — which IS
AEC3; the 1.x is package-API stability versioning, not algorithm
version). AEC3 includes a frequency-domain residual echo
suppressor + drift-tolerant delay estimator and runs at ~3-8% of
one Pi 5 core. See docs/HANDOFF-aec.md for the full investigation.

JTS previously read raw mic 0 (channel 2) but switched to channel
1 on 2026-05-15 after confirming via XMOS primary docs that
channels 2-5 bypass every chip DSP stage (no BF, NS, AGC, HPF,
not even MIC_GAIN). The canonical XVF3800 voice-assistant capture
is channel 0/1 — see HANDOFF-xvf3800.md §3.

Topology:

    pcm.jasper_capture (48k stereo, host clock)
       │  reference signal (what the speaker is being asked to play)
       ▼
    [downsample 48→16k, L+R summed to mono, HPF at 125 Hz]         16k mono ref
       │
       │      hw:Array,0 ch 1 (16k mono, chip clock)
       │  chip ASR beam: BF + NS + AGC + HPF, chip AEC disabled
       │       │
       ▼       ├──────────────────────────────────────────────────┐
    WebRTC AEC3 (jasper_aec3 binding)                             │
       │  AEC'd mono mic                                          │  chip-direct mic (pre-AEC3)
       ▼                                                          ▼
    UDP 127.0.0.1:JASPER_AEC_UDP_PORT (default 9876)      UDP 127.0.0.1:JASPER_AEC_UDP_PORT_RAW
       │  one packet per 1280 samples (80 ms, matches             │  (default 9877)
       │  MicCapture frame size)                                  │  same packet shape
       ▼                                                          ▼
    jasper-voice's UdpMicCapture (binds 9876)             jasper-voice's second
                                                          UdpMicCapture (binds 9877)
                                                          for dual-stream wake-word
                                                          detection (PR 2 of the
                                                          wake-telemetry series —
                                                          see docs/HANDOFF-wake-
                                                          telemetry.md).

Why UDP instead of the previous snd-aloop `LoopbackAEC` card: see
the `UdpMicCapture` docstring in jasper/audio_io.py. Short version:
snd-aloop's `loopback_cable` kernel struct wedges when a consumer
is SIGKILL'd, requiring a reboot to clear. UDP has no kernel-side
state and `sendto()` is non-blocking, which orthogonally fixes the
PortAudio-write SIGTERM-observability bug from the 2026-05-11
incident. Validated end-to-end in PR 2 of the resilience-ladder
series.

Caveats this implementation does NOT yet address:
  - Reference and mic are on independent clock domains (kernel
    timer vs XVF chip's USB UAC2 SYNC). They WILL drift over
    time — AEC3's delay estimator tolerates some drift but not
    unbounded. Future fix: drift-compensate via resampling, or
    expose a USB-side reference channel from the chip (would
    require custom firmware).
  - Frame alignment between two PortAudio streams isn't perfectly
    synchronized — the ref-mic offset can vary by a few ms each
    restart. AEC3 auto-adapts (the `stream_delay_ms` hint is just
    a starting point for its delay estimator) but convergence
    takes longer when the offset drifts.
  - The engine is the linear AEC3 + residual suppressor only; no
    neural residual stage. See docs/HANDOFF-aec.md "Deep tuning
    landscape" for the staged options if AEC3 + REF_GAIN + MIC_GAIN
    isn't enough.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from queue import Queue, Empty, Full
from typing import Optional

import numpy as np
import sounddevice as sd
from scipy.signal import butter, resample_poly, sosfilt

from jasper.watchdog import Heartbeat
from ..mics import xvf3800 as _mic_profile

logger = logging.getLogger("jasper.aec_bridge")

# Frame size: 320 samples @ 16 kHz = 20 ms, a multiple of WebRTC
# AEC3's 10 ms frame requirement (160 samples). The binding splits
# 320 → 2×160 internally per the AEC3 API contract. AEC3 manages
# its own filter length internally.
FRAME_SAMPLES = 320
SAMPLE_RATE = 16000

# Capture device for the reference (host-clocked dsnoop on the
# renderer→camilla loopback). `jasper_ref` is a plug-wrapped alias
# of `jasper_capture` defined in /root/.asoundrc — the plug layer
# resamples from whatever rate the snd-aloop loopback is locked
# at to REF_RATE below. Without the plug wrapping, the bridge
# silently received zero-RMS audio whenever a 44.1 kHz source
# (AirPlay, librespot 44.1k tracks, BT A2DP) locked the loopback
# at a non-48 kHz rate — the regression that broke production AEC
# after PR #75 changed shairport to output at native 44.1 kHz.
# CamillaDSP uses the same plug pattern via `plug:jasper_capture`
# in v1.yml — this just extends it to the bridge.
REF_DEVICE = "jasper_ref"
REF_RATE = 48000  # what we ask plug for; plug resamples slave to this
REF_CHANNELS = 2

# Capture device for the mic. Chip's 6-ch firmware exposes
# channels 0=Conference, 1=ASR (both go through BF + NS + AGC +
# HPF; the chip's own AEC stage is disabled via SHF_BYPASS=1 in
# jasper-aec-init), 2-5=raw mics 0-3 (no chip processing of any
# kind). The mic profile pins MIC_CHANNEL_INDEX=1 (ASR beam) —
# canonical XVF3800 voice-assistant choice per Seeed wiki and
# every public reference design.
# Device names are PortAudio substring matches (sounddevice's
# backend) — NOT ALSA pcm strings. PortAudio enumerates ALSA
# cards by their card description, not by hw:CARD= syntax.
# Default matches "Array: USB Audio (hw:N,0)".
MIC_DEVICE = os.environ.get("JASPER_AEC_MIC_DEVICE", _mic_profile.ALSA_CARD_NAME)
MIC_CHANNELS = _mic_profile.RECOMMENDED_FIRMWARE.capture_channels
MIC_CHANNEL_INDEX = _mic_profile.MIC_CHANNEL_INDEX

# Output transport: UDP localhost. Bridge sends AEC'd mono int16
# frames to `127.0.0.1:JASPER_AEC_UDP_PORT`; jasper-voice's
# `UdpMicCapture` binds the same port and receives.
#
# Why UDP instead of the old snd-aloop `LoopbackAEC` card: see the
# `UdpMicCapture` docstring in jasper/audio_io.py. Short version:
# snd-aloop's `loopback_cable` kernel struct wedges if a consumer
# is SIGKILL'd, requiring `rmmod && modprobe` (with every consumer
# stopped first) or a reboot to recover. UDP has no kernel-side
# state to corrupt and `sendto()` is non-blocking, which
# orthogonally fixes the daemon's SIGTERM-observability bug from
# the 2026-05-11 incident.
OUT_HOST = os.environ.get("JASPER_AEC_UDP_HOST", "127.0.0.1")
OUT_PORT = int(os.environ.get("JASPER_AEC_UDP_PORT", "9876"))
OUT_RATE = 16000

# Secondary UDP output: chip-direct mic stream, pre-AEC3 — exactly
# the same near-end input AEC3 consumes (chip ch 1 = ASR beam with
# chip BF + NS + AGC + HPF applied, chip AEC disabled via
# SHF_BYPASS=1). Emitted on a separate port so jasper-voice's wake
# loop (PR 2 of the wake-telemetry series) can score wake-word
# detection on BOTH the post-AEC stream (OUT_PORT) and the chip-
# direct stream (OUT_PORT_RAW). Same 1280-sample / 16 kHz mono
# int16 packet shape as the primary stream so the consumer sees
# identical chunk sizes.
#
# Why expose this: the 2026-05-20 wake-rate sweep showed the AEC ON
# and AEC OFF legs catch mostly-disjoint sets of utterances —
# test-1 yielded a 40 % union vs 25 % best single leg (see
# HANDOFF-aec.md "Open work streams — option C"). Emitting both
# lets the wake loop OR the detections without changing the AEC
# pipeline. See docs/HANDOFF-wake-telemetry.md for the end-to-end
# design.
#
# Nothing consumes 9877 today (PR 1 of the series is pure
# plumbing). Safe to deploy alone; jasper-voice ignores it until
# PR 2 ships.
OUT_PORT_RAW = int(os.environ.get("JASPER_AEC_UDP_PORT_RAW", "9877"))
# Voice consumes 1280-sample (80 ms) chunks. Aggregating four
# 320-sample AEC frames into one UDP packet keeps the
# bridge↔voice contract symmetric with the existing MicCapture
# frame size and halves packet rate to ~12.5 pps. The AEC engine
# still works on 320-sample windows internally.
OUT_FRAME_SAMPLES = 1280
OUT_FRAME_BYTES = OUT_FRAME_SAMPLES * 2  # int16

# Drop-frame threshold. If queues fill faster than they drain,
# something's wrong (CPU starvation, clock drift exceeded our
# margin). We log and drop rather than block.
QUEUE_MAXSIZE = 32

_shutdown = threading.Event()


class BridgeStalled(RuntimeError):
    """Mic capture has produced no frames for the configured
    threshold (JASPER_AEC_STALL_RESTART_SEC, default 5s).

    Raised by `_aec_loop` to bail with a non-zero exit code so
    systemd's `Restart=on-failure` revives us with a fresh
    `sd.InputStream`. PortAudio's InputStream is one-shot — once
    its ALSA capture PCM enters an unrecoverable state (typically
    after a USB underrun on the XVF chip's UAC2 endpoint), the
    callback simply stops being invoked. There's no in-process
    recovery path; only a new process gets a working stream.
    Hit in production 2026-05-11: bridge silently stopped feeding
    the voice mic path for ~10 minutes, wake-word detection got no audio,
    Hey Jarvis was unresponsive with no audible cue.
    """


class MicDeviceUnavailable(RuntimeError):
    """The configured PortAudio mic device is not currently present."""


# Clipping counters (module-level for cheap cross-thread access; small
# race conditions in increment+reset are benign — worst case a single
# log window's percentage is off by a frame). Tracked separately for
# the ref pre-clip stage (after JASPER_AEC_REF_GAIN_DB applied) and
# the post-AEC mic stage (after JASPER_AEC_MIC_GAIN_DB applied).
_ref_clipped_samples = 0
_ref_total_samples = 0
_out_clipped_samples = 0
_out_total_samples = 0

# Counter for `ref_q empty when main loop polled` events. See
# `_aec_loop` for why this happens (ALSA delivers ref in 2-period
# bursts every 40 ms, mic at smooth 20 ms cadence — half of main-loop
# polls land between bursts). Logged in the periodic RMS line so the
# rate is observable; an unusually high rate (say > 10 per 5 s window
# = > 2 Hz) indicates the timing balance has drifted and Fix A's
# stale-ref-reuse is doing heavier lifting than expected.
_ref_starved_frames = 0


def _env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in (
        "1", "true", "yes", "on",
    )


class _Aec3Engine:
    """WebRTC AEC3 via the jasper_aec3 v1.3-3 (legacy) pybind11 binding.

    Splits each FRAME_SAMPLES (20 ms) buffer into 2× 10 ms windows
    internally, calls ProcessReverseStream + ProcessStream per
    window, returns the joined AEC'd capture. Top-level
    AudioProcessing::Config knobs only (no deep EchoCanceller3Config
    access). Fallback engine when the v2 binding isn't built.
    """

    def __init__(self) -> None:
        from jasper_aec3 import Aec3

        ns_enabled = _env_bool("JASPER_AEC_NS_ENABLED", "1")
        ns_level = os.environ.get(
            "JASPER_AEC_NS_LEVEL", "low",
        ).strip().lower()
        agc1_enabled = _env_bool("JASPER_AEC_AGC1_ENABLED", "0")
        agc1_target_dbfs = int(os.environ.get(
            "JASPER_AEC_AGC1_TARGET_DBFS", "9",
        ))
        agc1_max_gain_db = int(os.environ.get(
            "JASPER_AEC_AGC1_MAX_GAIN_DB", "18",
        ))
        enable_agc2 = _env_bool("JASPER_AEC_AGC2", "0")
        self._aec = Aec3(
            enable_agc2=enable_agc2,
            ns_enabled=ns_enabled,
            ns_level=ns_level,
            agc1_enabled=agc1_enabled,
            agc1_target_dbfs=agc1_target_dbfs,
            agc1_max_gain_db=agc1_max_gain_db,
        )
        logger.info(
            "engine=aec3_v1 ns=%s/%s agc1=%s(target=%d,max=%ddB) "
            "agc2=%s frame=%d rate=%d",
            "on" if ns_enabled else "off", ns_level,
            "on" if agc1_enabled else "off",
            agc1_target_dbfs, agc1_max_gain_db,
            "on" if enable_agc2 else "off",
            FRAME_SAMPLES, SAMPLE_RATE,
        )

    def process(self, mic: bytes, ref: bytes) -> bytes:
        return self._aec.process(mic, ref)

    def close(self) -> None:
        # The pybind11 wrapper's std::unique_ptr<AudioProcessing>
        # is freed when the Python Aec3 instance is GC'd. No
        # explicit teardown needed.
        pass


class _Aec3V2Engine:
    """WebRTC AEC3 via the jasper_aec3 v2.1 vendored-static binding.

    Exposes the deep EchoCanceller3Config knobs the v1 binding can't
    reach — required for the BEST_A canonical config from the
    2026-05-22 tuning campaign. Defaults to BEST_A; env vars override
    each knob individually.

    BEST_A config (the Aec3V2 constructor's pybind11 defaults already
    match these — no override needed for default behavior):
        filter_refined_length_blocks=30
        ep_strength_bounded_erl=False
        ep_strength_default_gain=0.3
        erle_max_l=1.5, erle_max_h=1.0
        erle_onset_detection=False
        use_stationarity_properties=True
        conservative_hf_suppression=True
        normal_mask_hf_enr_transparent=0.3
        normal_mask_hf_enr_suppress=0.4
        normal_mask_hf_emr_transparent=0.3
        normal_max_dec_factor_lf=0.05

    Env-var overrides (all optional; default to BEST_A):
        JASPER_AEC_FILTER_LENGTH        (int, default 30)
        JASPER_AEC_BOUNDED_ERL          (bool, default false)
        JASPER_AEC_DEFAULT_GAIN         (float, default 0.3)
        JASPER_AEC_ERLE_MAX_L           (float, default 1.5)
        JASPER_AEC_ERLE_MAX_H           (float, default 1.0)
        JASPER_AEC_ERLE_ONSET           (bool, default false)
        JASPER_AEC_USE_STATIONARITY     (bool, default true)
        JASPER_AEC_CONSERVATIVE_HF      (bool, default true)
        JASPER_AEC_MASK_HF_ENR_T        (float, default 0.3)
        JASPER_AEC_MASK_HF_ENR_S        (float, default 0.4)
        JASPER_AEC_MASK_HF_EMR_T        (float, default 0.3)
        JASPER_AEC_MAX_DEC_LF           (float, default 0.05)
    """

    def __init__(self) -> None:
        from jasper_aec3 import Aec3V2

        # Top-level (shared with v1)
        ns_enabled = _env_bool("JASPER_AEC_NS_ENABLED", "1")
        ns_level = os.environ.get("JASPER_AEC_NS_LEVEL", "low").strip().lower()
        agc1_enabled = _env_bool("JASPER_AEC_AGC1_ENABLED", "1")  # BEST_A default
        agc1_target_dbfs = int(os.environ.get("JASPER_AEC_AGC1_TARGET_DBFS", "9"))
        agc1_max_gain_db = int(os.environ.get("JASPER_AEC_AGC1_MAX_GAIN_DB", "18"))
        enable_agc2 = _env_bool("JASPER_AEC_AGC2", "0")

        # Deep EchoCanceller3Config — defaults from BEST_A
        filter_length = int(os.environ.get("JASPER_AEC_FILTER_LENGTH", "30"))
        bounded_erl = _env_bool("JASPER_AEC_BOUNDED_ERL", "0")
        default_gain = float(os.environ.get("JASPER_AEC_DEFAULT_GAIN", "0.3"))
        erle_max_l = float(os.environ.get("JASPER_AEC_ERLE_MAX_L", "1.5"))
        erle_max_h = float(os.environ.get("JASPER_AEC_ERLE_MAX_H", "1.0"))
        erle_onset = _env_bool("JASPER_AEC_ERLE_ONSET", "0")
        use_stationarity = _env_bool("JASPER_AEC_USE_STATIONARITY", "1")
        conservative_hf = _env_bool("JASPER_AEC_CONSERVATIVE_HF", "1")
        mask_hf_enr_t = float(os.environ.get("JASPER_AEC_MASK_HF_ENR_T", "0.3"))
        mask_hf_enr_s = float(os.environ.get("JASPER_AEC_MASK_HF_ENR_S", "0.4"))
        mask_hf_emr_t = float(os.environ.get("JASPER_AEC_MASK_HF_EMR_T", "0.3"))
        max_dec_lf = float(os.environ.get("JASPER_AEC_MAX_DEC_LF", "0.05"))

        self._aec = Aec3V2(
            stream_delay_ms=40,
            enable_agc2=enable_agc2,
            ns_enabled=ns_enabled,
            ns_level=ns_level,
            agc1_enabled=agc1_enabled,
            agc1_target_dbfs=agc1_target_dbfs,
            agc1_max_gain_db=agc1_max_gain_db,
            filter_refined_length_blocks=filter_length,
            ep_strength_bounded_erl=bounded_erl,
            ep_strength_default_gain=default_gain,
            erle_max_l=erle_max_l,
            erle_max_h=erle_max_h,
            erle_onset_detection=erle_onset,
            use_stationarity_properties=use_stationarity,
            conservative_hf_suppression=conservative_hf,
            normal_mask_hf_enr_transparent=mask_hf_enr_t,
            normal_mask_hf_enr_suppress=mask_hf_enr_s,
            normal_mask_hf_emr_transparent=mask_hf_emr_t,
            normal_max_dec_factor_lf=max_dec_lf,
        )
        logger.info(
            "engine=aec3_v2(BEST_A) ns=%s/%s agc1=%s(target=%d,max=%ddB) "
            "agc2=%s filter_len=%d bounded_erl=%s default_gain=%.2f "
            "erle=%.2f/%.2f onset=%s stationarity=%s conservative_hf=%s "
            "mask_hf=%.2f/%.2f/%.2f max_dec_lf=%.3f",
            "on" if ns_enabled else "off", ns_level,
            "on" if agc1_enabled else "off",
            agc1_target_dbfs, agc1_max_gain_db,
            "on" if enable_agc2 else "off",
            filter_length, bounded_erl, default_gain,
            erle_max_l, erle_max_h,
            "on" if erle_onset else "off",
            "on" if use_stationarity else "off",
            "on" if conservative_hf else "off",
            mask_hf_enr_t, mask_hf_enr_s, mask_hf_emr_t,
            max_dec_lf,
        )

    def process(self, mic: bytes, ref: bytes) -> bytes:
        return self._aec.process(mic, ref)

    def close(self) -> None:
        pass


def _select_engine():
    """Pick the AEC engine to use.

    JASPER_AEC_BINDING=v2 forces v2; =v1 forces v1; default (=auto)
    tries v2 first, falls back to v1 if the v2 module isn't built.
    Returns an engine instance ready to call .process().
    """
    pref = os.environ.get("JASPER_AEC_BINDING", "auto").strip().lower()
    if pref == "v1":
        return _Aec3Engine()
    if pref == "v2":
        return _Aec3V2Engine()
    # auto
    try:
        import jasper_aec3
        if jasper_aec3.HAS_V2:
            return _Aec3V2Engine()
    except ImportError:
        pass
    logger.info(
        "jasper_aec3._aec3_v2 not available — falling back to v1 binding"
    )
    return _Aec3Engine()


def _validate_mic_device() -> None:
    """Fail before opening the shared reference tap if the mic is absent.

    The ref capture reads from `jasper_capture`, the same dsnoop PCM
    CamillaDSP uses for music. If the mic device is missing, starting
    the ref reader anyway creates a pointless second reader until the
    stall watchdog exits. Validate the mic first so missing hardware
    cannot perturb the music path.
    """
    try:
        sd.query_devices(MIC_DEVICE, "input")
    except Exception as e:  # noqa: BLE001
        raise MicDeviceUnavailable(
            f"mic device {MIC_DEVICE!r} unavailable: {e}"
        ) from e


def _ref_thread(ref_q: Queue) -> None:
    global _ref_clipped_samples, _ref_total_samples
    """Capture 48k stereo ref via alsaaudio (PortAudio doesn't see
    custom asoundrc PCMs like `jasper_capture`), sum L+R to mono,
    downsample to 16k. Push frames of exactly FRAME_SAMPLES samples
    (= 2*FRAME_SAMPLES bytes mono int16) onto the queue.

    Why L+R sum (not left-only): the speakers radiate the sum of
    L and R into a single mic. AEC3 is mono-reference, so we get
    one shot at modeling the echo path. Feeding it L-only would
    blind it to whatever is panned to R — bass, vocals, lead
    instruments — which for typical stereo music is a substantial
    portion of the energy. Summing matches what the room actually
    contains. (The XMOS chip's USB-IN AEC requires left-only per
    datasheet §3.3, but we are not using that path.)

    alsaaudio.PCM.read() can return partial reads (especially the
    first one as the stream warms up), so we accumulate at the 48k
    rate and only emit complete capture_block-sized chunks. This
    guarantees every queued frame matches the mic frame size — the
    WebRTC AEC3 engine enforces equal lengths strictly.

    Optional pre-AEC reference gain (`JASPER_AEC_REF_GAIN_DB`):
    boosts the digital ref before it enters the AEC engine. AEC3
    was tuned for conferencing setups where ref RMS ≈ mic RMS or
    ref is louder; in our smart-speaker setup the digital ref is
    typically 25-30 dB *quieter* than what the mic captures (amp +
    speakers + room amplify the chain). Boosting ref closes that
    gap so the adaptive filter operates near its design point. See
    docs/HANDOFF-aec.md "Tuning findings" for measured impact."""
    import alsaaudio
    import time as _time
    capture_block = FRAME_SAMPLES * (REF_RATE // SAMPLE_RATE)
    ref_gain_db = float(os.environ.get("JASPER_AEC_REF_GAIN_DB", "0"))
    ref_gain_lin = 10.0 ** (ref_gain_db / 20.0)

    # Reference HPF — matches the effective mic-side cutoff so AEC3
    # sees symmetric inputs. Default 125 Hz to match the chip's
    # AEC_HPFONOFF=on125 (4th-order Butter at mic ingress, applied
    # to channels 0/1 in the chip pipeline). Without symmetric
    # reference filtering, AEC3's adaptive filter wastes coefficients
    # trying to model an LF relationship the mic doesn't have.
    # 125 Hz is above openWakeWord's 60 Hz mel floor for the
    # reference; this filter applies to the reference, not the mic,
    # so wake-word accuracy is unaffected regardless. See
    # docs/HANDOFF-aec.md for the analysis.
    REF_HPF_HZ = float(os.environ.get("JASPER_AEC_REF_HPF_HZ", "125"))
    hpf_sos = butter(2, REF_HPF_HZ, btype="highpass", fs=SAMPLE_RATE,
                     output="sos")
    # Per-section state, shape (n_sections, 2) for order-2 SOS sections.
    # All zeros = starting from silence (correct for thread startup).
    hpf_zi = np.zeros((hpf_sos.shape[0], 2), dtype=np.float64)

    pcm = alsaaudio.PCM(
        type=alsaaudio.PCM_CAPTURE,
        mode=alsaaudio.PCM_NORMAL,  # blocking
        device=REF_DEVICE,
        rate=REF_RATE,
        channels=REF_CHANNELS,
        format=alsaaudio.PCM_FORMAT_S16_LE,
        periodsize=capture_block,
    )
    logger.info(
        "ref capture opened: %s @ %d Hz, %d ch "
        "(pre-AEC gain=%+.1f dB, HPF=%.0f Hz 2nd Butter)",
        REF_DEVICE, REF_RATE, REF_CHANNELS, ref_gain_db, REF_HPF_HZ,
    )
    accum_48 = np.empty(0, dtype=np.float32)
    # Drop-rate debouncing: during a mic stall the ref keeps producing
    # at ~50 Hz, so a naive per-frame WARNING floods the journal with
    # hundreds of entries that all say the same thing. Aggregate the
    # count and log one summary per second instead.
    drops_in_window = 0
    last_drop_log = 0.0
    try:
        while not _shutdown.is_set():
            length, data = pcm.read()
            if length <= 0:
                continue
            arr = np.frombuffer(data, dtype=np.int16)
            # interleaved stereo → sum L+R to mono (×0.5 to keep
            # peak-level the same, so REF_GAIN_DB tuning remains valid)
            left48 = arr[0::REF_CHANNELS].astype(np.float32)
            right48 = arr[1::REF_CHANNELS].astype(np.float32)
            mono48 = (left48 + right48) * 0.5
            accum_48 = np.concatenate([accum_48, mono48])
            # Emit exact-sized chunks at the 48k rate so each
            # downsample yields exactly FRAME_SAMPLES at 16k.
            while accum_48.size >= capture_block:
                chunk = accum_48[:capture_block]
                accum_48 = accum_48[capture_block:]
                mono16 = resample_poly(chunk, up=1, down=3)
                # HPF before gain — matches AEC3's internal HPF on the
                # capture side. Stateful across chunks (zi carried over)
                # so there's no per-chunk transient.
                mono16, hpf_zi = sosfilt(hpf_sos, mono16, zi=hpf_zi)
                if ref_gain_lin != 1.0:
                    mono16 = mono16 * ref_gain_lin
                # Track samples that the hard-clip below will saturate.
                # Reported in the periodic RMS log so we can see if the
                # gain stage is destroying peak information.
                _ref_clipped_samples += int(np.sum(np.abs(mono16) > 32767))
                _ref_total_samples += len(mono16)
                mono16 = np.clip(mono16, -32768, 32767).astype(np.int16)
                try:
                    ref_q.put_nowait(mono16.tobytes())
                except Full:
                    drops_in_window += 1
            now = _time.monotonic()
            if drops_in_window > 0 and now - last_drop_log >= 1.0:
                logger.warning(
                    "ref queue full, dropped %d frames in last %.1fs "
                    "(mic queue likely empty — see next stall log)",
                    drops_in_window, now - last_drop_log if last_drop_log else 1.0,
                )
                drops_in_window = 0
                last_drop_log = now
    finally:
        pcm.close()


def _mic_thread(mic_q: Queue) -> None:
    """Capture 16k 6ch from XVF chip (6-ch firmware), pluck
    channel MIC_CHANNEL_INDEX (default 1 = ASR beam, chip
    BF+NS+AGC+HPF applied, chip AEC disabled via SHF_BYPASS).
    Push mono int16 frames."""
    def cb(indata, frames, time_info, status):
        if status:
            logger.debug("mic status: %s", status)
        if _shutdown.is_set():
            return
        mono = indata[:, MIC_CHANNEL_INDEX].astype(np.int16, copy=True)
        try:
            mic_q.put_nowait(mono.tobytes())
        except Full:
            logger.warning("mic queue full, dropping frame")

    with sd.InputStream(
        device=MIC_DEVICE, samplerate=SAMPLE_RATE, channels=MIC_CHANNELS,
        dtype="int16", blocksize=FRAME_SAMPLES, callback=cb,
    ):
        _shutdown.wait()


def _aec_loop(  # noqa: PLR0915
    ref_q: Queue, mic_q: Queue, engine: _Aec3Engine,
    heartbeat: Optional[Heartbeat] = None,
) -> None:
    # Post-AEC static gain applied to the engine output before it
    # reaches jasper-voice over UDP. Restores level into openWakeWord's training
    # distribution — the HA Voice PE pattern (`gain_factor: 4`) — when
    # the chip's mic preamp delivers a quiet AEC output. Default 0 dB
    # (off). Soft-clipped via tanh on the way out so high gain doesn't
    # injecting hard-clip distortion into the wake-word input. See
    # docs/HANDOFF-aec.md tuning findings for tested values.
    global _ref_clipped_samples, _ref_total_samples
    global _out_clipped_samples, _out_total_samples
    global _ref_starved_frames
    mic_gain_db = float(os.environ.get("JASPER_AEC_MIC_GAIN_DB", "0"))
    mic_gain_lin = 10.0 ** (mic_gain_db / 20.0)
    # Stall-recovery threshold: consecutive seconds of empty mic_q
    # before we bail for a systemd-driven restart. 0 = disabled
    # (legacy "log forever" behaviour). See BridgeStalled docstring.
    stall_restart_sec = int(
        float(os.environ.get("JASPER_AEC_STALL_RESTART_SEC", "5"))
    )
    consecutive_empty_sec = 0
    """Drain both queues frame-by-frame, run the selected AEC
    engine, write to Loopback. The two queues drift independently;
    we loosely sync by always pulling one mic frame and the
    freshest ref frame we can grab without blocking — falling back
    to silence if no ref is available (shouldn't happen if camilla
    is running).

    Periodically logs the per-frame RMS of mic, ref, and AEC out
    so we can observe whether the engine is actually attenuating
    the echo. Comparing mic_rms vs aec_rms gives the running
    attenuation in dB.

    Debug-record mode: if `JASPER_AEC_DEBUG_RECORD_DIR` is set, the
    bridge writes the AEC engine's input mic stream and pre-gain
    output to two WAV files in that directory. Used by
    `scripts/aec-erle-record.sh` to capture both sides of the
    engine for offline ERLE analysis — couldn't otherwise be done
    with a second `arecord` because the bridge already holds the
    Array card exclusively via PortAudio."""
    import math
    import socket
    import time
    import wave
    # UDP output: localhost, non-blocking sendto. Replaces the old
    # PortAudio RawOutputStream writing to hw:LoopbackAEC,0. `sendto`
    # never blocks on `lo` at our rate (~256 kbps), so the main
    # thread can always observe SIGTERM and exit cleanly inside
    # `TimeoutStopSec=5s` — no more SIGKILL, no more snd-aloop
    # kernel-state corruption.
    out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out_sock.setblocking(False)
    out_dest = (OUT_HOST, OUT_PORT)
    # Secondary socket carries the chip-direct mic (pre-AEC3),
    # batched and packetized identically to the primary AEC ON
    # stream. See OUT_PORT_RAW comment above for the rationale.
    # Independent socket so a sendto failure on one stream doesn't
    # affect the other.
    out_sock_raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out_sock_raw.setblocking(False)
    out_dest_raw = (OUT_HOST, OUT_PORT_RAW)
    logger.info(
        "udp outputs: aec=%s:%d raw=%s:%d frame=%d samples (%d bytes)",
        OUT_HOST, OUT_PORT, OUT_HOST, OUT_PORT_RAW,
        OUT_FRAME_SAMPLES, OUT_FRAME_BYTES,
    )
    # Aggregate four AEC frames (320 samples each) into one UDP
    # packet (1280 samples = MicCapture.OUTPUT_FRAME_SAMPLES) so
    # voice's UdpMicCapture sees the same chunk size it gets from
    # the PortAudio path. Bytearray rather than list-of-bytes to
    # avoid per-frame allocation churn. The _raw batch tracks the
    # chip-direct mic stream on the same cadence.
    out_batch = bytearray()
    out_batch_raw = bytearray()
    silence = np.zeros(FRAME_SAMPLES, dtype=np.int16).tobytes()
    # Cold-start value for ref carry-forward. Used only until the first
    # real ref frame arrives — after that, last_ref_bytes always holds
    # a previously-real ref. See the drain block in the main loop for
    # why we carry forward instead of falling back to silence.
    last_ref_bytes = silence
    frames_processed = 0

    # Optional debug WAV writers — see `_aec_loop` docstring.
    debug_dir = os.environ.get("JASPER_AEC_DEBUG_RECORD_DIR", "").strip()
    mic_wav: Optional[wave.Wave_write] = None
    aec_wav: Optional[wave.Wave_write] = None
    ref_wav: Optional[wave.Wave_write] = None
    if debug_dir:
        try:
            os.makedirs(debug_dir, exist_ok=True)
            mic_wav = wave.open(f"{debug_dir}/mic_ch1.wav", "wb")
            mic_wav.setnchannels(1)
            mic_wav.setsampwidth(2)
            mic_wav.setframerate(SAMPLE_RATE)
            aec_wav = wave.open(f"{debug_dir}/aec_output.wav", "wb")
            aec_wav.setnchannels(1)
            aec_wav.setsampwidth(2)
            aec_wav.setframerate(SAMPLE_RATE)
            ref_wav = wave.open(f"{debug_dir}/ref.wav", "wb")
            ref_wav.setnchannels(1)
            ref_wav.setsampwidth(2)
            ref_wav.setframerate(SAMPLE_RATE)
            logger.warning(
                "DEBUG RECORD MODE: writing mic/aec/ref WAVs to %s "
                "until shutdown",
                debug_dir,
            )
        except OSError as e:
            logger.error(
                "failed to open debug record dir %s: %s; skipping",
                debug_dir, e,
            )
            mic_wav = aec_wav = ref_wav = None
    last_log = 0.0
    # Running sums for RMS computation across the log window.
    rms_window_frames = 0
    sum_mic_sq = 0.0
    sum_ref_sq = 0.0
    sum_aec_sq = 0.0

    try:
        while not _shutdown.is_set():
            try:
                mic_bytes = mic_q.get(timeout=1.0)
                consecutive_empty_sec = 0
            except Empty:
                consecutive_empty_sec += 1
                # Log once at stall onset, then every 2 s so the journal
                # shows the stall growing without flooding 1 line/sec.
                if consecutive_empty_sec == 1 or consecutive_empty_sec % 2 == 0:
                    logger.warning(
                        "mic queue empty for %ds — bridge stalled (will exit "
                        "non-zero at %ds for systemd restart)",
                        consecutive_empty_sec, stall_restart_sec,
                    )
                if (
                    stall_restart_sec > 0
                    and consecutive_empty_sec >= stall_restart_sec
                ):
                    raise BridgeStalled(
                        f"mic queue empty for {consecutive_empty_sec}s — "
                        "InputStream is dead (typically ALSA underrun on "
                        "XVF UAC2 capture), exiting non-zero so systemd "
                        "Restart=on-failure can spin up a fresh process"
                    )
                continue

            # Consume ONE ref frame per main-loop iteration, in order.
            # If the queue is empty, carry forward the previous ref.
            #
            # Background: ALSA's pcm.read() on jasper_ref returns
            # 1024-frame periods (negotiated up from the bridge's
            # request of 960 to match dsnoop's underlying period).
            # With buffer_size = 4 × period_size, ALSA delivers two
            # periods back-to-back every ~40 ms — the bursting is at
            # the ALSA layer, not the bridge. Meanwhile the mic
            # delivers smoothly at the bridge's 20 ms cadence.
            #
            # The original code's "drain to newest" pattern reacted to
            # the burst by discarding the older of the two frames and
            # using only the newest, then on the next iteration finding
            # an empty queue. Two failure modes followed:
            #   1. Fall back to `silence` → every other AEC frame got
            #      zeroed ref → 25 Hz envelope artefact, AEC's
            #      adaptive filter could not converge.
            #   2. Carry forward newest → every other AEC frame was a
            #      LITERAL byte-duplicate of its predecessor → 50 Hz
            #      envelope artefact (audible as buzzing) AND half the
            #      real ref data was being thrown away by the drain.
            #
            # Both prior approaches lost half the real reference. The
            # correct shape is to take one frame per iteration, in
            # arrival order. Burst → consume A this iteration, B next
            # iteration, then 1 replay-from-carry while waiting for
            # the next burst, repeat. Worst case: 1 in 3 frames is a
            # 20 ms-stale carry-forward; that staleness is well within
            # AEC3's delay-estimator tolerance and immediate replays
            # of the same bytes are eliminated entirely.
            #
            # See `docs/HANDOFF-aec.md` "Ref starvation bug (2026-05-19)"
            # for the full diagnosis trail.
            try:
                last_ref_bytes = ref_q.get_nowait()
                drained = 1
            except Empty:
                drained = 0
                _ref_starved_frames += 1
            ref_bytes = last_ref_bytes

            # Emit chip-direct mic on OUT_PORT_RAW BEFORE running
            # the AEC engine. This is the "AEC OFF" leg the
            # wake-telemetry dual-stream consumer wants — same
            # bytes AEC3 is about to receive as near-end input.
            # Batched and packetized identically to the primary
            # stream. sendto failures here never block the AEC
            # pipeline (independent socket, non-blocking, swallowed
            # on EWOULDBLOCK).
            out_batch_raw.extend(mic_bytes)
            if len(out_batch_raw) >= OUT_FRAME_BYTES:
                try:
                    out_sock_raw.sendto(
                        bytes(out_batch_raw[:OUT_FRAME_BYTES]),
                        out_dest_raw,
                    )
                except BlockingIOError:
                    logger.warning(
                        "udp raw sendto would block, dropping frame"
                    )
                del out_batch_raw[:OUT_FRAME_BYTES]

            clean = engine.process(mic_bytes, ref_bytes)
            # Save pre-gain output for the RMS metric — we want
            # "attenuation" to reflect what AEC actually accomplished,
            # not how much the post-gain stage amplified the residual.
            clean_aec_only = clean

            # Debug WAV record: writes happen here so the captured
            # frames are exactly what the bridge measured for its
            # internal "attenuation" log + what the AEC emitted before
            # the post-gain stage. Time-aligned to the sample.
            if mic_wav is not None:
                try:
                    mic_wav.writeframes(mic_bytes)
                    aec_wav.writeframes(clean_aec_only)
                    ref_wav.writeframes(ref_bytes)
                except OSError as e:
                    logger.error("debug wav write failed: %s", e)
                    mic_wav = aec_wav = ref_wav = None
            if mic_gain_lin != 1.0:
                arr = np.frombuffer(clean, dtype=np.int16).astype(np.float32) * mic_gain_lin
                _out_clipped_samples += int(np.sum(np.abs(arr) > 32767))
                _out_total_samples += len(arr)
                # tanh soft-clip: smoothly asymptotic to ±32767 instead
                # of hard-clipping. Below ±~26000 it's near-linear.
                arr = 32767.0 * np.tanh(arr / 32767.0)
                clean = arr.astype(np.int16).tobytes()
            out_batch.extend(clean)
            if len(out_batch) >= OUT_FRAME_BYTES:
                # `sendto` is non-blocking (setblocking(False) above).
                # On `lo` at ~256 kbps, the kernel UDP send buffer
                # never fills, so BlockingIOError essentially never
                # fires; if it ever does, dropping the packet is the
                # right call (voice sees an 80 ms gap, recovers next
                # frame).
                try:
                    out_sock.sendto(bytes(out_batch[:OUT_FRAME_BYTES]), out_dest)
                except BlockingIOError:
                    logger.warning("udp out sendto would block, dropping frame")
                del out_batch[:OUT_FRAME_BYTES]
            frames_processed += 1
            if heartbeat is not None:
                heartbeat.bump()

            mic_arr = np.frombuffer(mic_bytes, dtype=np.int16).astype(np.float32)
            ref_arr = np.frombuffer(ref_bytes, dtype=np.int16).astype(np.float32)
            aec_arr = np.frombuffer(clean_aec_only, dtype=np.int16).astype(np.float32)
            sum_mic_sq += float(np.mean(mic_arr * mic_arr))
            sum_ref_sq += float(np.mean(ref_arr * ref_arr))
            sum_aec_sq += float(np.mean(aec_arr * aec_arr))
            rms_window_frames += 1

            now = time.monotonic()
            if now - last_log > 5.0:
                if rms_window_frames > 0:
                    mic_rms = math.sqrt(sum_mic_sq / rms_window_frames)
                    ref_rms = math.sqrt(sum_ref_sq / rms_window_frames)
                    aec_rms = math.sqrt(sum_aec_sq / rms_window_frames)
                    if mic_rms > 1.0:
                        attn_db = 20.0 * math.log10(max(aec_rms, 1.0) / mic_rms)
                    else:
                        attn_db = 0.0
                    ref_clip_pct = (
                        100.0 * _ref_clipped_samples / _ref_total_samples
                        if _ref_total_samples else 0.0
                    )
                    out_clip_pct = (
                        100.0 * _out_clipped_samples / _out_total_samples
                        if _out_total_samples else 0.0
                    )
                    logger.info(
                        "rms over %.1fs: ref=%.0f mic=%.0f aec=%.0f → "
                        "attenuation=%.1f dB (frames=%d ref_q=%d mic_q=%d "
                        "ref_starve=%d ref_clip=%.2f%% out_clip=%.2f%%)",
                        rms_window_frames * FRAME_SAMPLES / SAMPLE_RATE,
                        ref_rms, mic_rms, aec_rms, attn_db,
                        frames_processed, ref_q.qsize(), mic_q.qsize(),
                        _ref_starved_frames,
                        ref_clip_pct, out_clip_pct,
                    )
                last_log = now
                rms_window_frames = 0
                sum_mic_sq = sum_ref_sq = sum_aec_sq = 0.0
                _ref_clipped_samples = _ref_total_samples = 0
                _out_clipped_samples = _out_total_samples = 0
                _ref_starved_frames = 0
    finally:
        out_sock.close()
        out_sock_raw.close()
        for w in (mic_wav, aec_wav, ref_wav):
            if w is not None:
                try:
                    w.close()
                except OSError:
                    pass


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s aec-bridge %(levelname)s %(message)s",
    )
    logger.info(
        "starting: ref=%s@%d mic=%s@%d ch=%d->ch%d "
        "aec_out=udp://%s:%d raw_out=udp://%s:%d @%d",
        REF_DEVICE, REF_RATE, MIC_DEVICE, SAMPLE_RATE,
        MIC_CHANNELS, MIC_CHANNEL_INDEX,
        OUT_HOST, OUT_PORT, OUT_HOST, OUT_PORT_RAW, OUT_RATE,
    )

    try:
        _validate_mic_device()
    except MicDeviceUnavailable as e:
        logger.error("%s", e)
        return 1

    engine = _select_engine()

    # Signal handlers for clean shutdown
    def on_signal(signum, _frame):
        logger.info("received signal %d, shutting down", signum)
        _shutdown.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    ref_q: Queue[bytes] = Queue(maxsize=QUEUE_MAXSIZE)
    mic_q: Queue[bytes] = Queue(maxsize=QUEUE_MAXSIZE)

    ref_t = threading.Thread(target=_ref_thread, args=(ref_q,), daemon=True)
    mic_t = threading.Thread(target=_mic_thread, args=(mic_q,), daemon=True)
    ref_t.start()
    mic_t.start()

    # Tier 1 of the resilience ladder. Bumped after each successful
    # frame in `_aec_loop`; if the loop wedges (e.g. mic InputStream
    # stops invoking its callback after a USB underrun on the XVF
    # UAC2 capture), systemd's `WatchdogSec=` expires and revives
    # us via `Restart=on-watchdog`. The original PortAudio
    # output-stream wedge that motivated this rung is now gone
    # under PR 2's UDP transport — `socket.sendto` is non-blocking
    # on `lo` at our rate — but the heartbeat still protects against
    # any future in-process hang. See jasper/watchdog.py header.
    heartbeat = Heartbeat(stale_threshold_sec=5.0, interval_sec=10.0)
    heartbeat.start()

    try:
        _aec_loop(ref_q, mic_q, engine, heartbeat=heartbeat)
    except BridgeStalled as e:
        logger.error("%s", e)
        _shutdown.set()
        return 1
    except Exception as e:  # noqa: BLE001
        logger.exception("aec loop crashed: %s", e)
        _shutdown.set()
        return 1
    finally:
        heartbeat.stop()
        engine.close()
        ref_t.join(timeout=2)
        mic_t.join(timeout=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
