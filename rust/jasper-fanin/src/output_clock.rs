// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Follow the reconciler's Snapcast source selection outside the audio thread.

use std::io::Read;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::Duration;

const ARGS_PATH: &str = "/run/jasper-grouping/snapcast-args.env";

fn source_is_nominal(text: &str) -> bool {
    text.lines().any(|line| {
        line.strip_prefix("JASPER_SNAPSERVER_ARGS=")
            .map(|value| !value.trim().trim_matches(['\'', '"']).is_empty())
            .unwrap_or(false)
    })
}

fn read_nominal_source() -> bool {
    let mut text = String::new();
    std::fs::File::open(ARGS_PATH)
        .and_then(|file| file.take(16 * 1024).read_to_string(&mut text))
        .map(|_| source_is_nominal(&text))
        .unwrap_or(false)
}

pub fn spawn(
    nominal: Arc<AtomicBool>,
    shutdown: Arc<AtomicBool>,
) -> std::io::Result<JoinHandle<()>> {
    nominal.store(read_nominal_source(), Ordering::Relaxed);
    std::thread::Builder::new()
        .name("fanin-output-clock".into())
        .stack_size(crate::HELPER_STACK_BYTES)
        .spawn(move || {
            while !shutdown.load(Ordering::Relaxed) {
                let next = read_nominal_source();
                if nominal.swap(next, Ordering::Relaxed) != next {
                    log::info!("event=fanin.output_clock.changed nominal={next}");
                }
                std::thread::sleep(Duration::from_millis(250));
            }
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_derived_server_source_selects_nominal_pacing() {
        for (text, expected) in [
            ("", false),
            ("JASPER_SNAPCLIENT_ARGS='--host localhost'\n", false),
            ("JASPER_SNAPSERVER_ARGS=\n", false),
            ("JASPER_SNAPSERVER_ARGS=''\n", false),
            ("JASPER_SNAPSERVER_ARGS=\"\"\n", false),
            (
                "JASPER_SNAPSERVER_ARGS='--stream.source pipe:///run/audio'\n",
                true,
            ),
        ] {
            assert_eq!(source_is_nominal(text), expected);
        }
    }
}
