//! Best-effort writer to the Snapcast pipe FIFO (multi-room LEADER only).
//!
//! ⚠️  DEAD CODE — DO NOT RE-WIRE WITHOUT TTS SEPARATION (inv-3 landmine).
//!
//! This type is currently UNWIRED: `grep SnapfifoSink rust/` returns only this
//! file. It was a live `ReferenceFanout` consumer in commit 050d334, but commit
//! 9102e13 (which moved assistant/TTS ingress into `jasper-fanin`, see
//! `lib.rs`) removed the `snapfifo_path` config field and the `main.rs` wiring.
//! `Config::from_env` no longer reads `JASPER_OUTPUTD_SNAPFIFO_PATH`, so the
//! reconciler's env write is inert and nothing here runs.
//!
//! Re-applying the 050d334 wiring AS-IS would ship a regression: on the live
//! `run_alsa` path the published `content_buf` is the fanin output = **music +
//! TTS** (TTS is mixed upstream by fanin), so this writer would stream the
//! LEADER's TTS to followers — an inv-3 violation (V1 is leader-LOCAL TTS only;
//! HANDOFF-multiroom.md §6). It MUST NOT be re-activated until `jasper-fanin`
//! emits a **music-only** stream for the tap. See the BLOCKER + corrected
//! TTS-separation design at the top of HANDOFF-multiroom.md §2 "inv-2
//! realization". Kept (not deleted) because that design names this the
//! music-half component to reuse once the music-only stream lands.
//!
//! A grouping leader's jasper-outputd taps a copy of the post-clamp stereo
//! program into this FIFO; `snapserver` reads it as a `pipe` source and
//! streams it to the room's followers (and the leader's own snapclient).
//! Tapping after the safety clamp is what makes the streamed audio inherit
//! the speaker's hardware-safety ceiling (HANDOFF-multiroom.md §2/§7).
//!
//! This is a SIDE sink. It must never back-pressure outputd's DAC write
//! loop (§2 invariant 1). The sink itself does blocking whole-packet writes
//! (a partial write would desync the byte stream); the non-back-pressure
//! guarantee comes from its caller — a dedicated writer thread fed by a
//! bounded, drop-on-full channel (see `spawn_snapfifo_writer` in main.rs).
//! A blocking write here stalls only that thread; the DAC loop keeps its
//! cadence and simply drops side packets when the channel is full.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

/// Reinterpret interleaved `i16` samples as their raw host-endian bytes. On
/// the Pi (little-endian) that is the `S16LE` the snapserver pipe source
/// expects.
///
/// SAFETY: `i16` has no padding and no invalid bit patterns, so viewing the
/// slice as bytes is sound; the returned slice borrows `samples` for the
/// same lifetime.
fn i16_as_bytes(samples: &[i16]) -> &[u8] {
    unsafe {
        std::slice::from_raw_parts(
            samples.as_ptr() as *const u8,
            std::mem::size_of_val(samples),
        )
    }
}

/// Lazily-opened, best-effort writer to the snapserver pipe FIFO.
pub struct SnapfifoSink {
    path: PathBuf,
    file: Option<File>,
    opens: u64,
}

