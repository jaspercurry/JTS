// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! JSON primitives for the daemons' hand-built observability objects: the
//! fixed outer shapes stay allocation-conscious, while string quoting — the
//! one correctness-sensitive boundary — goes through `serde_json`.

/// Serialize one string as a complete quoted JSON value.
///
/// The surrounding quotes are part of the return value. Keeping that contract
/// here prevents hand-built callers from escaping contents correctly but then
/// forgetting the quotes that make those contents a JSON string.
pub fn json_string(value: &str) -> String {
    // PANIC-AUDITED: serializing a &str into an in-memory Vec has no failing branch
    serde_json::to_string(value).expect("serializing a string to JSON cannot fail")
}

/// Stamp of an event that has not happened yet, for the `*_age_ms` recency
/// fields beside cumulative counters: no millisecond count reaches it.
pub const NEVER_MS: u64 = u64::MAX;

/// Milliseconds since `event_ms`, `None` (STATUS `null`) while the event has
/// never fired.
pub fn event_age_ms(now_ms: u64, event_ms: u64) -> Option<u64> {
    if event_ms == NEVER_MS {
        None
    } else {
        Some(now_ms.saturating_sub(event_ms))
    }
}

pub fn push_key(buf: &mut String, key: &str) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":"#);
}

pub fn push_kv_str(buf: &mut String, key: &str, value: &str) {
    push_key(buf, key);
    buf.push_str(&json_string(value));
}

pub fn push_kv_str_opt(buf: &mut String, key: &str, value: Option<&str>) {
    push_key(buf, key);
    match value {
        Some(value) => buf.push_str(&json_string(value)),
        None => buf.push_str("null"),
    }
}

pub fn push_kv_u64(buf: &mut String, key: &str, value: u64) {
    push_key(buf, key);
    buf.push_str(&value.to_string());
}

pub fn push_kv_u64_opt(buf: &mut String, key: &str, value: Option<u64>) {
    push_key(buf, key);
    match value {
        Some(value) => buf.push_str(&value.to_string()),
        None => buf.push_str("null"),
    }
}

pub fn push_kv_i64(buf: &mut String, key: &str, value: i64) {
    push_key(buf, key);
    buf.push_str(&value.to_string());
}

pub fn push_kv_i64_opt(buf: &mut String, key: &str, value: Option<i64>) {
    push_key(buf, key);
    match value {
        Some(value) => buf.push_str(&value.to_string()),
        None => buf.push_str("null"),
    }
}

pub fn push_kv_bool(buf: &mut String, key: &str, value: bool) {
    push_key(buf, key);
    buf.push_str(if value { "true" } else { "false" });
}

/// Render one float, substituting `null` for a non-finite value.
///
/// Serialization-boundary guarantee for every number these writers emit:
/// **a finite JSON number, or `null` — never a non-finite token.**
///
/// Rust formats `NaN`/`inf`/`-inf` verbatim and none of those is JSON: `inf`
/// makes a strict reader reject the whole STATUS document, and `NaN` is worse
/// — Python's `json` accepts it as a non-standard extension, so it arrives as
/// a float that passes `isinstance(v, float)` while every `abs(a - b) > tol`
/// comparison against it is False, i.e. a silently-passing contract check.
///
/// `null` rather than omission because the key sets here are pinned wire
/// contracts asserted present by the daemons' state tests. The substitution is
/// silent by design: these are polled renders, so a value nobody checks costs a
/// null in one reply, not a journal line per poll. The one doctor-guarded field
/// (`final_gain_db`) is WARNed on downstream by
/// `jasper/cli/doctor/audio_runtime_fanin.py`.
///
/// Filtering happens at this shared writer rather than at each producer because
/// outputd copies engine floats straight into its snapshot structs, while
/// fan-in's producers already clamp or pack to a sentinel; one writer makes the
/// guarantee hold for both from a single place.
fn push_f64_finite_or_null(buf: &mut String, value: f64, decimals: usize) {
    if value.is_finite() {
        buf.push_str(&format!("{:.*}", decimals, value));
    } else {
        buf.push_str("null");
    }
}

