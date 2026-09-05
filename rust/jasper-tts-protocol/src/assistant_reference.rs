// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Small, versioned persistence record for the last achieved assistant level.
//!
//! One record shape for both playout owners — fan-in's pre-DSP mix on a solo
//! speaker, outputd's post-DSP mix on a bonded member — so the learned
//! quiet-room reference survives restarts identically in either role. Each
//! daemon resolves its OWN path, so a box that flips between solo and bonded
//! never cross-contaminates the two engines' learned values.
//!
//! Disk I/O runs on a dedicated thread so the audio loop only performs a
//! non-blocking channel send.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Sender};
use std::thread::{self, JoinHandle};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::loudness::{HeldLoudnessReference, MAX_SAFE_ASSISTANT_CALIBRATION_LU};

const RECORD_VERSION: u8 = 2;
const MATERIAL_CHANGE_DB: f32 = 0.1;

/// What the hosting daemon supplies: the `event=` / thread-name prefix, the
/// writer thread's stack budget, and the two emit paths — fan-in routes them
/// through the `log` crate, outputd writes stderr directly.
#[derive(Clone, Copy)]
pub struct DaemonHooks {
    pub event_prefix: &'static str,
    pub writer_stack_bytes: usize,
    pub info: fn(&str),
    pub warn: fn(&str),
}

#[derive(Debug, Serialize, Deserialize)]
struct PersistedAssistantReference {
    version: u8,
    achieved_speaker_lufs: f32,
    canonical_db: f32,
    calibration_offset_lu: f32,
    updated_at_unix: u64,
}

pub fn load(path: &Path, hooks: DaemonHooks) -> Option<HeldLoudnessReference> {
    let prefix = hooks.event_prefix;
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return None,
        Err(error) => {
            (hooks.warn)(&format!(
                "event={prefix}.assistant_reference.load_failed path={} detail={error}",
                path.display(),
            ));
            return None;
        }
    };
    let record: PersistedAssistantReference = match serde_json::from_slice(&bytes) {
        Ok(record) => record,
        Err(error) => {
            (hooks.warn)(&format!(
                "event={prefix}.assistant_reference.load_failed path={} reason=invalid_json detail={error}",
                path.display(),
            ));
            return None;
        }
    };
    if record.version != RECORD_VERSION
        || !valid_db(record.achieved_speaker_lufs)
        || !valid_db(record.canonical_db)
        || !valid_calibration_offset(record.calibration_offset_lu)
    {
        (hooks.warn)(&format!(
            "event={prefix}.assistant_reference.load_failed path={} reason=invalid_record version={}",
            path.display(),
            record.version,
        ));
        return None;
    }
    (hooks.info)(&format!(
        "event={prefix}.assistant_reference.loaded path={} speaker_lufs={:.1} canonical_db={:.1}",
        path.display(),
        record.achieved_speaker_lufs,
        record.canonical_db,
    ));
    Some(HeldLoudnessReference {
        speaker_lufs: record.achieved_speaker_lufs,
        canonical_db: record.canonical_db,
        calibration_offset_lu: record.calibration_offset_lu,
    })
}

pub fn spawn_writer(
    path: PathBuf,
    initial: Option<HeldLoudnessReference>,
    hooks: DaemonHooks,
) -> std::io::Result<(Sender<HeldLoudnessReference>, JoinHandle<()>)> {
    let prefix = hooks.event_prefix;
    let (tx, rx) = mpsc::channel::<HeldLoudnessReference>();
    let handle = thread::Builder::new()
        .name(format!("{prefix}-assistant-reference-writer"))
        .stack_size(hooks.writer_stack_bytes)
        .spawn(move || {
            let mut last_written = initial;
            while let Ok(reference) = rx.recv() {
                if last_written.is_some_and(|previous| !materially_changed(previous, reference)) {
                    continue;
                }
                match write_atomic(&path, reference) {
                    Ok(()) => {
                        last_written = Some(reference);
                        (hooks.info)(&format!(
                            "event={prefix}.assistant_reference.persisted path={} speaker_lufs={:.1} canonical_db={:.1}",
                            path.display(),
                            reference.speaker_lufs,
                            reference.canonical_db,
                        ));
                    }
                    Err(error) => (hooks.warn)(&format!(
                        "event={prefix}.assistant_reference.persist_failed path={} detail={error}",
                        path.display(),
                    )),
                }
            }
        })?;
    Ok((tx, handle))
}

