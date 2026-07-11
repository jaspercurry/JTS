// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// main.js — the /rooms/ "Speakers" surface. Directory + wake-response.
//
// Fetches /rooms.json on load and re-polls every 7 s. Renders:
//   * a "this speaker" card — name, hostname, address, Room (with a "Change
//     in Speaker settings" link to /speaker/, since room lives in the
//     identity home now), and grouping status (off/solo, or the bond
//     role/channel/codec; a fail-LOUD error if grouping is enabled-but-broken).
//   * a "Wake response" card — a toggle for the household question "when
//     multiple speakers hear 'Hey Jarvis', only one answers", plus a
//     "Primary" checkbox (prefer this speaker in ties) shown when the toggle
//     is on. Changes POST /peering (CSRF via http.js) and the card reflects
//     the returned state. This is the ONE working write surface on this page.
//   * one row per sibling speaker (self excluded by the server), each a real
//     click-through <a> to that speaker's OWN /system/ page on its stable
//     .local web host. The value of the directory is discovery +
//     click-through, not config aggregation — see docs/HANDOFF-multiroom.md §6.
//
// The poll loop self-schedules (setTimeout after each completes) so a slow
// response can't overlap the next tick, and it separates a transport failure
// (→ "Disconnected", dimmed) from a render failure (isolated + logged, so one
// bad field never blanks the page or masquerades as a disconnect). The
// wake-response card is built ONCE (it has interactive state — a pending
// save — that a per-poll rebuild would stomp); the poll only reconciles its
// controls to the latest /rooms.json when no save is in flight.
//
// Security: every peer field (name, room, address, hostname-derived URL) and
// every grouping value is untrusted — it arrives over mDNS / from a config
// file. This module builds DOM exclusively through the shared h()/svg()
// helpers (/assets/shared/js/dom.js), whose text children become
// document.createTextNode (escaped by construction). There is NO innerHTML
// path and NO inline onclick with interpolated strings. The peer
// click-through href is additionally
// scheme-guarded (http/https only) as defense-in-depth against a poisoned
// mDNS address. The wake-response toggle needs no confirm; the bond card's
// destructive "Dissolve pair" action uses jtsConfirm (the styled <dialog>,
// never native confirm/alert — a static test forbids the natives). A save
// error surfaces inline in the card's status line.

import { getJSON, postJSON } from "/assets/shared/js/http.js";
import { jtsConfirm } from "/assets/shared/js/dialog.js";
import { localWebHost } from "/assets/shared/js/local-web-host.js";
import { h, svg, appendChildren } from "/assets/shared/js/dom.js";
import { createPairBalanceController } from "./pair-balance-controller.js";
import {
  BALANCE_MAX_DB,
  BALANCE_MIN_DB,
  addSubPlan,
  airplayLipSyncRow,
  balanceText,
  clampBalanceDb,
  formatBalanceDb,
  snapcastProvisionRow,
  subCornerLabel,
  trimsForBalance,
} from "./grouping-view.js";

const POLL_MS = 7000;
const BALANCE_LIVE_COMMIT_MS = 150;
const root = document.getElementById("app");

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

// app.css's #icon-chevron sprite, sized via .ico.
function chevron(cls) {
  return svg(`svg.ico${cls ? "." + cls : ""}`, { "aria-hidden": "true" },
    svg("use", { href: "#icon-chevron" }));
}

// Reject anything that isn't a plain http(s) URL before assigning it to an
// href — defense-in-depth against a poisoned mDNS address smuggling a
// javascript:/data: scheme. Returns "" (→ link is dropped) when unsafe.
function safeHttpUrl(value) {
  if (!value) return "";
  try {
    const u = new URL(value, window.location.origin);
    return (u.protocol === "http:" || u.protocol === "https:") ? u.href : "";
  } catch {
    return "";
  }
}

function defRow(label, value) {
  return [h("dt", null, label), h("dd", null, value)];
}

function groupingHasSubwoofer(g) {
  if (!g || typeof g !== "object") return false;
  if (g.subwoofer_present || g.channel === "sub") return true;
  const roster = Array.isArray(g.roster) ? g.roster : [];
  return roster.some((m) => m && m.channel === "sub");
}

