"""jasper.chip_aec_experiment — chip-AEC test daemon (EXPERIMENTAL).

Branch: chip-aec-experiment. Not for production.
See docs/CHIP-AEC-EXPERIMENT.md for the full plan.

Replaces jasper-aec-bridge for the duration of the experiment with a single
daemon that does two things in parallel:

  A) Reference feeder — reads the music chain at pre-CamillaDSP tap
     (plug:jasper_capture) and writes 16 kHz stereo S16_LE to the XVF3800's
     USB-IN endpoint (hw:CARD=Array,DEV=0). This is the AEC reference signal
     the chip's hardware AEC consumes. The chip uses LEFT channel only; RIGHT
     is duplicated to match the endpoint's 2-channel descriptor.

  B) UDP mic pump — reads the chip's 6-channel mic capture stream, extracts
     the selected processed chip channel, and emits 16 kHz mono S16_LE PCM
     frames to udp://127.0.0.1:9876. jasper-voice continues reading UDP — no
     voice-daemon changes.

Known limitation:
- The reference tap is pre-CamillaDSP. The chip sees un-ducked music while
  the speaker plays ducked music during wake events. This is OK for the
  steady-state music convergence test (Phase 3, no wake events fire) but
  would matter for productionization. See CHIP-AEC-EXPERIMENT.md.

Run:
    sudo /opt/jasper/.venv/bin/python -m jasper.chip_aec_experiment

Stop with SIGTERM/SIGINT.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time

import alsaaudio
import numpy as np

LOG = logging.getLogger("chip-aec-experiment")

CHIP_DEVICE = "hw:CARD=Array,DEV=0"
SOURCE_DEVICE = "plug:jasper_capture"
RATE = 16000
PERIODSIZE = 160  # 10 ms @ 16 kHz
UDP_TARGET = ("127.0.0.1", 9876)
DEFAULT_REF_DELAY_MS = float(os.environ.get("JASPER_CHIP_AEC_REF_DELAY_MS", "0"))
DEFAULT_MIC_CHANNEL = int(os.environ.get("JASPER_CHIP_AEC_MIC_CHANNEL", "0"))


class _Stop:
    def __init__(self) -> None:
        self.flag = False

    def trip(self, *_: object) -> None:
        self.flag = True


def _delay_mono_chunk(
    chunk: np.ndarray,
    delay_buffer: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray | None]:
    if delay_buffer is None or delay_buffer.size == 0:
        return chunk, delay_buffer
    combined = np.concatenate((delay_buffer, chunk))
    delayed = combined[: chunk.size].copy()
    next_buffer = combined[chunk.size :].copy()
    return delayed, next_buffer


def reference_feeder(stop: _Stop, source: str, chip: str, ref_delay_ms: float = 0.0) -> None:
    """Pump music chain into chip USB-IN at 16 kHz stereo S16_LE.

    Trips ``stop`` on any failure so the main loop exits and the
    operator sees the daemon die instead of hanging silently with
    one thread dead and the other live.
    """
    src: alsaaudio.PCM | None = None
    dst: alsaaudio.PCM | None = None
    try:
        delay_samples = max(0, int(round(ref_delay_ms * RATE / 1000.0)))
        delay_buffer = np.zeros(delay_samples, dtype=np.int16) if delay_samples else None
        try:
            src = alsaaudio.PCM(
                type=alsaaudio.PCM_CAPTURE,
                mode=alsaaudio.PCM_NORMAL,
                device=source,
                rate=RATE,
                channels=2,
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=PERIODSIZE,
            )
            dst = alsaaudio.PCM(
                type=alsaaudio.PCM_PLAYBACK,
                mode=alsaaudio.PCM_NORMAL,
                device=chip,
                rate=RATE,
                channels=2,
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=PERIODSIZE,
            )
        except alsaaudio.ALSAAudioError as e:
            LOG.error("ref feeder open failed: %s", e)
            stop.trip()
            return

        LOG.info(
            "ref feeder: %s -> %s @ 16k stereo S16_LE ref_delay_ms=%.2f ref_delay_samples=%d",
            source,
            chip,
            ref_delay_ms,
            delay_samples,
        )

        frames = 0
        underruns = 0
        next_log = time.monotonic() + 5
        while not stop.flag:
            try:
                length, data = src.read()
            except alsaaudio.ALSAAudioError as e:
                LOG.warning("ref read error: %s", e)
                time.sleep(0.01)
                continue
            if length <= 0:
                continue
            stereo = np.frombuffer(data, dtype=np.int16)
            # Mix L+R to mono, then duplicate to both channels (chip uses
            # L only, R duplicated matches the endpoint descriptor cleanly).
            mixed = ((stereo[0::2].astype(np.int32) + stereo[1::2].astype(np.int32)) // 2).astype(np.int16)
            delayed, delay_buffer = _delay_mono_chunk(mixed, delay_buffer)
            out = np.empty(delayed.size * 2, dtype=np.int16)
            out[0::2] = delayed
            out[1::2] = delayed
            try:
                dst.write(out.tobytes())
            except alsaaudio.ALSAAudioError as e:
                underruns += 1
                if underruns % 50 == 1:
                    LOG.warning("chip write error #%d: %s", underruns, e)
                # Avoid a tight error loop if the chip USB endpoint is
                # wedged — back off 1 ms between failed writes.
                time.sleep(0.001)

            frames += mixed.size
            if time.monotonic() >= next_log:
                rms = float(np.sqrt(np.mean(mixed.astype(np.float32) ** 2)))
                out_rms = float(np.sqrt(np.mean(delayed.astype(np.float32) ** 2))) if delayed.size else 0.0
                LOG.info(
                    "ref feeder: %d frames (%.0fs) RMS=%.0f out_RMS=%.0f underruns=%d ref_delay_samples=%d",
                    frames,
                    frames / RATE,
                    rms,
                    out_rms,
                    underruns,
                    delay_samples,
                )
                next_log = time.monotonic() + 5
    except Exception:
        # Anything outside ALSAAudioError: numpy buffer-shape mismatch,
        # MemoryError, etc. Without this the thread would die silently
        # while the main loop kept spinning forever.
        LOG.exception("ref feeder: unhandled exception, stopping daemon")
        stop.trip()
    finally:
        # Always release PCM handles, even on exception. Without the
        # finally, an uncaught exception leaks the file descriptors
        # until process exit.
        if src is not None:
            try:
                src.close()
            except Exception:
                LOG.exception("ref feeder: error closing src PCM")
        if dst is not None:
            try:
                dst.close()
            except Exception:
                LOG.exception("ref feeder: error closing dst PCM")
        LOG.info("ref feeder: stopped")


def udp_mic_pump(stop: _Stop, chip: str, mic_channel: int) -> None:
    """Pump selected chip channel to UDP 127.0.0.1:9876 as 16 kHz mono S16_LE.

    Trips ``stop`` on any failure so the main loop exits cleanly
    instead of hanging with a dead thread.
    """
    cap: alsaaudio.PCM | None = None
    sock: socket.socket | None = None
    try:
        try:
            cap = alsaaudio.PCM(
                type=alsaaudio.PCM_CAPTURE,
                mode=alsaaudio.PCM_NORMAL,
                device=chip,
                rate=RATE,
                channels=6,
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=PERIODSIZE,
            )
        except alsaaudio.ALSAAudioError as e:
            LOG.error("mic pump open failed: %s", e)
            stop.trip()
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if mic_channel < 0 or mic_channel >= 6:
            LOG.error("mic pump invalid channel: %d (expected 0..5)", mic_channel)
            stop.trip()
            return

        LOG.info("mic pump: %s ch%d -> udp://%s:%d", chip, mic_channel, *UDP_TARGET)

        frames = 0
        next_log = time.monotonic() + 5
        while not stop.flag:
            try:
                length, data = cap.read()
            except alsaaudio.ALSAAudioError as e:
                LOG.warning("mic read error: %s", e)
                time.sleep(0.01)
                continue
            if length <= 0:
                continue
            multi = np.frombuffer(data, dtype=np.int16)
            # 6-channel interleaved → take the selected processed channel.
            ch = multi[mic_channel::6].tobytes()
            try:
                sock.sendto(ch, UDP_TARGET)
            except OSError as e:
                LOG.warning("UDP send error: %s", e)

            frames += length
            if time.monotonic() >= next_log:
                mono = np.frombuffer(ch, dtype=np.int16)
                rms = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2))) if mono.size else 0.0
                LOG.info("mic pump: %d frames (%.0fs) ch%d RMS=%.0f", frames, frames / RATE, mic_channel, rms)
                next_log = time.monotonic() + 5
    except Exception:
        # See reference_feeder for why we catch broad Exception here.
        LOG.exception("mic pump: unhandled exception, stopping daemon")
        stop.trip()
    finally:
        if cap is not None:
            try:
                cap.close()
            except Exception:
                LOG.exception("mic pump: error closing capture PCM")
        if sock is not None:
            try:
                sock.close()
            except Exception:
                LOG.exception("mic pump: error closing UDP socket")
        LOG.info("mic pump: stopped")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SOURCE_DEVICE)
    parser.add_argument("--chip", default=CHIP_DEVICE)
    parser.add_argument(
        "--ref-delay-ms",
        type=float,
        default=DEFAULT_REF_DELAY_MS,
        help=(
            "delay the reference sent to XVF3800 USB-IN before chip AEC; "
            "test-only compensation for external speaker-path latency"
        ),
    )
    parser.add_argument(
        "--mic-channel",
        type=int,
        default=DEFAULT_MIC_CHANNEL,
        help="chip USB capture channel to emit on udp://127.0.0.1:9876 (default: 0, conference)",
    )
    parser.add_argument("--ref-only", action="store_true", help="skip UDP mic pump thread")
    parser.add_argument("--mic-only", action="store_true", help="skip reference feeder thread")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    stop = _Stop()
    signal.signal(signal.SIGTERM, stop.trip)
    signal.signal(signal.SIGINT, stop.trip)

    threads = []
    if not args.mic_only:
        t = threading.Thread(
            target=reference_feeder,
            args=(stop, args.source, args.chip, args.ref_delay_ms),
            name="ref-feeder",
            daemon=True,
        )
        t.start()
        threads.append(t)
    if not args.ref_only:
        t = threading.Thread(
            target=udp_mic_pump,
            args=(stop, args.chip, args.mic_channel),
            name="mic-pump",
            daemon=True,
        )
        t.start()
        threads.append(t)

    if not threads:
        LOG.error("nothing to do: both --ref-only and --mic-only specified? exiting")
        return 1

    while not stop.flag:
        time.sleep(0.5)

    LOG.info("draining threads")
    for t in threads:
        t.join(timeout=3)
    LOG.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
