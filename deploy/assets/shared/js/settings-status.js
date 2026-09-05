// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// settings-status.js — what a settings surface needs: capability gating for
// its `[data-requires]` rows and live `status-*` sublabels. Shared by the
// landing page and the area hubs; a page's own controls stay in that page's
// module (deploy/assets/landing/js/main.js).
//
// Every gated row ships `hidden`, so gating only ever reveals — a page is
// correct at first paint even with every backend daemon down.

import { getJSON, startPolling } from "/assets/shared/js/http.js";

// One snapshot serves every row; startPolling backs a hidden tab off further.
const POLL_MS = 20000;

export function setStatusText(id, value) {
  const el = document.getElementById(id);
  if (el && value != null && value !== "") el.textContent = value;
}

function providerName(provider) {
  if (provider === "openai") return "OpenAI";
  if (provider === "grok") return "Grok";
  if (provider === "gemini") return "Gemini";
  return provider || "Provider";
}

function homeAssistantStatus(ha) {
  if (!ha.configured) return "Not connected";
  return ha.connected ? ha.instance_name || "Connected" : "Unreachable";
}

function metricsSummary(cur) {
  const cores = cur.per_core_cpu_pct || [];
  const mean = cores.length
    ? cores.reduce((sum, pct) => sum + pct, 0) / cores.length
    : null;
  return [
    mean != null ? Math.round(mean) + "% CPU" : null,
    cur.temp_c != null ? Math.round(cur.temp_c) + " C" : null,
    cur.disk_used_pct != null ? Math.round(cur.disk_used_pct) + "% disk" : null,
  ].filter(Boolean).join(" · ");
}

// Live values only, never layout: a slow or failed snapshot cannot blank or
// restyle a page whose gating is already settled.
function renderSnapshot(snap, titleFollowsSpeakerName) {
  if (!snap) return;
  const name = snap.speaker_name && snap.speaker_name.name;
  if (name) {
    setStatusText("status-speaker-name", name);
    // Only the landing page IS the speaker; a hub's title is its own name
    // (docs/web-ia.md §2), so this is opt-in.
    if (titleFollowsSpeakerName) document.title = name;
  }
  if (snap.voice_provider) {
    setStatusText("status-voice", providerName(snap.voice_provider));
  }
  if (snap.home_assistant) {
    setStatusText("status-ha", homeAssistantStatus(snap.home_assistant));
  }
  const build = snap.build || {};
  const sha = build.JASPER_GIT_SHA;
  const short = !sha || sha === "unknown" ? "" : String(sha).slice(0, 7);
  setStatusText(
    "status-software",
    [short, build.JASPER_GIT_BRANCH || ""].filter(Boolean).join(" · "),
  );
  const cur = (snap.metrics && snap.metrics.current) || null;
  if (cur) {
    const summary = metricsSummary(cur);
    setStatusText("status-system", summary);
    setStatusText("system-summary", summary);
  }
}

// Gate on `caps` synchronously — before any fetch — then keep the sublabels
// current. Returns startPolling's stop().
export function initSettingsStatus({ caps, titleFollowsSpeakerName } = {}) {
  document.querySelectorAll("[data-requires]").forEach((el) => {
    const required = el.getAttribute("data-requires");
    if (required) el.hidden = !caps || caps[required] !== true;
  });
  return startPolling(
    async () => renderSnapshot(
      await getJSON("/system/data.json"), titleFollowsSpeakerName,
    ),
    { intervalMs: POLL_MS },
  );
}