fn materially_changed(a: HeldLoudnessReference, b: HeldLoudnessReference) -> bool {
    (a.speaker_lufs - b.speaker_lufs).abs() >= MATERIAL_CHANGE_DB
        || (a.canonical_db - b.canonical_db).abs() >= MATERIAL_CHANGE_DB
        || (a.calibration_offset_lu - b.calibration_offset_lu).abs() >= MATERIAL_CHANGE_DB
}

fn valid_db(value: f32) -> bool {
    value.is_finite() && (-120.0..=24.0).contains(&value)
}

fn valid_calibration_offset(value: f32) -> bool {
    value.is_finite() && value.abs() <= MAX_SAFE_ASSISTANT_CALIBRATION_LU
}

fn write_atomic(path: &Path, reference: HeldLoudnessReference) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let record = PersistedAssistantReference {
        version: RECORD_VERSION,
        achieved_speaker_lufs: reference.speaker_lufs,
        canonical_db: reference.canonical_db,
        calibration_offset_lu: reference.calibration_offset_lu,
        updated_at_unix: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs(),
    };
    let bytes = serde_json::to_vec_pretty(&record)?;
    let temp = path.with_extension("tmp");
    fs::write(&temp, bytes)?;
    fs::rename(temp, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    const HOOKS: DaemonHooks = DaemonHooks {
        event_prefix: "test",
        writer_stack_bytes: 512 * 1024,
        info: |_| {},
        warn: |_| {},
    };

    #[test]
    fn record_round_trips_and_invalid_json_fails_soft() {
        let dir = std::env::temp_dir().join(format!(
            "jts-assistant-reference-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let path = dir.join("reference.json");
        let reference = HeldLoudnessReference {
            speaker_lufs: -39.5,
            canonical_db: -30.0,
            calibration_offset_lu: 0.5,
        };
        write_atomic(&path, reference).unwrap();
        assert_eq!(load(&path, HOOKS), Some(reference));
        fs::write(&path, b"not-json").unwrap();
        assert_eq!(load(&path, HOOKS), None);

        for invalid in [
            r#"{"version":1,"achieved_speaker_lufs":-39.5,"canonical_db":-30.0,"calibration_offset_lu":0.0,"updated_at_unix":1}"#,
            r#"{"version":2,"achieved_speaker_lufs":-121.0,"canonical_db":-30.0,"calibration_offset_lu":0.0,"updated_at_unix":1}"#,
            r#"{"version":2,"achieved_speaker_lufs":-39.5,"canonical_db":-30.0,"calibration_offset_lu":24.1,"updated_at_unix":1}"#,
            r#"{"version":2,"achieved_speaker_lufs":null,"canonical_db":-30.0,"calibration_offset_lu":0.0,"updated_at_unix":1}"#,
        ] {
            fs::write(&path, invalid).unwrap();
            assert_eq!(
                load(&path, HOOKS),
                None,
                "record should fail closed: {invalid}"
            );
        }
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn material_change_includes_bounded_calibration_offset() {
        let base = HeldLoudnessReference {
            speaker_lufs: -39.5,
            canonical_db: -30.0,
            calibration_offset_lu: 0.5,
        };
        assert!(!materially_changed(
            base,
            HeldLoudnessReference {
                calibration_offset_lu: 0.55,
                ..base
            }
        ));
        assert!(materially_changed(
            base,
            HeldLoudnessReference {
                calibration_offset_lu: 0.7,
                ..base
            }
        ));
    }
}
