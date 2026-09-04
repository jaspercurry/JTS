// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Lane construction and the ALSA open/configure envelopes behind it: the
//! aloop capture lanes, the USB DIRECT gadget capture, and the pure helpers
//! that size and validate the geometry they negotiate.
//!
//! Open-time only: the per-period read and render paths, and the error
//! classifier they share, stay with the mixer's work loop.

use super::*;

/// PCM sample format for this daemon's snd-aloop capture lanes — the
/// per-renderer inputs, the only aloop lanes left since ADR-0100.
///
/// Lane ingress is NOT its own width axis: it is this box's one resolved wire
/// ([`Config::program_wire_is_wide`]), the same fact that decides the program
/// ring's payload and the assistant wire.
///
/// snd-aloop pins both halves of a cable to one format, so the renderer
/// aliases' slaves in `deploy/alsa/asoundrc.jasper` declare the same width and
/// move with this; `tests/test_fanin_wiring.py` and `check_fanin_asound_wiring`
/// pin that side.
pub(super) fn lane_capture_format(program_wire_is_wide: bool) -> Format {
    if program_wire_is_wide {
        Format::S32LE
    } else {
        Format::S16LE
    }
}

/// Compute the direct capture buffer for a given open period, honoring the
/// deep-buffer safety floor (≥ `DIRECT_BUFFER_MIN_PERIODS` periods AND ≥
/// `DIRECT_BUFFER_MIN_FRAMES`), then rounded UP to a whole period multiple so
/// the negotiated geometry is period-aligned (a fractional buffer would shear;
/// `direct_open_params_ok` rejects it). Pure so the floor math is unit-testable
/// without ALSA.
pub(super) fn resolve_direct_buffer_frames(period: u32) -> u32 {
    let by_periods = period.saturating_mul(DIRECT_BUFFER_MIN_PERIODS);
    let floor = by_periods.max(DIRECT_BUFFER_MIN_FRAMES);
    // Round up to the next whole period so buffer % period == 0.
    let period = period.max(1);
    floor.div_ceil(period).saturating_mul(period)
}

pub(super) fn open_input(
    pcm_name: &str,
    label: &str,
    config: &Config,
    resampler: Option<LaneResampler>,
) -> Result<Input> {
    // Non-blocking so a silent renderer's substream doesn't stall the work loop;
    // read_input handles -EAGAIN as "no data, treat as silence".
    let pcm = PCM::new(pcm_name, Direction::Capture, true)
        .with_context(|| format!("opening capture PCM {}", pcm_name))?;
    configure_pcm(&pcm, config, config.input_buffer_frames)
        .with_context(|| format!("configuring capture PCM {}", pcm_name))?;
    // Start the stream so reads return data (or EAGAIN) instead of
    // blocking forever in the PREPARED state.
    pcm.start()
        .with_context(|| format!("starting capture PCM {}", pcm_name))?;
    let period_samples = (config.period_frames as usize) * (CHANNELS as usize);
    Ok(Input {
        pcm: Some(pcm),
        direct: None,
        direct_opener: None,
        ring: None,
        ring_attacher: None,
        label: label.to_string(),
        pcm_name: pcm_name.to_string(),
        read_buf: vec![0i16; period_samples],
        read_buf_wide: spine_read_buf(config.program_wire_is_wide(), period_samples),
        xrun_count: Arc::new(AtomicU64::new(0)),
        frames_read: Arc::new(AtomicU64::new(0)),
        rms_dbfs_x100: Arc::new(AtomicI32::new((RMS_DBFS_FLOOR * 100.0) as i32)),
        catchup_resync_frames: Arc::new(AtomicU64::new(0)),
        catchup_events: Arc::new(AtomicU64::new(0)),
        resampler,
        trim: TrimControl::new(),
        muted: Arc::new(AtomicBool::new(false)),
        direct_obs: None,
        ring_obs: None,
        lane_fade: LaneFade::for_lane(label, config.sample_rate),
    })
}