// ---------------------------------------------------------------------------
// Grouping status → a key/value list (or a single "off (solo)" line).
// Shape from jasper.multiroom.state.read_grouping_state() + the airplay-fit
// composer (jasper.multiroom.airplay_latency.with_airplay_latency_fit):
//   {enabled, role, channel, bond_id, leader_addr, buffer_ms, codec, error,
//    runtime?: {health, detail, units},   -- runtime present only when enabled
//    airplay_latency_fit?: {applicable, tight?, residual_lag_sec?, …}}
//      -- applicable:true only on an active bonded leader; null on read error
// ---------------------------------------------------------------------------
function groupingBody(g) {
  if (!g || typeof g !== "object") {
    return h("p.info-card__note", null, "Grouping status unavailable.");
  }
  // Fail-LOUD: an enabled-but-broken config carries an error string.
  if (g.error) {
    const badge = h("span.badge", null, "Misconfigured");
    badge.style.setProperty("--tone", "var(--status-danger)");
    return h("div", null,
      h("div.badge-row", null, badge),
      h("p.info-card__note", null, String(g.error)),
    );
  }
  if (!g.enabled) {
    const badge = h("span.badge", null, "Solo");
    badge.style.setProperty("--tone", "var(--status-idle)");
    return h("div", null,
      h("div.badge-row", null, badge, "Not part of a bond"),
    );
  }
  // Enabled + valid: role / channel / bond / buffer / codec. A subwoofer
  // channel names its low-pass corner inline (e.g. "Subwoofer | 80 Hz
  // low-pass") so the row says what the box actually plays, not just "sub".
  const channelLabel = g.channel === "sub"
    ? "Subwoofer | " + subCornerLabel(g.crossover_hz)
    : (g.channel || "—");
  const rows = [
    defRow("Role", g.role || "—"),
    defRow("Channel", channelLabel),
    defRow("Bond", g.bond_id || "—"),
  ];
  if (g.role === "follower" && g.leader_addr) {
    const leaderHost = localWebHost(g.leader_addr);
    rows.push(defRow("Leader", leaderHost || "leader"));
  }
  if (g.buffer_ms != null) rows.push(defRow("Buffer", g.buffer_ms + " ms"));
  if (g.codec) rows.push(defRow("Codec", g.codec));
  // Bonded-leader AirPlay lip-sync (jasper.multiroom.airplay_latency): a row
  // only when this speaker is an active bonded leader. The presentation
  // DECISION is the pure airplayLipSyncRow (unit-tested); here we just build
  // the DOM. Quiet "Synced" badge when the offset fits; amber "Lagging ~N ms"
  // badge + a note (pushed below) when the sender's budget can't absorb the
  // bonded round-trip. Solo/follower => null => no row at all.
  const fitRow = airplayLipSyncRow(g.airplay_latency_fit);
  if (fitRow) {
    const fitBadge = h("span.badge", null, fitRow.label);
    fitBadge.style.setProperty("--tone", fitRow.tone);
    rows.push([h("dt", null, "AirPlay lip-sync"), h("dd", null, fitBadge)]);
  }
  // Snapcast provisioning (the grouping opt-in install): a row only while the
  // reconciler installs the binaries on first enable, or on a failed install.
  // The DECISION is the pure snapcastProvisionRow (unit-tested); the note is
  // pushed below.
  const provRow = snapcastProvisionRow(g);
  if (provRow) {
    const provBadge = h("span.badge", null, provRow.label);
    provBadge.style.setProperty("--tone", provRow.tone);
    rows.push([h("dt", null, "Snapcast"), h("dd", null, provBadge)]);
  }
  // Runtime health (jasper.multiroom.state.derive_grouping_runtime):
  //   {health: "ok"|"degraded"|…, detail}. Present when grouping is on.
  // A degraded bond — a follower that can't reach its leader, or (until the
  // producer ships) a leader with no FIFO — shows amber + the reason, not a
  // green "Grouped" hiding silent breakage. The detail (which may contain a
  // leader address) is rendered as a TEXT node, which h() escapes.
  const rt = g.runtime && typeof g.runtime === "object" ? g.runtime : null;
  const degraded = rt && rt.health === "degraded";
  const badge = h("span.badge", null, degraded ? "Degraded" : "Grouped");
  badge.style.setProperty(
    "--tone", degraded ? "var(--status-warn)" : "var(--status-ok)",
  );
  const out = [
    h("div.badge-row", null, badge),
    h("dl.deflist", null, rows.flat()),
  ];
  if (rt && rt.detail) {
    out.push(h("p.info-card__note", null, String(rt.detail)));
  }
  if (fitRow && fitRow.note) {
    out.push(h("p.info-card__note", null, fitRow.note));
  }
  if (provRow && provRow.note) {
    out.push(h("p.info-card__note", null, provRow.note));
  }
  return h("div", null, ...out);
}

// ---------------------------------------------------------------------------
// "This speaker" card body.
//
// Room is shown here but EDITED in the identity home (/speaker/) — the small
// link keeps a single source of truth and avoids reopening the two-homes
// drift (docs/HANDOFF-multiroom.md §6). The link is a static, trusted
// same-origin path, not derived from any untrusted field.
// ---------------------------------------------------------------------------
function roomValue(room) {
  return h("span.room-self__room", null,
    room || "—",
    " ",
    h("a.room-self__edit", { href: "/speaker/" }, "Change in Speaker settings"),
  );
}

function selfBody(self) {
  const s = self || {};
  const idRows = [
    defRow("Name", s.name || "—"),
    defRow("Hostname", s.hostname || "—"),
    defRow("Address", s.address || "—"),
    defRow("Room", roomValue(s.room)),
  ];
  return h("div", null,
    h("dl.deflist", null, idRows.flat()),
    h("div.section__head", { style: { "margin-top": "16px", padding: "0" } },
      h("p.eyebrow", null, "Grouping")),
    groupingBody(s.grouping),
  );
}

