// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Lab-only dual Apple USB-C DAC validation runner.
//!
//! This binary is intentionally outside the product output path. It opens
//! two serial-pinned Apple USB-C DAC ALSA hardware PCMs, writes silence
//! first, then optional low-level test periods, and aborts both outputs on
//! xrun, disconnect, or delay divergence.

use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use alsa::pcm::{Access, Format, HwParams, State, PCM};
use alsa::{Direction, ValueOr};
use anyhow::{anyhow, bail, Context, Result};
use signal_hook::consts::signal::{SIGINT, SIGTERM};
use signal_hook::flag;

const APPLE_VID_PID: &str = "05ac:110a";
const CHANNELS: usize = 2;
const DEFAULT_SAMPLE_RATE: u32 = 48_000;
const DEFAULT_PERIOD_FRAMES: u32 = 1024;
const DEFAULT_BUFFER_FRAMES: u32 = 4096;
const DEFAULT_DURATION_SEC: u64 = 600;
const DEFAULT_LEVEL_DB: f32 = -60.0;
const MAX_LEVEL_DB: f32 = -30.0;
const DEFAULT_REPORT_PERIODS: u64 = 50;
const DEFAULT_DELAY_DELTA_FRAMES: i64 = 2;

fn main() -> Result<()> {
    let args: Vec<String> = env::args().collect();
    let Some(command) = args.get(1).map(String::as_str) else {
        print_usage();
        bail!("missing command");
    };

    match command {
        "probe" => probe(),
        "run" => run(args[2..].to_vec()),
        "-h" | "--help" | "help" => {
            print_usage();
            Ok(())
        }
        other => {
            print_usage();
            bail!("unknown command {other}");
        }
    }
}

#[derive(Debug, Clone)]
struct DeviceInfo {
    card: u32,
    card_id: String,
    pcm: String,
    serial: String,
    usbid: String,
    has_playback: bool,
    stream_summary: String,
    sysfs_card_path: String,
    usb_device_path: String,
    controller: String,
    busnum: String,
    devpath: String,
    speed: String,
    by_id: Vec<String>,
    by_path: Vec<String>,
}

impl DeviceInfo {
    fn to_json(&self) -> String {
        format!(
            "{{\"card\":{},\"card_id\":{},\"pcm\":{},\"serial\":{},\"usbid\":{},\"has_playback\":{},\"stream_summary\":{},\"sysfs_card_path\":{},\"usb_device_path\":{},\"controller\":{},\"busnum\":{},\"devpath\":{},\"speed\":{},\"by_id\":{},\"by_path\":{}}}",
            self.card,
            json_string(&self.card_id),
            json_string(&self.pcm),
            json_string(&self.serial),
            json_string(&self.usbid),
            self.has_playback,
            json_string(&self.stream_summary),
            json_string(&self.sysfs_card_path),
            json_string(&self.usb_device_path),
            json_string(&self.controller),
            json_string(&self.busnum),
            json_string(&self.devpath),
            json_string(&self.speed),
            json_array(&self.by_id),
            json_array(&self.by_path),
        )
    }
}

#[derive(Debug, Clone)]
struct UsbDeviceInfo {
    serial: String,
    usb_device_path: String,
    controller: String,
    busnum: String,
    devpath: String,
    devnum: String,
    speed: String,
    active_configuration: String,
    configuration_count: String,
    interface_count: String,
    interface_classes: Vec<String>,
    has_alsa_card: bool,
}

