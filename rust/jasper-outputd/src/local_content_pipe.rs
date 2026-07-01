// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Local CamillaDSP -> outputd content pipe.
//!
//! This is the reader half of the end-to-end low-latency transport:
//! CamillaDSP writes post-DSP S32_LE stereo PCM to a FIFO, and outputd reads at
//! the DAC cadence before performing the blocking DAC write. Unlike
//! `dac_content`, this is not a multiroom round-trip lane: there is no fallback
//! to the snd-aloop direct lane and no period staging. Empty or partial reads
//! become silence for the missing samples so the final DAC loop stays alive.

use std::io;
use std::os::fd::RawFd;

pub const LOCAL_CONTENT_PIPE_FORMAT: &str = "S32_LE";
const S32_BYTES: usize = std::mem::size_of::<i32>();

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct LocalContentPipeMetrics {
    pub enabled: bool,
    pub open: bool,
    pub frames_read: u64,
    pub requested_pipe_bytes: u64,
    pub open_failures: u64,
    pub read_failures: u64,
    pub reopen_count: u64,
    pub startup_empty_periods: u64,
    pub empty_periods: u64,
    pub partial_periods: u64,
    pub misaligned_bytes: u64,
    pub available_bytes: u64,
    pub actual_pipe_bytes: u64,
}

pub struct LocalContentPipe {
    path: String,
    fd: Option<RawFd>,
    period_bytes: usize,
    frame_bytes: usize,
    requested_pipe_bytes: u32,
    read_buf: Vec<u8>,
    saw_payload: bool,
    metrics: LocalContentPipeMetrics,
}

impl LocalContentPipe {
    pub fn new(
        path: &str,
        period_frames: u32,
        channels: u16,
        requested_pipe_bytes: u32,
    ) -> io::Result<Self> {
        ensure_fifo(path)?;
        let frame_bytes = (channels as usize) * S32_BYTES;
        let period_bytes = (period_frames as usize) * frame_bytes;
        Ok(Self {
            path: path.to_string(),
            fd: None,
            period_bytes,
            frame_bytes,
            requested_pipe_bytes,
            read_buf: vec![0u8; period_bytes],
            saw_payload: false,
            metrics: LocalContentPipeMetrics {
                enabled: true,
                requested_pipe_bytes: requested_pipe_bytes as u64,
                ..LocalContentPipeMetrics::default()
            },
        })
    }

    pub fn path(&self) -> &str {
        &self.path
    }

    pub fn metrics(&self) -> LocalContentPipeMetrics {
        self.metrics
    }

    pub fn read_period(&mut self, out: &mut [i16]) -> io::Result<usize> {
        debug_assert_eq!(out.len() * S32_BYTES, self.period_bytes);
        out.fill(0);
        self.open_if_needed();
        let Some(fd) = self.fd else {
            self.mark_empty_period();
            return Ok(0);
        };

        self.metrics.available_bytes = pipe_available_bytes(fd).unwrap_or(0);
        self.read_buf.fill(0);
        let mut total = 0usize;
        while total < self.period_bytes {
            let n = unsafe {
                libc::read(
                    fd,
                    self.read_buf[total..].as_mut_ptr() as *mut libc::c_void,
                    self.period_bytes - total,
                )
            };
            if n > 0 {
                total += n as usize;
                continue;
            }
            if n == 0 {
                break;
            }
            let err = io::Error::last_os_error();
            match err.raw_os_error() {
                Some(libc::EAGAIN) => break,
                Some(libc::EINTR) => continue,
                _ => {
                    self.metrics.read_failures = self.metrics.read_failures.saturating_add(1);
                    self.close_fd();
                    return Err(err);
                }
            }
        }

        if total == 0 {
            self.mark_empty_period();
            return Ok(0);
        }
        self.saw_payload = true;
        if total < self.period_bytes {
            self.metrics.partial_periods = self.metrics.partial_periods.saturating_add(1);
        }
        let usable = total - (total % self.frame_bytes);
        if usable != total {
            self.metrics.misaligned_bytes = self
                .metrics
                .misaligned_bytes
                .saturating_add((total - usable) as u64);
        }
        let frames = usable / self.frame_bytes;
        let samples = frames * (self.frame_bytes / S32_BYTES);
        for (idx, sample_bytes) in self.read_buf[..samples * S32_BYTES]
            .chunks_exact(S32_BYTES)
            .enumerate()
        {
            out[idx] = s32le_to_i16(sample_bytes);
        }
        self.metrics.frames_read = self.metrics.frames_read.saturating_add(frames as u64);
        Ok(frames)
    }

