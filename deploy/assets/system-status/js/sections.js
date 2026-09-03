// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// sections.js — data → view builders, one per dashboard card. Each takes a
// slice of the /system/snapshot and returns DOM (or, for the in-place audio
// toggle, mutates its refs). Pure: no fetching, no polling — views.js owns
// orchestration. Status tones use the named helpers in format.js so the
// colour thresholds stay aligned with the researched Pi/Linux semantics.

import { h } from "/assets/shared/js/dom.js";
import { sparkline, cpuBars } from "./charts.js";
import {
  fmtBytes, fmtAgo, fmtDur, capacityPercent, fanStepInfo, FAN_STEPS,
  toneForMemoryHeadroom, memoryPressureInfo, loadPressureInfo, cpuUsageInfo,
  temperatureDisplay, toneForDiskUse,
} from "./format.js";
import { statCard, defList, badge } from "./components.js";

export const AUDIO_OPTIONS = [
  {
    converter: "samplerate_medium", label: "Medium",
    body: "Lower CPU; expected to sound the same for normal listening.",
  },
  {
    converter: "samplerate_best", label: "Best",
    body: "Highest ultrasonic-band fidelity; higher CPU and hardware-sensitive.",
  },
];

export function audioQualityLabel(converter) {
  if (converter === "samplerate_best") return "Best";
  if (converter === "samplerate_medium") return "Medium";
  return converter || "—";
}

// Placeholder shown in a metrics-dependent section before the first sample.
export function waitingNote(spanGrid) {
  const p = h("p.info-card__note", null, "Waiting for the first sample…");
  if (spanGrid) p.style.setProperty("grid-column", "1 / -1");
  return p;
}

// ---- transport parks -----------------------------------------------------

// Owner ruling 2026-08-27: no banner anywhere — every transport park names
// itself here, on the operator's own screen. Every FACT below (the class
// token, its tracked issue, its one-line reason, its remedy) is read from
// `/system/snapshot.transport_park`, the same verdict `/state.resilience
// .transport_park` and jasper-doctor read, so this card cannot name a park
// the other two surfaces do not. Only the framing sentence per status is
// written here.
//
// Class labels are DERIVED from the token rather than mapped, so a fifth
// class added in jasper/control/transport_park.py appears here with no JS
// edit and no second vocabulary to drift.
// Exported so the harness can pin status -> entry SELECTION by identity
// instead of matching the copy, which would make every reword a test edit.
export const PARK_HEADLINE = {
  parked: "No ring serves this box, so it emits nothing.",
  unclassified: "This box's saved layout resolves no ring geometry, and no named park describes it yet.",
  unavailable: "The saved layout or the output daemon's settings could not be read, so a park cannot be ruled out.",
};

// `converge_refused` is not a park — the box is ring-eligible and every
// status above reads clean — so it gets its own headline and its own row
// rather than being folded in with the named classes.
export const CONVERGE_HEADLINE =
  "The ring can serve this box, but its program was never moved onto the ring.";

// null while the transport is fine, so views.js can hide the whole card
// rather than showing a permanently-green one nobody reads. Doubles as the
// render memo key.
export function transportParkCard(state) {
  const park = state && typeof state === "object" ? state : {};
  const status = String(park.status || "");
  const rows = (Array.isArray(park.parks) ? park.parks : []).flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const label = String(entry.park_class || "").replaceAll("_", " ");
    const parts = [
      String(entry.detail || ""),
      entry.issue ? "Tracked: " + entry.issue : "",
      entry.remedy ? "Clear it with: " + entry.remedy : "",
    ].filter(Boolean);
    if (!label && !parts.length) return [];
    return [[label || "transport park", parts.join(" · ")]];
  });
  if (park.converge_refused) {
    rows.push(["ring converge refused", String(park.converge_refused)]);
  }
  if (park.error) rows.push(["read failed", String(park.error)]);
  // hasOwn, not a bare index: `status` is payload data, and a value like
  // "constructor" reaches Object.prototype and reads as a truthy headline.
  const headline = Object.hasOwn(PARK_HEADLINE, status)
    ? PARK_HEADLINE[status]
    : (rows.length ? CONVERGE_HEADLINE : "");
  if (!headline) return null;
  return { status, headline, rows };
}

// An ARRAY, not a wrapper: `.info-card > * + *` owns the spacing between a
// card's blocks, so a div around these would collapse the gap to zero.
export function transportParkBody(card) {
  return [
    h("p.info-card__note", null, card.headline),
    card.rows.length ? defList(card.rows, "parks") : null,
  ].filter(Boolean);
}

// ---- vitals --------------------------------------------------------------

