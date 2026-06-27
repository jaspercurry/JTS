// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Bounded named-pipe output — the `Coupling::Fifo` transport.
//!
//! Under `JASPER_FANIN_CAMILLA_COUPLING=fifo` the mixer's final per-period
//! write goes to a small named pipe that CamillaDSP File-captures, instead of
//! the ALSA snd-aloop substream. This removes the loopback ring + dsnoop hop
//! from the SHARED capture path; the pacing point moves from the blocking ALSA
//! `writei` to a blocking pipe `write` that backpressures when CamillaDSP (DAC-
//! paced) hasn't drained the ~3-period pipe.
//!
//! ## Why fan-in is NOT the usbsink writer shape (spec §1.3)
//!
//! The usbsink FIFO writer runs on a SEPARATE non-RT thread that synthesizes
//! silence on an empty queue. fan-in is the opposite: the mixer loop IS the
//! producer and the pacer. There is no separate queue to starve — every
//! `step()` produces exactly one period from summed inputs (idle inputs already
//! become silence in `read_input`). So the pipe write stays IN-BAND in `step()`
//! on the RT mixer thread, preserving the single-pace-point invariant; the
//! watchdog is fed by `bump_progress()` after each `step()` exactly as today.
//!
//! ## Format split (spec §0)
//!
//! fan-in mixes/outputs S16_LE internally. The shared CamillaDSP capture is
//! S32_LE. So this writer WIDENS each i16 sample to i32-LE on the wire — the
//! same width the proven usbsink lean writer emits and the File-capture config
//! declares (`jasper.fanin_coupling.FIFO_WIRE_FORMAT == "S32_LE"`).
//!
//! ## SIGPIPE (spec §0)
//!
//! Rust's std runtime installs `SIG_IGN` for SIGPIPE at startup, so a write to
//! a reader-gone pipe returns `EPIPE` rather than killing the process. We rely
//! on that (we do NOT re-arm `SIG_DFL`) and handle `EPIPE` in-band by closing +
//! reopening the write end on the next turn.
//!
//! ## Reader-gone window (spec §1.4)
//!
//! When CamillaDSP reloads its config, the reader vanishes briefly. The reopen
//! path must NOT spin and must NOT wedge the work loop past the watchdog stale
//! threshold. Each no-reader reopen attempt waits a bounded `REOPEN_WAIT` and
//! returns control so the caller can `bump_progress()` and re-check shutdown.

use std::io;
use std::os::unix::io::RawFd;
use std::time::Duration;

use log::{info, warn};

use crate::mixer::CHANNELS;

/// Bytes per S32 sample on the wire. fan-in widens its i16 mix to i32-LE.
const S32_BYTES: usize = 4;

/// Bounded wait inserted on each no-reader / reopen turn so the work loop never
/// spins hot and never wedges past the watchdog stale threshold (5 s). At
/// ≤200 ms per turn, several reopen attempts still fit inside the 5 s window
/// with `bump_progress()` firing between them (the caller bumps the heartbeat
/// after each `write_period` call, including the reopen-wait no-data turns).
const REOPEN_WAIT: Duration = Duration::from_millis(200);

/// Throttle interval for the persistently-absent-reader warning so a CamillaDSP
/// that stays down does not spam journald at the per-period cadence.
const REOPEN_WARN_THROTTLE: Duration = Duration::from_secs(5);

/// Result of one `write_period` attempt — lets `run()` decide whether to
/// `bump_progress()` (always — the loop made progress or waited a bounded
/// time, either way it is alive and must keep the heartbeat fresh).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FifoWriteOutcome {
    /// A full period was written and accepted by the reader.
    Wrote,
    /// No reader yet, or the reader vanished — we waited `REOPEN_WAIT` and the
    /// period was NOT written. The work loop should still bump the heartbeat
    /// (it is alive and bounded), then continue.
    Waited,
}

/// Owns the write end of the shared-capture pipe and the reader-first / reopen
/// state machine. Created once at mixer construction (after the pipe is
/// ensured); `write_period` is called in-band from `step()`.
pub struct FifoWriter {
    path: String,
    /// Requested `F_SETPIPE_SZ` size; the kernel rounds up. Logged with the
    /// read-back actual size the first time the fd opens.
    requested_pipe_bytes: u32,
    /// Current write-end fd, or `-1` when not open (reader absent).
    fd: RawFd,
    /// Reusable wire scratch: i32-LE bytes for one period. Sized to
    /// `period_frames * CHANNELS * S32_BYTES`. Avoids per-period allocation on
    /// the RT path.
    wire_buf: Vec<u8>,
    /// Monotonic instant of the last reopen-warning emission (throttle).
    last_reopen_warn: Option<std::time::Instant>,
    /// True once we have logged the resolved pipe size for the current fd.
    logged_pipe_size: bool,
}

