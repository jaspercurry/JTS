//! Append-only ring buffer of xrun events at
//! `/var/lib/jasper/fanin/xrun_history.jsonl`.
//!
//! Each line is one event. Newest entries at the tail; oldest get
//! truncated when the file crosses MAX_FILE_BYTES. Format: JSON
//! object per line so `jq` and `journalctl`-style tooling can grep
//! it without a parser.
//!
//! Why persist xrun history to disk separately from journald:
//!   - Survives daemon restarts (journald rotates eventually; this
//!     ring is bounded but per-event guaranteed).
//!   - Cheaper to grep when investigating "did the speaker have a
//!     bad night?" than a full journal scan.
//!   - Per-event JSON is machine-readable for downstream tooling
//!     (the /system dashboard could parse it later if useful).
//!
//! The on-disk format is deliberately simple: one JSON object per
//! line. No schema versioning, no metadata header — if the format
//! evolves, future readers can ignore older lines or rotate the
//! file on next deploy.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use log::warn;

/// Soft size cap. Rotated by retaining the last N bytes when the
/// file crosses this threshold. 10 KB is ~100 events at typical
/// JSON line length — enough to characterize a night of usage,
/// small enough to read at a glance.
pub const MAX_FILE_BYTES: u64 = 10_240;

/// On rotation, retain this many bytes from the tail. Less than
/// MAX_FILE_BYTES so the file shrinks meaningfully when it rotates
/// (instead of just shaving a line at a time, which would trigger
/// rotations every event once we're near the cap).
const RETAIN_BYTES_ON_ROTATE: u64 = 4_096;

pub struct XrunLog {
    path: PathBuf,
    /// Cached file size to avoid stat() on every write. Refreshed
    /// after every successful append. Slightly stale is fine; the
    /// cap is soft.
    cached_size: u64,
}

impl XrunLog {
    /// Open the log file's parent dir (create if missing), then
    /// the file. The file itself is opened lazily on the first
    /// append to keep startup cheap.
    pub fn new(path: impl Into<PathBuf>) -> Result<Self> {
        let path: PathBuf = path.into();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).with_context(|| {
                format!(
                    "creating xrun log parent dir {}",
                    parent.display()
                )
            })?;
        }
        let cached_size = std::fs::metadata(&path)
            .map(|m| m.len())
            .unwrap_or(0);
        Ok(Self { path, cached_size })
    }

    /// Append one event line. JSON shape:
    ///   `{"ts": "<RFC3339>", "source": "input|output", "label": "...", "frames": N, "count": M}`
    ///
    /// Errors are logged but non-fatal — xrun logging is observability,
    /// not load-bearing. A failure here doesn't disrupt audio.
    pub fn record(&mut self, event: &XrunEvent) {
        let line = serialize_event(event);
        if let Err(e) = self.append_line(&line) {
            warn!(
                "event=fanin.xrun_log.append_failed detail={:#}",
                e
            );
        }
    }

    fn append_line(&mut self, line: &str) -> Result<()> {
        // Check the rotation threshold before writing. The line
        // length added to the current size is what matters; using
        // cached_size + line.len() avoids re-stat'ing.
        let projected = self.cached_size + line.len() as u64 + 1;
        if projected > MAX_FILE_BYTES {
            self.rotate()?;
        }

        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .with_context(|| {
                format!("opening xrun log {}", self.path.display())
            })?;

        // Write line + newline atomically (single write() syscall on
        // pipes/socks; for files write+sync is the closest equivalent).
        file.write_all(line.as_bytes())?;
        file.write_all(b"\n")?;
        // fdatasync — flushes file content but not metadata. The
        // content (the new line) is what matters; the metadata
        // (mtime, atime) doesn't need persistence. fdatasync is
        // cheaper than fsync.
        file.sync_data()?;

        self.cached_size = projected;
        Ok(())
    }

    /// Truncate from the head: read the last RETAIN_BYTES_ON_ROTATE
    /// bytes, then atomically replace the file with them. The tail
    /// is preserved (the recent events), the head (older events) is
    /// discarded.
    ///
    /// Atomicity: write to a temp file, then rename. If the daemon
    /// crashes mid-rotation, either the original file is intact or
    /// the new file is in place — never a partial.
    fn rotate(&mut self) -> Result<()> {
        let raw = std::fs::read(&self.path).with_context(|| {
            format!("reading xrun log for rotation: {}", self.path.display())
        })?;

        // Find the line boundary at or just past the rotation point.
        // Keep whole lines only — JSONL parsers expect complete
        // objects per line.
        let keep_from = if raw.len() as u64 > RETAIN_BYTES_ON_ROTATE {
            let approx = raw.len() - RETAIN_BYTES_ON_ROTATE as usize;
            // Scan forward to the next newline so we don't keep a
            // partial first line.
            match raw[approx..].iter().position(|&b| b == b'\n') {
                Some(off) => approx + off + 1,
                None => raw.len(), // No newlines? Drop everything.
            }
        } else {
            0
        };

        let tail = &raw[keep_from..];
        let tmp_path = self.path.with_extension("jsonl.tmp");
        {
            let mut tmp = File::create(&tmp_path).with_context(|| {
                format!("creating xrun log tmp {}", tmp_path.display())
            })?;
            tmp.write_all(tail)?;
            tmp.sync_data()?;
        }
        std::fs::rename(&tmp_path, &self.path).with_context(|| {
            format!(
                "renaming xrun log tmp -> {}",
                self.path.display()
            )
        })?;
        self.cached_size = tail.len() as u64;
        Ok(())
    }

    /// Current cached file size — informational, may be stale.
    pub fn size_bytes(&self) -> u64 {
        self.cached_size
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

#[derive(Debug, Clone)]
pub struct XrunEvent {
    pub source: XrunSource,
    pub label: String,
    pub frames: u32,
    pub count: u64,
}

#[derive(Debug, Clone, Copy)]
pub enum XrunSource {
    Input,
    Output,
}

impl XrunSource {
    fn as_str(&self) -> &'static str {
        match self {
            XrunSource::Input => "input",
            XrunSource::Output => "output",
        }
    }
}

