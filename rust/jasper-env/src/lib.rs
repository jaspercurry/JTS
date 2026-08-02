// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Shared, policy-light environment parsing for JTS Rust daemons.
//!
//! This crate owns only behavior that is identical across consumers: unset or
//! blank values use the supplied default, numeric parse failures name the key
//! and raw value, and floating-point values must be finite. Domain policy stays
//! with the daemon (for example, outputd rejects a zero period while fan-in has
//! separate positive and non-negative dimensions).

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

/// Parse a finite `f32`, treating an unset or blank variable as `default`.
pub fn env_f32(name: &str, default: f32) -> Result<f32> {
    match std::env::var(name) {
        Ok(raw) if !raw.trim().is_empty() => parse_f32(name, &raw),
        _ => Ok(default),
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

    fn with_env<T>(name: &str, value: Option<&str>, f: impl FnOnce() -> T) -> T {
        let _guard = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let previous = std::env::var_os(name);
        match value {
            Some(value) => std::env::set_var(name, value),
            None => std::env::remove_var(name),
        }
        let result = f();
        match previous {
            Some(value) => std::env::set_var(name, value),
            None => std::env::remove_var(name),
        }
        result
    }

    #[test]
    fn unset_and_blank_values_use_defaults() {
        with_env("JASPER_ENV_TEST_U32", None, || {
            assert_eq!(
                env_parse("JASPER_ENV_TEST_U32", 17_u32, "an integer").unwrap(),
                17
            );
        });
        with_env("JASPER_ENV_TEST_U32", Some("  "), || {
            assert_eq!(
                env_parse("JASPER_ENV_TEST_U32", 17_u32, "an integer").unwrap(),
                17
            );
        });
    }

    #[test]
    fn parse_errors_name_the_key_and_raw_value() {
        with_env("JASPER_ENV_TEST_U32", Some("oops"), || {
            let error = env_parse("JASPER_ENV_TEST_U32", 17_u32, "an integer")
                .unwrap_err()
                .to_string();
            assert!(error.contains("JASPER_ENV_TEST_U32"), "{error}");
            assert!(error.contains("oops"), "{error}");
        });
    }

    #[test]
    fn f32_values_must_be_finite() {
        with_env("JASPER_ENV_TEST_F32", Some("NaN"), || {
            assert_eq!(
                env_f32("JASPER_ENV_TEST_F32", 1.0).unwrap_err().to_string(),
                "JASPER_ENV_TEST_F32 must be finite"
            );
        });
    }
}