// ---------------------------------------------------------------------------
// One peer row: a click-through <a> to the peer's own /system/ page.
// ---------------------------------------------------------------------------
function peerRow(peer) {
  const p = peer || {};
  const label = p.room || p.name || "(unnamed)";
  const addr = p.address || "?";
  const href = safeHttpUrl(p.system_url);

  const main = h("div.room-row__main", null,
    h("span.room-row__name", null, label),
    h("span.room-row__addr", null, addr),
  );

  if (href) {
    // Real link — middle-click / open-in-new-tab work; no inline JS. The
    // accessible name names the destination using the (escaped) label.
    return h("a.room-row", {
      href,
      "attr:aria-label": "Open " + label + " settings",
    }, main, chevron("room-row__go"));
  }
  // No resolvable address → render the row without a click-through rather
  // than a dead link.
  return h("div.room-row", null, main);
}

function peersBody(peers) {
  const list = Array.isArray(peers) ? peers : [];
  if (list.length === 0) {
    return h("p.room-empty", null,
      "No other JTS speakers found on this network yet.");
  }
  return h("ul.room-list", null, list.map(peerRow));
}

// ---------------------------------------------------------------------------
// Wake-response (peering) card — the ONE working write surface on this page.
//
// Built ONCE (it carries interactive state — an in-flight save and the
// not-yet-saved control positions — that a per-poll rebuild would stomp).
// The returned object exposes `el` (the card) plus `sync(p)`, which the poll
// loop calls to reconcile the controls to /rooms.json's `peering` block when
// no save is in flight. On a control change we optimistically flip, POST
// /peering, and either confirm from the returned state or revert + surface
// the error inline. No native confirm()/alert() — a toggle needs no confirm.
// ---------------------------------------------------------------------------
function makeWakeCard() {
  let saving = false;

  const enabledCb = h("input", {
    type: "checkbox",
    "attr:aria-label": 'Only one speaker answers "Hey Jarvis"',
  });
  const enabledToggle = h("label.toggle", null, enabledCb, h("span.track"));

  const primaryCb = h("input", {
    type: "checkbox",
    "attr:aria-label": "Prefer this speaker in ties",
  });
  // The Primary row is only meaningful when arbitration is on; we show/hide it
  // rather than disable so the card stays uncluttered on a single-speaker setup.
  const primaryRow = h("label.wake-row.wake-row--sub", null,
    primaryCb,
    h("span.wake-row__text", null,
      h("span.wake-row__label", null, "Primary"),
      h("span.wake-row__hint", null, "Prefer this speaker in ties.")),
  );

  const status = h("p.wake-status.info-card__note", { "attr:aria-live": "polite" });

  const body = h("div.info-card", null,
    h("p.info-card__note", null,
      "When several JTS speakers hear the wake word at once, only one needs " +
      "to answer. Turn this on so they coordinate; off (the default) means " +
      "each speaker decides on its own."),
    h("label.wake-row", null,
      enabledToggle,
      h("span.wake-row__text", null,
        h("span.wake-row__label", null,
          'When multiple speakers hear "Hey Jarvis", only one answers')),
    ),
    primaryRow,
    status,
  );

  const card = h("section.section", null,
    h("div.section__head", null, h("h2.section__title", null, "Wake response")),
    body,
  );

  function reflectPrimaryVisibility() {
    primaryRow.style.display = enabledCb.checked ? "" : "none";
  }

  // Reconcile controls to the server state. Skipped while a save is pending so
  // a mid-flight poll can't yank the toggle back under the user.
  function sync(p) {
    if (saving) return;
    const peering = (p && typeof p === "object") ? p : {};
    enabledCb.checked = !!peering.enabled;
    primaryCb.checked = !!peering.primary;
    reflectPrimaryVisibility();
  }

  async function save() {
    const enabled = enabledCb.checked;
    const primary = primaryCb.checked;
    reflectPrimaryVisibility();
    saving = true;
    enabledCb.disabled = true;
    primaryCb.disabled = true;
    status.textContent = "Saving…";
    try {
      // POST /peering returns {ok, peering:{enabled, primary}}; the card
      // reflects the returned (authoritative) state.
      const data = await postJSON("peering", { enabled, primary });
      const ret = (data && data.peering) || {};
      enabledCb.checked = !!ret.enabled;
      primaryCb.checked = !!ret.primary;
      reflectPrimaryVisibility();
      status.textContent = "Saved. Restarting (~5s)…";
    } catch (e) {
      console.error("rooms: wake-response save failed", e);
      // Revert the optimistic flip to the last-known server truth on the
      // next poll; show the error now.
      status.textContent = "Couldn't save: " + e.message;
    } finally {
      saving = false;
      enabledCb.disabled = false;
      primaryCb.disabled = false;
    }
  }

  enabledCb.addEventListener("change", save);
  primaryCb.addEventListener("change", save);

  return { el: card, sync };
}

