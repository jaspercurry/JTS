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

/// Open one member: the quoted `key` and its colon, ready for a value.
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

pub fn push_kv_f64(buf: &mut String, key: &str, value: f64, decimals: usize) {
    push_key(buf, key);
    buf.push_str(&format!("{:.*}", decimals, value));
}

pub fn push_kv_f64_opt(buf: &mut String, key: &str, value: Option<f64>, decimals: usize) {
    push_key(buf, key);
    match value {
        Some(value) => buf.push_str(&format!("{:.*}", decimals, value)),
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
        let parsed: serde_json::Value = serde_json::from_str(&format!("{{{buf}}}")).unwrap();
        assert!(parsed.is_object());
    }
}
