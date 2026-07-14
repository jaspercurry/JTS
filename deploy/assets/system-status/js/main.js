// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — entry point. Builds the shared page chrome, switches the two
// Status views in-document, and polls /system/snapshot once for the active
// view. Mounts into <div id="app">.
//
// The poll loop self-schedules (setTimeout after each completes) so a slow
// response can't overlap the next tick, and it separates a transport failure
// (→ "Disconnected", body dimmed) from rendering: a render failure is isolated
// + logged per-section inside update(), so one bad field never blanks the page
// or masquerades as a disconnect.

import { buildSystemPanel, update } from "./views.js";
import { buildAudioPanel, updateAudio } from "./audio-view.js";
import { header } from "./components.js";
import { getJSON } from "./api.js";
import { postAction, setQuality, runDiagnostics } from "./actions.js";

const POLL_MS = 5000;
const root = document.getElementById("app");
const initialView = root.dataset.view === "audio" ? "audio" : "system";

let activeView = null;
let activeEntry = null;
let latestSnapshot = null;
let disconnectMessage = null;
const entries = {};

const handlers = {
  restartVoice: (e) => postAction("restart/voice", e.target,
    ["Restart jasper-voice? Wake-word will be unavailable for ~30 s."],
    { statusEl: activeEntry.refs.actionsStatus, sentMessage: "Restarting voice — wake will be unavailable for ~30 s." }),
  restartAudio: (e) => postAction("restart/audio", e.target,
    ["Restart the audio chain (camilla + librespot + shairport + bluez)? Music will stop momentarily."],
    { statusEl: activeEntry.refs.actionsStatus, sentMessage: "Restarting the audio chain — music will resume in a moment." }),
  reboot: (e) => postAction("reboot", e.target,
    ["Reboot the speaker? This takes ~60 s.",
     "Are you sure? You will lose audio for about a minute."],
    { statusEl: activeEntry.refs.actionsStatus, danger: true,
      sentMessage: "Rebooting — this page will be unreachable for ~60 s, then it should reconnect on its own." }),
  // Stronger double-confirm than reboot: power off stays off until someone
  // physically re-plugs the cord.
  poweroff: (e) => postAction("poweroff", e.target,
    ["Power off the speaker? It will stay off until you physically re-plug power.",
     "Are you absolutely sure? You will need physical access to turn the speaker back on."],
    { statusEl: activeEntry.refs.actionsStatus, danger: true,
      sentMessage: "Powering off — the speaker will stay off until you physically re-plug power." }),
  setQuality: (converter) => setQuality(activeEntry.refs, converter, (quality) => {
    latestSnapshot = { ...(latestSnapshot || {}), audio_quality: quality };
  }),
  runDiagnostics: (btn, out) => runDiagnostics(btn, out),
};

function viewFromPath() {
  return location.pathname.replace(/\/+$/, "") === "/system/audio" ? "audio" : "system";
}

function entryFor(view) {
  if (entries[view]) return entries[view];
  const built = view === "audio"
    ? buildAudioPanel(handlers)
    : buildSystemPanel(handlers);
  const entry = {
    panel: built.panel,
    refs: built.refs,
    update: view === "audio" ? updateAudio : update,
  };
  entry.panel.hidden = true;
  root.append(entry.panel);
  entries[view] = entry;
  return entry;
}

function applyCurrentState() {
  if (!activeEntry) return;
  if (latestSnapshot) updateEntry(activeEntry, latestSnapshot);
  if (disconnectMessage) {
    document.body.classList.add("stale");
    activeEntry.refs.staleness.textContent = disconnectMessage;
  } else {
    document.body.classList.remove("stale");
  }
}

function updateEntry(entry, snapshot) {
  try {
    entry.update(entry.refs, snapshot);
  } catch (e) {
    console.error("status: page update failed", e);
    entry.refs.staleness.textContent = "Dashboard data was incomplete. Retrying…";
  }
}

function activateView(view, { push = false, announce = false, scroll = false } = {}) {
  const next = view === "audio" ? "audio" : "system";
  if (next === activeView) return;
  if (activeEntry) activeEntry.panel.hidden = true;
  activeView = next;
  activeEntry = entryFor(next);
  activeEntry.panel.hidden = false;
  chrome.setActive(next, { announce });
  applyCurrentState();
  root.setAttribute("aria-busy", "false");
  if (push) {
    history.pushState({ statusView: next }, "", next === "audio" ? "/system/audio/" : "/system/");
  }
  if (scroll) window.scrollTo(0, 0);
}

function onViewClick(view, event) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey ||
      event.shiftKey || event.altKey) return;
  event.preventDefault();
  activateView(view, { push: view !== activeView, announce: view !== activeView, scroll: view !== activeView });
}

const chrome = header({ title: "Status", backHref: "/", activeView: initialView, onViewClick });
root.replaceChildren(chrome.el);
activateView(initialView);

window.addEventListener("popstate", () => {
  activateView(viewFromPath(), { announce: true });
});

async function poll() {
  let snap;
  try {
    snap = await getJSON("/system/data.json");
  } catch (e) {
    disconnectMessage = "Disconnected (" + e.message + "). Retrying…";
    applyCurrentState();
    setTimeout(poll, POLL_MS);
    return;
  }
  // Fetch succeeded: a render failure is isolated + logged per-section inside
  // update(), so it can't masquerade as a disconnect here.
  latestSnapshot = snap;
  disconnectMessage = null;
  document.body.classList.remove("stale");
  try {
    updateEntry(activeEntry, snap);
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

poll();