impl FifoWriter {
    /// Ensure the FIFO exists and build the writer. Does NOT open the write end
    /// yet — the reader-first open happens lazily on the first `write_period`
    /// so daemon startup is never gated on CamillaDSP being up.
    pub fn new(path: &str, period_frames: u32, requested_pipe_bytes: u32) -> io::Result<Self> {
        ensure_fifo(path)?;
        let period_samples = (period_frames as usize) * (CHANNELS as usize);
        Ok(Self {
            path: path.to_string(),
            requested_pipe_bytes,
            fd: -1,
            wire_buf: vec![0u8; period_samples * S32_BYTES],
            last_reopen_warn: None,
            logged_pipe_size: false,
        })
    }

    /// Write one period (i16 interleaved) to the pipe, widening to S32_LE.
    ///
    /// Blocking-paced: when the reader is present, the blocking `write` returns
    /// only when the pipe has room — exactly the DAC-paced backpressure the
    /// blocking ALSA `writei` gave us. When the reader is absent (startup or a
    /// CamillaDSP reload), this opens reader-first (non-blocking, retried) and
    /// returns `Waited` after a bounded sleep instead of blocking forever.
    ///
    /// Never panics, never propagates an EPIPE — a reader going away is normal
    /// (CamillaDSP reload) and must not crash fan-in. Genuinely unexpected
    /// errors are logged and treated as a reopen (drop this period); the work
    /// loop stays alive.
    pub fn write_period(&mut self, buf: &[i16]) -> FifoWriteOutcome {
        debug_assert_eq!(buf.len() * S32_BYTES, self.wire_buf.len());

        // (Re)open the write end if needed. Bounded: on no-reader we wait and
        // return Waited so the caller bumps the heartbeat and re-checks
        // shutdown — never a hot spin, never an unbounded block.
        if self.fd < 0 {
            match self.try_open() {
                OpenOutcome::Opened => {}
                OpenOutcome::NoReaderWaited | OpenOutcome::ErrorWaited => {
                    return FifoWriteOutcome::Waited;
                }
            }
        }

        // Pack the period to i32-LE wire bytes.
        widen_i16_to_i32le(buf, &mut self.wire_buf);

        // Blocking write-all. A single write of < PIPE_BUF (4096) is atomic,
        // but we loop defensively to handle short writes on a partially-full
        // pipe and to retry the EINTR case.
        match write_all(self.fd, &self.wire_buf) {
            Ok(()) => FifoWriteOutcome::Wrote,
            Err(e) => {
                // EPIPE (reader gone — the SIG_IGN-for-SIGPIPE path) or any
                // other write error: close + reopen next turn, drop this
                // period. EPIPE is normal (CamillaDSP reload), so it is not a
                // warning; other errnos warn (throttled).
                let errno = e.raw_os_error().unwrap_or(0);
                self.close_fd();
                if errno == libc::EPIPE {
                    info!(
                        "event=fanin.fifo.reader_gone path={} (CamillaDSP reload?) — reopening",
                        self.path,
                    );
                } else {
                    warn!(
                        "event=fanin.fifo.write_error path={} errno={} detail={}",
                        self.path, errno, e,
                    );
                }
                FifoWriteOutcome::Waited
            }
        }
    }