impl UsbDeviceInfo {
    fn to_json(&self) -> String {
        format!(
            "{{\"serial\":{},\"usb_device_path\":{},\"controller\":{},\"busnum\":{},\"devpath\":{},\"devnum\":{},\"speed\":{},\"active_configuration\":{},\"configuration_count\":{},\"interface_count\":{},\"interface_classes\":{},\"has_alsa_card\":{}}}",
            json_string(&self.serial),
            json_string(&self.usb_device_path),
            json_string(&self.controller),
            json_string(&self.busnum),
            json_string(&self.devpath),
            json_string(&self.devnum),
            json_string(&self.speed),
            json_string(&self.active_configuration),
            json_string(&self.configuration_count),
            json_string(&self.interface_count),
            json_array(&self.interface_classes),
            self.has_alsa_card,
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mode {
    Silence,
    Identity,
    Ticks,
}

impl Mode {
    fn parse(value: &str) -> Result<Self> {
        match value {
            "silence" => Ok(Self::Silence),
            "identity" => Ok(Self::Identity),
            "ticks" => Ok(Self::Ticks),
            _ => bail!("unknown mode {value}; expected silence, identity, or ticks"),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Silence => "silence",
            Self::Identity => "identity",
            Self::Ticks => "ticks",
        }
    }
}

#[derive(Debug)]
struct RunConfig {
    dac_a_serial: String,
    dac_b_serial: String,
    mode: Mode,
    sample_rate: u32,
    period_frames: u32,
    buffer_frames: u32,
    duration_sec: u64,
    report_periods: u64,
    level_db: f32,
    max_delay_delta_frames: i64,
    require_link: bool,
    disable_link: bool,
    arm_no_speakers: bool,
}

#[derive(Debug, Clone, Copy)]
struct NegotiatedPcm {
    sample_rate: u32,
    period_frames: u32,
    buffer_frames: u32,
}

struct Sink {
    label: &'static str,
    device: DeviceInfo,
    pcm: PCM,
    frames_written: u64,
    negotiated_sample_rate: u32,
}

impl Sink {
    fn open(label: &'static str, device: DeviceInfo, config: &RunConfig) -> Result<Self> {
        let pcm = PCM::new(&device.pcm, Direction::Playback, false)
            .with_context(|| format!("opening {} PCM {}", label, device.pcm))?;
        let negotiated = configure_pcm(
            label,
            &device.pcm,
            &pcm,
            config.sample_rate,
            config.period_frames,
            config.buffer_frames,
        )
        .with_context(|| format!("configuring {} PCM {}", label, device.pcm))?;
        pcm.prepare()
            .with_context(|| format!("preparing {} PCM {}", label, device.pcm))?;
        println!(
            "{{\"event\":\"dual_apple_dac.opened\",\"ts_ms\":{},\"label\":{},\"pcm\":{},\"sample_rate\":{},\"period_frames\":{},\"buffer_frames\":{}}}",
            epoch_ms(),
            json_string(label),
            json_string(&device.pcm),
            negotiated.sample_rate,
            negotiated.period_frames,
            negotiated.buffer_frames,
        );
        Ok(Self {
            label,
            device,
            pcm,
            frames_written: 0,
            negotiated_sample_rate: negotiated.sample_rate,
        })
    }

    fn write_period(&mut self, samples: &[i16]) -> Result<Duration> {
        let started = Instant::now();
        let frames_total = samples.len() / CHANNELS;
        let io = self
            .pcm
            .io_i16()
            .with_context(|| format!("getting i16 IO for {}", self.label))?;
        let mut frames_done = 0usize;
        while frames_done < frames_total {
            let offset = frames_done * CHANNELS;
            match io.writei(&samples[offset..]) {
                Ok(0) => bail!(
                    "{} PCM {} writei returned 0 frames",
                    self.label,
                    self.device.pcm
                ),
                Ok(n) => {
                    frames_done += n;
                }
                Err(err) => {
                    let errno = err.errno();
                    if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                        bail!(
                            "{} PCM {} aborted on xrun/suspend errno={errno}",
                            self.label,
                            self.device.pcm
                        );
                    }
                    return Err(err).with_context(|| {
                        format!("writing {} PCM {}", self.label, self.device.pcm)
                    });
                }
            }
        }
        self.frames_written += frames_total as u64;
        Ok(started.elapsed())
    }

    fn delay_frames(&self) -> Result<i64> {
        self.pcm
            .delay()
            .with_context(|| format!("reading {} PCM delay", self.label))
    }

    fn state_name(&self) -> String {
        format!("{:?}", self.pcm.state())
    }

    fn drop_stream(&self) {
        let _ = self.pcm.drop();
    }
}

fn probe() -> Result<()> {
    let devices = discover_apple_devices()?;
    let usb_devices = discover_apple_usb_devices(&devices)?;
    println!(
        "{{\"event\":\"dual_apple_dac.probe\",\"ts_ms\":{},\"apple_vid_pid\":{},\"device_count\":{},\"alsa_device_count\":{},\"usb_device_count\":{},\"devices\":[{}],\"usb_devices\":[{}]}}",
        epoch_ms(),
        json_string(APPLE_VID_PID),
        devices.len(),
        devices.len(),
        usb_devices.len(),
        devices
            .iter()
            .map(DeviceInfo::to_json)
            .collect::<Vec<_>>()
            .join(","),
        usb_devices
            .iter()
            .map(UsbDeviceInfo::to_json)
            .collect::<Vec<_>>()
            .join(",")
    );
    Ok(())
}

fn run(args: Vec<String>) -> Result<()> {
    let config = parse_run_config(args)?;
    validate_run_config(&config)?;
    if !config.arm_no_speakers {
        bail!("refusing playback without --arm-no-speakers");
    }
    if config.dac_a_serial == config.dac_b_serial {
        bail!("dac A and dac B serials must be different");
    }
    ensure_normal_audio_owners_stopped()?;

    let shutdown = Arc::new(AtomicBool::new(false));
    flag::register(SIGTERM, Arc::clone(&shutdown)).context("registering SIGTERM handler")?;
    flag::register(SIGINT, Arc::clone(&shutdown)).context("registering SIGINT handler")?;

    let devices = discover_apple_devices()?;
    let usb_devices = discover_apple_usb_devices(&devices)?;
    validate_lab_device_shape(&devices, &usb_devices)?;
    let dac_a = select_device(&devices, &config.dac_a_serial, "dac_a")?;
    let dac_b = select_device(&devices, &config.dac_b_serial, "dac_b")?;

    println!(
        "{{\"event\":\"dual_apple_dac.run_start\",\"ts_ms\":{},\"safety\":\"no_speakers_connected_required\",\"mode\":{},\"sample_rate\":{},\"period_frames\":{},\"buffer_frames\":{},\"duration_sec\":{},\"level_db\":{},\"max_delay_delta_frames\":{},\"require_link\":{},\"disable_link\":{},\"dac_a\":{},\"dac_b\":{}}}",
        epoch_ms(),
        json_string(config.mode.as_str()),
        config.sample_rate,
        config.period_frames,
        config.buffer_frames,
        config.duration_sec,
        config.level_db,
        config.max_delay_delta_frames,
        config.require_link,
        config.disable_link,
        dac_a.to_json(),
        dac_b.to_json(),
    );

    let mut sink_a = Sink::open("dac_a", dac_a, &config)?;
    let mut sink_b = Sink::open("dac_b", dac_b, &config)?;
    validate_negotiated_sample_rates(
        config.sample_rate,
        sink_a.negotiated_sample_rate,
        sink_b.negotiated_sample_rate,
    )?;

    let mut linked = false;
    if !config.disable_link {
        match sink_a.pcm.link(&sink_b.pcm) {
            Ok(()) => {
                linked = true;
                println!(
                    "{{\"event\":\"dual_apple_dac.link\",\"ts_ms\":{},\"status\":\"ok\"}}",
                    epoch_ms()
                );
            }
            Err(err) if config.require_link => {
                bail!("ALSA snd_pcm_link failed and --require-link was set: {err}");
            }
            Err(err) => {
                println!(
                    "{{\"event\":\"dual_apple_dac.link\",\"ts_ms\":{},\"status\":\"failed\",\"error\":{}}}",
                    epoch_ms(),
                    json_string(&err.to_string())
                );
            }
        }
    }

    let amplitude = db_to_i16(config.level_db);
    let period_samples = config.period_frames as usize * CHANNELS;
    let mut zero_period = vec![0i16; period_samples];
    let mut period_a = vec![0i16; period_samples];
    let mut period_b = vec![0i16; period_samples];
    let prime_periods = ((config.buffer_frames / config.period_frames).saturating_sub(1)).max(1);

    for _ in 0..prime_periods {
        sink_a.write_period(&zero_period)?;
        sink_b.write_period(&zero_period)?;
    }

    let start_attempt = Instant::now();
    if linked {
        sink_a
            .pcm
            .start()
            .context("starting linked DAC PCM group")?;
    } else {
        sink_a.pcm.start().context("starting dac_a PCM")?;
        sink_b.pcm.start().context("starting dac_b PCM")?;
    }
    let start_elapsed = start_attempt.elapsed();
    println!(
        "{{\"event\":\"dual_apple_dac.started\",\"ts_ms\":{},\"linked\":{},\"start_elapsed_us\":{},\"prime_periods\":{},\"dac_a_state\":{},\"dac_b_state\":{}}}",
        epoch_ms(),
        linked,
        start_elapsed.as_micros(),
        prime_periods,
        json_string(&sink_a.state_name()),
        json_string(&sink_b.state_name()),
    );

    let requested_signal_frames = config
        .duration_sec
        .saturating_mul(config.sample_rate as u64);
    let max_periods = div_ceil(requested_signal_frames, config.period_frames as u64);
    let run_started = Instant::now();
    let mut delay_delta_baseline: Option<i64> = None;
    let mut result: Result<()> = Ok(());

    for period_index in 0..max_periods {
        if shutdown.load(Ordering::Relaxed) {
            break;
        }

        let start_frame = period_index.saturating_mul(config.period_frames as u64);
        let active_frames =
            active_frames_for_period(requested_signal_frames, start_frame, config.period_frames);
        fill_period(
            config.mode,
            0,
            start_frame,
            config.sample_rate,
            amplitude,
            active_frames,
            &mut period_a,
        );
        fill_period(
            config.mode,
            1,
            start_frame,
            config.sample_rate,
            amplitude,
            active_frames,
            &mut period_b,
        );

        let write_a = match sink_a.write_period(&period_a) {
            Ok(duration) => duration,
            Err(err) => {
                result = Err(err);
                break;
            }
        };
        let write_b = match sink_b.write_period(&period_b) {
            Ok(duration) => duration,
            Err(err) => {
                result = Err(err);
                break;
            }
        };

        let delay_a = match sink_a.delay_frames() {
            Ok(delay) => delay,
            Err(err) => {
                result = Err(err);
                break;
            }
        };
        let delay_b = match sink_b.delay_frames() {
            Ok(delay) => delay,
            Err(err) => {
                result = Err(err);
                break;
            }
        };
        let delay_delta = delay_a - delay_b;
        let baseline = *delay_delta_baseline.get_or_insert(delay_delta);
        let delta_error = (delay_delta - baseline).abs();

        if bad_state(sink_a.pcm.state()) || bad_state(sink_b.pcm.state()) {
            result = Err(anyhow!(
                "aborting on bad PCM state: dac_a={} dac_b={}",
                sink_a.state_name(),
                sink_b.state_name()
            ));
            break;
        }
        if delta_error > config.max_delay_delta_frames {
            result = Err(anyhow!(
                "aborting on delay divergence: current_delta={} baseline={} error={} max={}",
                delay_delta,
                baseline,
                delta_error,
                config.max_delay_delta_frames
            ));
            break;
        }

        if period_index == 0 || period_index % config.report_periods == 0 {
            println!(
                "{{\"event\":\"dual_apple_dac.telemetry\",\"ts_ms\":{},\"period\":{},\"elapsed_ms\":{},\"dac_a_frames_written\":{},\"dac_b_frames_written\":{},\"dac_a_delay_frames\":{},\"dac_b_delay_frames\":{},\"delay_delta_frames\":{},\"delay_delta_baseline_frames\":{},\"write_a_us\":{},\"write_b_us\":{},\"dac_a_state\":{},\"dac_b_state\":{}}}",
                epoch_ms(),
                period_index,
                run_started.elapsed().as_millis(),
                sink_a.frames_written,
                sink_b.frames_written,
                delay_a,
                delay_b,
                delay_delta,
                baseline,
                write_a.as_micros(),
                write_b.as_micros(),
                json_string(&sink_a.state_name()),
                json_string(&sink_b.state_name()),
            );
        }
    }

    zero_period.fill(0);
    let _ = sink_a.write_period(&zero_period);
    let _ = sink_b.write_period(&zero_period);
    sink_a.drop_stream();
    sink_b.drop_stream();

    match result {
        Ok(()) => {
            println!(
                "{{\"event\":\"dual_apple_dac.run_complete\",\"ts_ms\":{},\"elapsed_ms\":{},\"dac_a_frames_written\":{},\"dac_b_frames_written\":{},\"shutdown_requested\":{}}}",
                epoch_ms(),
                run_started.elapsed().as_millis(),
                sink_a.frames_written,
                sink_b.frames_written,
                shutdown.load(Ordering::Relaxed),
            );
            Ok(())
        }
        Err(err) => {
            println!(
                "{{\"event\":\"dual_apple_dac.abort\",\"ts_ms\":{},\"elapsed_ms\":{},\"error\":{},\"dac_a_frames_written\":{},\"dac_b_frames_written\":{}}}",
                epoch_ms(),
                run_started.elapsed().as_millis(),
                json_string(&err.to_string()),
                sink_a.frames_written,
                sink_b.frames_written,
            );
            Err(err)
        }
    }
}

fn configure_pcm(
    role: &str,
    pcm_name: &str,
    pcm: &PCM,
    sample_rate: u32,
    period_frames: u32,
    buffer_frames: u32,
) -> Result<NegotiatedPcm> {
    let negotiated;
    {
        let hwp = HwParams::any(pcm).context("creating HwParams::any")?;
        hwp.set_channels(CHANNELS as u32)
            .with_context(|| format!("{role} set_channels({CHANNELS})"))?;
        hwp.set_rate(sample_rate, ValueOr::Nearest)
            .with_context(|| format!("{role} set_rate({sample_rate})"))?;
        hwp.set_format(Format::S16LE)
            .with_context(|| format!("{role} set_format(S16_LE)"))?;
        hwp.set_access(Access::RWInterleaved)
            .with_context(|| format!("{role} set_access(RWInterleaved)"))?;
        hwp.set_period_size(period_frames as i64, ValueOr::Nearest)
            .with_context(|| format!("{role} set_period_size({period_frames})"))?;
        hwp.set_buffer_size(buffer_frames as i64)
            .with_context(|| format!("{role} set_buffer_size({buffer_frames})"))?;
        negotiated = NegotiatedPcm {
            sample_rate: hwp.get_rate().context("get_rate")?,
            period_frames: hwp.get_period_size().context("get_period_size")? as u32,
            buffer_frames: hwp.get_buffer_size().context("get_buffer_size")? as u32,
        };
        pcm.hw_params(&hwp)
            .with_context(|| format!("installing {role} HwParams"))?;
    }
    {
        let swp = pcm
            .sw_params_current()
            .with_context(|| format!("reading {role} SwParams"))?;
        swp.set_start_threshold(negotiated.buffer_frames as i64)
            .with_context(|| format!("setting {role} start_threshold"))?;
        pcm.sw_params(&swp)
            .with_context(|| format!("installing {role} SwParams"))?;
    }

    if negotiated.sample_rate != sample_rate {
        bail!(
            "{role} PCM {pcm_name} negotiated sample_rate={} but requires {}",
            negotiated.sample_rate,
            sample_rate
        );
    }
    if negotiated.period_frames != period_frames {
        bail!(
            "{role} PCM {pcm_name} negotiated period_frames={} but requires {}",
            negotiated.period_frames,
            period_frames
        );
    }
    if negotiated.buffer_frames < period_frames * 2 {
        bail!(
            "{role} PCM {pcm_name} negotiated buffer_frames={} but requires at least {}",
            negotiated.buffer_frames,
            period_frames * 2
        );
    }

    Ok(negotiated)
}

fn parse_run_config(args: Vec<String>) -> Result<RunConfig> {
    let mut config = RunConfig {
        dac_a_serial: String::new(),
        dac_b_serial: String::new(),
        mode: Mode::Silence,
        sample_rate: DEFAULT_SAMPLE_RATE,
        period_frames: DEFAULT_PERIOD_FRAMES,
        buffer_frames: DEFAULT_BUFFER_FRAMES,
        duration_sec: DEFAULT_DURATION_SEC,
        report_periods: DEFAULT_REPORT_PERIODS,
        level_db: DEFAULT_LEVEL_DB,
        max_delay_delta_frames: DEFAULT_DELAY_DELTA_FRAMES,
        require_link: false,
        disable_link: false,
        arm_no_speakers: false,
    };

    let mut iter = args.into_iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--dac-a-serial" => config.dac_a_serial = take_value(&mut iter, &arg)?,
            "--dac-b-serial" => config.dac_b_serial = take_value(&mut iter, &arg)?,
            "--mode" => config.mode = Mode::parse(&take_value(&mut iter, &arg)?)?,
            "--sample-rate" => {
                config.sample_rate = parse_value(&take_value(&mut iter, &arg)?, &arg)?
            }
            "--period-frames" => {
                config.period_frames = parse_value(&take_value(&mut iter, &arg)?, &arg)?
            }
            "--buffer-frames" => {
                config.buffer_frames = parse_value(&take_value(&mut iter, &arg)?, &arg)?
            }
            "--duration-sec" => {
                config.duration_sec = parse_value(&take_value(&mut iter, &arg)?, &arg)?
            }
            "--report-periods" => {
                config.report_periods = parse_value(&take_value(&mut iter, &arg)?, &arg)?
            }
            "--level-db" => config.level_db = parse_value(&take_value(&mut iter, &arg)?, &arg)?,
            "--max-delay-delta-frames" => {
                config.max_delay_delta_frames = parse_value(&take_value(&mut iter, &arg)?, &arg)?
            }
            "--require-link" => config.require_link = true,
            "--disable-link" => config.disable_link = true,
            "--arm-no-speakers" => config.arm_no_speakers = true,
            "-h" | "--help" => {
                print_usage();
                std::process::exit(0);
            }
            other => bail!("unknown run argument {other}"),
        }
    }