// ---------------------------------------------------------------------------
// Bond card — two faces, one card. When this speaker is NOT in a bond it is the
// one-flow "create a stereo pair": the user picks one other discovered speaker
// for the RIGHT channel (THIS speaker is the LEFT/leader); Save POSTs /bond,
// which fans the config out to both speakers' control APIs. When this speaker
// IS already in a bond (snap.self.grouping.enabled + bond_id set), the picker
// hides and the card instead shows a one-line legible summary of the current
// bond plus a DANGER "Dissolve pair" button (POST /unbond). Built ONCE (it
// holds a pending-save state + a selection a per-poll rebuild would stomp);
// sync() reconciles the two faces and refreshes the peer options only when no
// save is in flight. Untrusted peer name/address and the grouping fields
// (channel, leader_addr) reach the DOM through h() text nodes and the option
// value — never innerHTML, never inline onclick.
// ---------------------------------------------------------------------------
// One legible line from a failed bond action. postJSON attaches the
// server's parsed JSON verdict to err.body, so the per-member results,
// precondition reasons, and the swap rollback outcome actually reach the
// household instead of a bare "HTTP 502".
function describeBondFailure(e) {
  const body = e && e.body;
  if (!body) return e && e.message ? e.message : "unknown error";
  const failed = (Array.isArray(body.results) ? body.results : [])
    .filter((r) => r && !r.ok);
  let msg = failed.length
    ? failed.map((r) => (r.addr || "this speaker") + ": " +
        (r.detail || "failed")).join("; ")
    : (body.error || e.message || "unknown error");
  // A member with the control-token gate enabled rejects the grouping
  // fan-out until the browser supplies its X-JTS-Token. /rooms/ can't
  // prompt for it yet (a bond fans out to several speakers that each
  // carry their own token), so point the operator at the path that does
  // capture it: any /system/ action stores the token in this browser,
  // which /rooms/ then reuses. (detail is a server string rendered via
  // textContent at the call sites — no escaping concern.)
  const tokenGated = failed.some(
    (r) => typeof r.detail === "string" &&
      /control_token_required|X-JTS-Token/.test(r.detail));
  if (tokenGated) {
    msg = "a speaker needs its control token — set it once via the " +
      "System page (open /system/ and run any action), then retry " +
      "(see SECURITY.md). Details — " + msg;
  }
  if (body.rolled_back === true) {
    msg += " — the change was rolled back; both speakers kept their channels.";
  } else if (body.rolled_back === false) {
    msg += " — ROLLBACK ALSO FAILED: the pair may be on one channel; " +
      "press Swap again to repair it.";
  }
  return msg;
}

