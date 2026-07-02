// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// format.js — value humanisation + status-tone helpers.
//
// Tones are "ok" | "warn" | "danger", matching the --status-* tokens in
// app.css. Threshold helpers live here so the dashboard's colour semantics
// are named, testable, and easy to compare with jasper-doctor.

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
// metrics flag at different levels.
export function toneForPercent(pct, warnAt, failAt) {
  if (pct >= failAt) return "danger";
  if (pct >= warnAt) return "warn";
  return "ok";
}

export function capacityPercent(totalPct, coreCount) {
  if (!coreCount) return 0;
  return totalPct / coreCount;
}

// Memory pressure uses MemAvailable, not "used", because Linux keeps caches
// in RAM on purpose. Use percentage-of-capacity as the scalable rule, with
// low-RAM absolute floors so tiny boards don't wait until single-digit MB:
// warn below max(100 MB, 10% total), danger below max(30 MB, 3% total).
//
// These MIRROR jasper-doctor's `memory_headroom_thresholds`
// (jasper/cli/doctor/memory.py) on purpose — the dashboard tile and the doctor
// must give the same verdict. tests/test_system_status_thresholds.py pins them
// equal, so changing one side without the other fails CI. (The drift between
// the two was the original bug: a fixed 150 MB cutoff here vs the doctor's
// percentage model, so a healthy small board showed RED on the dashboard only.)
export function memoryHeadroomLimits(totalMb) {
  const total = Math.max(0, Number(totalMb) || 0);
  return {
    warnMb: Math.max(100, Math.floor(total * 0.10)),
    dangerMb: Math.max(30, Math.floor(total * 0.03)),
  };
}

export function toneForMemoryHeadroom(availableMb, totalMb) {
  const available = Math.max(0, Number(availableMb) || 0);
  const limits = memoryHeadroomLimits(totalMb);
  if (available < limits.dangerMb) return "danger";
  if (available < limits.warnMb) return "warn";
  return "ok";
}

// /proc/loadavg counts jobs running/runnable or waiting in uninterruptible
// I/O. Above the core count means real queueing; 75% is a calmer "busy soon"
// warning point for the 1-minute average.
export function loadPressureInfo(load, coreCount) {
  const capacity = Math.max(1, Number(coreCount) || 1);
  const value = Math.max(0, Number(load) || 0);
  if (value > capacity) return { tone: "danger", label: "Queueing", capacity };
  if (value >= capacity * 0.75) return { tone: "warn", label: "Busy", capacity };
  return { tone: "ok", label: "Low demand", capacity };
}

export function cpuUsageInfo(cores) {
  const values = cores || [];
  if (!values.length) return { tone: "ok", value: "—" };
  const totalCpu = values.reduce((a, b) => a + b, 0);
  const avgCpu = capacityPercent(totalCpu, values.length);
  const maxCore = Math.max(...values);
  let tone = "ok";
  if (avgCpu >= 95 || maxCore >= 98) tone = "danger";
  else if (avgCpu >= 75 || maxCore >= 90) tone = "warn";
  return { tone, value: Math.round(avgCpu) + "%" };
}

function finiteNumberOrNull(value) {
  if (value == null) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

// Raspberry Pi firmware progressively throttles Arm cores from 80-85C, so
// warn before that band and go red when current throttling or 80C appears.
export function temperatureInfo(tempC, throttledNow, throttledHistory) {
  const temp = finiteNumberOrNull(tempC);
  let tone = "ok";
  if (temp == null) {
    tone = throttledNow ? "danger" : (throttledHistory ? "warn" : "idle");
  }
  else if (temp >= 80 || throttledNow) tone = "danger";
  else if (temp >= 75 || throttledHistory) tone = "warn";
  return { tone };
}

export function temperatureDisplay(tempC, throttledNow, throttledHistory) {
  const temp = finiteNumberOrNull(tempC);
  const tone = temperatureInfo(temp, throttledNow, throttledHistory).tone;
  if (temp == null) {
    let sub = "Thermal sensor unavailable";
    if (throttledNow) sub += " · throttling now";
    else if (throttledHistory) sub += " · throttled since boot";
    return { value: "Unavailable", sub, tone, chartable: false };
  }
  let sub = temp.toFixed(1) + "°C";
  if (throttledNow) sub += " · throttling now";
  else if (throttledHistory) sub += " · throttled since boot";
  return {
    value: (temp * 9 / 5 + 32).toFixed(0) + "°F",
    sub,
    tone,
    chartable: true,
  };
}

// 85% warn / 95% danger mirror jasper-doctor's `_DEFAULT_DISK_WARN_PERCENT` /
// `_DISK_FAIL_PERCENT` (jasper/cli/doctor/memory.py), pinned equal by
// tests/test_system_status_thresholds.py — same dashboard↔doctor agreement the
// memory tile keeps. A full root partition is the failure that turns a routine
// power-cut into ext4 corruption, so danger fires before writes start failing.
export function toneForDiskUse(pct) {
  return toneForPercent(Number(pct) || 0, 85, 95);
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