/// A lane's SPINE-SCALE period buffer — allocated on a wide wire, empty on a
/// narrow one. The ONE place that decides, for every lane source (aloop, ring,
/// USB direct), whether that lane carries its period at spine scale.
///
/// Non-empty `read_buf_wide` is not merely a buffer — it is the lane's OWN
/// width switch, read by the drain (which side of the capture fork), by the
/// render (which `render_period`), and by the sum (which entry). Inverting it
/// points every one of those at the wrong scale at once, which is why the
/// decision is a testable pure helper rather than an inline `if` on a `&Config`.
/// An empty `Vec` has no capacity, so a narrow box allocates nothing.
pub(super) fn spine_read_buf(program_wire_is_wide: bool, period_samples: usize) -> Vec<i32> {
    if program_wire_is_wide {
        vec![0i32; period_samples]
    } else {
        Vec::new()
    }
}

/// Build the USB DIRECT lane. Opens `hw:UAC2Gadget` (or the override) with the
/// proven envelope; on failure the lane starts `Absent` and renders silence with
/// a bounded reopen retry, so a gadget-absent box never fails the daemon. The
/// aloop substream is NOT opened (`pcm: None`): this lane's audio comes only
/// from the gadget capture. Never returns `Err` — the fail-hard "every input
/// required" contract is exempted for this lane alone.
pub(super) fn open_direct_input(
    label: &str,
    pcm_name: &str,
    config: &Config,
    resampler: Option<LaneResampler>,
) -> Input {
    let device = config.usb_direct_device.clone();
    let open_period = config.usb_direct_period_frames;
    // The buffer the lane ACTUALLY negotiated at open; the request is
    // `resolve_direct_buffer_frames(open_period)`, but the kernel may round
    // `set_buffer_size_near` up, so seed from the request and overwrite with the
    // negotiated size on a successful open. Absent-at-startup keeps the request
    // as a best-effort placeholder (present=false makes the number advisory).
    let buffer_frames = Arc::new(AtomicU64::new(
        resolve_direct_buffer_frames(open_period) as u64
    ));
    let present = Arc::new(AtomicBool::new(false));
    let opens = Arc::new(AtomicU64::new(0));
    let retries = Arc::new(AtomicU64::new(0));
    let direct = match open_direct_capture(&device, open_period) {
        Ok((pcm, negotiated_buffer)) => {
            present.store(true, Ordering::Relaxed);
            opens.fetch_add(1, Ordering::Relaxed);
            buffer_frames.store(negotiated_buffer as u64, Ordering::Relaxed);
            info!(
                "event=fanin.usb_direct.present device={} period_frames={} buffer_frames={} (initial open) opens=1 retries=0",
                device, open_period, negotiated_buffer,
            );
            DirectCapture::Present(pcm)
        }
        Err(e) => {
            // Gadget absent at startup (source not yet advertised, unplugged
            // host, or another process holds hw:UAC2Gadget). Not fatal: the lane
            // renders silence and retries on its own cadence.
            warn!(
                "event=fanin.usb_direct.absent device={} errno={} detail={:#} (startup; will retry ~every {}s)",
                device,
                errno_of(&e),
                e,
                DIRECT_REOPEN_RETRY_PERIODS * (config.period_frames as u64) / (config.sample_rate.max(1) as u64),
            );
            DirectCapture::Absent {
                periods_until_retry: DIRECT_REOPEN_RETRY_PERIODS,
            }
        }
    };
    let period_samples = (config.period_frames as usize) * (CHANNELS as usize);
    let read_buf_wide = spine_read_buf(config.program_wire_is_wide(), period_samples);
    // The deferred device-open channel (#2533). Spawned at construction, before
    // `main` calls `mlockall`, like every other fan-in helper thread. A spawn
    // failure leaves the lane WITHOUT self-heal rather than restoring an inline
    // `snd_pcm_open` inside the render loop's 5.33 ms period budget.
    let reopen_pending = Arc::new(AtomicBool::new(false));
    let direct_opener = match direct_capture::DirectOpener::spawn(Arc::clone(&reopen_pending)) {
        Ok(opener) => Some(opener),
        Err(e) => {
            warn!(
                "event=fanin.usb_direct.opener_unavailable device={} detail={} — the gadget \
                 capture will NOT be reopened after a loss until fan-in restarts (audio \
                 unaffected; the lane renders silence)",
                device, e,
            );
            None
        }
    };
    Input {
        // The direct lane does NOT open its aloop substream — its only source
        // is the gadget capture in `direct`.
        pcm: None,
        direct: Some(direct),
        direct_opener,
        ring: None,
        ring_attacher: None,
        label: label.to_string(),
        pcm_name: pcm_name.to_string(),
        read_buf: vec![0i16; period_samples],
        read_buf_wide,
        xrun_count: Arc::new(AtomicU64::new(0)),
        frames_read: Arc::new(AtomicU64::new(0)),
        rms_dbfs_x100: Arc::new(AtomicI32::new((RMS_DBFS_FLOOR * 100.0) as i32)),
        catchup_resync_frames: Arc::new(AtomicU64::new(0)),
        catchup_events: Arc::new(AtomicU64::new(0)),
        resampler,
        trim: TrimControl::new(),
        muted: Arc::new(AtomicBool::new(false)),
        direct_obs: Some(DirectObservability {
            device,
            period_frames: open_period,
            buffer_frames,
            present,
            streaming: Arc::new(AtomicBool::new(false)),
            stream_starts: Arc::new(AtomicU64::new(0)),
            stream_stops: Arc::new(AtomicU64::new(0)),
            notify_attempts: Arc::new(AtomicU64::new(0)),
            notify_failures: Arc::new(AtomicU64::new(0)),
            opens,
            retries,
            reopen_pending,
            reopens: Arc::new(AtomicU64::new(0)),
            zero_avail_streak: Arc::new(AtomicU64::new(0)),
            frames_flowed_since_open: Arc::new(AtomicBool::new(false)),
            liveness_last_checked_drain: Arc::new(AtomicU64::new(0)),
            card_gen_reopens: Arc::new(AtomicU64::new(0)),
            drain_stats: DrainStats::new(),
        }),
        ring_obs: None,
        lane_fade: LaneFade::for_lane(label, config.sample_rate),
    }
}