function makeBondCard() {
  let saving = false;
  let selfAddr = "";
  // The add-sub picker's reachable peers + the existing-members plan from the
  // last poll, so the addSub handler re-posts the SAME bond with the new sub.
  let lastSubReachable = [];
  let lastAddSubPlan = { show: false, members: [], reason: "init" };
  // Current bond_id from the poll — passed back on the add-sub re-post so it
  // ADDS to the existing bond rather than minting a fresh one.
  let lastBondId = "";
  let lastBonded = null;
  let advancedSubOpen = false;

  // --- Create face: pick one sibling; the backend owns the pair topology. ---
  const select = h("select.bond-select", {
    "attr:aria-label": "Speaker to pair with",
  });
  const createBtn = h("button.btn.btn--primary",
    { type: "button" }, "Create stereo pair");

  const createIntro = h("p.info-card__note", null,
    "Create a stereo pair with one other speaker. This speaker becomes the " +
    "left channel and the paired speaker becomes the right channel.");
  const pickerLabel = h("span.bond-row__label", null,
    "Pair with");
  const picker = h("div.bond-row", null,
    pickerLabel,
    select,
    createBtn,
  );

  // --- Dissolve face: a legible summary + the danger button ---------------
  // The summary line is filled per-sync (it depends on role/channel); kept as
  // a single element so sync() can rewrite its text children safely.
  const currentSummary = h("p.bond-current");
  const dissolveBtn = h("button.btn.btn--danger",
    { type: "button" }, "Dissolve group");
  // Neutral "group" wording: this face also shows a main+subwoofer bond, not
  // only a stereo pair (the leader can't tell the peer's role from its own
  // grouping state, so the copy must read true for both).
  const dissolveIntro = h("p.info-card__note", null,
    "This speaker is grouped with another. Dissolving sends both speakers " +
    "back to playing on their own.");
  const swapBtn = h("button.btn",
    { type: "button" }, "Swap left \u2194 right");
  const dissolveRow = h("div.bond-dissolve", null, swapBtn, dissolveBtn);

  // Pair balance: one signed slider, rendered as absolute left/right trims.
  // The server maps the value to the headroom-maximising attenuate-only pair:
  // one side remains at 0 dB, the other side is <= 0 dB.
  const balanceRange = h("input.balance-slider", {
    type: "range",
    min: String(BALANCE_MIN_DB),
    max: String(BALANCE_MAX_DB),
    step: "0.5",
    value: "0",
    "attr:aria-label": "Left-right balance",
  });
  const balanceValue = h("span.balance-value", null, "Centered");
  const balanceTrims = h("span.balance-trims", null, "L 0.0 dB / R 0.0 dB");
  const balanceReset = h("button.btn", { type: "button" }, "Center");
  const balanceStatus = h("p.balance-status.info-card__note",
    { "attr:aria-live": "polite" });

  function reflectBalance(value) {
    const db = clampBalanceDb(value);
    balanceRange.value = String(db);
    balanceValue.textContent = balanceText(db);
    const trims = trimsForBalance(db);
    balanceTrims.textContent =
      "L " + formatBalanceDb(trims.left) + " / R "
      + formatBalanceDb(trims.right);
  }

  function setBalanceEnabled(on) {
    balanceRange.disabled = !on;
    balanceReset.disabled = !on;
  }

  function balanceApplyMessage(data, balance) {
    const details = Array.isArray(data && data.results)
      ? data.results.map((r) => String(r && r.detail || ""))
      : [];
    if (details.some((d) => d.includes("audio update scheduled"))) {
      return "Saved; audio update scheduled.";
    }
    if (balance && balance.clamped) {
      return "Applied at the trim limit.";
    }
    return "Applied.";
  }

  const balanceController = createPairBalanceController({
    commitDelayMs: BALANCE_LIVE_COMMIT_MS,
    readValue: () => balanceRange.value,
    reflectBalance,
    postTrim: (request) => postJSON("trim", request),
    setStatus: (message) => { balanceStatus.textContent = message; },
    applyMessage: balanceApplyMessage,
    describeFailure: describeBondFailure,
    logError: (error) => console.error("rooms: balance failed", error),
  });

  balanceRange.addEventListener("input", () => {
    balanceController.input(balanceRange.value);
  });
  balanceRange.addEventListener("change", () => {
    void balanceController.change();
  });
  balanceReset.addEventListener("click", () => {
    void balanceController.reset();
  });

  const trimIntro = h("p.info-card__note", null,
    "Balance the pair by ear while keeping the louder side at full trim.");
  const balanceLink = h("a", {
    href: "https://" + location.hostname + "/balance/",
  }, "Balance automatically with your phone’s microphone ↗");
  const balanceNote = h("p.info-card__note", null,
    "Or: ", balanceLink,
    " (uses the same measurement certificate as room correction).");
  const balanceBlock = h("div.balance-block", null,
    trimIntro,
    h("div.balance-control", null,
      h("span.balance-end", null, "Left"),
      balanceRange,
      h("span.balance-end", null, "Right")),
    h("div.balance-readout", null, balanceValue, balanceTrims, balanceReset),
    balanceStatus,
    balanceNote);

  const mainsHpCb = h("input", {
    type: "checkbox",
    "attr:aria-label": "High-pass main speakers when a subwoofer is active",
  });
  const mainsHpToggle = h("label.toggle", null, mainsHpCb, h("span.track"));
  const mainsHpRow = h("label.wake-row", null,
    mainsHpToggle,
    h("span.wake-row__text", null,
      h("span.wake-row__label", null, "High-pass main speakers"),
      h("span.wake-row__hint", null,
        "Match the mains to the subwoofer crossover.")),
  );

  // --- Add-subwoofer sub-panel (dissolve face, stereo-leader only) --------
  // Shown only when this speaker is a bonded stereo-pair LEADER with no sub
  // yet (decided by the pure addSubPlan helper). Adds a third member to the
  // SAME bond — it does NOT dissolve the pair.
  const addSubSelect = h("select.bond-select", {
    "attr:aria-label": "Speaker to add as a subwoofer",
  });
  const addSubCrossover = h("input.bond-crossover", {
    type: "number", min: "40", max: "200", step: "1", value: "80",
    "attr:aria-label": "Subwoofer low-pass corner (Hz)",
  });
  const addSubBtn = h("button.btn.btn--ghost",
    { type: "button" }, "Add subwoofer");
  const advancedSubBtn = h("button.btn.btn--ghost",
    { type: "button" }, "Advanced");
  const addSubIntro = h("p.info-card__note", null,
    "Optional: add a subwoofer to this pair. The speaker you pick plays only the low " +
    "end (low-passed locally on that box). The pair keeps playing as-is.");
  const addSubPanel = h("div.add-sub-panel", null,
    addSubIntro,
    h("div.bond-row", null,
      h("span.bond-row__label", null, "Add subwoofer"),
      addSubSelect),
    h("div.bond-row", null,
      h("span.bond-row__label", null, "Low-pass (Hz)"),
      addSubCrossover,
      addSubBtn),
  );

  const status = h("p.bond-status.info-card__note",
    { "attr:aria-live": "polite" });
  const loadingNote = h("p.info-card__note", null, "Loading speaker grouping…");

  const body = h("div.info-card", null,
    loadingNote,
    createIntro,
    picker,
    dissolveIntro,
    currentSummary,
    mainsHpRow,
    balanceBlock,
    advancedSubBtn,
    addSubPanel,
    dissolveRow,
    status,
  );
  const title = h("h2.section__title", null, "Create a stereo pair");
  const card = h("section.section", null,
    h("div.section__head", null, title),
    body,
  );

  function setEnabled(on) {
    select.disabled = !on;
    createBtn.disabled = !on;
  }

  function showLoading() {
    loadingNote.style.display = "";
    createIntro.style.display = "none";
    picker.style.display = "none";
    dissolveIntro.style.display = "none";
    currentSummary.style.display = "none";
    dissolveRow.style.display = "none";
    mainsHpRow.style.display = "none";
    balanceBlock.style.display = "none";
    advancedSubBtn.style.display = "none";
    addSubPanel.style.display = "none";
    title.textContent = "Speaker grouping";
    setEnabled(false);
  }

  // Show exactly one face (create vs. dissolve). Bonded → dissolve; else create.
  function showFace(bonded) {
    loadingNote.style.display = "none";
    createIntro.style.display = bonded ? "none" : "";
    picker.style.display = bonded ? "none" : "";
    dissolveIntro.style.display = bonded ? "" : "none";
    currentSummary.style.display = bonded ? "" : "none";
    dissolveRow.style.display = bonded ? "" : "none";
    mainsHpRow.style.display = "none";
    balanceBlock.style.display = bonded ? "" : "none";
    advancedSubBtn.style.display = "none";
    addSubPanel.style.display = "none";
    title.textContent = bonded ? "Speaker grouping" : "Create a stereo pair";
  }

  function syncBalance(g) {
    const b = g && typeof g.balance === "object" ? g.balance : null;
    if (!b || !b.applicable) {
      balanceBlock.style.display = "none";
      return;
    }
    balanceBlock.style.display = "";
    if (!b.ok) {
      setBalanceEnabled(false);
      balanceStatus.textContent = "Balance unavailable — " + (b.error || "pair not ready");
      return;
    }
    setBalanceEnabled(true);
    if (typeof b.balance_db === "number") {
      balanceController.syncConfirmed(b.balance_db);
    }
  }

  // One-line legible summary of the current bond. Untrusted channel/leader_addr
  // are passed as h() text-node children (escaped). Raw IP leader handles are
  // intentionally not shown as browser-facing hosts.
  function summarize(g) {
    const channel = g.channel || "";
    if (g.role === "follower") {
      const leaderHost = localWebHost(g.leader_addr);
      // A subwoofer follower reads differently from a left/right channel: it
      // plays only the low end, low-passed locally at the saved corner.
      const parts = channel === "sub"
        ? [
            "Subwoofer — this speaker plays the low end below ",
            h("strong", null, subCornerLabel(g.crossover_hz)),
            ".",
          ]
        : [
            "Paired — this speaker plays the ",
            h("strong", null, channel || "second"),
            " channel.",
          ];
      if (leaderHost) {
        parts.push(" Following ", h("code.bond-current__addr", null, leaderHost), ".");
      } else if (g.leader_addr) {
        parts.push(" Following the leader.");
      }
      return parts;
    }
    if (g.role === "leader") {
      return [
        "Leading this pair — ",
        h("strong", null, channel || "first"),
        " channel.",
      ];
    }
    // Enabled + bonded but an unrecognised role: still legible, no throw.
    return [
      "Paired",
      channel ? [" — ", h("strong", null, channel), " channel."] : ".",
    ];
  }

  // Reconcile both faces from the latest snapshot. Skipped while saving so a
  // mid-flight poll can't yank the selection / swap the face under the user.
  function sync(snap) {
    if (saving) return;
    const self = (snap && snap.self) || {};
    selfAddr = self.address || "";
    const g = (self.grouping && typeof self.grouping === "object") ? self.grouping : {};
    const view = (snap && snap.view && typeof snap.view === "object") ? snap.view : {};
    const bonded = typeof view.bonded === "boolean"
      ? view.bonded
      : !!(g.enabled && g.bond_id && !g.error);
    const peers = Array.isArray(snap && snap.peers) ? snap.peers : [];

    showFace(bonded);
    if (lastBonded !== null && bonded !== lastBonded) {
      status.textContent = "";
    }
    lastBonded = bonded;

    if (bonded) {
      // Dissolve face: rebuild the summary text from the current grouping.
      currentSummary.textContent = "";
      appendChildren(currentSummary, summarize(g));
      syncBalance(g);
      const hasSub = groupingHasSubwoofer(g);
      mainsHpRow.style.display = hasSub ? "" : "none";
      mainsHpCb.checked = g.mains_highpass_enabled !== false;
      mainsHpCb.disabled = !hasSub;
      dissolveBtn.disabled = false;
      // Swap is only well-defined for a left/right pair; the backend
      // re-validates (exactly one same-bond peer) — this just avoids
      // showing the button on a mono/multi-member bond.
      swapBtn.style.display =
        (g.channel === "left" || g.channel === "right") ? "" : "none";
      swapBtn.disabled = false;
      // Add-subwoofer affordance: only for a stereo-pair leader with no sub
      // yet (the pure addSubPlan decides). Stash the existing-members plan so
      // the handler re-posts the SAME bond with the new sub appended.
      lastAddSubPlan = addSubPlan({ ...g, self_addr: selfAddr });
      lastBondId = g.bond_id || "";
      if (lastAddSubPlan.show && view.show_subwoofer_controls === true) {
        advancedSubBtn.style.display = "";
        advancedSubBtn.textContent = advancedSubOpen ? "Hide advanced" : "Advanced";
        addSubPanel.style.display = advancedSubOpen ? "" : "none";
        // Exclude self + every existing bond member from the sub picker.
        const inBond = new Set(
          lastAddSubPlan.members
            .map((mm) => mm.addr)
            .filter(Boolean),
        );
        const subReachable = peers.filter(
          (p) => p && p.address && !inBond.has(p.address));
        const prev = addSubSelect.value;
        addSubSelect.textContent = "";
        if (!subReachable.length) {
          addSubSelect.appendChild(h("option", { value: "" },
            "No other speakers found — add one to use as a subwoofer"));
          addSubSelect.disabled = true;
          addSubCrossover.disabled = true;
          addSubBtn.disabled = true;
        } else {
          lastSubReachable = subReachable;
          for (const p of subReachable) {
            const label = (p.name || p.address) + " (" + p.address + ")";
            addSubSelect.appendChild(h("option", { value: p.address }, label));
          }
          if (subReachable.some((p) => p.address === prev)) {
            addSubSelect.value = prev;
          }
          addSubSelect.disabled = false;
          addSubCrossover.disabled = false;
          addSubBtn.disabled = false;
        }
      } else {
        advancedSubOpen = false;
        advancedSubBtn.style.display = "none";
        addSubPanel.style.display = "none";
      }
      return;
    }

    // Create face: refresh the peer options (reusing `peers` from above).
    const reachable = peers.filter((p) => p && p.address);  // need an address to pair
    const prev = select.value;
    select.textContent = "";
    if (!selfAddr || !reachable.length) {
      select.appendChild(h("option", { value: "" },
        !selfAddr
          ? "This speaker has no network address yet"
          : "No other speakers found — add one to pair"));
      setEnabled(false);
      return;
    }
    for (const p of reachable) {
      const label = (p.name || p.address) + " (" + p.address + ")";
      select.appendChild(h("option", { value: p.address }, label));
    }
    if (reachable.some((p) => p.address === prev)) select.value = prev;  // keep selection
    setEnabled(view.can_create_pair !== false);
  }

  async function create() {
    const rightAddr = select.value;
    if (!selfAddr || !rightAddr) return;
    let created = false;
    saving = true;
    setEnabled(false);
    status.textContent = "Creating the pair…";
    try {
      const data = await postJSON("bond", { peer_addr: rightAddr });
      if (data && data.ok) {
        created = true;
        status.textContent = "Stereo pair created — both speakers are configuring (~10s).";
        setTimeout(poll, 1200);
      }
    } catch (e) {
      console.error("rooms: bond create failed", e);
      status.textContent = "Couldn't pair — " + describeBondFailure(e);
    } finally {
      saving = false;
      if (!created) setEnabled(true);
    }
  }

  async function dissolve() {
    const ok = await jtsConfirm(
      "Dissolve this speaker group? Both speakers go back to playing on their own.",
      { danger: true },
    );
    if (!ok) return;
    saving = true;
    dissolveBtn.disabled = true;
    status.textContent = "Dissolving…";
    try {
      // POST /unbond returns {ok, results?, error?}. On ok the speakers are
      // already reconfiguring; otherwise surface a short reason.
      const data = await postJSON("unbond", {});
      if (data && data.ok) {
        status.textContent = "Pair dissolved.";
        setTimeout(poll, 1200);
      }
    } catch (e) {
      console.error("rooms: bond dissolve failed", e);
      status.textContent = "Couldn't dissolve — " + describeBondFailure(e);
    } finally {
      saving = false;
      dissolveBtn.disabled = false;
    }
  }

  async function swap() {
    const ok = await jtsConfirm(
      "Swap channels? The left and right speakers trade sides — each " +
      "briefly restarts its output (~2s).",
    );
    if (!ok) return;
    saving = true;
    swapBtn.disabled = true;
    dissolveBtn.disabled = true;
    status.textContent = "Swapping channels\u2026";
    try {
      const data = await postJSON("swap", {});
      status.textContent = data && data.repaired
        ? "Pair repaired to left/right — both speakers are reconfiguring (~10s)."
        : "Channels swapped — both speakers are reconfiguring (~10s).";
    } catch (e) {
      console.error("rooms: channel swap failed", e);
      status.textContent = "Couldn't swap — " + describeBondFailure(e);
    } finally {
      saving = false;
      swapBtn.disabled = false;
      dissolveBtn.disabled = false;
    }
  }

  async function addSub() {
    const subAddr = addSubSelect.value;
    if (!subAddr || !lastAddSubPlan.show) return;
    saving = true;
    addSubSelect.disabled = true;
    addSubCrossover.disabled = true;
    addSubBtn.disabled = true;
    status.textContent = "Adding the subwoofer…";
    try {
      const picked = lastSubReachable.find((p) => p.address === subAddr);
      const pickedName = (picked && picked.name) || "";
      // Re-post the FULL desired roster to the SAME bond (bond_id reused from
      // the current grouping): the existing members from the plan + the new
      // sub. The sub plays only the low end, low-passed locally in outputd.
      const members = [
        ...lastAddSubPlan.members,
        { addr: subAddr, role: "follower", channel: "sub",
          crossover_hz: Number(addSubCrossover.value), name: pickedName },
      ];
      const body = { members };
      if (lastBondId) body.bond_id = lastBondId;
      const data = await postJSON("bond", body);
      if (data && data.ok) {
        status.textContent = "Subwoofer added — reconfiguring (~10s).";
      }
    } catch (e) {
      console.error("rooms: add subwoofer failed", e);
      status.textContent = "Couldn't add the subwoofer — " + describeBondFailure(e);
    } finally {
      saving = false;
      addSubSelect.disabled = false;
      addSubCrossover.disabled = false;
      addSubBtn.disabled = false;
    }
  }

  async function setMainsHighpass() {
    const enabled = mainsHpCb.checked;
    saving = true;
    mainsHpCb.disabled = true;
    status.textContent = "Saving bass management…";
    try {
      const data = await postJSON("mains-highpass", { enabled });
      if (data && data.ok) {
        status.textContent = enabled
          ? "Bass management on — mains are reconfiguring (~10s)."
          : "Bass management off — mains are reconfiguring (~10s).";
      }
    } catch (e) {
      console.error("rooms: mains high-pass toggle failed", e);
      mainsHpCb.checked = !enabled;
      status.textContent = "Couldn't save bass management — " +
        describeBondFailure(e);
    } finally {
      saving = false;
      mainsHpCb.disabled = false;
    }
  }

  createBtn.addEventListener("click", create);
  swapBtn.addEventListener("click", swap);
  addSubBtn.addEventListener("click", addSub);
  advancedSubBtn.addEventListener("click", () => {
    advancedSubOpen = !advancedSubOpen;
    advancedSubBtn.textContent = advancedSubOpen ? "Hide advanced" : "Advanced";
    addSubPanel.style.display =
      advancedSubOpen && lastAddSubPlan.show ? "" : "none";
  });
  mainsHpCb.addEventListener("change", setMainsHighpass);
  dissolveBtn.addEventListener("click", dissolve);
  // Default to a neutral loading face; the first /rooms.json decides whether
  // create or paired controls are valid. This avoids initial layout/state lies.
  showLoading();
  return { el: card, sync };
}