export function vitalsCards(cur, hist, cores) {
  const cards = [];

  // Memory
  const memAvailHist = hist.mem_available_mb || [];
  const memAvail = memAvailHist[memAvailHist.length - 1] || 0;
  const memTotal = cur.mem_total_mb || 1;
  const memUsed = memTotal - memAvail;
  const swapHist = hist.swap_used_mb || [];
  const swap = swapHist[swapHist.length - 1] || 0;
  const memTone = toneForMemoryHeadroom(memAvail, memTotal);
  const memSub = [Math.round(memAvail) + " MB available"];
  const memCg = cur.memory_cgroup || null;
  if (memCg && memCg.total_mb != null) {
    memSub.push(
      "cgroup " + Math.round(memCg.total_mb) + " MB" +
      " (anon " + Math.round(memCg.anon_mb || 0) +
      " · file " + Math.round(memCg.file_mb || 0) +
      " · kernel " + Math.round(memCg.kernel_mb || 0) +
      " · other " + Math.round(memCg.other_mb || 0) + ")"
    );
  }
  if (swap > 0) memSub.push(Math.round(swap) + " MB swap");
  cards.push(statCard({
    label: "Memory",
    value: Math.round(memUsed) + " / " + Math.round(memTotal) + " MB",
    sub: memSub.join(" · "),
    tone: memTone,
    chart: sparkline(hist.mem_used_mb, { min: 0, max: memTotal, tone: memTone, fill: true }),
  }));

  // Memory pressure — kernel PSI + the boot's OOM-kill count. Both fields are
  // omitted by the sampler on a kernel that publishes neither, so the tile
  // only appears where at least one number is real.
  if (cur.mem_psi_some_avg60 != null || cur.oom_kill != null) {
    const psiInfo = memoryPressureInfo(cur.mem_psi_some_avg60, cur.oom_kill);
    cards.push(statCard({
      label: "Memory pressure",
      value: psiInfo.value,
      sub: psiInfo.sub,
      tone: psiInfo.tone,
      chart: psiInfo.killed ? badge("OOM killed", "danger") : null,
    }));
  }

  // Load pressure
  const loadHist = hist.load_1m || [];
  const load = loadHist[loadHist.length - 1] || 0;
  const loadInfo = loadPressureInfo(load, cores.length || 4);
  const loadCapacity = loadInfo.capacity;
  cards.push(statCard({
    label: "Load pressure",
    value: load.toFixed(2) + " / " + loadCapacity.toFixed(1),
    sub: loadInfo.label,
    tone: loadInfo.tone,
    chart: sparkline(loadHist, { min: 0, max: Math.max(loadCapacity, ...loadHist), tone: loadInfo.tone, fill: true }),
  }));

  // CPU usage (per-core bars)
  const cpuInfo = cpuUsageInfo(cores);
  cards.push(statCard({
    label: "CPU usage", value: cpuInfo.value, tone: cpuInfo.tone, chart: cpuBars(cores),
  }));

  // Temperature
  const throttledNow = cur.throttled_now || 0;
  const throttledHist = cur.throttled_history || 0;
  const tempDisplay = temperatureDisplay(cur.temp_c, throttledNow, throttledHist);
  const tempHist = hist.temp_c || [];
  let tempOpts = { tone: tempDisplay.tone, fill: true };
  if (tempDisplay.chartable && tempHist.length) {
    const tMin = Math.min(...tempHist), tMax = Math.max(...tempHist);
    const pad = Math.max(2, (tMax - tMin) * 0.15);
    tempOpts = { ...tempOpts, min: Math.max(0, tMin - pad), max: tMax + pad };
  }
  cards.push(statCard({
    label: "Temperature", value: tempDisplay.value, sub: tempDisplay.sub,
    tone: tempDisplay.tone,
    chart: tempDisplay.chartable ? sparkline(tempHist, tempOpts) : null,
  }));

  // Fan — only on hardware with a pwm-fan device.
  if (cur.fan_present && cur.fan_rpm != null) {
    const rpm = cur.fan_rpm;
    const step = fanStepInfo(cur.fan_pwm || 0);
    const fanTone = step.index >= FAN_STEPS.length - 1 ? "warn" : "ok";
    cards.push(statCard({
      label: "Fan", value: step.label, sub: rpm > 0 ? rpm + " RPM" : "",
      tone: fanTone,
      chart: sparkline(hist.fan_rpm, { min: 0, max: Math.max(1000, ...(hist.fan_rpm || [])), tone: fanTone }),
    }));
  }

  // Disk
  const diskPct = cur.disk_used_pct || 0;
  const diskTotal = cur.disk_total_gb || 0;
  const diskTone = toneForDiskUse(diskPct);
  cards.push(statCard({
    label: "Disk", value: diskPct.toFixed(1) + "%", sub: "of " + diskTotal.toFixed(0) + " GB",
    tone: diskTone,
    chart: diskTone === "danger" ? badge("Full", "danger")
      : (diskTone === "warn" ? badge("High", "warn") : null),
  }));

  return cards;
}