impl SnapfifoSink {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            path: path.into(),
            file: None,
            opens: 0,
        }
    }

    /// How many times the FIFO has been (re)opened — a reopen counter for
    /// observability and tests. A climbing value means snapserver keeps
    /// reconnecting.
    pub fn opens(&self) -> u64 {
        self.opens
    }

    /// Open the FIFO for writing, or return `None` when it cannot be opened
    /// yet — a missing FIFO or a snapserver that is not reading. Both are
    /// normal transient states (the caller retries on the next packet), not
    /// errors.
    fn try_open(path: &Path) -> Option<File> {
        // O_WRONLY | O_NONBLOCK so the open returns ENXIO (=> None) when no
        // reader has the FIFO open, instead of blocking the writer thread
        // forever waiting for snapserver. Once open, clear O_NONBLOCK so
        // writes block until the WHOLE packet is accepted — a partial
        // non-blocking write would desync snapserver's byte stream.
        let file = OpenOptions::new()
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(path)
            .ok()?;
        let fd = file.as_raw_fd();
        // SAFETY: `fd` is a valid open descriptor owned by `file` for the
        // duration of these calls.
        unsafe {
            let flags = libc::fcntl(fd, libc::F_GETFL);
            if flags >= 0 {
                let _ = libc::fcntl(fd, libc::F_SETFL, flags & !libc::O_NONBLOCK);
            }
        }
        Some(file)
    }

    /// Write one interleaved-`i16` stereo packet. Best-effort and total:
    /// returns `true` if the bytes were written, `false` if dropped (no
    /// reader yet, or a broken pipe forced a reopen). Never panics.
    pub fn write(&mut self, samples: &[i16]) -> bool {
        if self.file.is_none() {
            self.file = Self::try_open(&self.path);
            if self.file.is_some() {
                self.opens += 1;
            }
        }
        let Some(file) = self.file.as_mut() else {
            return false; // snapserver not reading yet — drop, retry next packet
        };
        match file.write_all(i16_as_bytes(samples)) {
            Ok(()) => true,
            Err(_) => {
                // Broken pipe / reader gone: drop this packet and force a
                // reopen on the next one (snapserver may have restarted).
                self.file = None;
                false
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;
    use std::io::Read;
    use std::os::unix::fs::OpenOptionsExt;
    use std::time::Duration;

    /// A throwaway named pipe that cleans itself up.
    struct TempFifo {
        path: PathBuf,
    }

    impl TempFifo {
        fn new(tag: &str) -> Self {
            let path = std::env::temp_dir()
                .join(format!("jts-snapfifo-{}-{}", tag, std::process::id()));
            let _ = std::fs::remove_file(&path);
            let c = CString::new(path.to_str().unwrap()).unwrap();
            let rc = unsafe { libc::mkfifo(c.as_ptr(), 0o600) };
            assert_eq!(rc, 0, "mkfifo {} failed", path.display());
            Self { path }
        }
    }

    impl Drop for TempFifo {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.path);
        }
    }

    #[test]
    fn drop_when_no_reader_does_not_panic() {
        let fifo = TempFifo::new("noreader");
        let mut sink = SnapfifoSink::new(&fifo.path);
        // No process holds the FIFO open for reading => O_WRONLY|O_NONBLOCK
        // open returns ENXIO => write drops, returns false, never opens.
        assert!(!sink.write(&[0i16; 4]));
        assert_eq!(sink.opens(), 0);
    }

    #[test]
    fn missing_fifo_drops_without_panic() {
        let path = std::env::temp_dir()
            .join(format!("jts-snapfifo-missing-{}", std::process::id()));
        let _ = std::fs::remove_file(&path);
        let mut sink = SnapfifoSink::new(&path);
        assert!(!sink.write(&[1i16, 2, 3, 4]));
        assert_eq!(sink.opens(), 0);
    }

    #[test]
    fn streams_whole_packets_to_a_reader() {
        let fifo = TempFifo::new("reader");
        // Hold the read end open NON-BLOCKING so it exists immediately
        // (without waiting for a writer), which lets the sink's
        // O_WRONLY|O_NONBLOCK open succeed.
        let mut reader = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(&fifo.path)
            .expect("open fifo read end");

        let mut sink = SnapfifoSink::new(&fifo.path);
        let packet: Vec<i16> = vec![100, -200, 300, -400];
        assert!(sink.write(&packet), "write should succeed with a reader present");
        assert_eq!(sink.opens(), 1);

        let expected = i16_as_bytes(&packet).to_vec();
        let mut got = Vec::new();
        let mut buf = [0u8; 64];
        // Non-blocking read end: retry briefly until the bytes arrive.
        for _ in 0..200 {
            match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    got.extend_from_slice(&buf[..n]);
                    if got.len() >= expected.len() {
                        break;
                    }
                }
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(5));
                }
                Err(e) => panic!("read failed: {e}"),
            }
        }
        assert_eq!(got, expected, "reader should receive the exact S16LE bytes");
    }
}
