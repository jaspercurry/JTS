// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Shared, policy-light environment parsing for JTS Rust daemons.
//!
//! This crate owns the parsing shapes both daemons share: string reads default
//! only when unset and otherwise preserve the configured value; scalar parsers
//! default when unset or blank, numeric parse failures name the key and raw
//! value, and floating-point values must be finite.
//!
//! The daemons disagree about a configured `0` on a value they later divide by,
//! so that policy is not one function with a hidden branch — it is two named
//! ones, [`env_u32_positive_or_default`] and [`env_u32_positive_or_bail`], and
//! the call site says which it takes. Vocabulary that is genuinely one daemon's
//! (fan-in's `enabled`-only feature gate, outputd's boolean accept-set, the
//! list and optional-string shapes) stays with that daemon.

use std::str::FromStr;

use anyhow::{Context, Result};

/// Read a string verbatim, using `default` only when the variable is unset.
pub fn env_str(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

/// Parse a scalar value, treating an unset or blank variable as `default`.
///
/// `expected` is the user-facing type phrase included in parse diagnostics,
/// such as `"a non-negative integer"` or `"a number"`.
pub fn env_parse<T>(name: &str, default: T, expected: &str) -> Result<T>
where
    T: FromStr,
    T::Err: std::error::Error + Send + Sync + 'static,
{
    match std::env::var(name) {
        Ok(raw) if !raw.trim().is_empty() => raw
            .trim()
            .parse::<T>()
            .with_context(|| format!("{name} must be {expected}; got {raw:?}")),
        _ => Ok(default),
    }
}

/// Parse a `u32`, treating an unset or blank variable as `default`.
///
/// A configured `0` parses. Callers that divide by the value take one of the
/// two positive-dimension helpers below instead.
pub fn env_u32(name: &str, default: u32) -> Result<u32> {
    env_parse(name, default, "a non-negative integer")
}

/// Parse a `u64`, treating an unset or blank variable as `default`.
pub fn env_u64(name: &str, default: u64) -> Result<u64> {
    env_parse(name, default, "a non-negative integer")
}

/// Parse an `i64`, treating an unset or blank variable as `default`.
pub fn env_i64(name: &str, default: i64) -> Result<i64> {
    env_parse(name, default, "an integer")
}

/// Read `name`, falling back to `fallback_name` when `name` is unset or blank,
/// and to `default` when neither is set. The `u32` sibling of
/// [`env_f32_fallback`], for a key that was renamed and kept its old spelling.
pub fn env_u32_fallback(name: &str, fallback_name: &str, default: u32) -> Result<u32> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => env_u32(name, default),
        _ => env_u32(fallback_name, default),
    }
}

/// A strictly-positive dimension whose configured `0` falls back to `default`
/// with a WARN — fan-in's `sample_rate` and `period_frames`.
///
/// A parsed `0` is a legal `u32` yet a nonsensical dimension: fan-in's
/// per-period math divides by both, unguarded, and release builds compile out
/// the `debug_assert!`s. With `panic = "abort"` and the unit's
/// `Restart=on-failure`, a divide-by-zero panic is an endless crash-restart
/// loop that takes all audio down, the audible-cue path with it. Bailing would
/// be its own config-parse restart loop, so the speaker keeps playing on the
/// documented default instead. A non-numeric or negative value still fails
/// loud; only a valid-but-zero dimension is recovered.
///
/// Unifying both daemons on [`env_u32_positive_or_bail`] is the owner's ruling;
/// it waits on the #5267 escalation fix, so that a config fault cannot count
/// toward the reboot escalation first. Delete this arm when that lands.
pub fn env_u32_positive_or_default(name: &str, default: u32) -> Result<u32> {
    let parsed = env_u32(name, default)?;
    if parsed == 0 {
        log::warn!(
            "event=env.config_ignored key={name} value=0 reason=dimension_must_be_positive default={default}"
        );
        return Ok(default);
    }
    Ok(parsed)
}

/// A strictly-positive dimension whose configured `0` fails the parse —
/// outputd's rates and frame counts, which the caller classes as EX_CONFIG.
pub fn env_u32_positive_or_bail(name: &str, default: u32) -> Result<u32> {
    let parsed = env_parse(name, default, "a positive integer")?;
    if parsed == 0 {
        anyhow::bail!("{} must be > 0", name);
    }
    Ok(parsed)
}

/// Parse a finite `f32`, treating an unset or blank variable as `default`.
pub fn env_f32(name: &str, default: f32) -> Result<f32> {
    match std::env::var(name) {
        Ok(raw) if !raw.trim().is_empty() => parse_f32(name, &raw),
        _ => Ok(default),
    }
}

pub fn env_f32_fallback(name: &str, fallback_name: &str, default: f32) -> Result<f32> {
    match std::env::var(name) {
        Ok(s) if !s.trim().is_empty() => parse_f32(name, &s),
        _ => env_f32(fallback_name, default),
    }
}