    if config.dac_a_serial.is_empty() {
        bail!("missing --dac-a-serial");
    }
    if config.dac_b_serial.is_empty() {
        bail!("missing --dac-b-serial");
    }
    if config.report_periods == 0 {
        bail!("--report-periods must be > 0");
    }
    if config.require_link && config.disable_link {
        bail!("--require-link and --disable-link conflict");
    }

    Ok(config)
}

fn validate_run_config(config: &RunConfig) -> Result<()> {
    if !config.level_db.is_finite() {
        bail!("--level-db must be finite");
    }
    if config.level_db > MAX_LEVEL_DB {
        bail!(
            "refusing level_db={} dB; max allowed is {} dB for this lab binary",
            config.level_db,
            MAX_LEVEL_DB
        );
    }
    if config.sample_rate == 0 {
        bail!("--sample-rate must be > 0");
    }
    if config.period_frames == 0 {
        bail!("--period-frames must be > 0");
    }
    if config.buffer_frames < config.period_frames.saturating_mul(2) {
        bail!("invalid period/buffer: buffer must be at least 2x period");
    }
    if config.duration_sec == 0 {
        bail!("--duration-sec must be > 0");
    }
    Ok(())
}

fn validate_negotiated_sample_rates(requested: u32, dac_a: u32, dac_b: u32) -> Result<()> {
    if dac_a != dac_b {
        bail!(
            "refusing playback because negotiated sample rates disagree: dac_a={} dac_b={}",
            dac_a,
            dac_b
        );
    }
    if dac_a != requested {
        bail!(
            "refusing playback because ALSA negotiated {} Hz instead of requested {} Hz",
            dac_a,
            requested
        );
    }
    Ok(())
}