    /// Reader-first open of the write end. Mirrors the proven usbsink /
    /// snapserver SNAPFIFO idiom: open `O_WRONLY|O_NONBLOCK` (so startup is not
    /// gated on the reader), retry on `ENXIO` ("no reader yet"), then clear
    /// `O_NONBLOCK` so subsequent writes block-and-pace on the reader. On open
    /// success, set the small pipe buffer with `F_SETPIPE_SZ` and log the
    /// read-back actual size (the kernel silently rounds the request up).
    fn try_open(&mut self) -> OpenOutcome {
        // SAFETY: open() with a NUL-terminated path; we own the returned fd.
        let c_path = match std::ffi::CString::new(self.path.as_str()) {
            Ok(p) => p,
            Err(_) => {
                // A NUL in a config path is a structural misconfig; warn
                // (throttled) and wait so we don't hot-spin.
                self.throttled_reopen_warn(0, "fifo path contains NUL");
                std::thread::sleep(REOPEN_WAIT);
                return OpenOutcome::ErrorWaited;
            }
        };
        let fd = unsafe { libc::open(c_path.as_ptr(), libc::O_WRONLY | libc::O_NONBLOCK) };
        if fd < 0 {
            let err = io::Error::last_os_error();
            let errno = err.raw_os_error().unwrap_or(0);
            if errno == libc::ENXIO {
                // No reader yet (CamillaDSP not up / reloading). Bounded wait,
                // then let the caller bump the heartbeat and retry next turn.
                std::thread::sleep(REOPEN_WAIT);
                return OpenOutcome::NoReaderWaited;
            }
            self.throttled_reopen_warn(errno, "open failed");
            std::thread::sleep(REOPEN_WAIT);
            return OpenOutcome::ErrorWaited;
        }

        // Reader present. Clear O_NONBLOCK so writes block-and-pace.
        if let Err(e) = clear_nonblock(fd) {
            warn!(
                "event=fanin.fifo.set_blocking_failed path={} detail={} — closing, retry",
                self.path, e,
            );
            // SAFETY: closing the fd we just opened; not used elsewhere.
            unsafe { libc::close(fd) };
            std::thread::sleep(REOPEN_WAIT);
            return OpenOutcome::ErrorWaited;
        }

        // Set the small pipe buffer. Best-effort: a F_SETPIPE_SZ failure (e.g.
        // below page size, or above /proc/sys/fs/pipe-max-size) is non-fatal —
        // the default 64 KiB pipe still paces, just deeper. Log the read-back.
        self.set_and_log_pipe_size(fd);

        self.fd = fd;
        self.last_reopen_warn = None; // reader is back; reset the throttle
        info!("event=fanin.fifo.opened path={}", self.path);
        OpenOutcome::Opened
    }

    /// Apply `F_SETPIPE_SZ` (best-effort) and log the requested vs actual size.
    /// The kernel rounds the request up to a power-of-two ≥ page size, so the
    /// read-back is the only honest record of the live pipe depth.
    fn set_and_log_pipe_size(&mut self, fd: RawFd) {
        // SAFETY: F_SETPIPE_SZ / F_GETPIPE_SZ are integer-returning fcntls on a
        // pipe fd we own; no pointer aliasing.
        let set_rc = unsafe {
            libc::fcntl(
                fd,
                libc::F_SETPIPE_SZ,
                self.requested_pipe_bytes as libc::c_int,
            )
        };
        let actual = unsafe { libc::fcntl(fd, libc::F_GETPIPE_SZ) };
        if !self.logged_pipe_size {
            if set_rc < 0 {
                let err = io::Error::last_os_error();
                warn!(
                    "event=fanin.fifo.pipe_size_set_failed path={} requested={} detail={} — \
                     using kernel default (still DAC-paced, deeper)",
                    self.path, self.requested_pipe_bytes, err,
                );
            }
            info!(
                "event=fanin.fifo.pipe_sized path={} requested={} actual={}",
                self.path,
                self.requested_pipe_bytes,
                if actual >= 0 {
                    actual.to_string()
                } else {
                    "unknown".to_string()
                },
            );
            self.logged_pipe_size = true;
        }
    }

    fn close_fd(&mut self) {
        if self.fd >= 0 {
            // SAFETY: closing the write-end fd we own; set to -1 after so it is
            // never double-closed or reused.
            unsafe { libc::close(self.fd) };
            self.fd = -1;
        }
    }

    fn throttled_reopen_warn(&mut self, errno: i32, what: &str) {
        let now = std::time::Instant::now();
        if let Some(last) = self.last_reopen_warn {
            if now.duration_since(last) < REOPEN_WARN_THROTTLE {
                return;
            }
        }
        self.last_reopen_warn = Some(now);
        warn!(
            "event=fanin.fifo.reopen_error path={} errno={} detail={}",
            self.path, errno, what,
        );
    }
}

impl Drop for FifoWriter {
    fn drop(&mut self) {
        self.close_fd();
    }
}

enum OpenOutcome {
    Opened,
    NoReaderWaited,
    ErrorWaited,
}

/// Widen i16 interleaved samples to i32-LE wire bytes. The shared CamillaDSP
/// File capture reads S32_LE; CamillaDSP's `File` backend interprets the raw
/// bytes per its declared format, so we promote each S16 sample to the high 16
/// bits of an S32 (left-shift by 16) — the standard lossless S16→S32 promotion,
/// matching what the `plug:` did for the loopback path. Pulled out for unit
/// testing (no fd needed).
fn widen_i16_to_i32le(src: &[i16], dst: &mut [u8]) {
    debug_assert_eq!(src.len() * S32_BYTES, dst.len());
    for (i, &s) in src.iter().enumerate() {
        // Promote S16 to the top 16 bits of S32 (<< 16), lossless and
        // amplitude-preserving — the same scaling the loopback `plug:` applied.
        let widened = (s as i32) << 16;
        let bytes = widened.to_le_bytes();
        let off = i * S32_BYTES;
        dst[off..off + S32_BYTES].copy_from_slice(&bytes);
    }
}

