"""System dashboard at /system/.

Read-only(ish) view of what the speaker is doing — RAM/CPU/temp/disk
with 60-min sparklines, software version, cloud activity (per-provider
sessions/tokens/cost), network + renderer state, and a few action
buttons (restart voice / audio / reboot, run diagnostics).

Data comes from jasper-control:
  GET  /system/snapshot     metrics + cloud + build (5 s ring buffer)
  GET  /system/diagnostics  runs jasper-doctor --json (~3-5 s)
  POST /system/restart/*    restart voice / audio chain
  POST /system/reboot       full Pi reboot

Wake detection lives on /wake/ — the model picker, the AEC + per-leg
toggles, and the sensitivity slider all share that page now since they
share a restart cycle. /system/ no longer carries an AEC card.

This wizard's job is to render HTML and proxy the JSON. Polling is
client-side (fetch /data.json every 5 s); the server keeps a thin
proxy connection to jasper-control on 127.0.0.1:8780.

Socket-activated like the other wizards, with a longer idle window
(30 min) since a power user may leave the dashboard open in a tab
for monitoring. Idle exit + cold-start still apply.
"""
from __future__ import annotations

import argparse
import logging
import os
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ._common import (
    DEFAULT_CONTROL_BASE,
    begin_request,
    csrf_fetch_helpers_js,
    csrf_meta_html,
    proxy_get,
    proxy_post,
    reject_csrf,
    send_html_response,
    send_proxy_json,
    verify_csrf,
    wrap_page,
)

logger = logging.getLogger(__name__)


# Longer than the other wizards' 10-min default. The dashboard is a
# monitoring surface; some users will leave it open in a tab. 30 min
# strikes a balance between not respawning constantly + not lingering
# resident forever.
IDLE_SHUTDOWN_SEC = 1800.0


# ---------- HTML / JS / CSS -----------------------------------------------

_EXTRA_STYLE = """
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.75em; margin: 1em 0; }
.tile { background: #fafafa; border: 1px solid #e6e6e6; border-radius: 6px; padding: 0.7em; }
.tile.hidden { display: none; }
.tile .label { font-size: 0.78em; color: #666; text-transform: uppercase; letter-spacing: 0.04em; }
.tile .value { font-size: 1.4em; font-weight: 600; color: #222; margin: 0.15em 0 0.2em; }
.tile .sub { font-size: 0.78em; color: #888; }
.tile .sub:empty { display: none; }
.tile.warn { background: #fff7e6; border-color: #f0c060; }
.tile.fail { background: #ffeaea; border-color: #e08080; }
.tile svg { width: 100%; height: 32px; display: block; margin-top: 0.4em; }
.tile svg .area { fill: #1db95433; }
.tile svg .line { stroke: #1db954; stroke-width: 1.5; fill: none; }
.tile.warn svg .area { fill: #f0c06033; }
.tile.warn svg .line { stroke: #f0c060; }
.tile.fail svg .area { fill: #e0808033; }
.tile.fail svg .line { stroke: #e08080; }

.card { background: #fafafa; border: 1px solid #e6e6e6; border-radius: 6px; padding: 0.7em 1em; margin: 1em 0; }
.card h2 { font-size: 0.92em; margin: 0 0 0.5em; text-transform: uppercase; letter-spacing: 0.04em; color: #666; }
.card .kv { display: grid; grid-template-columns: max-content 1fr; gap: 0.3em 1em; font-size: 0.92em; }
.card .kv .k { color: #666; }
.card .kv .v { color: #222; font-variant-numeric: tabular-nums; }

.ap-pill { display: inline-block; min-width: 5.5em; text-align: center;
  border-radius: 999px; padding: 0.12em 0.55em; font-size: 0.82em;
  font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
.ap-pill.ok { color: #14542a; background: #dff5e7; }
.ap-pill.watch, .ap-pill.unknown { color: #6a4a10; background: #fff1cc; }
.ap-pill.issue { color: #8a1f1f; background: #ffdede; }
.ap-pill.inactive { color: #555; background: #eee; }
.ap-events { margin-top: 0.65em; display: grid; gap: 0.25em; }
.ap-event { border-left: 3px solid #ddd; padding-left: 0.55em;
  font-size: 0.84em; color: #333; }
.ap-event.watch { border-left-color: #f0c060; }
.ap-event.issue { border-left-color: #e08080; }
.ap-event .when { color: #888; font-variant-numeric: tabular-nums; }

.cloud-table, .nx-table, .svc-table { width: 100%; border-collapse: collapse; font-size: 0.88em; margin-top: 0.4em; }
.cloud-table th, .cloud-table td, .nx-table th, .nx-table td, .svc-table th, .svc-table td {
  text-align: left; padding: 0.3em 0.5em; border-bottom: 1px solid #eee;
  font-variant-numeric: tabular-nums;
}
.cloud-table th, .nx-table th, .svc-table th { color: #666; font-weight: 600; font-size: 0.82em; }
.svc-table td.num, .svc-table th.num { text-align: right; }
.svc-table tr.totals td { border-top: 1px solid #ccc; border-bottom: 0;
  font-weight: 600; padding-top: 0.45em; }
.svc-table tr.totals td.muted { font-weight: 400; }

.warn-banner { background: #fff7e6; border: 1px solid #f0c060;
  border-radius: 4px; padding: 0.55em 0.7em; margin: 0 0 0.5em;
  font-size: 0.85em; color: #6a4a10; }
.warn-banner code { background: rgba(0,0,0,0.06); padding: 0.05em 0.3em;
  border-radius: 3px; }

/* Per-core CPU bars — one tall column per logical CPU. */
.cpu-bars { display: flex; gap: 0.35em; height: 92px; margin-top: 0.45em; }
.cpu-bar-cell { flex: 1; min-width: 0; }
.cpu-bar { height: 100%; background: #e8e8e8; border-radius: 3px;
  position: relative; overflow: hidden; }
.cpu-bar-fill { position: absolute; bottom: 0; left: 0; right: 0;
  background: #1db954; transition: height 0.3s ease-out; }
.tile.warn .cpu-bar-fill { background: #f0c060; }
.tile.fail .cpu-bar-fill { background: #e08080; }
.cpu-bar-label { position: absolute; left: 0; right: 0; bottom: 0.22em;
  z-index: 1; font-size: 0.68em; text-align: center; color: #333;
  font-variant-numeric: tabular-nums; text-shadow: 0 1px 1px rgba(255,255,255,0.85); }
.temp-c { display: block; color: #666; font-size: 0.72em; line-height: 1.15; }

.actions { display: flex; flex-wrap: wrap; gap: 0.5em; margin-top: 0.5em; }
.actions button { background: #1db954; color: white; border: 0; padding: 0.55em 1em;
  border-radius: 4px; font-weight: 600; cursor: pointer; }
.actions button.secondary { background: #4a4a4a; }
.actions button.danger { background: #d44; }
.actions button:disabled { background: #b8b8b8; cursor: wait; }

.quality-toggle { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.5em; margin-top: 0.75em; }
.quality-toggle button { background: #f4f4f4; color: #222; border: 1px solid #d8d8d8;
  text-align: left; padding: 0.65em 0.75em; border-radius: 6px; cursor: pointer; }
.quality-toggle button strong { display: block; margin-bottom: 0.15em; }
.quality-toggle button span { display: block; color: #666; font-size: 0.82em; line-height: 1.25; }
.quality-toggle button.active { background: #e6f9ec; border-color: #1db954; }
.quality-toggle button:disabled { opacity: 0.65; cursor: wait; }
.quality-status { min-height: 1.2em; margin: 0.55em 0 0; font-size: 0.85em; }

#diag-output { background: #fff; border: 1px solid #e6e6e6; border-radius: 4px;
  padding: 0.5em; margin-top: 0.5em; font-size: 0.85em; }
#diag-output .ok { color: #1a8a3a; }
#diag-output .warn { color: #c08020; }
#diag-output .fail { color: #c02020; }
#diag-output table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
#diag-output td { padding: 0.2em 0.5em; }

.muted { color: #888; }
.stale { opacity: 0.55; }
.ago { color: #666; font-size: 0.85em; }
"""