fn ensure_normal_audio_owners_stopped() -> Result<()> {
    let units = ["jasper-outputd.service", "jasper-voice.service"];
    let mut active = Vec::new();
    for unit in units {
        let status = Command::new("systemctl")
            .args(["is-active", "--quiet", unit])
            .status()
            .with_context(|| format!("checking systemd unit {unit}"))?;
        if status.success() {
            active.push(unit);
        }
    }
    if !active.is_empty() {
        bail!(
            "refusing playback while normal audio owner(s) are active: {}; run `sudo systemctl stop jasper-voice jasper-outputd` first",
            active.join(", ")
        );
    }
    Ok(())
}

fn take_value(iter: &mut impl Iterator<Item = String>, flag: &str) -> Result<String> {
    iter.next()
        .ok_or_else(|| anyhow!("missing value for {flag}"))
}

fn parse_value<T>(value: &str, flag: &str) -> Result<T>
where
    T: std::str::FromStr,
    T::Err: std::fmt::Display,
{
    value
        .parse::<T>()
        .map_err(|err| anyhow!("invalid value for {flag}: {err}"))
}

fn select_device(devices: &[DeviceInfo], serial: &str, label: &str) -> Result<DeviceInfo> {
    let matches: Vec<_> = devices
        .iter()
        .filter(|device| device.serial == serial)
        .cloned()
        .collect();
    match matches.as_slice() {
        [] => bail!("{label} serial {serial} was not found as an Apple ALSA card"),
        [device] if !device.has_playback => {
            bail!("{label} serial {serial} is present but does not expose ALSA playback")
        }
        [device] => Ok(device.clone()),
        _ => bail!("{label} serial {serial} matched multiple ALSA cards"),
    }
}

