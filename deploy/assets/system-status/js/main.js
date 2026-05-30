// main.js — entry point. Builds the page, wires the action handlers, and
// polls /system/snapshot. Mounts into <div id="app">.
//
// The poll loop self-schedules (setTimeout after each completes) so a slow
// response can't overlap the next tick, and it separates a transport failure
// (→ "Disconnected", body dimmed) from rendering: a render failure is isolated
// + logged per-section inside update(), so one bad field never blanks the page
// or masquerades as a disconnect.

import { buildPage, update } from "./views.js";
import { getJSON } from "./api.js";
import { postAction, setQuality, runDiagnostics } from "./actions.js";

const POLL_MS = 5000;
const root = document.getElementById("app");

let refs;
const handlers = {
  restartVoice: (e) => postAction("restart/voice", e.target,
    ["Restart jasper-voice? Wake-word will be unavailable for ~30 s."],
    { statusEl: refs.actionsStatus, sentMessage: "Restarting voice — wake will be unavailable for ~30 s." }),
  restartAudio: (e) => postAction("restart/audio", e.target,
    ["Restart the audio chain (camilla + librespot + shairport + bluez)? Music will stop momentarily."],
    { statusEl: refs.actionsStatus, sentMessage: "Restarting the audio chain — music will resume in a moment." }),
  reboot: (e) => postAction("reboot", e.target,
    ["Reboot the speaker? This takes ~60 s.",
     "Are you sure? You will lose audio for about a minute."],
    { statusEl: refs.actionsStatus,
      sentMessage: "Rebooting — this page will be unreachable for ~60 s, then it should reconnect on its own." }),
  // Stronger double-confirm than reboot: power off stays off until someone
  // physically re-plugs the cord.
  poweroff: (e) => postAction("poweroff", e.target,
    ["Power off the speaker? It will stay off until you physically re-plug power.",
     "Are you absolutely sure? You will need physical access to turn the speaker back on."],
    { statusEl: refs.actionsStatus,
      sentMessage: "Powering off — the speaker will stay off until you physically re-plug power." }),
  setQuality: (converter) => setQuality(refs, converter),
  runDiagnostics: (btn, out) => runDiagnostics(btn, out),
};
refs = buildPage(root, handlers);

async function poll() {
  let snap;
  try {
    snap = await getJSON("data.json");
  } catch (e) {
    document.body.classList.add("stale");
    refs.staleness.textContent = "Disconnected (" + e.message + "). Retrying…";
    setTimeout(poll, POLL_MS);
    return;
  }
  // Fetch succeeded: a render failure is isolated + logged per-section inside
  // update(), so it can't masquerade as a disconnect here.
  document.body.classList.remove("stale");
  update(refs, snap);
  setTimeout(poll, POLL_MS);
}

poll();