_PAGE_BODY = """
<p class="muted" id="staleness" style="margin-top:-0.5em">Loading…</p>

<div class="tiles">
  <div class="tile" id="tile-memory">
    <div class="label">Memory</div>
    <div class="value"><span id="mem-value">—</span></div>
    <div class="sub"><span id="mem-sub">—</span></div>
    <svg viewBox="0 0 100 32" preserveAspectRatio="none" id="spark-memory"></svg>
  </div>
  <div class="tile" id="tile-load">
    <div class="label">Load avg (1m)</div>
    <div class="value"><span id="load-value">—</span></div>
    <div class="sub"><span id="load-sub">—</span></div>
    <svg viewBox="0 0 100 32" preserveAspectRatio="none" id="spark-load"></svg>
  </div>
  <div class="tile" id="tile-cpu">
    <div class="label">CPU (per core)</div>
    <div class="value"><span id="cpu-value">—</span></div>
    <div class="cpu-bars" id="cpu-bars"></div>
    <div class="sub"><span id="cpu-sub">—</span></div>
  </div>
  <div class="tile" id="tile-temp">
    <div class="label">Temperature</div>
    <div class="value"><span id="temp-value">—</span></div>
    <div class="sub"><span id="temp-sub">—</span></div>
    <svg viewBox="0 0 100 32" preserveAspectRatio="none" id="spark-temp"></svg>
  </div>
  <div class="tile hidden" id="tile-fan">
    <div class="label">Fan</div>
    <div class="value"><span id="fan-value">—</span></div>
    <div class="sub"><span id="fan-sub">—</span></div>
    <svg viewBox="0 0 100 32" preserveAspectRatio="none" id="spark-fan"></svg>
  </div>
  <div class="tile" id="tile-disk">
    <div class="label">Disk</div>
    <div class="value"><span id="disk-value">—</span></div>
    <div class="sub"><span id="disk-sub">—</span></div>
  </div>
</div>

<div class="card">
  <h2>Software</h2>
  <div class="kv">
    <div class="k">Version</div><div class="v" id="sw-version">—</div>
    <div class="k">Branch</div><div class="v" id="sw-branch">—</div>
    <div class="k">Installed</div><div class="v" id="sw-installed">—</div>
    <div class="k">Uptime</div><div class="v" id="sw-uptime">—</div>
    <div class="k">Voice provider</div><div class="v" id="sw-provider">—</div>
  </div>
</div>

<div class="card">
  <h2>Cloud activity</h2>
  <div class="kv">
    <div class="k">Sessions today</div><div class="v" id="cloud-today">—</div>
    <div class="k">Last 24h spend</div><div class="v" id="cloud-24h">—</div>
    <div class="k">Month to date</div><div class="v" id="cloud-mtd">—</div>
    <div class="k">Last cloud call</div><div class="v" id="cloud-last">—</div>
  </div>
  <table class="cloud-table">
    <thead><tr><th>Provider</th><th>Sessions</th><th>In tokens</th><th>Out tokens</th><th>Est. cost</th><th>Last call</th></tr></thead>
    <tbody id="cloud-rows"><tr><td colspan="6" class="muted">No sessions this month yet.</td></tr></tbody>
  </table>
</div>

<div class="card" id="ha-card">
  <h2>Home Assistant</h2>
  <div class="kv">
    <div class="k">Status</div><div class="v" id="ha-status">—</div>
    <div class="k">URL</div><div class="v" id="ha-url">—</div>
    <div class="k">Version</div><div class="v" id="ha-version">—</div>
  </div>
  <p class="muted" style="margin: 0.6em 0 0; font-size: 0.85em;" id="ha-detail"></p>
  <p style="margin: 0.6em 0 0; font-size: 0.9em;">
    Configure at <a href="/ha/">jts.local/ha</a>.
  </p>
</div>

<div class="card" id="airplay-card">
  <h2>AirPlay</h2>
  <div class="kv">
    <div class="k">Status</div><div class="v" id="ap-status">—</div>
    <div class="k">Now</div><div class="v" id="ap-now">—</div>
    <div class="k">Last 5m</div><div class="v" id="ap-5m">—</div>
    <div class="k">Last 30m</div><div class="v" id="ap-30m">—</div>
    <div class="k">Fan-in</div><div class="v" id="ap-fanin">—</div>
    <div class="k">Camilla</div><div class="v" id="ap-camilla">—</div>
  </div>
  <div class="ap-events" id="ap-events"></div>
</div>

<div class="card" id="audio-quality-card">
  <h2>Audio conversion</h2>
  <div class="kv">
    <div class="k">Requested</div><div class="v" id="aq-requested">—</div>
    <div class="k">Active</div><div class="v" id="aq-active">—</div>
  </div>
  <p class="muted" style="margin: 0.6em 0 0; font-size: 0.85em;">
    Medium saves CPU and keeps the speech/AEC band clean. Best keeps
    the extreme top edge of hearing for critical listening.
    Changing this restarts music renderers briefly.
  </p>
  <div class="quality-toggle" role="group" aria-label="Audio conversion quality">
    <button id="btn-aq-medium" data-converter="samplerate_medium" type="button">
      <strong>Medium</strong>
      <span>Lower CPU; expected to sound the same for normal listening.</span>
    </button>
    <button id="btn-aq-best" data-converter="samplerate_best" type="button">
      <strong>Best</strong>
      <span>Highest ultrasonic-band fidelity; uses more CPU.</span>
    </button>
  </div>
  <p class="quality-status muted" id="aq-status"></p>
</div>

<div class="card">
  <h2>Network</h2>
  <div class="kv">
    <div class="k">Total RX since boot</div><div class="v" id="net-rx">—</div>
    <div class="k">Total TX since boot</div><div class="v" id="net-tx">—</div>
    <div class="k">Throttle bits</div><div class="v" id="throttle">—</div>
  </div>
</div>

<div class="card">
  <h2>Actions</h2>
  <p class="muted" style="margin: 0 0 0.5em; font-size: 0.85em;">
    Anyone on the same WiFi can trigger these. No confirmation
    afterwards — the page just spins until the daemon comes back.
  </p>
  <div class="actions">
    <button class="secondary" id="btn-restart-voice">Restart voice</button>
    <button class="secondary" id="btn-restart-audio">Restart audio</button>
    <button class="danger" id="btn-reboot">Reboot speaker</button>
    <button class="danger" id="btn-poweroff">Power off</button>
  </div>
  <p class="muted" style="margin: 0.6em 0 0; font-size: 0.85em;">
    Power off before changing cables or swapping power. The speaker
    stays off until you physically re-plug power — yanking the cord
    mid-run can corrupt config files on the SD card.
  </p>
</div>

<details class="disclosure">
  <summary>Run diagnostics</summary>
  <div class="disclosure-body">
    <p class="muted" style="margin-top:0">Runs <code>jasper-doctor</code> on the speaker. Takes ~3-5 s.</p>
    <button id="btn-diag" class="secondary">Run diagnostics now</button>
    <div id="diag-output" style="display:none"></div>
  </div>
</details>

<div class="card" id="services-card">
  <h2>Per-service usage</h2>
  <p class="muted" style="margin: 0 0 0.4em; font-size: 0.85em;">
    Cgroup-reported CPU and resident memory for each running
    <code>jasper-*</code> unit. CPU is per-core (100% = one fully
    saturated core; 400% = all four). New services show "—" for CPU
    until the next 5 s sample provides a delta. Sorted by CPU descending.
    Totals row at the bottom compares the jasper subtotal against
    total-system CPU so the gap (everything not in this table) is
    visible.
  </p>
  <div id="svc-warn" class="warn-banner" style="display:none"></div>
  <table class="svc-table">
    <thead><tr><th>Service</th><th class="num">CPU</th><th class="num">RSS</th></tr></thead>
    <tbody id="svc-rows"><tr><td colspan="3" class="muted">Loading…</td></tr></tbody>
  </table>
</div>
"""

