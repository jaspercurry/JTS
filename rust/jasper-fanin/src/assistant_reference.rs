// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Small, versioned persistence record for the last achieved assistant level.
//!
//! The mixer is the single owner of this value. Disk I/O runs on a dedicated
//! thread so the audio loop only performs a non-blocking channel send.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Sender};
use std::thread::{self, JoinHandle};
use std::time::{SystemTime, UNIX_EPOCH};

use log::{info, warn};
use serde::{Deserialize, Serialize};

use crate::loudness::HeldLoudnessReference;

const RECORD_VERSION: u8 = 1;
const MATERIAL_CHANGE_DB: f32 = 0.1;

#[derive(Debug, Serialize, Deserialize)]
struct PersistedAssistantReference {
    version: u8,
    achieved_speaker_lufs: f32,
    canonical_db: f32,
    updated_at_unix: u64,
}

pub fn load(path: &Path) -> Option<HeldLoudnessReference> {
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return None,
        Err(error) => {
            warn!(
                "event=fanin.assistant_reference.load_failed path={} detail={}",
                path.display(),
                error
            );
            return None;
        }
    };
    let record: PersistedAssistantReference = match serde_json::from_slice(&bytes) {
        Ok(record) => record,
        Err(error) => {
            warn!(
                "event=fanin.assistant_reference.load_failed path={} reason=invalid_json detail={}",
                path.display(),
                error
            );
            return None;
        }
    };
    if record.version != RECORD_VERSION
        || !valid_db(record.achieved_speaker_lufs)
        || !valid_db(record.canonical_db)
    {
        warn!(
            "event=fanin.assistant_reference.load_failed path={} reason=invalid_record version={}",
            path.display(),
            record.version
        );
        return None;
    }
    info!(
        "event=fanin.assistant_reference.loaded path={} speaker_lufs={:.1} canonical_db={:.1}",
        path.display(),
        record.achieved_speaker_lufs,
        record.canonical_db
    );
    Some(HeldLoudnessReference {
        speaker_lufs: record.achieved_speaker_lufs,
        canonical_db: record.canonical_db,
    })
}

pub fn spawn_writer(
    path: PathBuf,
    initial: Option<HeldLoudnessReference>,
) -> std::io::Result<(Sender<HeldLoudnessReference>, JoinHandle<()>)> {
    let (tx, rx) = mpsc::channel::<HeldLoudnessReference>();
    let handle = thread::Builder::new()
        .name("fanin-assistant-reference-writer".to_string())
        .spawn(move || {
            let mut last_written = initial;
            while let Ok(reference) = rx.recv() {
                if last_written.is_some_and(|previous| !materially_changed(previous, reference)) {
                    continue;
                }
                match write_atomic(&path, reference) {
                    Ok(()) => {
                        last_written = Some(reference);
                        info!(
                            "event=fanin.assistant_reference.persisted path={} speaker_lufs={:.1} canonical_db={:.1}",
                            path.display(),
                            reference.speaker_lufs,
                            reference.canonical_db
                        );
                    }
                    Err(error) => warn!(
                        "event=fanin.assistant_reference.persist_failed path={} detail={}",
                        path.display(),
                        error
                    ),
                }
            }
        })?;
    Ok((tx, handle))
}

fn materially_changed(a: HeldLoudnessReference, b: HeldLoudnessReference) -> bool {
    (a.speaker_lufs - b.speaker_lufs).abs() >= MATERIAL_CHANGE_DB
        || (a.canonical_db - b.canonical_db).abs() >= MATERIAL_CHANGE_DB
}

fn valid_db(value: f32) -> bool {
    value.is_finite() && (-120.0..=24.0).contains(&value)
}

fn write_atomic(path: &Path, reference: HeldLoudnessReference) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let record = PersistedAssistantReference {
        version: RECORD_VERSION,
        achieved_speaker_lufs: reference.speaker_lufs,
        canonical_db: reference.canonical_db,
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
        };
        write_atomic(&path, reference).unwrap();
        assert_eq!(load(&path), Some(reference));
        fs::write(&path, b"not-json").unwrap();
        assert_eq!(load(&path), None);
        let _ = fs::remove_dir_all(dir);
    }
}
