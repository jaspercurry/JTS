// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Theme TOKEN -> fixed CSS value maps for the capture page (build step 3).
//
// The Pi sends only an allowlisted *token* (e.g. accent="sage"); THIS page owns
// the actual CSS value the token maps to. The relay/Pi can never deliver a raw
// CSS string. A token outside the map falls back to the default — never to an
// attacker-controlled value. The token vocabulary mirrors the Pi-side allowlist
// (jasper/capture_relay/spec.py: THEME_ACCENTS / THEME_FONTS); the two must stay
// in lockstep, but each side enforces its own copy (the spec crosses an
// untrusted relay, so the page does not trust it).

export const THEME_ACCENT_VARS = Object.freeze({
  sage: "oklch(0.72 0.045 150)",
  beige: "oklch(0.85 0.03 85)",
  clay: "oklch(0.66 0.085 45)",
});

export const THEME_FONT_VARS = Object.freeze({
  figtree: "'Figtree', system-ui, -apple-system, sans-serif",
  outfit: "'Outfit', system-ui, -apple-system, sans-serif",
});

export const DEFAULT_THEME = Object.freeze({ accent: "sage", font: "figtree" });

// Resolve a theme spec to safe CSS values. Unknown/absent tokens -> defaults.
export function resolveTheme(theme) {
  const t = theme && typeof theme === "object" ? theme : {};
  const accent = Object.prototype.hasOwnProperty.call(THEME_ACCENT_VARS, t.accent)
    ? t.accent
    : DEFAULT_THEME.accent;
  const font = Object.prototype.hasOwnProperty.call(THEME_FONT_VARS, t.font)
    ? t.font
    : DEFAULT_THEME.font;
  return {
    accent,
    font,
    accentVar: THEME_ACCENT_VARS[accent],
    fontVar: THEME_FONT_VARS[font],
  };
}