// ---------------------------------------------------------------------------
// Build the page shell once; the poll loop swaps card bodies in place.
// ---------------------------------------------------------------------------
function buildPage(mount) {
  mount.textContent = "";

  const staleness = h("p.eyebrow", null, "Loading…");

  const selfCard = h("div.info-card.info-card--accent");
  const selfSection = h("section.section", null,
    h("div.section__head", null, h("h2.section__title", null, "This speaker")),
    selfCard,
  );

  // The wake-response + bond cards are built ONCE (interactive state); the
  // poll only reconciles their controls via .sync().
  const wakeCard = makeWakeCard();
  const bondCard = makeBondCard();

  const peersCard = h("div.info-card");
  const peersSection = h("section.section", null,
    h("div.section__head", null, h("h2.section__title", null, "Speakers on this network")),
    peersCard,
  );

  // Honest note — configuration is automatic now; perfect sample-lock across a
  // pair is the remaining on-hardware validation step (see §0/§2).
  const note = h("section.section.room-note", null,
    h("div.info-card", null,
      h("p.info-card__hint", null,
        "Creating a pair configures both speakers automatically. Perfect " +
        "sample-lock across the pair is still being validated on hardware, so " +
        "treat stereo pairing as a preview for now. ",
        h("a", { href: "https://github.com/jaspercurry/JTS/blob/main/docs/HANDOFF-multiroom.md" },
          "Details"),
        ".",
      ),
    ),
  );

  const wrap = h("div", { style: { display: "flex", "flex-direction": "column", gap: "20px" } },
    h("div.control-head", null,
      h("p.eyebrow", null, "Speakers"),
      staleness,
    ),
    selfSection,
    bondCard.el,
    peersSection,
    wakeCard.el,
    note,
  );
  mount.appendChild(wrap);

  return { staleness, selfCard, wakeCard, bondCard, peersCard };
}

