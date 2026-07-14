// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — /sources/ playback-source on/off toggles.
//
// The page server-renders four toggles (AirPlay, Bluetooth, Spotify Connect,
// USB Audio Input) with ids t-<source>. This module wires each to the same
// backend the legacy inline script did, behaviour-for-behaviour:
//
//   * Optimistic UI — flip the checkbox immediately, POST ./set {source,
//     enabled}, then reconcile from desired + effective state. If persistence
//     succeeds but runtime convergence fails, desired stays checked and the
//     degraded reason is shown; only a failed write rolls the choice back.
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
import { jtsAlert, jtsConfirm } from "/assets/shared/js/dialog.js";

const POLL_MS = 4000;
const SOURCES = ["airplay", "bluetooth", "spotify_connect", "usbsink"];
const dirty = {};
let ignorePollUntil = 0;
let latestState = {};
let stateKnown = false;
let postInFlight = false;
let stateFetchPromise = null;

const el = (id) => document.getElementById(id);

function applyState(state) {
  stateKnown = true;
  latestState = state;
  if (el("sources-state-error")) {
    el("sources-state-error").style.display = "none";
  }
  // A bonded follower parks every local source — toggles read disabled and the pair note
  // explains (POST /set 409s server-side regardless).
  const parked = !!(state.pair && state.pair.parked);
  if (el("pair-note")) el("pair-note").style.display = parked ? "" : "none";
  for (const name of SOURCES) {
    const s = state[name] || {};
    const input = el("t-" + name);
    if (!input) continue;
    if (dirty[name]) continue; // user toggled mid-flight; don't clobber
    input.checked = !!s.enabled;
    // Missing hardware/install pieces block On, never the safer Off repair.
    input.disabled =
      postInFlight || parked || (s.available === false && !s.enabled);
    const note = el(name + "-unavailable-note");
    if (note) {
      const unavailable = s.available === false;
      const degraded =
        typeof s.degradedReason === "string" && !!s.degradedReason;
      note.style.display = unavailable || degraded ? "" : "none";
      if (
        unavailable &&
        typeof s.unavailableReason === "string" &&
        s.unavailableReason
      ) {
        note.textContent = s.unavailableReason;
      } else if (degraded) {
        note.textContent = s.degradedReason;
      }
    }
  }
  const bt = state.bluetooth || {};
  const btUnavailable = bt.available === false;
  const btDegraded =
    typeof bt.degradedReason === "string" && !!bt.degradedReason;
  if (el("bt-note")) {
    el("bt-note").style.display = btUnavailable || btDegraded ? "" : "none";
    if (
      btUnavailable &&
      typeof bt.unavailableReason === "string" &&
      bt.unavailableReason
    ) {
      el("bt-note").textContent = bt.unavailableReason;
    } else if (btDegraded) {
      el("bt-note").textContent = bt.degradedReason;
    }
  }
  // USB sink has two warning shapes: unavailable means the toggle cannot
  // work; degraded means the host-visible gadget is up but the bridge is
  // direct fan-in lane is not healthy yet. Both hide the ordinary "plug a
  // computer in" note.
  const usb = state.usbsink || {};
  const usbUnavailable = usb.available === false;
  const usbDegraded =
    typeof usb.degradedReason === "string" && usb.degradedReason;
  const usbWarning = usbUnavailable || usbDegraded;
  const usbUnavailableNote = el("usbsink-unavailable-note");
  if (usbUnavailableNote) {
    usbUnavailableNote.style.display = usbWarning ? "" : "none";
    if (usbDegraded) {
      usbUnavailableNote.textContent = usb.degradedReason;
    } else if (
      usbUnavailable &&
      typeof usb.unavailableReason === "string" &&
      usb.unavailableReason
    ) {
      usbUnavailableNote.textContent = usb.unavailableReason;
    }
  }
  if (el("usbsink-note")) {
    el("usbsink-note").style.display = usbWarning ? "none" : "";
  }
}

function showStateError(message) {
  const error = el("sources-state-error");
  if (error) {
    error.textContent = message;
    error.style.display = "";
  }
  for (const name of SOURCES) {
    const input = el("t-" + name);
    if (input) input.disabled = true;
  }
}

