// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// format.js — value humanisation + status-tone helpers.
//
// Ported verbatim from the previous inline /system/ script: same
// formatting and the same warn/danger thresholds, so the rebuilt page
// reads identically to the one it replaces. Tones are "ok" | "warn" |
// "danger", matching the --status-* tokens in app.css.

export function fmtBytes(n) {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
  return v >= 100 ? v.toFixed(0) + " " + u[i] : v.toFixed(1) + " " + u[i];
}

function relAgo(sec) {
  sec = Math.max(0, sec);
  if (sec < 60) return sec + "s ago";
  if (sec < 3600) return Math.round(sec / 60) + "m ago";
  if (sec < 86400) return Math.round(sec / 3600) + "h ago";
  return Math.round(sec / 86400) + "d ago";
}

export function fmtAgo(iso) {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (isNaN(t)) return "—";
  return relAgo(Math.round((Date.now() - t) / 1000));
}

export function fmtEpochAgo(epochSec) {
  if (epochSec == null) return "—";
  return relAgo(Math.round(Date.now() / 1000 - Number(epochSec)));
}

export function fmtDur(sec) {
  if (sec == null) return "—";
  const s = Math.floor(sec);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const parts = [];
  if (d) parts.push(d + "d");
  if (h || d) parts.push(h + "h");
  parts.push(m + "m");
  return parts.join(" ");
}

export function fmtMsAge(ms) {
  if (ms == null) return "never";
  const sec = Math.max(0, Number(ms) / 1000);
  if (sec < 1) return Math.round(Number(ms)) + "ms ago";
  if (sec < 60) return sec.toFixed(sec < 10 ? 1 : 0) + "s ago";
  if (sec < 3600) return Math.round(sec / 60) + "m ago";
  return Math.round(sec / 3600) + "h ago";
}

export function fmtRatePerHour(value) {
  if (value == null) return "0/h";
  const n = Number(value);
  if (!isFinite(n) || n <= 0) return "0/h";
  if (n < 1) return n.toFixed(2) + "/h";
  if (n < 10) return n.toFixed(1) + "/h";
  return Math.round(n) + "/h";
}

export function baseName(path) {
  if (!path) return "";
  return String(path).split("/").filter(Boolean).pop() || String(path);
}

export function fmtUSD(n) {
  if (n == null) return "—";
  return "$" + Number(n).toFixed(2);
}

// --- status tones ---------------------------------------------------------

// Derive a tone from a percentage, given the warn + danger break points.
// Kept as explicit thresholds (not a single table) because different
// metrics flag at different levels — the previous page's values are
// preserved at each call site.
export function toneForPercent(pct, warnAt, failAt) {
  if (pct >= failAt) return "danger";
  if (pct >= warnAt) return "warn";
  return "ok";
}

export function capacityPercent(totalPct, coreCount) {
  if (!coreCount) return 0;
  return totalPct / coreCount;
}

// Pi 5 pwm-fan cooling levels. The fan card stays intentionally terse;
// the temperature tile is the alarm surface for thermal pressure.
export const FAN_STEPS = [
  { pwm: 0,   label: "Off",    range: "below 50°C" },
  { pwm: 75,  label: "Low",    range: "50–60°C" },
  { pwm: 125, label: "Medium", range: "60–67.5°C" },
  { pwm: 175, label: "High",   range: "67.5–75°C" },
  { pwm: 250, label: "Max",    range: "above 75°C" },
];

// Snap a raw PWM value to the closest known step — tolerates kernel/DTB
// drift; in practice the read is exact.
export function fanStepInfo(pwm) {
  let best = 0;
  let bestDiff = Math.abs(pwm - FAN_STEPS[0].pwm);
  for (let i = 1; i < FAN_STEPS.length; i++) {
    const d = Math.abs(pwm - FAN_STEPS[i].pwm);
    if (d < bestDiff) { best = i; bestDiff = d; }
  }
  return { ...FAN_STEPS[best], index: best };
}