// Per-section isolated render: a throw in one section is logged and contained
// so it can't blank the whole page.
function renderInto(container, label, builder) {
  try {
    const node = builder();
    container.textContent = "";
    if (node) container.appendChild(node);
  } catch (e) {
    console.error("rooms: failed to render " + label, e);
  }
}

function update(refs, snap) {
  renderInto(refs.selfCard, "self", () => selfBody(snap.self));
  // The wake card is persistent (interactive state); reconcile its controls
  // rather than rebuild. sync() no-ops while a save is in flight and is itself
  // guarded so a malformed `peering` block can't blank the page.
  try {
    refs.wakeCard.sync((snap.self || {}).peering);
  } catch (e) {
    console.error("rooms: failed to sync wake card", e);
  }
  // Bond card is persistent too (selection + pending save); reconcile its
  // peer options from the full snapshot, guarded so a bad field can't blank.
  try {
    refs.bondCard.sync(snap);
  } catch (e) {
    console.error("rooms: failed to sync bond card", e);
  }
  renderInto(refs.peersCard, "peers", () => peersBody(snap.peers));
  const count = Array.isArray(snap.peers) ? snap.peers.length : 0;
  refs.staleness.textContent =
    "Live · " + count + (count === 1 ? " other speaker" : " other speakers");
}

// ---------------------------------------------------------------------------
// Poll loop.
// ---------------------------------------------------------------------------
const refs = buildPage(root);

async function poll() {
  let snap;
  try {
    snap = await getJSON("rooms.json");
  } catch (e) {
    document.body.classList.add("stale");
    refs.staleness.textContent = "Disconnected (" + e.message + "). Retrying…";
    setTimeout(poll, POLL_MS);
    return;
  }
  document.body.classList.remove("stale");
  update(refs, snap);
  setTimeout(poll, POLL_MS);
}

poll();