_SCRIPT = r"""
(() => {
  const POLL_MS = 5000;
  const SAMPLE_SEC = 5;

  function fmtBytes(n) {
    if (n == null) return '—';
    const u = ['B','KB','MB','GB','TB'];
    let i = 0, v = Number(n);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
    return v >= 100 ? v.toFixed(0) + ' ' + u[i] : v.toFixed(1) + ' ' + u[i];
  }
  function fmtAgo(iso) {
    if (!iso) return '—';
    const t = Date.parse(iso);
    if (isNaN(t)) return '—';
    const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (sec < 60) return sec + 's ago';
    if (sec < 3600) return Math.round(sec / 60) + 'm ago';
    if (sec < 86400) return Math.round(sec / 3600) + 'h ago';
    return Math.round(sec / 86400) + 'd ago';
  }
  function fmtDur(sec) {
    if (sec == null) return '—';
    const s = Math.floor(sec);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const parts = [];
    if (d) parts.push(d + 'd');
    if (h || d) parts.push(h + 'h');
    parts.push(m + 'm');
    return parts.join(' ');
  }
  function fmtEpochAgo(epochSec) {
    if (epochSec == null) return '—';
    const sec = Math.max(0, Math.round(Date.now() / 1000 - Number(epochSec)));
    if (sec < 60) return sec + 's ago';
    if (sec < 3600) return Math.round(sec / 60) + 'm ago';
    if (sec < 86400) return Math.round(sec / 3600) + 'h ago';
    return Math.round(sec / 86400) + 'd ago';
  }
  function fmtUSD(n) {
    if (n == null) return '—';
    return '$' + Number(n).toFixed(2);
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  // Pi 5 pwm-fan cooling levels. The card stays intentionally terse;
  // the temperature tile is the alarm surface for thermal pressure.
  const FAN_STEPS = [
    { pwm: 0,   label: 'Off',    range: 'below 50°C' },
    { pwm: 75,  label: 'Low',    range: '50–60°C' },
    { pwm: 125, label: 'Medium', range: '60–67.5°C' },
    { pwm: 175, label: 'High',   range: '67.5–75°C' },
    { pwm: 250, label: 'Max',    range: 'above 75°C' },
  ];
  function fanStepInfo(pwm) {
    // Snap to the closest known step — tolerates kernel/DTB drift
    // (e.g. a hypothetical future board with slightly different
    // cooling-levels values). In practice the read is exact.
    let best = 0;
    let bestDiff = Math.abs(pwm - FAN_STEPS[0].pwm);
    for (let i = 1; i < FAN_STEPS.length; i++) {
      const d = Math.abs(pwm - FAN_STEPS[i].pwm);
      if (d < bestDiff) { best = i; bestDiff = d; }
    }
    return Object.assign({}, FAN_STEPS[best], {
      index: best,
    });
  }

  // Per-core CPU bars renderer — one tall bar per logical CPU.
  function renderCpuBars(percents) {
    const container = document.getElementById('cpu-bars');
    if (!container) return;
    if (!percents || !percents.length) {
      container.innerHTML = '';
      return;
    }
    container.innerHTML = percents.map((p, i) => {
      const pct = Math.min(100, Math.max(0, p || 0));
      return '<div class="cpu-bar-cell">' +
             '<div class="cpu-bar"><div class="cpu-bar-fill" style="height:' +
             pct.toFixed(1) + '%"></div>' +
             '<div class="cpu-bar-label">' + Math.round(pct) + '%</div></div>' +
             '</div>';
    }).join('');
  }

  function sparkline(svgId, values, opts) {
    const svg = document.getElementById(svgId);
    if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (!values || !values.length) return;
    const min = opts && opts.min != null ? opts.min : Math.min(...values);
    let max = opts && opts.max != null ? opts.max : Math.max(...values);
    if (max - min < 1e-6) max = min + 1;
    const n = values.length;
    const W = 100, H = 32;
    const x = (i) => (i / Math.max(1, n - 1)) * W;
    const y = (v) => H - ((v - min) / (max - min)) * H;
    const pts = values.map((v, i) => x(i).toFixed(2) + ',' + y(v).toFixed(2)).join(' ');
    const area = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
    area.setAttribute('class', 'area');
    area.setAttribute('points', '0,' + H + ' ' + pts + ' ' + W + ',' + H);
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    line.setAttribute('class', 'line');
    line.setAttribute('points', pts);
    svg.appendChild(area);
    svg.appendChild(line);
  }

  function setTile(id, status) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('warn', 'fail');
    if (status === 'warn') el.classList.add('warn');
    if (status === 'fail') el.classList.add('fail');
  }

  function renderAirPlay(h) {
    const statusEl = document.getElementById('ap-status');
    const nowEl = document.getElementById('ap-now');
    const fiveEl = document.getElementById('ap-5m');
    const thirtyEl = document.getElementById('ap-30m');
    const faninEl = document.getElementById('ap-fanin');
    const camillaEl = document.getElementById('ap-camilla');
    const eventsEl = document.getElementById('ap-events');
    if (!statusEl) return;
    if (!h) {
      statusEl.innerHTML = '<span class="ap-pill unknown">Unknown</span>';
      nowEl.textContent = 'sampler unavailable';
      fiveEl.textContent = '—';
      thirtyEl.textContent = '—';
      faninEl.textContent = '—';
      camillaEl.textContent = '—';
      eventsEl.innerHTML = '';
      return;
    }

    let status = h.status || 'unknown';
    if (!['ok', 'watch', 'issue', 'inactive', 'unknown'].includes(status)) {
      status = 'unknown';
    }
    statusEl.innerHTML =
      '<span class="ap-pill ' + status + '">' + esc(status) + '</span> ' +
      '<span class="muted">' + esc(h.reason || '') + '</span>';

    const cur = h.current || {};
    const fanin = cur.fanin || {};
    const airplay = fanin.airplay || {};
    const output = fanin.output || {};
    const rate = airplay.frames_per_sec;
    const mpris = cur.mpris || {};
    const isStreaming = rate != null && rate >= 1000;
    nowEl.textContent = isStreaming
      ? Math.round(rate).toLocaleString() + ' frames/s'
      : (mpris.playing ? 'MPRIS playing · no fan-in frames' : 'idle');

    function summarize(s) {
      if (!s) return '—';
      const parts = [];
      if (s.shairport_packet_drops) parts.push(s.shairport_packet_drops + ' packet drops');
      if (s.shairport_sync_errors) parts.push(s.shairport_sync_errors + ' sync corrections');
      if (s.shairport_underruns) parts.push(s.shairport_underruns + ' shairport underruns');
      if (s.fanin_airplay_xruns) parts.push(s.fanin_airplay_xruns + ' AirPlay fan-in xruns');
      if (s.fanin_output_xruns) parts.push(s.fanin_output_xruns + ' output xruns');
      if (s.camilla_short_reads) parts.push(s.camilla_short_reads + ' Camilla short reads');
      if (s.camilla_playback_underruns) parts.push(s.camilla_playback_underruns + ' Camilla underruns');
      return parts.length ? parts.join(' · ') : 'clean';
    }
    fiveEl.textContent = summarize(h.summary_5m);
    thirtyEl.textContent = summarize(h.summary_30m);

    faninEl.textContent = fanin.available
      ? 'input ' + (fanin.input_buffer_frames || '—') +
        ' / output ' + (output.buffer_frames || fanin.output_buffer_frames || '—') +
        ' frames · xruns ' + (airplay.xrun_count || 0) +
        '/' + (output.xrun_count || 0)
      : 'unavailable';

    const camilla = cur.camilla || null;
    camillaEl.textContent = camilla
      ? 'buffer ' + (camilla.buffer_level || 0) +
        ' · rate ' + (camilla.rate_adjust == null ? '—' : Number(camilla.rate_adjust).toFixed(6))
      : 'journal only';

    const events = (h.events || []).slice(-5).reverse();
    if (events.length) {
      eventsEl.innerHTML = events.map(ev => {
        const sev = ['watch', 'issue'].includes(ev.severity)
          ? ev.severity : 'watch';
        return '<div class="ap-event ' + sev + '">' +
          '<strong>' + esc(ev.title || ev.type || 'event') + '</strong> ' +
          '<span class="muted">' + esc(ev.detail || '') + '</span> ' +
          '<span class="when">' + fmtEpochAgo(ev.ts) + '</span>' +
        '</div>';
      }).join('');
    } else {
      eventsEl.innerHTML =
        '<div class="ap-event"><span class="muted">No recent AirPlay events.</span></div>';
    }
  }

  function audioQualityLabel(converter) {
    if (converter === 'samplerate_best') return 'Best';
    if (converter === 'samplerate_medium') return 'Medium';
    return converter || '—';
  }

  function renderAudioQuality(q) {
    const reqEl = document.getElementById('aq-requested');
    if (!reqEl) return;
    const activeEl = document.getElementById('aq-active');
    const statusEl = document.getElementById('aq-status');
    const requested = q && q.converter;
    const active = q && q.active_converter;
    reqEl.textContent = audioQualityLabel(requested);
    activeEl.textContent = active
      ? audioQualityLabel(active)
      : 'unknown';
    if (statusEl && q && q.error) {
      statusEl.textContent = 'State warning: ' + q.error;
    } else if (statusEl && q && q.summary) {
      statusEl.textContent = q.summary;
    } else if (statusEl) {
      statusEl.textContent = '';
    }
    ['samplerate_medium', 'samplerate_best'].forEach(converter => {
      const btn = document.querySelector('[data-converter="' + converter + '"]');
      if (!btn) return;
      btn.classList.toggle('active', converter === requested);
      btn.disabled = false;
    });
  }

  function render(snap) {
    if (!snap || !snap.metrics) {
      renderAirPlay(snap && snap.airplay_health);
      document.getElementById('staleness').textContent =
        'No metrics yet (jasper-control sampler still warming up?).';
      return;
    }
    const m = snap.metrics;
    const cur = m.current;
    const hist = m.history;
    const lastSampled = m.last_sample_at;
    const stale = lastSampled
      ? Math.max(0, Date.now() / 1000 - lastSampled)
      : null;
    document.getElementById('staleness').textContent =
      lastSampled
        ? ('Live · sampler ' + (stale < 12 ? 'OK' : ('stale ' + Math.round(stale) + 's')))
        : 'Sampler not running.';

    // Memory tile
    const memAvail = hist.mem_available_mb[hist.mem_available_mb.length - 1] || 0;
    const memTotal = cur.mem_total_mb || 1;
    const memUsed = memTotal - memAvail;
    const swap = hist.swap_used_mb[hist.swap_used_mb.length - 1] || 0;
    document.getElementById('mem-value').textContent =
      Math.round(memUsed) + ' / ' + Math.round(memTotal) + ' MB';
    document.getElementById('mem-sub').textContent =
      Math.round(memAvail) + ' MB available' +
      (swap > 0 ? ' · ' + Math.round(swap) + ' MB swap' : '');
    let memStatus = 'ok';
    if (memAvail < 150) memStatus = 'fail';
    else if (memAvail < 250 || swap > 150) memStatus = 'warn';
    setTile('tile-memory', memStatus);
    sparkline('spark-memory', hist.mem_used_mb, { min: 0, max: memTotal });

    // Load tile
    const load = hist.load_1m[hist.load_1m.length - 1] || 0;
    document.getElementById('load-value').textContent = load.toFixed(2);
    document.getElementById('load-sub').textContent = '';
    let loadStatus = 'ok';
    if (load > 4) loadStatus = 'fail';
    else if (load > 3) loadStatus = 'warn';
    setTile('tile-load', loadStatus);
    sparkline('spark-load', hist.load_1m, { min: 0, max: Math.max(4, ...hist.load_1m) });

    // Per-core CPU tile
    const cores = m.current.per_core_cpu_pct || [];
    renderCpuBars(cores);
    if (cores.length) {
      const totalCpu = cores.reduce((a, b) => a + b, 0);
      const maxCore = Math.max(...cores);
      document.getElementById('cpu-value').textContent =
        Math.round(totalCpu) + '% total';
      document.getElementById('cpu-sub').textContent = '';
      let cpuStatus = 'ok';
      // Mirror the load thresholds: total = 3 cores ≈ warn, total = 4
      // cores fully saturated = fail. Also flag a single pegged core.
      if (totalCpu > 380 || maxCore >= 98) cpuStatus = 'fail';
      else if (totalCpu > 300 || maxCore >= 90) cpuStatus = 'warn';
      setTile('tile-cpu', cpuStatus);
    } else {
      document.getElementById('cpu-value').textContent = '—';
      document.getElementById('cpu-sub').textContent = '';
      setTile('tile-cpu', 'ok');
    }

    // Temp tile — Pi-side reads °C; put Fahrenheit first for the UI.
    const temp = cur.temp_c || 0;
    const tempF = temp * 9 / 5 + 32;
    document.getElementById('temp-value').innerHTML =
      tempF.toFixed(0) + '°F<span class="temp-c">' +
      temp.toFixed(1) + '°C</span>';
    const throttledNow = cur.throttled_now || 0;
    const throttledHist = cur.throttled_history || 0;
    document.getElementById('temp-sub').textContent =
      throttledNow
        ? 'THROTTLING NOW (bits=0x' + throttledNow.toString(16) + ')'
        : (throttledHist
            ? 'throttled since boot (bits=0x' + throttledHist.toString(16) + ')'
            : '');
    let tempStatus = 'ok';
    if (temp >= 80 || throttledNow) tempStatus = 'fail';
    else if (temp >= 75 || throttledHist) tempStatus = 'warn';
    setTile('tile-temp', tempStatus);
    if (hist.temp_c && hist.temp_c.length) {
      const tempMin = Math.min(...hist.temp_c);
      const tempMax = Math.max(...hist.temp_c);
      const pad = Math.max(2, (tempMax - tempMin) * 0.15);
      sparkline('spark-temp', hist.temp_c, {
        min: Math.max(0, tempMin - pad),
        max: tempMax + pad,
      });
    }

    // Fan tile — hidden entirely on hardware without a pwm-fan device.
    const fanTile = document.getElementById('tile-fan');
    if (cur.fan_present && cur.fan_rpm != null) {
      fanTile.classList.remove('hidden');
      const rpm = cur.fan_rpm;
      const pwm = cur.fan_pwm || 0;
      const step = fanStepInfo(pwm);
      document.getElementById('fan-value').textContent = step.label;
      document.getElementById('fan-sub').textContent =
        rpm > 0 ? rpm + ' RPM' : 'fan off';
      let fanStatus = 'ok';
      if (step.index >= FAN_STEPS.length - 1) fanStatus = 'warn';
      setTile('tile-fan', fanStatus);
      sparkline('spark-fan', hist.fan_rpm, {
        min: 0,
        max: Math.max(1000, ...hist.fan_rpm),
      });
    } else {
      fanTile.classList.add('hidden');
    }

    // Disk tile
    const diskPct = cur.disk_used_pct || 0;
    const diskTotal = cur.disk_total_gb || 0;
    document.getElementById('disk-value').textContent = diskPct.toFixed(1) + '%';
    document.getElementById('disk-sub').textContent =
      'of ' + diskTotal.toFixed(0) + ' GB';
    let diskStatus = 'ok';
    if (diskPct > 90) diskStatus = 'fail';
    else if (diskPct > 75) diskStatus = 'warn';
    setTile('tile-disk', diskStatus);

    // Software card
    const build = snap.build || {};
    document.getElementById('sw-version').textContent =
      build.JASPER_GIT_SHA || 'unknown';
    document.getElementById('sw-branch').textContent =
      build.JASPER_GIT_BRANCH || 'unknown';
    document.getElementById('sw-installed').textContent =
      build.JASPER_INSTALL_AT ? fmtAgo(build.JASPER_INSTALL_AT) : 'unknown';
    document.getElementById('sw-uptime').textContent = fmtDur(cur.uptime_sec);
    document.getElementById('sw-provider').textContent =
      snap.voice_provider || '—';

    // Cloud activity card
    const cloud = snap.cloud || { available: false };
    if (cloud.available) {
      document.getElementById('cloud-today').textContent =
        cloud.sessions_today != null ? cloud.sessions_today : '—';
      document.getElementById('cloud-24h').textContent =
        fmtUSD(cloud.spend_last_24h_usd);
      document.getElementById('cloud-mtd').textContent =
        fmtUSD(cloud.spend_month_to_date_usd);
      document.getElementById('cloud-last').textContent =
        cloud.last_successful_turn_at
          ? fmtAgo(cloud.last_successful_turn_at)
          : 'never';
      const tbody = document.getElementById('cloud-rows');
      if (cloud.by_provider && cloud.by_provider.length) {
        tbody.innerHTML = cloud.by_provider.map(row =>
          '<tr>' +
          '<td>' + (row.provider || '—') + '</td>' +
          '<td>' + row.sessions + '</td>' +
          '<td>' + (row.input_tokens || 0).toLocaleString() + '</td>' +
          '<td>' + (row.output_tokens || 0).toLocaleString() + '</td>' +
          '<td>' + fmtUSD(row.cost_usd) + '</td>' +
          '<td>' + fmtAgo(row.last_session_at) + '</td>' +
          '</tr>'
        ).join('');
      } else {
        tbody.innerHTML =
          '<tr><td colspan="6" class="muted">No sessions this month yet.</td></tr>';
      }
    } else {
      document.getElementById('cloud-today').textContent = '—';
      document.getElementById('cloud-mtd').textContent = '—';
    }

    // Home Assistant card
    const ha = snap.home_assistant || { configured: false };
    const statusEl = document.getElementById('ha-status');
    const urlEl = document.getElementById('ha-url');
    const versionEl = document.getElementById('ha-version');
    const detailEl = document.getElementById('ha-detail');
    if (!ha.configured) {
      statusEl.textContent = 'Not configured';
      statusEl.style.color = '';
      urlEl.textContent = '—';
      versionEl.textContent = '—';
      detailEl.textContent = '';
    } else if (ha.connected) {
      statusEl.textContent = '✓ Connected';
      statusEl.style.color = '#14542a';
      urlEl.textContent = ha.url || '—';
      versionEl.textContent = (ha.instance_name || 'Home Assistant') +
        (ha.version ? ' (' + ha.version + ')' : '');
      detailEl.textContent = '';
    } else {
      statusEl.textContent = '✗ Unreachable';
      statusEl.style.color = '#a33';
      urlEl.textContent = ha.url || '—';
      versionEl.textContent = '—';
      detailEl.textContent = ha.error || 'Connection failed.';
    }

    renderAirPlay(snap.airplay_health);
    renderAudioQuality(snap.audio_quality);

    // Network
    document.getElementById('net-rx').textContent = fmtBytes(cur.net_rx_bytes);
    document.getElementById('net-tx').textContent = fmtBytes(cur.net_tx_bytes);
    document.getElementById('throttle').textContent =
      throttledNow || throttledHist
        ? ('0x' + (throttledNow || 0).toString(16) +
           ' (since-boot 0x' + (throttledHist || 0).toString(16) + ')')
        : '0x0 (healthy)';

    // Per-service usage. Sort CPU% desc with null/unknown last so the
    // heavy hitters surface at the top — useful during experimental-
    // branch shakedowns where you want to know which daemon spiked.
    // The totals row at the bottom shows jasper sum vs total-system
    // CPU (from per-core sampler), making "where's the rest of the
    // load coming from?" answerable at a glance.
    const services = m.services || [];
    const svcRows = document.getElementById('svc-rows');
    const svcWarn = document.getElementById('svc-warn');
    // Memory-cgroup-controller availability warning. Surfaces the
    // running-kernel-vs-cmdline.txt gap so the next person doesn't
    // ask why every service's RSS column reads '—'.
    if (m.current.memory_cgroup_enabled === false) {
      svcWarn.style.display = '';
      svcWarn.innerHTML =
        '<strong>RSS unavailable:</strong> the running kernel was ' +
        'booted with <code>cgroup_disable=memory</code> (Pi 5 default) ' +
        'and the memory cgroup controller is off, so ' +
        '<code>/sys/fs/cgroup/system.slice/*/memory.current</code> ' +
        "doesn't exist. <code>install.sh</code> has already added " +
        '<code>cgroup_enable=memory</code> to ' +
        '<code>/boot/firmware/cmdline.txt</code>; ' +
        '<strong>reboot the speaker</strong> to apply.';
    } else {
      svcWarn.style.display = 'none';
    }
    if (services.length) {
      const sorted = services.slice().sort((a, b) => {
        const ac = a.cpu_pct == null ? -1 : a.cpu_pct;
        const bc = b.cpu_pct == null ? -1 : b.cpu_pct;
        return bc - ac;
      });
      const rows = sorted.map(s => {
        const cpu = s.cpu_pct == null ? '—' : s.cpu_pct.toFixed(1) + '%';
        const rss = s.rss_mb == null ? '—' : Math.round(s.rss_mb) + ' MB';
        return '<tr><td>' + s.name + '</td>' +
               '<td class="num">' + cpu + '</td>' +
               '<td class="num">' + rss + '</td></tr>';
      });
      // Totals row. Jasper subtotal is the sum of known cpu_pct's
      // (skips first-tick None values). System total comes from
      // per-core CPU (sum of all cores, max 4*100=400 on a 4-core
      // Pi). Headroom is the gap; "not in this table" is everything
      // else (kernel threads, audio renderers, etc.).
      const jasperCpu = sorted.reduce((acc, s) =>
        acc + (s.cpu_pct == null ? 0 : s.cpu_pct), 0);
      const jasperRss = sorted.reduce((acc, s) =>
        acc + (s.rss_mb == null ? 0 : s.rss_mb), 0);
      const anyRss = sorted.some(s => s.rss_mb != null);
      const corePcts = m.current.per_core_cpu_pct || [];
      let totalsLine = '';
      if (corePcts.length) {
        const systemCpu = corePcts.reduce((a, b) => a + b, 0);
        const maxScale = corePcts.length * 100;
        const headroom = Math.max(0, maxScale - systemCpu);
        const nonJasper = Math.max(0, systemCpu - jasperCpu);
        totalsLine =
          '<tr class="totals">' +
          '<td>System total · jasper / non-jasper / free</td>' +
          '<td class="num">' +
            Math.round(systemCpu) + '% (' +
            Math.round(jasperCpu) + ' + ' + Math.round(nonJasper) +
            ' + ' + Math.round(headroom) + ' / ' + maxScale + '%)' +
          '</td>' +
          '<td class="num">' +
            (anyRss ? Math.round(jasperRss) + ' MB' : '—') +
          '</td></tr>';
      } else {
        // Per-core sampler hasn't produced a delta yet (first tick
        // after boot or non-Linux). Show jasper-only totals.
        totalsLine =
          '<tr class="totals">' +
          '<td>Jasper subtotal</td>' +
          '<td class="num">' + Math.round(jasperCpu) + '%</td>' +
          '<td class="num">' +
            (anyRss ? Math.round(jasperRss) + ' MB' : '—') +
          '</td></tr>';
      }
      svcRows.innerHTML = rows.join('') + totalsLine;
    } else {
      svcRows.innerHTML =
        '<tr><td colspan="3" class="muted">No jasper-* cgroups visible (cgroup-v2 unavailable, or dev env).</td></tr>';
    }
  }

  async function poll() {
    try {
      const r = await fetch('data.json', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const snap = await r.json();
      render(snap);
      document.body.classList.remove('stale');
    } catch (e) {
      document.body.classList.add('stale');
      document.getElementById('staleness').textContent =
        'Disconnected (' + e.message + '). Retrying…';
    }
  }

  // Initial fetch + poll loop
  poll();
  setInterval(poll, POLL_MS);

  __CSRF_FETCH_HELPERS__
  async function postAction(path, btn) {
    if (!btn) return;
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Working…';
    try {
      const r = await fetch(path, {
        method: 'POST',
        headers: csrfHeaders(),
      });
      const body = await r.json();
      btn.textContent = r.ok ? 'Sent' : ('Failed: ' + (body.error || r.status));
    } catch (e) {
      btn.textContent = 'Failed: ' + e.message;
    }
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = original;
    }, 3000);
  }

  async function setAudioQuality(converter) {
    const buttons = Array.from(document.querySelectorAll('[data-converter]'));
    buttons.forEach(b => { b.disabled = true; });
    const status = document.getElementById('aq-status');
    if (status) status.textContent = 'Applying…';
    try {
      const r = await fetch('audio-quality', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': CSRF,
        },
        body: JSON.stringify({ converter }),
      });
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
      renderAudioQuality(body.audio_quality);
      if (status) status.textContent = 'Applied. Music renderers are restarting briefly.';
    } catch (e) {
      if (status) status.textContent = 'Failed: ' + e.message;
      buttons.forEach(b => { b.disabled = false; });
    }
  }

  document.getElementById('btn-restart-voice').addEventListener('click', e => {
    if (!confirm('Restart jasper-voice? Wake-word will be unavailable for ~30 s.')) return;
    postAction('restart/voice', e.target);
  });
  document.getElementById('btn-restart-audio').addEventListener('click', e => {
    if (!confirm('Restart the audio chain (camilla + librespot + shairport + bluez)? Music will stop momentarily.')) return;
    postAction('restart/audio', e.target);
  });
  document.getElementById('btn-reboot').addEventListener('click', e => {
    if (!confirm('Reboot the speaker? This takes ~60 s.')) return;
    if (!confirm('Are you sure? You will lose audio for about a minute.')) return;
    postAction('reboot', e.target);
  });
  document.getElementById('btn-poweroff').addEventListener('click', e => {
    // Stronger double-confirm than reboot — power off leaves the
    // Pi off until someone physically re-plugs the cord, which is a
    // higher commitment than a 60-second auto-recovering reboot.
    if (!confirm('Power off the speaker? It will stay off until you physically re-plug power.')) return;
    if (!confirm('Are you absolutely sure? You will need physical access to turn the speaker back on.')) return;
    postAction('poweroff', e.target);
  });
  document.querySelectorAll('[data-converter]').forEach(btn => {
    btn.addEventListener('click', e => {
      const converter = e.currentTarget.getAttribute('data-converter');
      if (!confirm('Change audio conversion quality? Music renderers will restart briefly.')) return;
      setAudioQuality(converter);
    });
  });

  // Diagnostics
  document.getElementById('btn-diag').addEventListener('click', async e => {
    const btn = e.target;
    btn.disabled = true;
    const out = document.getElementById('diag-output');
    out.style.display = 'block';
    out.innerHTML = '<span class="muted">Running jasper-doctor…</span>';
    try {
      const r = await fetch('diagnostics.json', { cache: 'no-store' });
      const body = await r.json();
      if (body.error) {
        out.innerHTML = '<span class="fail">Error: ' +
          escapeHtml(body.error) + '</span>';
      } else {
        const rows = (body.results || []).map(c => {
          const cls = c.status === 'fail' ? 'fail'
            : (c.status === 'warn' ? 'warn' : 'ok');
          const mark = c.status === 'fail' ? '✗'
            : (c.status === 'warn' ? '!' : '✓');
          return '<tr><td class="' + cls + '">' + mark + '</td>' +
                 '<td>' + escapeHtml(c.name) + '</td>' +
                 '<td>' + escapeHtml(c.detail || '') + '</td></tr>';
        }).join('');
        out.innerHTML = '<table>' + rows + '</table>' +
          '<p class="muted" style="margin:0.5em 0 0">' +
          body.fails + ' failed, ' + body.warns + ' warning(s).</p>';
      }
    } catch (err) {
      out.innerHTML = '<span class="fail">Failed: ' + err.message + '</span>';
    }
    btn.disabled = false;
  });

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
})();
"""