/// Parse and validate one finite `f32` value already obtained from an env var.
pub fn parse_f32(name: &str, raw: &str) -> Result<f32> {
    let parsed = raw
        .trim()
        .parse::<f32>()
        .with_context(|| format!("{name} must be a number; got {raw:?}"))?;
    if !parsed.is_finite() {
        anyhow::bail!("{name} must be finite");
    }
    Ok(parsed)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    /// Set several variables for one closure. `ENV_LOCK` is a plain (non-
    /// reentrant) mutex, so this is the only place that takes it: nesting two
    /// fixtures on one thread would deadlock.
    fn with_envs<T>(vars: &[(&str, Option<&str>)], f: impl FnOnce() -> T) -> T {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let restore: Vec<_> = vars
            .iter()
            .map(|(name, value)| {
                let previous = std::env::var_os(name);
                match value {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
                (*name, previous)
            })
            .collect();
        let result = f();
        for (name, previous) in restore {
            match previous {
                Some(value) => std::env::set_var(name, value),
                None => std::env::remove_var(name),
            }
        }
        result
    }

    fn with_env<T>(name: &str, value: Option<&str>, f: impl FnOnce() -> T) -> T {
        with_envs(&[(name, value)], f)
    }

    #[test]
    fn string_values_default_only_when_unset() {
        with_env("JTS_ENVCRATE_TEST_STRING", None, || {
            assert_eq!(env_str("JTS_ENVCRATE_TEST_STRING", "fallback"), "fallback");
        });
        with_env("JTS_ENVCRATE_TEST_STRING", Some("  "), || {
            assert_eq!(env_str("JTS_ENVCRATE_TEST_STRING", "fallback"), "  ");
        });
    }

    #[test]
    fn scalar_unset_and_blank_values_use_defaults() {
        with_env("JTS_ENVCRATE_TEST_U32", None, || {
            assert_eq!(
                env_parse("JTS_ENVCRATE_TEST_U32", 17_u32, "an integer").unwrap(),
                17
            );
        });
        with_env("JTS_ENVCRATE_TEST_U32", Some("  "), || {
            assert_eq!(
                env_parse("JTS_ENVCRATE_TEST_U32", 17_u32, "an integer").unwrap(),
                17
            );
        });
    }

    #[test]
    fn parse_errors_name_the_key_and_raw_value() {
        with_env("JTS_ENVCRATE_TEST_U32", Some("oops"), || {
            let error = env_parse("JTS_ENVCRATE_TEST_U32", 17_u32, "an integer")
                .unwrap_err()
                .to_string();
            assert!(error.contains("JTS_ENVCRATE_TEST_U32"), "{error}");
            assert!(error.contains("oops"), "{error}");
        });
    }

    #[test]
    fn a_zero_dimension_falls_back_to_the_default_under_the_or_default_policy() {
        for raw in ["0", " 0 ", "00"] {
            with_env("JTS_ENVCRATE_TEST_DIM", Some(raw), || {
                assert_eq!(
                    env_u32_positive_or_default("JTS_ENVCRATE_TEST_DIM", 256).unwrap(),
                    256,
                    "raw={raw:?}"
                );
            });
        }
        for (raw, expected) in [(None, 256_u32), (Some("48000"), 48_000)] {
            with_env("JTS_ENVCRATE_TEST_DIM", raw, || {
                assert_eq!(
                    env_u32_positive_or_default("JTS_ENVCRATE_TEST_DIM", 256).unwrap(),
                    expected,
                    "raw={raw:?}"
                );
            });
        }
        with_env("JTS_ENVCRATE_TEST_DIM", Some("-1"), || {
            assert!(env_u32_positive_or_default("JTS_ENVCRATE_TEST_DIM", 256).is_err());
        });
    }

    #[test]
    fn a_zero_dimension_fails_the_parse_under_the_or_bail_policy() {
        for raw in ["0", " 0 ", "00"] {
            with_env("JTS_ENVCRATE_TEST_DIM", Some(raw), || {
                assert!(
                    env_u32_positive_or_bail("JTS_ENVCRATE_TEST_DIM", 256).is_err(),
                    "raw={raw:?}"
                );
            });
        }
        for (raw, expected) in [(None, 256_u32), (Some("48000"), 48_000)] {
            with_env("JTS_ENVCRATE_TEST_DIM", raw, || {
                assert_eq!(
                    env_u32_positive_or_bail("JTS_ENVCRATE_TEST_DIM", 256).unwrap(),
                    expected,
                    "raw={raw:?}"
                );
            });
        }
        with_env("JTS_ENVCRATE_TEST_DIM", Some("-1"), || {
            assert!(env_u32_positive_or_bail("JTS_ENVCRATE_TEST_DIM", 256).is_err());
        });
    }

    #[test]
    fn a_renamed_key_reads_its_fallback_spelling_only_while_unset_or_blank() {
        for (new_raw, old_raw, expected) in [
            (None, Some("11"), 11_u32),
            (Some("  "), Some("11"), 11),
            (Some("22"), Some("11"), 22),
            (None, None, 33),
        ] {
            with_envs(
                &[
                    ("JTS_ENVCRATE_TEST_NEW", new_raw),
                    ("JTS_ENVCRATE_TEST_OLD", old_raw),
                ],
                || {
                    assert_eq!(
                        env_u32_fallback("JTS_ENVCRATE_TEST_NEW", "JTS_ENVCRATE_TEST_OLD", 33)
                            .unwrap(),
                        expected,
                        "new_raw={new_raw:?} old_raw={old_raw:?}"
                    );
                },
            );
        }
    }

    #[test]
    fn f32_values_must_be_finite() {
        with_env("JTS_ENVCRATE_TEST_F32", Some("NaN"), || {
            assert_eq!(
                env_f32("JTS_ENVCRATE_TEST_F32", 1.0)
                    .unwrap_err()
                    .to_string(),
                "JTS_ENVCRATE_TEST_F32 must be finite"
            );
        });
    }
}