fn validate_lab_device_shape(
    alsa_devices: &[DeviceInfo],
    usb_devices: &[UsbDeviceInfo],
) -> Result<()> {
    if usb_devices.len() != 2 {
        bail!(
            "expected exactly two Apple USB devices but found {}",
            usb_devices.len()
        );
    }
    if alsa_devices.len() != 2 {
        bail!(
            "expected exactly two Apple ALSA cards but found {}",
            alsa_devices.len()
        );
    }

    let inactive_usb: Vec<_> = usb_devices
        .iter()
        .filter(|device| !device.has_alsa_card)
        .map(|device| device.serial.as_str())
        .collect();
    if !inactive_usb.is_empty() {
        bail!(
            "Apple USB device(s) present without ALSA card: {}",
            inactive_usb.join(", ")
        );
    }

    let missing_playback: Vec<_> = alsa_devices
        .iter()
        .filter(|device| !device.has_playback)
        .map(|device| device.serial.as_str())
        .collect();
    if !missing_playback.is_empty() {
        bail!(
            "Apple ALSA card(s) present without playback: {}",
            missing_playback.join(", ")
        );
    }

    let missing_serials: Vec<_> = alsa_devices
        .iter()
        .filter(|device| device.serial.is_empty())
        .map(|device| device.pcm.as_str())
        .collect();
    if !missing_serials.is_empty() {
        bail!(
            "Apple ALSA card(s) missing serial evidence: {}",
            missing_serials.join(", ")
        );
    }

    Ok(())
}

fn discover_apple_devices() -> Result<Vec<DeviceInfo>> {
    let mut devices = Vec::new();
    let proc_asound = Path::new("/proc/asound");
    if !proc_asound.exists() {
        return Ok(devices);
    }

    for entry in fs::read_dir(proc_asound).context("reading /proc/asound")? {
        let entry = entry.context("reading /proc/asound entry")?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        let Some(card_suffix) = name.strip_prefix("card") else {
            continue;
        };
        let Ok(card) = card_suffix.parse::<u32>() else {
            continue;
        };
        let card_dir = entry.path();
        let usbid = read_trim(card_dir.join("usbid")).unwrap_or_default();
        if usbid.trim() != APPLE_VID_PID {
            continue;
        }
        let card_id = read_trim(card_dir.join("id")).unwrap_or_else(|| format!("card{card}"));
        let stream_summary = read_stream_summary(&card_dir);
        let has_playback = stream_summary.contains("Playback:");
        let sysfs_card_path = PathBuf::from(format!("/sys/class/sound/card{card}"));
        let sysfs_device_path = sysfs_card_path.join("device");
        let canonical_card = fs::canonicalize(&sysfs_device_path).unwrap_or(sysfs_device_path);
        let usb_device_path = find_usb_device_path(&canonical_card).unwrap_or_default();
        let serial = read_trim(Path::new(&usb_device_path).join("serial")).unwrap_or_default();
        let busnum = read_trim(Path::new(&usb_device_path).join("busnum")).unwrap_or_default();
        let devpath = read_trim(Path::new(&usb_device_path).join("devpath")).unwrap_or_default();
        let speed = read_trim(Path::new(&usb_device_path).join("speed")).unwrap_or_default();
        let controller = find_controller(&canonical_card);

        devices.push(DeviceInfo {
            card,
            card_id,
            pcm: format!("hw:{card},0"),
            serial,
            usbid,
            has_playback,
            stream_summary,
            sysfs_card_path: canonical_card.display().to_string(),
            usb_device_path,
            controller,
            busnum,
            devpath,
            speed,
            by_id: find_snd_links("/dev/snd/by-id", card),
            by_path: find_snd_links("/dev/snd/by-path", card),
        });
    }

    devices.sort_by_key(|device| device.card);
    Ok(devices)
}

fn discover_apple_usb_devices(alsa_devices: &[DeviceInfo]) -> Result<Vec<UsbDeviceInfo>> {
    let mut devices = Vec::new();
    let sys_usb = Path::new("/sys/bus/usb/devices");
    if !sys_usb.exists() {
        return Ok(devices);
    }

    for entry in fs::read_dir(sys_usb).context("reading /sys/bus/usb/devices")? {
        let entry = entry.context("reading /sys/bus/usb/devices entry")?;
        let path = entry.path();
        if read_trim(path.join("idVendor")).as_deref() != Some("05ac")
            || read_trim(path.join("idProduct")).as_deref() != Some("110a")
        {
            continue;
        }

        let canonical = fs::canonicalize(&path).unwrap_or(path);
        let usb_device_path = canonical.display().to_string();
        let serial = read_trim(canonical.join("serial")).unwrap_or_default();
        let interface_classes = read_interface_classes(&canonical);
        let has_alsa_card = alsa_devices.iter().any(|device| {
            (!serial.is_empty() && device.serial == serial)
                || device.usb_device_path == usb_device_path
        });

        devices.push(UsbDeviceInfo {
            serial,
            usb_device_path,
            controller: find_controller(&canonical),
            busnum: read_trim(canonical.join("busnum")).unwrap_or_default(),
            devpath: read_trim(canonical.join("devpath")).unwrap_or_default(),
            devnum: read_trim(canonical.join("devnum")).unwrap_or_default(),
            speed: read_trim(canonical.join("speed")).unwrap_or_default(),
            active_configuration: read_trim(canonical.join("bConfigurationValue"))
                .unwrap_or_default(),
            configuration_count: read_trim(canonical.join("bNumConfigurations"))
                .unwrap_or_default(),
            interface_count: read_trim(canonical.join("bNumInterfaces")).unwrap_or_default(),
            interface_classes,
            has_alsa_card,
        });
    }

    devices.sort_by(|left, right| {
        left.busnum
            .cmp(&right.busnum)
            .then_with(|| left.devpath.cmp(&right.devpath))
            .then_with(|| left.serial.cmp(&right.serial))
    });
    Ok(devices)
}