/// Write the entire buffer, retrying on partial writes and EINTR. Returns Err
/// on EPIPE (reader gone) or any other unrecoverable write error — the caller
/// decides (close + reopen). EAGAIN cannot occur here: the fd is blocking by
/// the time we write (O_NONBLOCK was cleared after the reader-first open).
fn write_all(fd: RawFd, buf: &[u8]) -> io::Result<()> {
    let mut written = 0usize;
    while written < buf.len() {
        // SAFETY: write() into a valid slice region of a fd we own.
        let n = unsafe {
            libc::write(
                fd,
                buf[written..].as_ptr() as *const libc::c_void,
                buf.len() - written,
            )
        };
        if n < 0 {
            let err = io::Error::last_os_error();
            match err.raw_os_error() {
                Some(libc::EINTR) => continue, // interrupted; retry
                _ => return Err(err),
            }
        }
        if n == 0 {
            // A zero-length blocking write should not happen on a pipe; treat
            // as a broken pipe to force a reopen rather than spin.
            return Err(io::Error::from_raw_os_error(libc::EPIPE));
        }
        written += n as usize;
    }
    Ok(())
}

/// Clear `O_NONBLOCK` on an fd so subsequent writes block-and-pace.
fn clear_nonblock(fd: RawFd) -> io::Result<()> {
    // SAFETY: F_GETFL / F_SETFL on a fd we own; integer flags only.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 {
        return Err(io::Error::last_os_error());
    }
    let rc = unsafe { libc::fcntl(fd, libc::F_SETFL, flags & !libc::O_NONBLOCK) };
    if rc < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// Create the FIFO if absent. Idempotent. Errors only if the path exists and is