/// Open the USB DIRECT capture PCM with the hardware-validated gadget envelope —
/// deliberately NOT fanin's aloop-tuned `configure_pcm`, which sets an exact
/// buffer. S32_LE 2ch 48k, `set_period_size(open_period, Nearest)`,
/// `set_buffer_size_near(resolve_direct_buffer_frames(open_period))`, then the
/// post-negotiation checks. `open_period` is 256 (the on-device-proven value) or
/// the `JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES` override. Non-blocking and
/// `start()`ed so reads return data / EAGAIN.
///
/// Returns `(open PCM, negotiated buffer frames)`; the second element is the live
/// `hwp.get_buffer_size()`, so STATUS reports the buffer the PCM is really
/// running rather than the requested size. On failure returns an `alsa::Error`
/// the caller maps to the `Absent` state.
pub(super) fn open_direct_capture(
    device: &str,
    open_period: u32,
) -> std::result::Result<(PCM, u32), alsa::Error> {
    let want_buffer = resolve_direct_buffer_frames(open_period);
    let pcm = PCM::new(device, Direction::Capture, true)?;
    let negotiated_buffer;
    {
        let hwp = HwParams::any(&pcm)?;
        hwp.set_channels(CHANNELS)?;
        hwp.set_rate(SAMPLE_RATE_HZ, ValueOr::Nearest)?;
        hwp.set_format(Format::S32LE)?;
        hwp.set_access(Access::RWInterleaved)?;
        hwp.set_period_size(open_period as i64, ValueOr::Nearest)?;
        hwp.set_buffer_size_near(want_buffer as i64)?;
        let rate = hwp.get_rate()?;
        let period = hwp.get_period_size()? as u32;
        let buffer = hwp.get_buffer_size()? as u32;
        pcm.hw_params(&hwp)?;
        // Rate/period MUST land exactly; the buffer is warn-on-near-mismatch but
        // must clear the deep-buffer + alignment floor. A validation failure
        // drops the PCM and returns an error, so the lane goes Absent.
        if let Err(reason) = direct_open_params_ok(rate, period, buffer, open_period) {
            warn!(
                "event=fanin.usb_direct.open_rejected device={} rate={} period={} buffer={} reason={}",
                device, rate, period, buffer, reason,
            );
            // An errno-bearing alsa::Error keeps the caller's Absent-path log
            // shape consistent. EINVAL = "negotiated an unusable geometry".
            return Err(alsa::Error::new("direct_open_params", libc::EINVAL));
        }
        if buffer != want_buffer {
            warn!(
                "event=fanin.usb_direct.buffer_near device={} requested_frames={} negotiated_frames={}",
                device, want_buffer, buffer,
            );
        }
        negotiated_buffer = buffer;
    }
    pcm.start()?;
    Ok((pcm, negotiated_buffer))
}

