// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

use alsa::pcm::{Access, Format, HwParams, PCM};
use alsa::ValueOr;

#[derive(Debug, Clone, Copy)]
pub enum BufferSize {
    Exact(u32),
    Nearest(u32),
}

#[derive(Debug, Clone, Copy)]
pub struct HwRequest {
    pub channels: u32,
    pub sample_rate: u32,
    pub format: Format,
    pub period_frames: u32,
    pub buffer: BufferSize,
}

/// Callers own installation and readback: direct capture reads before install,
/// outputd reads after install, and aloop capture needs no readback.
pub fn prepare_hw_params(pcm: &PCM, request: HwRequest) -> alsa::Result<HwParams<'_>> {
    let hwp = HwParams::any(pcm)?;
    hwp.set_channels(request.channels)?;
    hwp.set_rate(request.sample_rate, ValueOr::Nearest)?;
    hwp.set_format(request.format)?;
    hwp.set_access(Access::RWInterleaved)?;
    hwp.set_period_size(request.period_frames as i64, ValueOr::Nearest)?;
    match request.buffer {
        BufferSize::Exact(frames) => hwp.set_buffer_size(frames as i64)?,
        BufferSize::Nearest(frames) => {
            hwp.set_buffer_size_near(frames as i64)?;
        }
    }
    Ok(hwp)
}

pub fn is_xrun_errno(errno: i32) -> bool {
    errno == libc::EPIPE || errno == libc::ESTRPIPE
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn xrun_errnos_exclude_wait_interrupt_and_device_failure() {
        for (errno, expected) in [
            (libc::EPIPE, true),
            (libc::ESTRPIPE, true),
            (libc::EAGAIN, false),
            (libc::EINTR, false),
            (libc::ENODEV, false),
            (libc::EIO, false),
            (0, false),
            (-libc::EPIPE, false),
            (-libc::ESTRPIPE, false),
        ] {
            assert_eq!(is_xrun_errno(errno), expected, "errno={errno}");
        }
    }
}