fn serialize_event(event: &XrunEvent) -> String {
    // Hand-rolled JSON to avoid a serde dependency for one line shape.
    // The shape is stable and trivial; if it ever grows, switch to serde.
    // Label is the only field that could contain JSON-meaningful chars;
    // simple-escape it.
    let ts = rfc3339_now();
    format!(
        r#"{{"ts":"{}","source":"{}","label":"{}","frames":{},"count":{}}}"#,
        ts,
        event.source.as_str(),
        escape_json(&event.label),
        event.frames,
        event.count,
    )
}

fn rfc3339_now() -> String {
    // chrono would be cleaner but we don't have it as a dep yet.
    // The shape is: 2026-05-25T15:09:32Z. Compute manually from
    // UNIX_EPOCH so we don't pull in a date library for one line.
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = now.as_secs();

    // Days since 1970-01-01.
    let days_since_epoch = secs / 86_400;
    let secs_today = secs % 86_400;
    let h = secs_today / 3600;
    let m = (secs_today % 3600) / 60;
    let s = secs_today % 60;

    let (year, month, day) = days_to_ymd(days_since_epoch as i64);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        year, month, day, h, m, s,
    )
}

/// Convert days-since-1970-01-01 to (year, month, day). Algorithm:
/// Howard Hinnant's "date" library convention (civil_from_days).
/// Public domain.
fn days_to_ymd(days_since_epoch: i64) -> (i32, u32, u32) {
    // Shift epoch to 0000-03-01 so we can use a uniform 400-year
    // cycle and unbiased month math.
    let z = days_since_epoch + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = (yoe as i64) + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = (if mp < 10 { mp + 3 } else { mp - 9 }) as u32;
    let year = if m <= 2 { y + 1 } else { y } as i32;
    (year, m, d)
}

