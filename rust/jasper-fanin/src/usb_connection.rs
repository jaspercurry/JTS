// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::os::fd::AsRawFd;
use std::time::{Duration, Instant};

#[derive(Default)]
pub struct UsbConnection {
    state: Option<File>,
    serial: u64,
    epoch: u64,
    retry_at: Option<Instant>,
}

impl UsbConnection {
    // The existing helper thread polls this; never the audio thread. UDC
    // sysfs_notify survives a quick disconnect/reconnect between reads.
    pub fn poll(&mut self) -> u64 {
        if self.state.is_none() {
            if self.retry_at.is_some_and(|when| Instant::now() < when) {
                return 0;
            }
            self.retry_at = Some(Instant::now() + Duration::from_secs(1));
            self.state = Self::open();
            self.read_edge();
        } else if let Some(file) = &self.state {
            let mut fd = libc::pollfd {
                fd: file.as_raw_fd(),
                events: libc::POLLPRI | libc::POLLERR,
                revents: 0,
            };
            // SAFETY: fd points to one initialized pollfd for this call.
            let result = unsafe { libc::poll(&mut fd, 1, 0) };
            if result < 0 || fd.revents & (libc::POLLNVAL | libc::POLLHUP) != 0 {
                self.state = None;
                self.epoch = 0;
            } else if result > 0 {
                self.read_edge();
            }
        }
        self.epoch
    }

    fn open() -> Option<File> {
        let mut entries = std::fs::read_dir("/sys/class/udc").ok()?;
        let entry = entries.next()?.ok()?;
        if entries.next().is_some() {
            return None;
        }
        File::open(entry.path().join("state")).ok()
    }

    fn read_edge(&mut self) {
        let mut bytes = [0u8; 64];
        let state = self.state.as_mut().and_then(|file| {
            file.seek(SeekFrom::Start(0)).ok()?;
            let n = file.read(&mut bytes).ok()?;
            Some(&bytes[..n] == b"configured\n")
        });
        self.serial = self.serial.saturating_add(1);
        self.epoch = if state == Some(true) { self.serial } else { 0 };
        if state.is_none() {
            self.state = None;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn each_notification_invalidates_even_if_final_state_is_configured() {
        let path = std::env::temp_dir().join(format!("jts-udc-test-{}", std::process::id()));
        let mut writer = File::create(&path).unwrap();
        writer.write_all(b"configured\n").unwrap();
        let mut connection = UsbConnection {
            state: Some(File::open(&path).unwrap()),
            ..Default::default()
        };
        connection.read_edge();
        let first = connection.epoch;
        assert_ne!(first, 0);
        connection.read_edge();
        assert!(connection.epoch > first);
        writer.set_len(0).unwrap();
        writer.rewind().unwrap();
        writer.write_all(b"not attached\n").unwrap();
        connection.read_edge();
        assert_eq!(connection.epoch, 0);
        std::fs::remove_file(path).unwrap();
    }
}
