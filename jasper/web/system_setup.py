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

This wizard's job is to render HTML and proxy the JSON. Polling is
client-side (fetch /data.json every 5 s); the server keeps a thin
proxy connection to jasper-control on 127.0.0.1:8780.

Socket-activated like the other wizards, with a longer idle window
(30 min) since a power user may leave the dashboard open in a tab
for monitoring. Idle exit + cold-start still apply.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ._common import NAV_BACK_HTML, PAGE_STYLE, wrap_page

logger = logging.getLogger(__name__)


# jasper-control's HTTP API. We assume same host (127.0.0.1) because
# the wizard runs alongside on the same Pi. Override via env for tests.
DEFAULT_CONTROL_BASE = "http://127.0.0.1:8780"

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

.cloud-table, .nx-table { width: 100%; border-collapse: collapse; font-size: 0.88em; margin-top: 0.4em; }
.cloud-table th, .cloud-table td, .nx-table th, .nx-table td {
  text-align: left; padding: 0.3em 0.5em; border-bottom: 1px solid #eee;
  font-variant-numeric: tabular-nums;
}
.cloud-table th, .nx-table th { color: #666; font-weight: 600; font-size: 0.82em; }

.actions { display: flex; flex-wrap: wrap; gap: 0.5em; margin-top: 0.5em; }
.actions button { background: #1db954; color: white; border: 0; padding: 0.55em 1em;
  border-radius: 4px; font-weight: 600; cursor: pointer; }
.actions button.secondary { background: #4a4a4a; }
.actions button.danger { background: #d44; }
.actions button:disabled { background: #b8b8b8; cursor: wait; }

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
  <div class="tile" id="tile-load" title="Linux 1-minute load average: roughly the average number of processes in the run queue or running over the last 60 s. Not a percentage. On 4 cores: 4.0 = fully utilized, &gt;4 = oversubscribed.">
    <div class="label">Load avg (1m)</div>
    <div class="value"><span id="load-value">—</span></div>
    <div class="sub"><span id="load-sub">— / 4 cores</span></div>
    <svg viewBox="0 0 100 32" preserveAspectRatio="none" id="spark-load"></svg>
  </div>
  <div class="tile" id="tile-temp">
    <div class="label">Temperature</div>
    <div class="value"><span id="temp-value">—</span></div>
    <div class="sub"><span id="temp-sub">—</span></div>
    <svg viewBox="0 0 100 32" preserveAspectRatio="none" id="spark-temp"></svg>
  </div>
  <div class="tile hidden" id="tile-fan" title="PWM fan RPM (tachometer) and duty cycle. The pwm-fan kernel driver steps duty 0–255 in response to SoC temp trip points (50 / 60 / 67°C).">
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
  </div>
</div>

<details class="disclosure">
  <summary>Run diagnostics</summary>
  <div class="disclosure-body">
    <p class="muted" style="margin-top:0">Runs <code>jasper-doctor</code> on the speaker. Takes ~3-5 s.</p>
    <button id="btn-diag" class="secondary">Run diagnostics now</button>
    <div id="diag-output" style="display:none"></div>
  </div>
</details>
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
  function fmtUSD(n) {
    if (n == null) return '—';
    return '$' + Number(n).toFixed(2);
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

  function render(snap) {
    if (!snap || !snap.metrics) {
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
    document.getElementById('load-sub').textContent =
      load.toFixed(2) + ' / 4 cores · ~' + Math.round((load / 4) * 100) + '% saturated';
    let loadStatus = 'ok';
    if (load > 4) loadStatus = 'fail';
    else if (load > 3) loadStatus = 'warn';
    setTile('tile-load', loadStatus);
    sparkline('spark-load', hist.load_1m, { min: 0, max: Math.max(4, ...hist.load_1m) });

    // Temp tile — show both Celsius and Fahrenheit. Pi-side reads
    // vcgencmd, which only emits °C; convert in the browser.
    const temp = cur.temp_c || 0;
    const tempF = temp * 9 / 5 + 32;
    document.getElementById('temp-value').textContent =
      temp.toFixed(1) + '°C · ' + tempF.toFixed(0) + '°F';
    const throttledNow = cur.throttled_now || 0;
    const throttledHist = cur.throttled_history || 0;
    document.getElementById('temp-sub').textContent =
      throttledNow
        ? 'THROTTLING NOW (bits=0x' + throttledNow.toString(16) + ')'
        : (throttledHist
            ? 'throttled since boot (bits=0x' + throttledHist.toString(16) + ')'
            : 'no throttle events');
    let tempStatus = 'ok';
    if (temp >= 80 || throttledNow) tempStatus = 'fail';
    else if (temp >= 75 || throttledHist) tempStatus = 'warn';
    setTile('tile-temp', tempStatus);
    // For temp the sparkline is constant (same value at every tick
    // since temp samples at 30s not 5s) so just suppress it.

    // Fan tile — hidden entirely on hardware without a pwm-fan device
    // (dev machines, Pi without an Active Cooler attached).
    const fanTile = document.getElementById('tile-fan');
    if (cur.fan_present && cur.fan_rpm != null) {
      fanTile.classList.remove('hidden');
      const rpm = cur.fan_rpm;
      const pwm = cur.fan_pwm || 0;
      const pwmMax = cur.fan_pwm_max || 255;
      const pct = Math.round((pwm / pwmMax) * 100);
      document.getElementById('fan-value').textContent = rpm + ' RPM';
      document.getElementById('fan-sub').textContent =
        'PWM ' + pct + '% · ' + pwm + '/' + pwmMax;
      // Fail if the fan reports no RPM while the kernel is commanding
      // it on (4-pin disconnected, stalled blades, dead tachometer).
      // Warn at >=90% duty — usually means the SoC is approaching the
      // top thermal trip and the kernel is asking for max airflow.
      let fanStatus = 'ok';
      if (pwm > 0 && rpm === 0) fanStatus = 'fail';
      else if (pct >= 90) fanStatus = 'warn';
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

    // Network
    document.getElementById('net-rx').textContent = fmtBytes(cur.net_rx_bytes);
    document.getElementById('net-tx').textContent = fmtBytes(cur.net_tx_bytes);
    document.getElementById('throttle').textContent =
      throttledNow || throttledHist
        ? ('0x' + (throttledNow || 0).toString(16) +
           ' (since-boot 0x' + (throttledHist || 0).toString(16) + ')')
        : '0x0 (healthy)';
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

  // Action buttons
  async function postAction(path, btn) {
    if (!btn) return;
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Working…';
    try {
      const r = await fetch(path, { method: 'POST' });
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


def _render_page() -> bytes:
    # wrap_page emits its own <h1>{title}</h1> right after NAV_BACK_HTML;
    # _PAGE_BODY picks up the rest. The dashboard-specific styles go in
    # a <style> tag at the top of body — valid HTML5 and avoids forking
    # wrap_page's signature for a one-off feature.
    body = (
        f"<style>{_EXTRA_STYLE}</style>\n"
        + _PAGE_BODY
        + f"\n<script>{_SCRIPT}</script>\n"
    )
    return wrap_page("System", body)


def _proxy_get(path: str, control_base: str = DEFAULT_CONTROL_BASE,
               timeout: float = 30.0) -> tuple[int, bytes]:
    """Proxy a GET to jasper-control. Returns (status, body) or 502
    on connection failure."""
    url = control_base.rstrip("/") + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b'{"error":"upstream HTTP error"}'
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        body = json.dumps({"error": f"jasper-control unreachable: {e}"}).encode()
        return 502, body


def _proxy_post(path: str, control_base: str = DEFAULT_CONTROL_BASE,
                timeout: float = 5.0) -> tuple[int, bytes]:
    """Same as _proxy_get but POST (with empty body — actions don't
    take parameters)."""
    url = control_base.rstrip("/") + path
    req = urllib.request.Request(
        url, data=b"", method="POST",
        headers={"Content-Type": "application/json", "Content-Length": "0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b'{"error":"upstream HTTP error"}'
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        body = json.dumps({"error": f"jasper-control unreachable: {e}"}).encode()
        return 502, body


def _make_handler(
    control_base: str = DEFAULT_CONTROL_BASE,
) -> type[BaseHTTPRequestHandler]:

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_raw_json(self, body: bytes, *, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            # nginx strips the /system/ prefix so we see paths like
            # "/" and "/data.json".
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                self._send_html(_render_page())
                return
            if path == "/data.json":
                status, body = _proxy_get("/system/snapshot", control_base)
                self._send_raw_json(body, status=status)
                return
            if path == "/diagnostics.json":
                status, body = _proxy_get(
                    "/system/diagnostics", control_base, timeout=30.0,
                )
                self._send_raw_json(body, status=status)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path in ("/restart/voice", "/restart/audio", "/reboot"):
                status, body = _proxy_post(
                    "/system" + path, control_base,
                )
                self._send_raw_json(body, status=status)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

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