    fn open_if_needed(&mut self) {
        if self.fd.is_some() {
            return;
        }
        let c_path = match std::ffi::CString::new(self.path.as_bytes()) {
            Ok(path) => path,
            Err(_) => {
                self.metrics.open_failures = self.metrics.open_failures.saturating_add(1);
                return;
            }
        };
        let fd = unsafe {
            libc::open(
                c_path.as_ptr(),
                libc::O_RDONLY | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            self.metrics.open_failures = self.metrics.open_failures.saturating_add(1);
            return;
        }
        if let Err(err) = set_pipe_capacity_bytes(fd, self.requested_pipe_bytes) {
            eprintln!(
                "event=outputd.local_content_pipe.size_failed pipe={} requested_pipe_bytes={} detail={}",
                self.path, self.requested_pipe_bytes, err
            );
        }
        self.metrics.reopen_count = self.metrics.reopen_count.saturating_add(1);
        self.metrics.open = true;
        self.metrics.actual_pipe_bytes = pipe_capacity_bytes(fd).unwrap_or(0);
        eprintln!(
            "event=outputd.local_content_pipe.opened pipe={} requested_pipe_bytes={} actual_pipe_bytes={}",
            self.path, self.metrics.requested_pipe_bytes, self.metrics.actual_pipe_bytes
        );
        self.fd = Some(fd);
    }

    fn close_fd(&mut self) {
        if let Some(fd) = self.fd.take() {
            unsafe { libc::close(fd) };
        }
        self.metrics.open = false;
        self.metrics.actual_pipe_bytes = 0;
        self.metrics.available_bytes = 0;
    }

    fn mark_empty_period(&mut self) {
        if self.saw_payload {
            self.metrics.empty_periods = self.metrics.empty_periods.saturating_add(1);
        } else {
            self.metrics.startup_empty_periods =
                self.metrics.startup_empty_periods.saturating_add(1);
        }
    }
}

impl Drop for LocalContentPipe {
    fn drop(&mut self) {
        self.close_fd();
    }
}

fn ensure_fifo(path: &str) -> io::Result<()> {
    if let Some(parent) = std::path::Path::new(path).parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let c_path = std::ffi::CString::new(path)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "fifo path contains NUL"))?;
    let rc = unsafe { libc::mkfifo(c_path.as_ptr(), 0o660) };
    if rc < 0 {
        let err = io::Error::last_os_error();
        if err.raw_os_error() == Some(libc::EEXIST) {
            let meta = std::fs::metadata(path)?;
            use std::os::unix::fs::FileTypeExt;
            if meta.file_type().is_fifo() {
                return Ok(());
            }
            if is_owned_runtime_pipe_path(path) {
                std::fs::remove_file(path)?;
                eprintln!(
                    "event=outputd.local_content_pipe.replaced_non_fifo pipe={}",
                    path
                );
                let retry = unsafe { libc::mkfifo(c_path.as_ptr(), 0o660) };
                if retry < 0 {
                    return Err(io::Error::last_os_error());
                }
                return Ok(());
            }
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                format!("local content pipe {path:?} exists and is not a FIFO"),
            ));
        }
        return Err(err);
    }
    Ok(())
}

fn is_owned_runtime_pipe_path(path: &str) -> bool {
    let path = std::path::Path::new(path);
    path.parent() == Some(std::path::Path::new("/run/jasper-outputd"))
}

fn pipe_capacity_bytes(fd: RawFd) -> io::Result<u64> {
    let actual = unsafe { libc::fcntl(fd, libc::F_GETPIPE_SZ) };
    if actual < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(actual as u64)
}