// ---- software ------------------------------------------------------------

export function softwareList(snap, cur) {
  const build = snap.build || {};
  return defList([
    ["Version", build.JASPER_GIT_SHA || "unknown"],
    ["Branch", build.JASPER_GIT_BRANCH || "unknown"],
    ["Installed", build.JASPER_INSTALL_AT ? fmtAgo(build.JASPER_INSTALL_AT) : "unknown"],
    ["Uptime", fmtDur(cur.uptime_sec)],
    ["Voice provider", snap.voice_provider || "—"],
  ]);
}

// ---- home assistant ------------------------------------------------------

export function haBody(ha) {
  ha = ha || { configured: false };
  let statusNode, url = "—", version = "—", detail = "";
  if (ha.checking) {
    statusNode = badge("Checking", "idle");
    url = ha.url || "—";
    version = ha.instance_name
      ? (ha.instance_name + (ha.version ? " (" + ha.version + ")" : ""))
      : "—";
    if (ha.stale) detail = "Refreshing Home Assistant status.";
  } else if (ha.stale && ha.error) {
    statusNode = badge("Refresh failed", "warn");
    url = ha.url || "—";
    version = ha.instance_name
      ? (ha.instance_name + (ha.version ? " (" + ha.version + ")" : ""))
      : "—";
    detail = ha.error;
  } else if (!ha.configured) {
    statusNode = "Not configured";
  } else if (ha.connected) {
    statusNode = badge("Connected", "ok");
    url = ha.url || "—";
    version = (ha.instance_name || "Home Assistant") + (ha.version ? " (" + ha.version + ")" : "");
  } else {
    statusNode = badge("Unreachable", "danger");
    url = ha.url || "—";
    detail = ha.error || "Connection failed.";
  }
  const out = [defList([["Status", statusNode], ["URL", url], ["Version", version]])];
  if (detail) out.push(h("p.info-card__note", null, detail));
  out.push(h("p.info-card__note", null,
    "Configure at ", h("a.link", { href: "/ha/" }, "jts.local/ha"), "."));
  return out;
}

// ---- audio conversion (in-place update of the persistent buttons) --------

export function updateAudioQuality(aq, q) {
  const requested = q && q.converter;
  const active = q && q.active_converter;
  aq.requested.textContent = audioQualityLabel(requested);
  aq.active.textContent = active ? audioQualityLabel(active) : "unknown";
  if (q && q.error) aq.status.textContent = "State warning: " + q.error;
  else if (q && q.summary) aq.status.textContent = q.summary;
  // (else leave whatever transient "Applying…/Failed" text the action set.)
  aq.buttons.forEach((b) => {
    b.el.setAttribute("aria-pressed", b.converter === requested ? "true" : "false");
    if (!b.el.dataset.applying) b.el.disabled = false;
  });
}

// ---- USB latency (in-place update of the persistent buttons) ------------

function usbLatencyLabel(mode) {
  return { low: "Low", medium: "Medium", high: "High" }[mode] || mode || "unknown";
}

function effectiveUsbLatencyLabel(state) {
  const mode = state && state.effective_mode;
  if (state && state.state === "idle") return "Not active";
  if (state && state.state === "starting") return "Starting";
  if (state && state.state === "fallback") {
    return mode ? usbLatencyLabel(mode) + " · stable fallback" : "Stable fallback";
  }
  if (state && state.state === "recovery" && !mode) return "Adjusting";
  return usbLatencyLabel(mode);
}

export function updateUsbLatency(refs, state) {
  const selected = state && state.selected_mode;
  const effective = state && state.effective_mode;
  const requestApplying = refs.buttons.some((button) => button.el.dataset.applying);
  refs.preference.textContent = usbLatencyLabel(selected);
  refs.effective.textContent = effectiveUsbLatencyLabel(state);
  refs.live.textContent = state && Number.isFinite(state.live_buffer_ms)
    ? state.live_buffer_ms.toFixed(1) + " ms" : "unknown";
  if (!requestApplying) {
    if (state && state.error) refs.status.textContent = "Could not apply: " + state.error;
    else if (state && state.detail) refs.status.textContent = state.detail;
  }
  refs.buttons.forEach((button) => {
    button.el.setAttribute("aria-pressed", button.mode === effective ? "true" : "false");
    if (!button.el.dataset.applying) button.el.disabled = false;
  });
}

// ---- network -------------------------------------------------------------

