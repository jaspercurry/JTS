// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// format.js — a value and the string the page shows for it. No page state,
// no network.
import { escapeHtml as escapeText } from "/assets/shared/js/escape.js";

// Gauge fix (2026-07-24): "0deg"/"90deg" -> a household-legible degree
// symbol; "unknown" (or anything else unrecognized) renders nothing
// rather than a confusing "unknown orientation" — the honest common case
// for a manual upload with no declared orientation.
export function orientationLabel(orientation) {
  if (orientation === '0deg') return '0°';
  if (orientation === '90deg') return '90°';
  return null;
}

export function formatAppliedAt(epoch) {
  if (!epoch) return '';
  var d = new Date(epoch * 1000);
  if (isNaN(d.getTime())) return '';
  var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  var pad = function (n) { return n < 10 ? '0' + n : String(n); };
  return days[d.getDay()] + ' ' + d.getFullYear() + '-' +
    pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
    pad(d.getHours()) + ':' + pad(d.getMinutes());
}

function numberOrNull(value) {
  var n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function formatMaybeDb(value) {
  var n = numberOrNull(value);
  return n === null ? '—' : n.toFixed(1) + ' dB';
}

export function formatBytes(bytes) {
  var n = Number(bytes || 0);
  if (!isFinite(n) || n <= 0) return '0 B';
  var units = ['B', 'KB', 'MB', 'GB'];
  var idx = 0;
  while (n >= 1024 && idx < units.length - 1) {
    n = n / 1024;
    idx += 1;
  }
  return (idx === 0 ? String(Math.round(n)) : n.toFixed(1)) + ' ' + units[idx];
}

export function reportIssueList(items, fallback) {
  items = (items || []).filter(function (item) { return !!item; }).slice(0, 8);
  if (!items.length) return '<p class="hint">' + escapeText(fallback) + '</p>';
  return '<ul>' + items.map(function (item) {
    return '<li>' + escapeText(
      item.message || item.reason || item.code || item.kind || String(item)
    ) + '</li>';
  }).join('') + '</ul>';
}

export function describeFilters(peqs) {
  return peqs.map(function (f) {
    var g = Number(f.gain_db);
    var sign = g >= 0 ? '+' : '';
    return Math.round(Number(f.freq_hz)) + ' Hz, Q ' + Number(f.q).toFixed(1)
      + ', ' + sign + g.toFixed(1) + ' dB';
  }).join('  •  ');
}