fn read_interface_classes(usb_device_path: &Path) -> Vec<String> {
    let mut classes = Vec::new();
    let Ok(entries) = fs::read_dir(usb_device_path) else {
        return classes;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
            continue;
        };
        if !name.contains(':') {
            continue;
        }
        let class = read_trim(path.join("bInterfaceClass")).unwrap_or_default();
        let subclass = read_trim(path.join("bInterfaceSubClass")).unwrap_or_default();
        let protocol = read_trim(path.join("bInterfaceProtocol")).unwrap_or_default();
        classes.push(format!("{name}:{class}/{subclass}/{protocol}"));
    }
    classes.sort();
    classes
}

fn read_stream_summary(card_dir: &Path) -> String {
    let mut streams = Vec::new();
    if let Ok(entries) = fs::read_dir(card_dir) {
        for entry in entries.flatten() {
            let name = entry.file_name();
            let Some(name) = name.to_str() else {
                continue;
            };
            if !name.starts_with("stream") {
                continue;
            }
            if let Some(content) = read_trim(entry.path()) {
                let compact = content
                    .lines()
                    .map(str::trim)
                    .filter(|line| !line.is_empty())
                    .collect::<Vec<_>>()
                    .join(" | ");
                streams.push(format!("{name}: {compact}"));
            }
        }
    }
    streams.sort();
    streams.join(" || ")
}

fn find_usb_device_path(card_path: &Path) -> Option<String> {
    let mut current = Some(card_path);
    while let Some(path) = current {
        let vendor = read_trim(path.join("idVendor"));
        let product = read_trim(path.join("idProduct"));
        if vendor.as_deref() == Some("05ac") && product.as_deref() == Some("110a") {
            return Some(path.display().to_string());
        }
        current = path.parent();
    }
    None
}

fn find_controller(card_path: &Path) -> String {
    for component in card_path.components() {
        let value = component.as_os_str().to_string_lossy();
        if value.starts_with("xhci-hcd.") {
            return value.into_owned();
        }
    }
    String::new()
}

fn find_snd_links(dir: &str, card: u32) -> Vec<String> {
    let mut links = Vec::new();
    let Ok(entries) = fs::read_dir(dir) else {
        return links;
    };
    let needle_control = format!("controlC{card}");
    let needle_pcm = format!("pcmC{card}D");
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(target) = fs::read_link(&path) else {
            continue;
        };
        let target_text = target.display().to_string();
        if target_text.contains(&needle_control) || target_text.contains(&needle_pcm) {
            links.push(path.display().to_string());
        }
    }
    links.sort();
    links
}

fn read_trim(path: impl AsRef<Path>) -> Option<String> {
    let content = fs::read_to_string(path).ok()?;
    Some(content.trim().to_string())
}

fn fill_period(
    mode: Mode,
    sink_index: usize,
    start_frame: u64,
    sample_rate: u32,
    amplitude: i16,
    active_frames: usize,
    out: &mut [i16],
) {
    out.fill(0);
    let active_samples = active_frames
        .min(out.len() / CHANNELS)
        .saturating_mul(CHANNELS);
    let active_out = &mut out[..active_samples];
    match mode {
        Mode::Silence => {}
        Mode::Identity => {
            fill_identity(sink_index, start_frame, sample_rate, amplitude, active_out)
        }
        Mode::Ticks => fill_ticks(start_frame, sample_rate, amplitude, active_out),
    }
}

fn active_frames_for_period(
    requested_signal_frames: u64,
    start_frame: u64,
    period_frames: u32,
) -> usize {
    requested_signal_frames
        .saturating_sub(start_frame)
        .min(period_frames as u64) as usize
}

fn fill_identity(
    sink_index: usize,
    start_frame: u64,
    sample_rate: u32,
    amplitude: i16,
    out: &mut [i16],
) {
    let frames = out.len() / CHANNELS;
    let sample_rate = sample_rate as u64;
    for frame in 0..frames {
        let absolute = start_frame + frame as u64;
        let second = absolute / sample_rate;
        let within_second = absolute % sample_rate;
        if within_second >= sample_rate / 10 {
            continue;
        }
        let target = (second % 4) as usize;
        let this_channel = match (sink_index, target) {
            (0, 0) => Some(0),
            (0, 1) => Some(1),
            (1, 2) => Some(0),
            (1, 3) => Some(1),
            _ => None,
        };
        if let Some(channel) = this_channel {
            let pulse = if (absolute / 24) % 2 == 0 {
                amplitude
            } else {
                -amplitude
            };
            out[frame * CHANNELS + channel] = pulse;
        }
    }
}

fn fill_ticks(start_frame: u64, sample_rate: u32, amplitude: i16, out: &mut [i16]) {
    let frames = out.len() / CHANNELS;
    let sample_rate = sample_rate as u64;
    for frame in 0..frames {
        let absolute = start_frame + frame as u64;
        let within_second = absolute % sample_rate;
        if within_second >= 2048 {
            continue;
        }
        let bit = prbs_bit(absolute);
        let sample = if bit { amplitude } else { -amplitude };
        out[frame * CHANNELS] = sample;
        out[frame * CHANNELS + 1] = sample;
    }
}

