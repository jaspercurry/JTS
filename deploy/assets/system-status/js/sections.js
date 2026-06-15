// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// sections.js — data → view builders, one per dashboard card. Each takes a
// slice of the /system/snapshot and returns DOM (or, for the in-place audio
// toggle, mutates its refs). Pure: no fetching, no polling — views.js owns
// orchestration. Status tones use the named helpers in format.js so the
// colour thresholds stay aligned with the researched Pi/Linux semantics.

import { h } from "./dom.js";
import { sparkline, cpuBars } from "./charts.js";
import {
  fmtBytes, fmtAgo, fmtEpochAgo, fmtDur, fmtMsAge, fmtRatePerHour,
  baseName, capacityPercent, fanStepInfo, FAN_STEPS,
  toneForMemoryHeadroom, loadPressureInfo, cpuUsageInfo, temperatureInfo,
  toneForDiskUse,
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
  cards.push(statCard({
    label: "Memory",
    value: Math.round(memUsed) + " / " + Math.round(memTotal) + " MB",
    sub: swap > 0
      ? Math.round(memAvail) + " MB available · " + Math.round(swap) + " MB swap"
      : Math.round(memAvail) + " MB available",
    tone: memTone,
    chart: sparkline(hist.mem_used_mb, { min: 0, max: memTotal, tone: memTone, fill: true }),
  }));

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
  const temp = cur.temp_c || 0;
  const tempF = temp * 9 / 5 + 32;
  const throttledNow = cur.throttled_now || 0;
  const throttledHist = cur.throttled_history || 0;
  const tempTone = temperatureInfo(temp, throttledNow, throttledHist).tone;
  let tempSub = temp.toFixed(1) + "°C";
  if (throttledNow) tempSub += " · throttling now";
  else if (throttledHist) tempSub += " · throttled since boot";
  const tempHist = hist.temp_c || [];
  let tempOpts = { tone: tempTone, fill: true };
  if (tempHist.length) {
    const tMin = Math.min(...tempHist), tMax = Math.max(...tempHist);
    const pad = Math.max(2, (tMax - tMin) * 0.15);
    tempOpts = { ...tempOpts, min: Math.max(0, tMin - pad), max: tMax + pad };
  }
  cards.push(statCard({
    label: "Temperature", value: tempF.toFixed(0) + "°F", sub: tempSub,
    tone: tempTone, chart: sparkline(tempHist, tempOpts),
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
  if (!ha.configured) {
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

// ---- airplay + outputd ---------------------------------------------------

function summarizeAnomalies(s) {
  if (!s) return "—";
  const parts = [];
  if (s.shairport_packet_drops) parts.push(s.shairport_packet_drops + " packet drops");
  if (s.shairport_sync_errors) parts.push(s.shairport_sync_errors + " sync corrections");
  if (s.shairport_underruns) parts.push(s.shairport_underruns + " shairport underruns");
  // shairport_events buckets out-of-sequence / broken-pipe / offset-too-short
  // (bonded-leader lip-sync) — surfaced here so those counts are not invisible.
  if (s.shairport_events) parts.push(s.shairport_events + " shairport events");
  if (s.fanin_airplay_xruns) parts.push(s.fanin_airplay_xruns + " AirPlay fan-in xruns");
  if (s.fanin_output_xruns) parts.push(s.fanin_output_xruns + " output xruns");
  if (s.camilla_short_reads) parts.push(s.camilla_short_reads + " Camilla short reads");
  if (s.camilla_playback_underruns) parts.push(s.camilla_playback_underruns + " Camilla underruns");
  return parts.length ? parts.join(" · ") : "clean";
}

function outputdLine(o, services) {
  if (!o) return "unavailable";
  const content = o.content || {}, bridge = o.content_bridge || {}, dac = o.dac || {}, mix = o.mix || {}, tts = o.tts || {};
  const parts = [
    o.backend || "unknown",
    "content/DAC buffer " + (content.buffer_frames || "—") + "/" + (dac.buffer_frames || "—"),
    "content/DAC xruns " + (content.xrun_count || 0) + "/" + (dac.xrun_count || 0),
    "content empty " + (content.empty_periods || 0),
    "content EAGAIN " + (content.eagain_count || 0),
    "tts " + (tts.pending_frames || 0) + "f",
  ];
  if (bridge.enabled) {
    parts.push(
      "bridge " + (bridge.locked ? "locked" : "unlocked") +
      " fill " + (bridge.fill_frames ?? "—") + "/" + (bridge.target_fill_frames ?? "—") + "f"
    );
    if (bridge.ratio_ppm != null) parts.push("bridge ratio " + Number(bridge.ratio_ppm).toFixed(2) + " ppm");
    const bridgeIssues = [];
    if (bridge.underrun_frames) bridgeIssues.push("underrun " + bridge.underrun_frames + "f");
    if (bridge.overrun_frames) bridgeIssues.push("overrun " + bridge.overrun_frames + "f");
    if (bridge.resync_count) bridgeIssues.push("resync " + bridge.resync_count);
    if (bridge.reset_count) bridgeIssues.push("reset " + bridge.reset_count);
    if (bridge.ratio_clamp_count) bridgeIssues.push("clamp " + bridge.ratio_clamp_count);
    if (bridgeIssues.length) parts.push("bridge " + bridgeIssues.join("/"));
  }
  if ((content.xrun_count || 0) > 0) {
    parts.push("last content xrun " + fmtMsAge(content.last_xrun_age_ms));
    if (content.xrun_rate_per_hour != null) parts.push("content xrun rate " + fmtRatePerHour(content.xrun_rate_per_hour));
  }
  if ((dac.xrun_count || 0) > 0) {
    parts.push("last DAC xrun " + fmtMsAge(dac.last_xrun_age_ms));
    if (dac.xrun_rate_per_hour != null) parts.push("DAC xrun rate " + fmtRatePerHour(dac.xrun_rate_per_hour));
  }
  const svc = (services || []).find((s) => s && (s.name === "jasper-outputd" || s.unit === "jasper-outputd.service"));
  if (svc && svc.memory_mb != null) parts.push("mem " + Math.round(svc.memory_mb) + " MB");
  if (tts.over_budget || tts.over_budget_ms) parts.push("tts over " + (tts.over_budget_ms || 0) + "ms");
  if (tts.dropped_commands || tts.dropped_audio_frames) {
    parts.push("tts dropped " + (tts.dropped_commands || 0) + " cmds/" + (tts.dropped_audio_frames || 0) + "f");
  }
  if (mix.last_period_clipped_samples) parts.push("clip " + mix.last_period_clipped_samples);
  return parts.join(" · ");
}

const AP_STATUS_TONE = { ok: "ok", watch: "warn", issue: "danger", inactive: "idle", unknown: "warn" };

export function airplayBody(hp, outputd, services) {
  if (!hp) {
    return [
      h("div.badge-row", null, badge("Unknown", "idle"), h("span", null, "sampler unavailable")),
      defList([["Now", "—"], ["Last 5m", "—"], ["Last 30m", "—"],
        ["Fan-in", "—"], ["Outputd", outputdLine(outputd, services)], ["Camilla", "—"]]),
      h("p.info-card__hint", null, "No recent AirPlay events."),
    ];
  }
  let status = hp.status || "unknown";
  if (!AP_STATUS_TONE[status]) status = "unknown";

  const cur = hp.current || {};
  const fanin = cur.fanin || {}, airplay = fanin.airplay || {}, output = fanin.output || {};
  const rate = airplay.frames_per_sec;
  const mpris = cur.mpris || {};
  // The airplay lane free-runs at ~48 kHz of SILENCE whenever the pipeline
  // is up, so the raw rate only means "streaming" while shairport is
  // actually playing (mpris). At idle, show "idle" — not a phantom rate
  // (the 2026-06-22 "why are there frames with nothing playing?" report).
  const now = mpris.playing === true
    ? ((rate != null && rate >= 1000)
        ? Math.round(rate).toLocaleString() + " frames/s"
        : "playing · no fan-in frames")
    : (mpris.playing === false ? "idle" : "—");

  const fanText = fanin.available
    ? "input " + (fanin.input_buffer_frames || "—") +
      " / output " + (output.buffer_frames || fanin.output_buffer_frames || "—") +
      " frames · AirPlay/output xruns " + (airplay.xrun_count || 0) + "/" + (output.xrun_count || 0)
    : "unavailable";

  const camilla = cur.camilla || null;
  let camillaText;
  if (camilla) {
    const cp = [
      "buffer " + (camilla.buffer_level || 0),
      "rate " + (camilla.rate_adjust == null ? "—" : Number(camilla.rate_adjust).toFixed(6)),
    ];
    if (camilla.target_level || camilla.chunksize) {
      cp.push("target/chunk " + (camilla.target_level || "—") + "/" + (camilla.chunksize || "—"));
    }
    if (camilla.config_path) cp.push("config " + baseName(camilla.config_path));
    camillaText = cp.join(" · ");
  } else {
    camillaText = "journal only";
  }

  const out = [
    h("div.badge-row", null, badge(status, AP_STATUS_TONE[status]), h("span", null, hp.reason || "")),
    defList([
      ["Now", now],
      ["Last 5m", summarizeAnomalies(hp.summary_5m)],
      ["Last 30m", summarizeAnomalies(hp.summary_30m)],
      ["Fan-in", fanText],
      ["Outputd", outputdLine(outputd, services)],
      ["Camilla", camillaText],
    ]),
  ];

  const events = (hp.events || []).slice(-5).reverse();
  if (events.length) {
    out.push(h("div.ap-events", null, events.map((ev) => {
      const sev = ["watch", "issue"].includes(ev.severity) ? ev.severity : "watch";
      return h("div.ap-event." + sev, null,
        h("strong", null, ev.title || ev.type || "event"), " ",
        h("span.muted", null, ev.detail || ""), " ",
        h("span.when", null, fmtEpochAgo(ev.ts)));
    })));
  } else {
    out.push(h("p.info-card__hint", null, "No recent AirPlay events."));
  }
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