pub fn push_kv_f64(buf: &mut String, key: &str, value: f64, decimals: usize) {
    push_key(buf, key);
    push_f64_finite_or_null(buf, value, decimals);
}

pub fn push_kv_f64_opt(buf: &mut String, key: &str, value: Option<f64>, decimals: usize) {
    push_key(buf, key);
    match value {
        Some(value) => push_f64_finite_or_null(buf, value, decimals),
        None => buf.push_str("null"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_string_serialization_covers_specials_controls_and_unicode() {
        assert_eq!(json_string("plain"), r#""plain""#);
        assert_eq!(json_string("a\"b"), r#""a\"b""#);
        assert_eq!(json_string("a\\b"), r#""a\\b""#);
        assert_eq!(json_string("a\nb"), r#""a\nb""#);
        assert_eq!(json_string("a\u{0008}b"), r#""a\bb""#);
        assert_eq!(json_string("a\u{000c}b"), r#""a\fb""#);
        assert_eq!(json_string("a\u{0001}b"), r#""a\u0001b""#);
        assert_eq!(json_string("a\u{007f}\u{0085}b"), "\"a\u{007f}\u{0085}b\"",);
        assert_eq!(json_string("café"), r#""café""#);
    }

    #[test]
    fn hostile_and_control_strings_round_trip_exactly() {
        for value in [
            "plain",
            "quote\"backslash\\",
            "line\nreturn\rtab\tbackspace\u{0008}formfeed\u{000c}",
            "nul\0unit\u{001f}",
            "delete\u{007f}next-line\u{0085}",
            "ordinary café 日本語",
        ] {
            let encoded = json_string(value);
            let decoded: String = serde_json::from_str(&encoded).unwrap();
            assert_eq!(decoded, value, "encoded={encoded:?}");
        }
    }

    #[test]
    fn scalar_members_render_with_their_key_and_null_for_absent_values() {
        let mut buf = String::new();
        push_kv_str(&mut buf, "label", "a\"b");
        buf.push(',');
        push_kv_str_opt(&mut buf, "pcm", None);
        buf.push(',');
        push_kv_u64(&mut buf, "frames", 7);
        buf.push(',');
        push_kv_u64_opt(&mut buf, "delay", None);
        buf.push(',');
        push_kv_i64(&mut buf, "offset", -3);
        buf.push(',');
        push_kv_i64_opt(&mut buf, "skew", Some(-3));
        buf.push(',');
        push_kv_bool(&mut buf, "locked", true);
        buf.push(',');
        push_kv_f64(&mut buf, "gain_db", -1.5, 2);
        buf.push(',');
        push_kv_f64_opt(&mut buf, "trim_db", None, 2);

        assert_eq!(
            buf,
            concat!(
                r#""label":"a\"b","pcm":null,"frames":7,"delay":null,"#,
                r#""offset":-3,"skew":-3,"locked":true,"gain_db":-1.50,"#,
                r#""trim_db":null"#,
            )
        );
    }

    #[test]
    fn non_finite_floats_render_as_null_and_finite_ones_keep_their_decimals() {
        for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let mut buf = String::new();
            push_kv_f64(&mut buf, "gain_db", value, 2);
            assert_eq!(buf, r#""gain_db":null"#, "value={value}");

            let mut buf = String::new();
            push_kv_f64_opt(&mut buf, "gain_db", Some(value), 2);
            assert_eq!(buf, r#""gain_db":null"#, "value={value}");
        }

        let mut buf = String::new();
        push_kv_f64(&mut buf, "gain_db", -1.5, 2);
        buf.push(',');
        push_kv_f64_opt(&mut buf, "trim_db", Some(0.0), 3);
        assert_eq!(buf, r#""gain_db":-1.50,"trim_db":0.000"#);
    }
}
