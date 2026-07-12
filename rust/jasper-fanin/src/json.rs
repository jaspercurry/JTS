// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! JSON primitives shared by fan-in's hand-built observability objects.

/// Serialize one string as a complete quoted JSON value.
///
/// The surrounding quotes are part of the return value. Keeping that contract
/// here prevents hand-built callers from escaping contents correctly but then
/// forgetting the quotes that make those contents a JSON string.
pub(crate) fn json_string(value: &str) -> String {
    serde_json::to_string(value).expect("serializing a string to JSON cannot fail")
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
}
