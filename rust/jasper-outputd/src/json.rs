// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! JSON string serialization shared by outputd's hand-built wire payloads.

/// Serialize one string as a complete quoted JSON value.
pub(crate) fn json_string(value: &str) -> String {
    // PANIC-AUDITED: serializing a &str into an in-memory Vec has no failing branch
    serde_json::to_string(value).expect("serializing a string cannot fail")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hostile_and_control_strings_round_trip_exactly() {
        for value in [
            "plain",
            "quote\"backslash\\",
            "line\nreturn\rtab\tbackspace\u{0008}formfeed\u{000c}",
            "nul\0unit\u{001f}",
            "ordinary café 日本語",
        ] {
            let encoded = json_string(value);
            let decoded: String = serde_json::from_str(&encoded).unwrap();
            assert_eq!(decoded, value, "encoded={encoded:?}");
        }
    }
}