fn set_pipe_capacity_bytes(fd: RawFd, requested_bytes: u32) -> io::Result<u64> {
    let actual = unsafe { libc::fcntl(fd, libc::F_SETPIPE_SZ, requested_bytes as libc::c_int) };
    if actual < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(actual as u64)
}

fn pipe_available_bytes(fd: RawFd) -> io::Result<u64> {
    let mut available: libc::c_int = 0;
    let rc = unsafe { libc::ioctl(fd, libc::FIONREAD, &mut available) };
    if rc < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(available.max(0) as u64)
}

fn s32le_to_i16(bytes: &[u8]) -> i16 {
    let sample = i32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]);
    (sample >> 16) as i16
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::os::fd::FromRawFd;

    #[test]
    fn ensure_fifo_rejects_regular_file() {
        let dir =
            std::env::temp_dir().join(format!("outputd-local-pipe-file-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("content.pipe");
        std::fs::write(&path, b"not a fifo").unwrap();

        let err = ensure_fifo(path.to_str().unwrap()).expect_err("regular file rejected");
        assert_eq!(err.kind(), io::ErrorKind::AlreadyExists);

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir(&dir);
    }

    #[test]
    fn owned_runtime_pipe_reclaim_is_narrow() {
        assert!(is_owned_runtime_pipe_path(
            "/run/jasper-outputd/content.pipe"
        ));
        assert!(!is_owned_runtime_pipe_path(
            "/run/jasper-outputd/nested/content.pipe"
        ));
        assert!(!is_owned_runtime_pipe_path(
            "/tmp/jasper-outputd/content.pipe"
        ));
    }

    #[test]
    fn read_period_zero_fills_when_no_writer() {
        let dir =
            std::env::temp_dir().join(format!("outputd-local-pipe-empty-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("content.pipe");
        let _ = std::fs::remove_file(&path);

        let mut pipe = LocalContentPipe::new(path.to_str().unwrap(), 2, 2, 4096).unwrap();
        let mut out = [7i16; 4];
        let frames = pipe.read_period(&mut out).unwrap();
        assert_eq!(frames, 0);
        assert_eq!(out, [0, 0, 0, 0]);
        assert_eq!(pipe.metrics().startup_empty_periods, 1);
        assert_eq!(pipe.metrics().empty_periods, 0);

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir(&dir);
    }

    #[test]
    fn read_period_reads_one_period_without_staging() {
        let dir =
            std::env::temp_dir().join(format!("outputd-local-pipe-read-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("content.pipe");
        let _ = std::fs::remove_file(&path);
        ensure_fifo(path.to_str().unwrap()).unwrap();

        let c_path = std::ffi::CString::new(path.to_str().unwrap()).unwrap();
        let writer_fd = unsafe { libc::open(c_path.as_ptr(), libc::O_RDWR | libc::O_CLOEXEC) };
        assert!(writer_fd >= 0);
        let mut writer = unsafe { std::fs::File::from_raw_fd(writer_fd) };

        let mut pipe = LocalContentPipe::new(path.to_str().unwrap(), 2, 2, 4096).unwrap();
        let samples = [1i16, -2, 32767, -32768];
        for sample in samples {
            writer
                .write_all(&((sample as i32) << 16).to_le_bytes())
                .unwrap();
        }

        let mut out = [0i16; 4];
        let frames = pipe.read_period(&mut out).unwrap();
        assert_eq!(frames, 2);
        assert_eq!(out, samples);
        assert_eq!(pipe.metrics().requested_pipe_bytes, 4096);
        assert_eq!(pipe.metrics().startup_empty_periods, 0);
        assert_eq!(pipe.metrics().empty_periods, 0);

        drop(writer);
        let frames = pipe.read_period(&mut out).unwrap();
        assert_eq!(frames, 0);
        assert_eq!(pipe.metrics().startup_empty_periods, 0);
        assert_eq!(pipe.metrics().empty_periods, 1);

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir(&dir);
    }
}