fn prbs_bit(mut value: u64) -> bool {
    value ^= value >> 33;
    value = value.wrapping_mul(0xff51afd7ed558ccd);
    value ^= value >> 33;
    value = value.wrapping_mul(0xc4ceb9fe1a85ec53);
    value ^= value >> 33;
    value & 1 == 1
}

fn db_to_i16(db: f32) -> i16 {
    let linear = 10.0_f32.powf(db / 20.0);
    let value = (i16::MAX as f32 * linear).round();
    value.clamp(1.0, i16::MAX as f32) as i16
}

fn bad_state(state: State) -> bool {
    matches!(state, State::XRun | State::Suspended | State::Disconnected)
}

fn div_ceil(a: u64, b: u64) -> u64 {
    if b == 0 {
        0
    } else {
        (a / b) + u64::from(a % b != 0)
    }
}

fn epoch_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

fn json_array(values: &[String]) -> String {
    format!(
        "[{}]",
        values
            .iter()
            .map(|value| json_string(value))
            .collect::<Vec<_>>()
            .join(",")
    )
}

fn json_string(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            ch if ch.is_control() => {
                use std::fmt::Write;
                let _ = write!(out, "\\u{:04x}", ch as u32);
            }
            ch => out.push(ch),
        }
    }
    out.push('"');
    out
}

