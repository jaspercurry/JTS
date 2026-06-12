// main.js — /sources/ playback-source on/off toggles.
//
// The page server-renders four toggles (AirPlay, Bluetooth, Spotify Connect,
// USB Audio Input) with ids t-<source>. This module wires each to the same
// backend the legacy inline script did, behaviour-for-behaviour:
//
//   * Optimistic UI — flip the checkbox immediately, POST ./set {source,
//     enabled}, then reconcile from the JSON response (roll back on failure).
//   * Poll ./state every 4 s while the tab is visible, so an external
//     `systemctl stop shairport-sync` from SSH shows up without a reload. A
//     short ignore-window after a POST keeps a racing poll from reverting the
//     value we just set.
//   * Bluetooth guard — before turning BT off while a wireless remote (volume
//     knob, etc.) is paired, confirm via the shared <dialog> helper. The
//     remote silently stops working until BT is back on, so this is a
//     destructive action (danger:true).
//
// CSRF rides in the <meta name="jts-csrf"> tag; jsonHeaders() (shared
// /assets/shared/js/http.js) reads it lazily so this cached module carries no
// secret. Confirm uses jtsConfirm (shared /assets/shared/js/dialog.js), never
// the native popup, which the browser can suppress.

import { jsonHeaders } from "/assets/shared/js/http.js";
import { jtsConfirm } from "/assets/shared/js/dialog.js";

const POLL_MS = 4000;
const SOURCES = ["airplay", "bluetooth", "spotify_connect", "usbsink"];
const dirty = {};
let ignorePollUntil = 0;
let latestState = {};

const el = (id) => document.getElementById(id);

function applyState(state) {
  latestState = state;
  // Dumb-follower profile: every source is parked while this speaker
  // is a bonded follower — toggles read disabled and the pair note
  // explains (POST /set 409s server-side regardless).
  const parked = !!(state.pair && state.pair.parked);
  if (el("pair-note")) el("pair-note").style.display = parked ? "" : "none";
  for (const name of SOURCES) {
    const s = state[name] || {};
    const input = el("t-" + name);
    if (!input) continue;
    if (dirty[name]) continue; // user toggled mid-flight; don't clobber
    input.checked = !!s.enabled;
    input.disabled = parked || s.available === false;
  }
  const btUnavailable =
    state.bluetooth && state.bluetooth.available === false;
  if (el("bt-note")) el("bt-note").style.display = btUnavailable ? "" : "none";
  // USB sink shows a "needs reboot" note when the dtoverlay is missing
  // (install.sh hasn't been run with the gadget section, or it's been
  // manually removed).
  const usbUnavailable = state.usbsink && state.usbsink.available === false;
  if (el("usbsink-note")) {
    el("usbsink-note").style.display = usbUnavailable ? "none" : "";
  }
  if (el("usbsink-unavailable-note")) {
    el("usbsink-unavailable-note").style.display = usbUnavailable ? "" : "none";
  }
}

async function fetchState() {
  if (document.visibilityState === "hidden") return;
  if (Date.now() < ignorePollUntil) return;
  try {
    const resp = await fetch("./state", { cache: "no-store" });
    if (resp.ok) applyState(await resp.json());
  } catch (_) {
    // Transient — the next poll retries. Toggles keep their last state.
  }
}

async function postToggle(name, want) {
  // Optimistic flip already happened on change. Mark dirty so polls don't
  // overwrite while we wait for the server, and pause polling briefly so a
  // poll fired right before this POST doesn't reconcile back to the old value.
  dirty[name] = true;
  ignorePollUntil = Date.now() + 1500;
  try {
    const resp = await fetch("./set", {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ source: name, enabled: want }),
    });
    if (resp.ok) {
      const state = await resp.json();
      dirty[name] = false;
      applyState(state);
    } else {
      // Server refused — roll back the optimistic flip.
      dirty[name] = false;
      const input = el("t-" + name);
      if (input) input.checked = !want;
    }
  } catch (_) {
    dirty[name] = false;
    const input = el("t-" + name);
    if (input) input.checked = !want;
  }
}

for (const name of SOURCES) {
  const input = el("t-" + name);
  if (!input) continue;
  input.addEventListener("change", async () => {
    // Warn before turning Bluetooth off while a wireless remote (volume knob,
    // etc.) is paired — otherwise the remote silently stops working until BT
    // is turned back on.
    if (
      name === "bluetooth" &&
      !input.checked &&
      latestState.bluetooth &&
      latestState.bluetooth.hasPairedHid
    ) {
      const ok = await jtsConfirm(
        "Turning Bluetooth off will also disconnect paired wireless remotes " +
          "(volume knob, etc.). They will not work again until Bluetooth is " +
          "turned back on.\n\nTurn Bluetooth off anyway?",
        { danger: true }
      );
      if (!ok) {
        // Revert the optimistic flip and skip the POST entirely.
        input.checked = true;
        return;
      }
    }
    postToggle(name, input.checked);
  });
}

setInterval(fetchState, POLL_MS);
fetchState();