def _render_page(csrf_token: str = "") -> bytes:
    # wrap_page emits its own <h1>{title}</h1> right after NAV_BACK_HTML;
    # _PAGE_BODY picks up the rest. The dashboard-specific styles go in
    # a <style> tag at the top of body — valid HTML5 and avoids forking
    # wrap_page's signature for a one-off feature.
    #
    body = (
        f"<style>{_EXTRA_STYLE}</style>\n"
        + (csrf_meta_html(csrf_token) if csrf_token else "")
        + _PAGE_BODY
        + "\n<script>"
        + _SCRIPT.replace("__CSRF_FETCH_HELPERS__", csrf_fetch_helpers_js())
        + "</script>\n"
    )
    return wrap_page("System", body)


def _make_handler(
    control_base: str = DEFAULT_CONTROL_BASE,
) -> type[BaseHTTPRequestHandler]:

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            # nginx strips the /system/ prefix so we see paths like
            # "/" and "/data.json".
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                ctx = begin_request(self)
                send_html_response(self, _render_page(ctx["csrf_token"]))
                return
            if path == "/data.json":
                status, body = proxy_get(
                    "/system/snapshot", control_base=control_base,
                )
                send_proxy_json(self, body, status=status)
                return
            if path == "/diagnostics.json":
                status, body = proxy_get(
                    "/system/diagnostics",
                    control_base=control_base, timeout=30.0,
                )
                send_proxy_json(self, body, status=status)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            POST_ROUTES = (
                "/restart/voice", "/restart/audio", "/reboot", "/poweroff",
                "/audio-quality",
            )
            if path not in POST_ROUTES:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not verify_csrf(self):
                reject_csrf(self)
                return
            body = None
            if path == "/audio-quality":
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                except ValueError:
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                if length < 0 or length > 4096:
                    self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                body = self.rfile.read(length) if length else b"{}"
            status, body = proxy_post(
                "/system" + path, control_base=control_base, body=body,
            )
            send_proxy_json(self, body, status=status)

    return Handler