/// Pure post-negotiation validation of the direct capture geometry,
/// unit-testable without ALSA. Rate must be exactly 48000 and period exactly the
/// requested `want_period`; buffer must clear the deep-buffer floor (≥
/// `DIRECT_BUFFER_MIN_PERIODS` periods AND ≥ `DIRECT_BUFFER_MIN_FRAMES`) and be
/// a whole multiple of the period, since a fractional buffer would shear.
/// Returns the rejection reason string on failure.
pub(super) fn direct_open_params_ok(
    rate: u32,
    period: u32,
    buffer: u32,
    want_period: u32,
) -> std::result::Result<(), String> {
    if rate != SAMPLE_RATE_HZ {
        return Err(format!("rate {rate} != 48000"));
    }
    if period != want_period {
        return Err(format!("period {period} != {want_period}"));
    }
    let min_buffer = period
        .saturating_mul(DIRECT_BUFFER_MIN_PERIODS)
        .max(DIRECT_BUFFER_MIN_FRAMES);
    if buffer < min_buffer {
        return Err(format!(
            "buffer {buffer} < deep-buffer floor ({min_buffer}: max({}×period, {}))",
            DIRECT_BUFFER_MIN_PERIODS, DIRECT_BUFFER_MIN_FRAMES,
        ));
    }
    if period == 0 || buffer % period != 0 {
        return Err(format!("buffer {buffer} not period-aligned to {period}"));
    }
    Ok(())
}

/// Pull the errno out of an `alsa::Error` for logging. It matches the `libc::E*`
/// constants the read paths compare against.
pub(super) fn errno_of(e: &alsa::Error) -> i32 {
    e.errno()
}

fn configure_pcm(pcm: &PCM, config: &Config, buffer_frames: u32) -> Result<()> {
    // HwParams must be dropped before pcm.hw_params() is called, hence the
    // nested scope.
    {
        let hwp = HwParams::any(pcm).context("creating HwParams::any")?;
        hwp.set_channels(CHANNELS)
            .with_context(|| format!("set_channels({})", CHANNELS))?;
        hwp.set_rate(config.sample_rate, ValueOr::Nearest)
            .with_context(|| format!("set_rate({})", config.sample_rate))?;
        let format = lane_capture_format(config.program_wire_is_wide());
        hwp.set_format(format)
            .with_context(|| format!("set_format({:?})", format))?;
        hwp.set_access(Access::RWInterleaved)
            .context("set_access(RWInterleaved)")?;
        hwp.set_period_size(config.period_frames as i64, ValueOr::Nearest)
            .with_context(|| format!("set_period_size({})", config.period_frames))?;
        hwp.set_buffer_size(buffer_frames as i64)
            .with_context(|| format!("set_buffer_size({})", buffer_frames))?;
        pcm.hw_params(&hwp).context("installing HwParams")?;
    }
    Ok(())
}
