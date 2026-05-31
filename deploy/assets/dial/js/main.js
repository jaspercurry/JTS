// main.js — /dial/ rotary-dial onboarding (setup page behaviour).
//
// The /dial/ setup page is fetch-driven: it polls GET ./scan every 2 s for
// plugged-in ESP32-S3 devices and renders a card per device with Provision
// (smart) and Force-flash buttons. Clicking one POSTs to ./onboard, which runs
// jasper-dial-onboard server-side (flash + WiFi provision via Improv). The
// landing page (GET /) is a static server-rendered card with a Continue link
// and loads no JS — only the setup page references this module.
//
// Relocated verbatim from the page's old inline <script> when /dial/ moved onto
// the canonical design system. Two seams changed, nothing else:
//   * jsonHeaders() now comes from the shared http.js module (was injected as
//     csrf_fetch_helpers_js); it reads the CSRF token from the <meta name=
//     "jts-csrf"> tag canonical_page() emits and attaches X-CSRF-Token to the
//     mutating ./onboard POST.
//   * the per-device Provision buttons were already data-action delegated; the
//     handler now lives on document so it survives the innerHTML re-renders.
//
// Device fields (port / VID / PID / serial / description) come off pyserial and
// are UNTRUSTED — every interpolation into innerHTML goes through escapeHtml().
// No inline onclick carries a port; the port rides in an escaped data-port
// attribute read by the one delegated click handler.

import { jsonHeaders } from "/assets/shared/js/http.js";

const statusEl = document.getElementById("status");
const devicesEl = document.getElementById("devices");
const resultEl = document.getElementById("result");

let pollTimer = null;
let lastDevices = [];
let busy = false; // true while a provision call is in flight

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// Poll the server for the current set of plugged-in ESP32-S3 devices.
// Read-only; skipped while a provision is running (the chip reset mid-flash
// would churn the list and the polling loop would fight the provision call).
async function scan() {
  if (busy) return;
  try {
    const r = await fetch("scan", { cache: "no-store" });
    const data = await r.json();
    render(data.devices || []);
  } catch (e) {
    // Transient: the next poll retries. Don't surface a scary banner for a
    // single missed poll.
    console.warn("scan failed", e);
  }
}

function render(devices) {
  // No-op if the device set is unchanged — avoids trashing a card the user is
  // hovering / mid-click on every 2 s poll.
  if (
    devices.length === lastDevices.length &&
    devices.every((d, i) => d.port === (lastDevices[i] && lastDevices[i].port))
  ) {
    return;
  }
  lastDevices = devices;
  if (devices.length === 0) {
    statusEl.innerHTML =
      '<span class="spinner"></span>Waiting for a USB device…';
    devicesEl.innerHTML = "";
    return;
  }
  statusEl.textContent = devices.length + " device(s) detected:";
  devicesEl.innerHTML = devices
    .map(
      (d) => `
    <div class="device-card">
      <div class="port">${escapeHtml(d.port)}</div>
      <div class="meta">
        VID ${escapeHtml(d.vid)} · PID ${escapeHtml(d.pid)} · Serial ${escapeHtml(d.serial || "(none)")}<br>
        ${escapeHtml(d.description)}
      </div>
      <div class="actions">
        <button class="btn btn--primary" data-action="provision"
                data-port="${escapeHtml(d.port)}" data-force="false">Provision (smart)</button>
        <button class="btn btn--ghost" data-action="provision"
                data-port="${escapeHtml(d.port)}" data-force="true">Force flash + provision</button>
      </div>
      <p class="form-hint hint">
        <strong>Smart</strong> probes the device first — if it's already
        running JTS firmware, only the WiFi creds get pushed (no flash).
        Use <strong>Force</strong> only if the device is stuck or you
        want to bring an unflashed ESP32-S3 onto JTS.
      </p>
    </div>
  `,
    )
    .join("");
}

// Run the onboard flow for one device. Pauses polling for the duration (the
// chip resets during flash), POSTs to ./onboard, then renders the result and
// resumes polling so the user can onboard another dial without reloading.
async function provision(port, force) {
  if (busy) return;
  busy = true;
  clearInterval(pollTimer);
  resultEl.innerHTML =
    '<div class="result info"><span class="spinner"></span> Provisioning ' +
    escapeHtml(port) +
    "…<br><small>This can take 30-90 seconds. Don't unplug.</small></div>";
  try {
    const r = await fetch("onboard", {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ port: port, force_flash: force }),
    });
    const data = await r.json();
    if (data.ok) {
      resultEl.innerHTML = `
        <div class="result ok">
          <strong>Done.</strong> ${escapeHtml(data.message || "Dial is online.")}
          <p style="margin: 6px 0 0;"><small>You can unplug from the Pi now and connect to USB power.</small></p>
        </div>
        ${data.log ? "<div class=\"result\"><details><summary>Show log</summary><pre>" + escapeHtml(data.log) + "</pre></details></div>" : ""}
      `;
    } else {
      resultEl.innerHTML = `
        <div class="result err">
          <strong>Failed.</strong> ${escapeHtml(data.error || "Unknown error")}
        </div>
        ${data.log ? "<div class=\"result\"><details open><summary>Log</summary><pre>" + escapeHtml(data.log) + "</pre></details></div>" : ""}
      `;
    }
  } catch (e) {
    resultEl.innerHTML =
      '<div class="result err">Request failed: ' + escapeHtml(String(e)) + "</div>";
  } finally {
    busy = false;
    pollTimer = setInterval(scan, 2000);
  }
}

// One delegated click handler for the per-device Provision buttons. The port
// rides in an escaped data-port attribute, never an inline onclick, so an
// untrusted device path can't land in generated JS.
document.addEventListener("click", (e) => {
  const btn = e.target.closest('[data-action="provision"]');
  if (!btn) return;
  provision(btn.dataset.port || "", btn.dataset.force === "true");
});

// Initial scan + start the 2 s poll loop.
scan();
pollTimer = setInterval(scan, 2000);