export function networkList(cur) {
  const tn = cur.throttled_now || 0, th = cur.throttled_history || 0;
  return defList([
    ["Total RX since boot", fmtBytes(cur.net_rx_bytes)],
    ["Total TX since boot", fmtBytes(cur.net_tx_bytes)],
    ["Throttle bits", (tn || th)
      ? "0x" + (tn || 0).toString(16) + " (since-boot 0x" + (th || 0).toString(16) + ")"
      : "0x0 (healthy)"],
  ]);
}

// ---- per-service usage ---------------------------------------------------

export function servicesTable(m, services) {
  const wrap = h("div");
  if (m.current && m.current.memory_cgroup_enabled === false) {
    wrap.append(h("p.info-card__hint", null,
      "Memory unavailable: the running kernel was booted with " +
      "cgroup_disable=memory (Pi 5 default), so per-service memory reads " +
      "are off. install.sh has added cgroup_enable=memory; reboot to apply."));
  }
  if (!services.length) {
    wrap.append(h("p.info-card__note", null,
      "No tracked service cgroups visible (cgroup-v2 unavailable, or dev env)."));
    return wrap;
  }

  function serviceSeverity(s) {
    const active = s.active_state || "";
    const result = s.result || "";
    if (active === "failed") return 4;
    if (result && result !== "success") return 3;
    if ((s.n_restarts || 0) > 0) return 2;
    if (active && !["active", "inactive"].includes(active)) return 1;
    return 0;
  }

  function stateCell(s) {
    const active = s.active_state || (s.cgroup ? "active" : "unknown");
    let tone = "idle";
    if (active === "active") tone = "ok";
    else if (active === "failed") tone = "danger";
    else if (["activating", "deactivating", "reloading"].includes(active)) tone = "warn";
    const parts = [];
    if (s.sub_state && s.sub_state !== active) parts.push(s.sub_state);
    if (s.result && s.result !== "success") parts.push(s.result);
    return h("div", null,
      badge(active, tone),
      parts.length ? h("p.service-group", null, parts.join(" · ")) : null);
  }

  const sorted = services.slice().sort((a, b) => {
    const sevDelta = serviceSeverity(b) - serviceSeverity(a);
    if (sevDelta) return sevDelta;
    return (b.cpu_pct == null ? -1 : b.cpu_pct) - (a.cpu_pct == null ? -1 : a.cpu_pct);
  });

  const bodyRows = sorted.map((s) => h("tr", null,
    h("td", null,
      h("p.service-name", null, s.name),
      h("p.service-group", null, s.group || "Service")),
    h("td", null, stateCell(s)),
    h("td.num", null, s.n_restarts == null ? "—" : String(s.n_restarts)),
    h("td.num", null, s.cpu_pct == null ? "—" : s.cpu_pct.toFixed(1) + "%"),
    h("td.num", null, s.memory_mb == null ? "—" : Math.round(s.memory_mb) + " MB"),
  ));

  const shownCpu = sorted.reduce((a, s) => a + (s.cpu_pct == null ? 0 : s.cpu_pct), 0);
  const shownMem = sorted.reduce((a, s) => a + (s.memory_mb == null ? 0 : s.memory_mb), 0);
  const anyMem = sorted.some((s) => s.memory_mb != null);
  const cores = (m.current && m.current.per_core_cpu_pct) || [];
  let totalsRow;
  if (cores.length) {
    const systemCpu = cores.reduce((a, b) => a + b, 0);
    const maxScale = cores.length * 100;
    const headroom = Math.max(0, maxScale - systemCpu);
    const unshown = Math.max(0, systemCpu - shownCpu);
    totalsRow = h("tr.totals", null,
      h("td", null, "System total · shown / unshown / free"),
      h("td", null, ""),
      h("td.num", null, ""),
      h("td.num", null, Math.round(capacityPercent(systemCpu, cores.length)) + "% (" +
        Math.round(shownCpu) + " + " + Math.round(unshown) + " + " + Math.round(headroom) + " / " + maxScale + "%)"),
      h("td.num", null, anyMem ? Math.round(shownMem) + " MB" : "—"));
  } else {
    totalsRow = h("tr.totals", null,
      h("td", null, "Shown subtotal"),
      h("td", null, ""),
      h("td.num", null, ""),
      h("td.num", null, Math.round(shownCpu) + "%"),
      h("td.num", null, anyMem ? Math.round(shownMem) + " MB" : "—"));
  }

  wrap.append(h("div.table-wrap", null,
    h("table.table.table--services", null,
      h("thead", null, h("tr", null,
        h("th", null, "Service"), h("th", null, "State"),
        h("th.num", null, "Restarts"), h("th.num", null, "CPU"),
        h("th.num", null, "Mem"))),
      h("tbody", null, ...bodyRows, totalsRow))));
  return wrap;
}