fn print_usage() {
    let bin = env::args()
        .next()
        .unwrap_or_else(|| "jasper-dual-dac-lab".to_string());
    eprintln!(
        "Usage:\n  {bin} probe\n  {bin} run --dac-a-serial SERIAL --dac-b-serial SERIAL --arm-no-speakers [--mode silence|identity|ticks] [--duration-sec N] [--level-db DB]\n\nPlayback modes require disconnected amps/speakers or dummy loads. This binary opens direct hw:CARD,0 PCMs only."
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base_config() -> RunConfig {
        RunConfig {
            dac_a_serial: "A".to_string(),
            dac_b_serial: "B".to_string(),
            mode: Mode::Silence,
            sample_rate: DEFAULT_SAMPLE_RATE,
            period_frames: DEFAULT_PERIOD_FRAMES,
            buffer_frames: DEFAULT_BUFFER_FRAMES,
            duration_sec: DEFAULT_DURATION_SEC,
            report_periods: DEFAULT_REPORT_PERIODS,
            level_db: DEFAULT_LEVEL_DB,
            max_delay_delta_frames: DEFAULT_DELAY_DELTA_FRAMES,
            require_link: false,
            disable_link: false,
            arm_no_speakers: false,
        }
    }

    fn device(serial: &str, card: u32, has_playback: bool) -> DeviceInfo {
        DeviceInfo {
            card,
            card_id: format!("card{card}"),
            pcm: format!("hw:{card},0"),
            serial: serial.to_string(),
            usbid: APPLE_VID_PID.to_string(),
            has_playback,
            stream_summary: String::new(),
            sysfs_card_path: String::new(),
            usb_device_path: format!("/sys/fake/{serial}"),
            controller: "xhci-hcd.0".to_string(),
            busnum: "1".to_string(),
            devpath: card.to_string(),
            speed: "12".to_string(),
            by_id: Vec::new(),
            by_path: Vec::new(),
        }
    }

    fn usb_device(serial: &str, has_alsa_card: bool) -> UsbDeviceInfo {
        UsbDeviceInfo {
            serial: serial.to_string(),
            usb_device_path: format!("/sys/fake/{serial}"),
            controller: "xhci-hcd.0".to_string(),
            busnum: "1".to_string(),
            devpath: "1".to_string(),
            devnum: "1".to_string(),
            speed: "12".to_string(),
            active_configuration: "2".to_string(),
            configuration_count: "3".to_string(),
            interface_count: "4".to_string(),
            interface_classes: Vec::new(),
            has_alsa_card,
        }
    }

    fn stereo_frame(samples: &[i16], frame: usize) -> [i16; CHANNELS] {
        [samples[frame * CHANNELS], samples[frame * CHANNELS + 1]]
    }

    #[test]
    fn validate_run_config_accepts_defaults() {
        validate_run_config(&base_config()).unwrap();
    }

    #[test]
    fn validate_run_config_rejects_hot_or_nonfinite_level() {
        let mut config = base_config();
        config.level_db = -12.0;
        assert!(validate_run_config(&config)
            .unwrap_err()
            .to_string()
            .contains("max allowed"));

        config.level_db = f32::NAN;
        assert!(validate_run_config(&config)
            .unwrap_err()
            .to_string()
            .contains("finite"));
    }

    #[test]
    fn validate_run_config_rejects_invalid_timing() {
        let mut config = base_config();
        config.sample_rate = 0;
        assert!(validate_run_config(&config)
            .unwrap_err()
            .to_string()
            .contains("sample-rate"));

        config = base_config();
        config.buffer_frames = config.period_frames;
        assert!(validate_run_config(&config)
            .unwrap_err()
            .to_string()
            .contains("2x period"));

        config = base_config();
        config.duration_sec = 0;
        assert!(validate_run_config(&config)
            .unwrap_err()
            .to_string()
            .contains("duration-sec"));
    }

    #[test]
    fn negotiated_sample_rates_must_match_each_other_and_the_request() {
        validate_negotiated_sample_rates(48_000, 48_000, 48_000).unwrap();

        let disagreement = validate_negotiated_sample_rates(48_000, 48_000, 44_100)
            .unwrap_err()
            .to_string();
        assert!(disagreement.contains("negotiated sample rates disagree"));
        assert!(disagreement.contains("dac_a=48000 dac_b=44100"));

        let nearest = validate_negotiated_sample_rates(48_000, 44_100, 44_100)
            .unwrap_err()
            .to_string();
        assert!(nearest.contains("negotiated 44100 Hz instead of requested 48000 Hz"));
    }

    #[test]
    fn identity_follows_documented_four_second_channel_order_and_wraps() {
        const RATE: u32 = 1_000;
        const AMPLITUDE: i16 = 777;
        let expected = [
            (Some(0), None),
            (Some(1), None),
            (None, Some(0)),
            (None, Some(1)),
            (Some(0), None),
        ];

        for (second, (dac_a_channel, dac_b_channel)) in expected.into_iter().enumerate() {
            for (sink, target_channel) in [dac_a_channel, dac_b_channel].into_iter().enumerate() {
                let mut out = vec![i16::MAX; RATE as usize * CHANNELS];
                fill_period(
                    Mode::Identity,
                    sink,
                    second as u64 * RATE as u64,
                    RATE,
                    AMPLITUDE,
                    RATE as usize,
                    &mut out,
                );

                for frame in 0..RATE as usize {
                    for channel in 0..CHANNELS {
                        let sample = stereo_frame(&out, frame)[channel];
                        let should_pulse =
                            frame < RATE as usize / 10 && target_channel == Some(channel);
                        if should_pulse {
                            assert_eq!(sample.abs(), AMPLITUDE);
                        } else {
                            assert_eq!(sample, 0);
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn identity_pulse_stops_at_exact_one_tenth_second_boundary() {
        const RATE: u32 = 1_000;
        let mut out = vec![123; 3 * CHANNELS];
        fill_period(Mode::Identity, 0, 99, RATE, 500, 3, &mut out);

        assert_ne!(stereo_frame(&out, 0)[0], 0);
        assert_eq!(stereo_frame(&out, 0)[1], 0);
        assert_eq!(stereo_frame(&out, 1), [0, 0]);
        assert_eq!(stereo_frame(&out, 2), [0, 0]);
    }

    #[test]
    fn identity_preserves_order_when_nonzero_start_frame_crosses_boundaries() {
        const RATE: u32 = 1_000;
        for (start_frame, expected_channel) in [(998_u64, 1_usize), (3_998, 0)] {
            let mut out = vec![123; 6 * CHANNELS];
            fill_period(Mode::Identity, 0, start_frame, RATE, 500, 6, &mut out);

            assert_eq!(stereo_frame(&out, 0), [0, 0]);
            assert_eq!(stereo_frame(&out, 1), [0, 0]);
            for frame in 2..6 {
                let samples = stereo_frame(&out, frame);
                assert_ne!(samples[expected_channel], 0);
                assert_eq!(samples[1 - expected_channel], 0);
            }
        }
    }

    #[test]
    fn identity_zeroes_dirty_non_target_channels_and_inactive_tail() {
        const RATE: u32 = 1_000;
        let mut non_target_sink = vec![123; 50 * CHANNELS];
        fill_period(
            Mode::Identity,
            0,
            2 * RATE as u64,
            RATE,
            500,
            50,
            &mut non_target_sink,
        );
        assert!(non_target_sink.iter().all(|sample| *sample == 0));

        let mut target_sink = vec![123; 50 * CHANNELS];
        fill_period(
            Mode::Identity,
            1,
            2 * RATE as u64,
            RATE,
            500,
            25,
            &mut target_sink,
        );
        for frame in 0..50 {
            let samples = stereo_frame(&target_sink, frame);
            if frame < 25 {
                assert_ne!(samples[0], 0);
            } else {
                assert_eq!(samples[0], 0);
            }
            assert_eq!(samples[1], 0);
        }
    }

    #[test]
    fn twenty_second_partial_period_cannot_start_next_identity_or_tick_cycle() {
        const RATE: u32 = 48_000;
        const PERIOD_FRAMES: u64 = 1_024;
        let requested_frames = 20_u64 * RATE as u64;
        let periods = div_ceil(requested_frames, PERIOD_FRAMES);
        let start_frame = (periods - 1) * PERIOD_FRAMES;
        let active_frames =
            active_frames_for_period(requested_frames, start_frame, PERIOD_FRAMES as u32);
        assert_eq!(active_frames, 512);
        assert_eq!(
            active_frames_for_period(requested_frames, 0, PERIOD_FRAMES as u32),
            PERIOD_FRAMES as usize
        );
        assert_eq!(
            active_frames_for_period(requested_frames, requested_frames, PERIOD_FRAMES as u32),
            0
        );
        assert_eq!(
            active_frames_for_period(
                requested_frames,
                requested_frames + PERIOD_FRAMES,
                PERIOD_FRAMES as u32,
            ),
            0
        );

        for mode in [Mode::Identity, Mode::Ticks] {
            let mut full_period = vec![123; PERIOD_FRAMES as usize * CHANNELS];
            fill_period(
                mode,
                0,
                start_frame,
                RATE,
                500,
                PERIOD_FRAMES as usize,
                &mut full_period,
            );
            assert!(full_period[active_frames * CHANNELS..]
                .iter()
                .any(|sample| *sample != 0));

            let mut bounded_period = vec![123; PERIOD_FRAMES as usize * CHANNELS];
            fill_period(
                mode,
                0,
                start_frame,
                RATE,
                500,
                active_frames,
                &mut bounded_period,
            );
            assert!(bounded_period.iter().all(|sample| *sample == 0));
        }
    }

    #[test]
    fn silence_mode_clears_dirty_full_period_and_inactive_tail() {
        let mut out = vec![i16::MAX; 16 * CHANNELS];
        fill_period(Mode::Silence, 0, 123, 48_000, 500, 3, &mut out);
        assert!(out.iter().all(|sample| *sample == 0));
    }

    #[test]
    fn validate_lab_device_shape_requires_two_active_playback_cards() {
        let alsa = vec![device("A", 3, true), device("B", 4, true)];
        let usb = vec![usb_device("A", true), usb_device("B", true)];
        validate_lab_device_shape(&alsa, &usb).unwrap();

        let usb_with_hid_only = vec![usb_device("A", true), usb_device("B", false)];
        assert!(validate_lab_device_shape(&alsa, &usb_with_hid_only)
            .unwrap_err()
            .to_string()
            .contains("without ALSA card"));

        let alsa_with_capture_only = vec![device("A", 3, true), device("B", 4, false)];
        assert!(validate_lab_device_shape(&alsa_with_capture_only, &usb)
            .unwrap_err()
            .to_string()
            .contains("without playback"));
    }

    #[test]
    fn div_ceil_does_not_overflow() {
        assert_eq!(div_ceil(0, 1024), 0);
        assert_eq!(div_ceil(1024, 1024), 1);
        assert_eq!(div_ceil(1025, 1024), 2);
        assert_eq!(div_ceil(u64::MAX, u64::MAX), 1);
    }
}