fn escape_json(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;

    #[test]
    fn serialize_event_emits_valid_json_line() {
        let line = serialize_event(&XrunEvent {
            source: XrunSource::Input,
            label: "spotify".to_string(),
            frames: 82,
            count: 3,
        });
        // Shape sanity: starts with {, ends with }, no newline embedded.
        assert!(line.starts_with('{'));
        assert!(line.ends_with('}'));
        assert!(!line.contains('\n'));
        assert!(line.contains(r#""source":"input""#));
        assert!(line.contains(r#""label":"spotify""#));
        assert!(line.contains(r#""frames":82"#));
        assert!(line.contains(r#""count":3"#));
        // The timestamp should look like an RFC3339 'Z' suffix.
        assert!(
            line.contains(r#""ts":""#) && line.contains(r#"Z""#),
            "missing RFC3339 ts: {}",
            line,
        );
    }

    #[test]
    fn escape_json_handles_quotes_backslashes_and_controls() {
        assert_eq!(escape_json("plain"), "plain");
        assert_eq!(escape_json(r#"with "quote""#), r#"with \"quote\""#);
        assert_eq!(escape_json("back\\slash"), "back\\\\slash");
        assert_eq!(escape_json("new\nline"), "new\\nline");
        // Control character below 0x20:
        let s = "x\x01y";
        assert_eq!(escape_json(s), "x\\u0001y");
    }

    #[test]
    fn days_to_ymd_matches_known_dates() {
        // 1970-01-01 = day 0 (definitionally).
        assert_eq!(days_to_ymd(0), (1970, 1, 1));
        // 2000-01-01 = day 10957: 30 years × 365 + 7 leap days
        // (1972, 76, 80, 84, 88, 92, 96).
        assert_eq!(days_to_ymd(10_957), (2000, 1, 1));
        // Year transitions: 2024 was a leap year, 2025 isn't.
        // 2025-01-01: 55 years × 365 + 14 leaps = 20089.
        assert_eq!(days_to_ymd(20_089), (2025, 1, 1));
        // Round-trip through the algorithm for a leap-day case:
        // 2024-02-29 = day 19782.
        assert_eq!(days_to_ymd(19_782), (2024, 2, 29));
    }

    #[test]
    fn append_creates_file_and_writes_lines() {
        let tmp = tempfile_path("fanin_xrun_log_append");
        let _ = std::fs::remove_file(&tmp);

        let mut log = XrunLog::new(&tmp).expect("open xrun log");
        log.record(&XrunEvent {
            source: XrunSource::Output,
            label: "out".to_string(),
            frames: 256,
            count: 1,
        });
        log.record(&XrunEvent {
            source: XrunSource::Input,
            label: "spotify".to_string(),
            frames: 128,
            count: 2,
        });

        let mut content = String::new();
        File::open(&tmp)
            .unwrap()
            .read_to_string(&mut content)
            .unwrap();
        let lines: Vec<_> = content.lines().collect();
        assert_eq!(lines.len(), 2);
        assert!(lines[0].contains(r#""source":"output""#));
        assert!(lines[1].contains(r#""source":"input""#));

        let _ = std::fs::remove_file(&tmp);
    }

    #[test]
    fn rotation_truncates_when_file_grows_past_max_bytes() {
        let tmp = tempfile_path("fanin_xrun_log_rotation");
        let _ = std::fs::remove_file(&tmp);

        let mut log = XrunLog::new(&tmp).expect("open xrun log");

        // Each event line is ~120 bytes. To exceed MAX_FILE_BYTES
        // (10 KB) we need ~100 entries. Write 150 to ensure we
        // trip rotation at least once.
        for i in 0..150u64 {
            log.record(&XrunEvent {
                source: XrunSource::Input,
                label: format!("renderer_{}", i),
                frames: 100,
                count: i,
            });
        }

        // After rotation the cached_size should be well below the
        // pre-rotation peak.
        let final_size = std::fs::metadata(&tmp).unwrap().len();
        assert!(
            final_size <= MAX_FILE_BYTES,
            "post-rotation size {} should be <= MAX_FILE_BYTES {}",
            final_size,
            MAX_FILE_BYTES,
        );
        // And we should have retained roughly RETAIN_BYTES_ON_ROTATE
        // worth of recent events.
        let mut content = String::new();
        File::open(&tmp)
            .unwrap()
            .read_to_string(&mut content)
            .unwrap();
        // The first line in the retained tail should be a complete
        // JSON object (not a partial mid-line truncation).
        let first_line = content.lines().next().expect("at least one line");
        assert!(
            first_line.starts_with('{') && first_line.ends_with('}'),
            "rotation left a partial first line: {:?}",
            first_line,
        );

        let _ = std::fs::remove_file(&tmp);
    }

    /// Returns a unique-ish path under /tmp for a test.
    fn tempfile_path(stem: &str) -> PathBuf {
        let pid = std::process::id();
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .subsec_nanos();
        std::env::temp_dir().join(format!("{}_{}_{}.jsonl", stem, pid, nanos))
    }
}