async function fetchState() {
  if (postInFlight) return;
  if (document.visibilityState === "hidden") return;
  if (Date.now() < ignorePollUntil) return;
  if (stateFetchPromise !== null) return stateFetchPromise;
  stateFetchPromise = (async () => {
    try {
      const resp = await fetch("./state", { cache: "no-store" });
      if (resp.ok) {
        applyState(await resp.json());
        return;
      }
      const payload = await resp.json().catch(() => ({}));
      showStateError(
        payload.error ||
          "Source settings could not be read. Run jasper-doctor or re-run install.sh."
      );
    } catch (_) {
      // Keep a previously-authoritative snapshot through a transient network
      // miss. On initial load there is no truth to preserve, so say so plainly.
      if (!stateKnown) {
        showStateError(
          "Source settings are unavailable. Check the speaker connection and retry."
        );
      }
    }
  })();
  try {
    return await stateFetchPromise;
  } finally {
    stateFetchPromise = null;
  }
}

async function refreshAfterMutation() {
  // A GET may have started just before the mutation gate closed. Join it, then
  // issue one fresh authoritative read; never overlap state snapshots.
  if (stateFetchPromise !== null) {
    try {
      await stateFetchPromise;
    } catch (_) {
      // fetchState already owns user-facing failure policy.
    }
  }
  // The joined pre-mutation snapshot rendered while postInFlight was still
  // true, so controls remained disabled. Clear the gate and start the fresh GET
  // in this same JavaScript turn; no second change event can enter between them.
  postInFlight = false;
  ignorePollUntil = 0;
  return fetchState();
}

async function postToggle(name, want) {
  if (postInFlight) return;
  // Optimistic flip already happened on change. Mark dirty so polls don't
  // overwrite while we wait for the server, and pause polling briefly so a
  // poll fired right before this POST doesn't reconcile back to the old value.
  dirty[name] = true;
  postInFlight = true;
  ignorePollUntil = Date.now() + 1500;
  for (const source of SOURCES) {
    const control = el("t-" + source);
    if (control) control.disabled = true;
  }
  try {
    const resp = await fetch("./set", {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ source: name, enabled: want }),
    });
    const payload = await resp.json().catch(() => ({}));
    if (resp.ok) {
      dirty[name] = false;
      applyState(payload);
    } else {
      dirty[name] = false;
      if (payload.state && typeof payload.state === "object") {
        // The desired write landed but reconciliation failed. Preserve the
        // user's choice and surface the mismatch instead of lying via rollback.
        applyState(payload.state);
      } else if (
        payload.intentRecorded === true &&
        typeof payload.desired === "boolean"
      ) {
        // Persistence succeeded but authoritative state hydration failed. Keep
        // the durable choice instead of inventing a rollback; the next poll
        // will fill in effective state or show the global read error.
        const input = el("t-" + name);
        if (input) input.checked = payload.desired;
      } else {
        // The request was rejected before durable intent changed.
        const input = el("t-" + name);
        if (input) input.checked = !want;
      }
      await jtsAlert(payload.error || "Could not update this source.");
    }
  } catch (error) {
    dirty[name] = false;
    // The POST may have committed before its response was lost. Disable the
    // controls and keep the optimistic position until a GET supplies truth;
    // rolling back here would be a second unsupported guess.
    showStateError("The update result is unknown. Refreshing source state…");
    await jtsAlert(error && error.message ? error.message : "Could not update this source.");
  } finally {
    await refreshAfterMutation();
  }
}

for (const name of SOURCES) {
  const input = el("t-" + name);
  if (!input) continue;
  input.addEventListener("change", async () => {
    // Confirmation is asynchronous and polling continues behind the modal.
    // Capture the user's transition now; never re-read a checkbox that a poll
    // may have reconciled while the dialog was open.
    const want = !!input.checked;
    const current = latestState[name] || {};
    const previous = typeof current.enabled === "boolean"
      ? current.enabled
      : !want;
    // Warn before turning Bluetooth off while a wireless remote (volume knob,
    // etc.) is paired — otherwise the remote silently stops working until BT
    // is turned back on.
    if (
      name === "bluetooth" &&
      !want &&
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
        input.checked = previous;
        return;
      }
    }
    // A state poll may have redrawn this control during confirmation. Restore
    // the captured choice both visually and in the immutable POST argument.
    input.checked = want;
    await postToggle(name, want);
  });
}

setInterval(fetchState, POLL_MS);
fetchState();