def make_server(target, *, control_base: str = DEFAULT_CONTROL_BASE) -> ThreadingHTTPServer:
    """Build the dashboard server. `target` is a socket / (host, port)
    tuple / int port per _systemd.make_http_server's contract."""
    from . import _systemd
    return _systemd.make_http_server(target, _make_handler(control_base))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-system-web",
        description="System dashboard at /system/ for the JTS speaker",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("JASPER_SYSTEM_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_SYSTEM_WEB_PORT", "8772")),
    )
    parser.add_argument(
        "--control-base",
        default=os.environ.get(
            "JASPER_CONTROL_BASE", DEFAULT_CONTROL_BASE,
        ),
        help="jasper-control HTTP base URL (default 127.0.0.1:8780)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from . import _systemd
    sockets = _systemd.adopt_systemd_sockets()
    target = sockets[0] if sockets else (args.host, args.port)
    server = make_server(target, control_base=args.control_base)

    handler_cls = server.RequestHandlerClass
    tracker = _systemd.IdleShutdownTracker(
        idle_threshold_sec=IDLE_SHUTDOWN_SEC,
    )
    _systemd.install_request_idle_bump(handler_cls, tracker)
    tracker.start()

    if sockets:
        logger.info(
            "jasper-system-web adopting systemd fd (control=%s, idle=%ds)",
            args.control_base, int(IDLE_SHUTDOWN_SEC),
        )
    else:
        logger.info(
            "jasper-system-web listening on http://%s:%d (control=%s)",
            args.host, args.port, args.control_base,
        )

    _systemd.notify_ready()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    _systemd.notify_stopping()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