/// NOT a FIFO — a real file there is a config error we must not write into.
/// Mirrors usbsink's `_ensure_fifo`: the PRODUCER owns the pipe (CamillaDSP's
/// File backend opens it read-only and does not create it).
fn ensure_fifo(path: &str) -> io::Result<()> {
    if let Some(parent) = std::path::Path::new(path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let c_path = std::ffi::CString::new(path)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "fifo path contains NUL"))?;
    // SAFETY: mkfifo with a NUL-terminated path and a fixed mode.
    let rc = unsafe { libc::mkfifo(c_path.as_ptr(), 0o660) };
    if rc < 0 {
        let err = io::Error::last_os_error();
        if err.raw_os_error() == Some(libc::EEXIST) {
            // Already exists — verify it is a FIFO.
            let meta = std::fs::metadata(path)?;
            use std::os::unix::fs::FileTypeExt;
            if !meta.file_type().is_fifo() {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    format!("fifo path {path:?} exists and is not a FIFO"),
                ));
            }
            return Ok(());
        }
        return Err(err);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;
    use std::os::unix::io::FromRawFd;

    // ---- widen_i16_to_i32le: the format-split correctness ----

    #[test]
    fn widen_promotes_s16_to_high_16_bits_of_s32le() {
        // 1 sample: 0x1234 -> 0x12340000, little-endian bytes.
        let src = [0x1234i16];
        let mut dst = [0u8; 4];
        widen_i16_to_i32le(&src, &mut dst);
        assert_eq!(dst, 0x1234_0000i32.to_le_bytes());
    }

    #[test]
    fn widen_preserves_sign_for_negative_samples() {
        let src = [-1i16]; // 0xFFFF -> (-1 << 16) = 0xFFFF0000 = -65536
        let mut dst = [0u8; 4];
        widen_i16_to_i32le(&src, &mut dst);
        assert_eq!(i32::from_le_bytes(dst), -65536);
    }

    #[test]
    fn widen_full_scale_samples() {
        let src = [i16::MAX, i16::MIN];
        let mut dst = [0u8; 8];
        widen_i16_to_i32le(&src, &mut dst);
        assert_eq!(
            i32::from_le_bytes(dst[0..4].try_into().unwrap()),
            (i16::MAX as i32) << 16
        );
        assert_eq!(
            i32::from_le_bytes(dst[4..8].try_into().unwrap()),
            (i16::MIN as i32) << 16
        );
    }

    // ---- ensure_fifo: idempotency + non-FIFO rejection ----

    #[test]
    fn ensure_fifo_creates_and_is_idempotent() {
        let dir = std::env::temp_dir().join(format!("fanin-fifo-test-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("camilla.pipe");
        let path_s = path.to_str().unwrap();
        // Clean any leftover from a prior run.
        let _ = std::fs::remove_file(&path);

        ensure_fifo(path_s).expect("create fifo");
        use std::os::unix::fs::FileTypeExt;
        assert!(std::fs::metadata(path_s).unwrap().file_type().is_fifo());
        // Second call is a no-op (idempotent).
        ensure_fifo(path_s).expect("idempotent create");

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir(&dir);
    }

    #[test]
    fn ensure_fifo_rejects_a_real_file_at_the_path() {
        let dir = std::env::temp_dir().join(format!("fanin-fifo-real-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("not-a-fifo");
        let path_s = path.to_str().unwrap();
        std::fs::write(&path, b"i am a regular file").unwrap();

        let err = ensure_fifo(path_s).expect_err("must reject a non-FIFO path");
        assert_eq!(err.kind(), io::ErrorKind::AlreadyExists);

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir(&dir);
    }

    // ---- reader-gone / reopen behavior (the safety heart) ----
    //
    // We can exercise the no-reader and reader-present paths with a real FIFO
    // and a controllable reader fd, without ALSA or CamillaDSP.

    #[test]
    fn write_period_waits_when_no_reader_present() {
        let dir = std::env::temp_dir().join(format!("fanin-noreader-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("camilla.pipe");
        let path_s = path.to_str().unwrap().to_string();
        let _ = std::fs::remove_file(&path);

        let mut w = FifoWriter::new(&path_s, 4, 8192).expect("new writer");
        // 4 frames * 2ch = 8 i16 samples.
        let period = vec![0i16; 8];
        // No reader has opened the read end → open() returns ENXIO → Waited.
        let outcome = w.write_period(&period);
        assert_eq!(outcome, FifoWriteOutcome::Waited);
        assert!(w.fd < 0, "fd must remain unopened with no reader");

        drop(w);
        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir(&dir);
    }

    #[test]
    fn write_period_writes_then_reader_gone_triggers_reopen() {
        let dir = std::env::temp_dir().join(format!("fanin-readergone-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("camilla.pipe");
        let path_s = path.to_str().unwrap().to_string();
        let _ = std::fs::remove_file(&path);

        ensure_fifo(&path_s).expect("create fifo");

        // Open a reader (blocking read end). Opening O_RDONLY blocks until a
        // writer appears, so open the read end NON-BLOCKING first.
        let c_path = std::ffi::CString::new(path_s.clone()).unwrap();
        let rfd = unsafe { libc::open(c_path.as_ptr(), libc::O_RDONLY | libc::O_NONBLOCK) };
        assert!(rfd >= 0, "open read end");

        let mut w = FifoWriter::new(&path_s, 4, 8192).expect("new writer");
        let period = vec![1234i16; 8]; // 4 frames * 2ch

        // Reader present → write succeeds.
        let outcome = w.write_period(&period);
        assert_eq!(outcome, FifoWriteOutcome::Wrote);
        assert!(w.fd >= 0, "fd open after a successful write");

        // Read back the widened bytes and verify the format split.
        let mut reader = unsafe { std::fs::File::from_raw_fd(rfd) };
        let mut got = vec![0u8; 8 * S32_BYTES];
        let n = reader.read(&mut got).expect("read back");
        assert_eq!(n, got.len(), "full period readable");
        // First sample widened: 1234 << 16.
        assert_eq!(
            i32::from_le_bytes(got[0..4].try_into().unwrap()),
            (1234i32) << 16
        );

        // Now the reader goes away (CamillaDSP reload). Drop the read fd.
        drop(reader);

        // The next write hits EPIPE (SIGPIPE is SIG_IGN under Rust std) and
        // returns Waited after closing the fd — NEVER a crash. The pipe buffer
        // may absorb one more write before EPIPE surfaces, so write until we
        // observe the reopen (fd closed). Bounded to a few turns.
        let mut saw_reopen = false;
        for _ in 0..8 {
            let oc = w.write_period(&period);
            if oc == FifoWriteOutcome::Waited && w.fd < 0 {
                saw_reopen = true;
                break;
            }
        }
        assert!(
            saw_reopen,
            "reader-gone must trigger a close+reopen, not a crash"
        );

        drop(w);
        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir(&dir);
    }
}
