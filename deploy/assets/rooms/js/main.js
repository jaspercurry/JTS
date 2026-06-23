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
// file. This module builds DOM exclusively through the h()/svg() helpers
// below, whose text children become document.createTextNode (escaped by
// construction). There is NO innerHTML path and NO inline onclick with
// interpolated strings. The peer click-through href is additionally
// scheme-guarded (http/https only) as defense-in-depth against a poisoned
// mDNS address. The wake-response toggle needs no confirm; the bond card's
// destructive "Dissolve pair" action uses jtsConfirm (the styled <dialog>,
// never native confirm/alert — a static test forbids the natives). A save
// error surfaces inline in the card's status line.

import { getJSON, postJSON } from "/assets/shared/js/http.js";
import { jtsConfirm } from "/assets/shared/js/dialog.js";
import { localWebHost } from "/assets/shared/js/local-web-host.js";
import { airplayLipSyncRow, createFaceCopy, subCornerLabel } from "./grouping-view.js";

const POLL_MS = 7000;
const root = document.getElementById("app");

// ---------------------------------------------------------------------------
// Tiny hyperscript helper — a self-contained twin of the system-status dom.js
// h()/svg(). Kept inline because this page owns only main.js. Text children
// become text nodes, so untrusted strings are escaped by the DOM; there is no
// innerHTML to forget to sanitise.
// ---------------------------------------------------------------------------
const SVG_NS = "http://www.w3.org/2000/svg";

function parseTag(tag) {
  let tagName = "div";
  const classes = [];
  let id = "";
  const match = tag.match(/^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)$/);
  if (match) {
    if (match[1]) tagName = match[1];
    for (const part of (match[2] || "").match(/[.#][\w-]+/g) || []) {
      if (part[0] === ".") classes.push(part.slice(1));
      else id = part.slice(1);
    }
  } else {
    tagName = tag;
  }
  return { tagName, classes, id };
}

function isChildLike(v) {
  return (
    v instanceof Node ||
    Array.isArray(v) ||
    typeof v === "string" ||
    typeof v === "number"
  );
}

function appendChildren(el, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) appendChildren(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
}

function h(tag, props, ...children) {
  const { tagName, classes, id } = parseTag(tag);
  const el = document.createElement(tagName);
  if (id) el.id = id;
  if (classes.length) el.className = classes.join(" ");
  if (props && typeof props === "object" && !isChildLike(props)) {
    for (const key in props) {
      const value = props[key];
      if (value == null || value === false) continue;
      if (key === "class" || key === "className") {
        el.className = el.className ? `${el.className} ${value}` : value;
      } else if (key === "style" && typeof value === "object") {
        for (const prop in value) {
          if (prop.includes("-")) el.style.setProperty(prop, value[prop]);
          else el.style[prop] = value[prop];
        }
      } else if (key.startsWith("attr:")) {
        el.setAttribute(key.slice(5), value);
      } else if (key in el) {
        try { el[key] = value; } catch { el.setAttribute(key, value); }
      } else {
        el.setAttribute(key, value);
      }
    }
  } else if (props !== undefined) {
    children.unshift(props);
  }
  appendChildren(el, children);
  return el;
}

function svg(tag, props, ...children) {
  const { tagName, classes, id } = parseTag(tag);
  const el = document.createElementNS(SVG_NS, tagName);
  if (id) el.setAttribute("id", id);
  if (classes.length) el.setAttribute("class", classes.join(" "));
  if (props && typeof props === "object" && !isChildLike(props)) {
    for (const key in props) {
      const value = props[key];
      if (value == null || value === false) continue;
      if (key === "class" || key === "className") el.setAttribute("class", value);
      else el.setAttribute(key, value);
    }
  } else if (props !== undefined) {
    children.unshift(props);
  }
  for (const c of children) {
    if (c == null || c === false) continue;
    if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
  return el;
}

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
  // Latest reachable peer rows from the poll — kept so the bond submit
  // can carry the picked peer's directory NAME (the leader records it
  // as the bond roster for DHCP re-resolution).
  let lastReachable = [];

  // --- Create face: pick a sibling for the right channel ------------------
  const select = h("select.bond-select", {
    "attr:aria-label": "Speaker for the right channel",
  });
  const createBtn = h("button.btn.btn--primary",
    { type: "button" }, "Create stereo pair");

  // Role of the picked follower: a stereo-pair right channel (default,
  // unchanged behaviour) or a subwoofer that low-passes a mono sum locally.
  const roleSelect = h("select.bond-select", {
    "attr:aria-label": "Role for the picked speaker",
  },
    h("option", { value: "right" }, "Right channel (stereo pair)"),
    h("option", { value: "sub" }, "Subwoofer"));

  // Corner for the subwoofer's local low-pass. 40..200 Hz, default 80; only
  // sent (and only shown) when the picked speaker is a subwoofer.
  const crossoverInput = h("input.bond-crossover", {
    type: "number", min: "40", max: "200", step: "1", value: "80",
    "attr:aria-label": "Subwoofer low-pass corner (Hz)",
  });
  const crossoverRow = h("div.bond-row", null,
    h("span.bond-row__label", null, "Low-pass (Hz)"),
    crossoverInput);

  const createIntro = h("p.info-card__note", null,
    "Create a stereo pair: this speaker plays the left channel and the one " +
    "you pick plays the right. Both are configured automatically — no " +
    "settings files, no per-speaker setup.");
  const pickerLabel = h("span.bond-row__label", null,
    "This speaker is Left — pair with");
  const picker = h("div.bond-row", null,
    pickerLabel,
    select,
    roleSelect,
    createBtn,
  );

  // Retitle the create face to match the picked role: a button reading "Create
  // stereo pair" must never be how you add a subwoofer. The corner input is
  // shown only for the sub role; the picker keeps its shape otherwise (no
  // surprise field). title is owned by showFace (bonded vs create) and re-read
  // there from the same helper, so a poll can't revert the create-face title.
  // NB: the initial call is deferred until after `title` is defined (below) to
  // stay out of its const temporal-dead-zone.
  function syncRoleControls() {
    const copy = createFaceCopy(roleSelect.value);
    crossoverRow.style.display = roleSelect.value === "sub" ? "" : "none";
    title.textContent = copy.title;
    createIntro.textContent = copy.intro;
    pickerLabel.textContent = copy.label;
    createBtn.textContent = copy.button;
  }
  roleSelect.addEventListener("change", syncRoleControls);

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

  // Pair-balance trim: one row per member, \u00b10.5 dB nudges. Delta
  // semantics — the server resolves the peer and returns the new value,
  // so this card carries no peer addressing or trim state.
  function makeTrimRow(label, target) {
    const value = h("span.trim-value", null, "\u2014");
    const minus = h("button.btn", { type: "button",
      "attr:aria-label": label + " quieter" }, "\u22120.5 dB");
    const plus = h("button.btn", { type: "button",
      "attr:aria-label": label + " louder" }, "+0.5 dB");
    async function nudge(delta) {
      minus.disabled = plus.disabled = true;
      try {
        const data = await postJSON("trim", { target, delta_db: delta });
        value.textContent = data.trim_db.toFixed(1) + " dB";
        status.textContent = "";
      } catch (e) {
        console.error("rooms: trim failed", e);
        status.textContent = "Couldn't trim \u2014 " + describeBondFailure(e);
      } finally {
        minus.disabled = plus.disabled = false;
      }
    }
    minus.addEventListener("click", () => nudge(-0.5));
    plus.addEventListener("click", () => nudge(0.5));
    return {
      el: h("div.trim-row", null,
        h("span.trim-row__label", null, label), minus, value, plus),
      value,
    };
  }
  const trimSelf = makeTrimRow("This speaker", "self");
  const trimPeer = makeTrimRow("Paired speaker", "peer");
  const trimIntro = h("p.info-card__note", null,
    "Balance the pair by ear: trim the LOUDER speaker down (0.0 dB = " +
    "no trim; trims only attenuate, never boost).");
  const balanceLink = h("a", {
    href: "https://" + location.hostname + "/balance/",
  }, "Balance automatically with your phone’s microphone ↗");
  const balanceNote = h("p.info-card__note", null,
    "Or: ", balanceLink,
    " (uses the same measurement certificate as room correction).");
  const trimBlock = h("div.trim-block", null,
    trimIntro, trimSelf.el, trimPeer.el, balanceNote);

  const status = h("p.bond-status.info-card__note",
    { "attr:aria-live": "polite" });

  const body = h("div.info-card", null,
    createIntro,
    picker,
    crossoverRow,
    dissolveIntro,
    currentSummary,
    trimBlock,
    dissolveRow,
    status,
  );
  const title = h("h2.section__title", null, "Create a stereo pair");
  const card = h("section.section", null,
    h("div.section__head", null, title),
    body,
  );
  // Initial create-face copy (deferred to here — out of `title`'s TDZ above).
  syncRoleControls();

  function setEnabled(on) {
    select.disabled = !on;
    roleSelect.disabled = !on;
    crossoverInput.disabled = !on;
    createBtn.disabled = !on;
  }

  // Show exactly one face (create vs. dissolve). Bonded → dissolve; else create.
  function showFace(bonded) {
    createIntro.style.display = bonded ? "none" : "";
    picker.style.display = bonded ? "none" : "";
    // The corner input rides the create face AND the subwoofer role; hide it
    // on the dissolve face regardless, then let syncRoleControls own it back.
    crossoverRow.style.display =
      bonded ? "none" : (roleSelect.value === "sub" ? "" : "none");
    dissolveIntro.style.display = bonded ? "" : "none";
    currentSummary.style.display = bonded ? "" : "none";
    dissolveRow.style.display = bonded ? "" : "none";
    trimBlock.style.display = bonded ? "" : "none";
    // Bonded → a neutral dissolve-face title; create → the role-aware title
    // (so a 7 s poll re-asserts "Add a wireless subwoofer", not "stereo pair").
    title.textContent = bonded
      ? "Speaker grouping"
      : createFaceCopy(roleSelect.value).title;
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
    const bonded = !!(g.enabled && g.bond_id && !g.error);

    showFace(bonded);

    if (bonded) {
      // Dissolve face: rebuild the summary text from the current grouping.
      currentSummary.textContent = "";
      appendChildren(currentSummary, summarize(g));
      if (typeof g.trim_db === "number") {
        trimSelf.value.textContent = g.trim_db.toFixed(1) + " dB";
      }
      dissolveBtn.disabled = false;
      // Swap is only well-defined for a left/right pair; the backend
      // re-validates (exactly one same-bond peer) — this just avoids
      // showing the button on a mono/multi-member bond.
      swapBtn.style.display =
        (g.channel === "left" || g.channel === "right") ? "" : "none";
      swapBtn.disabled = false;
      return;
    }

    // Create face: refresh the peer options.
    const peers = Array.isArray(snap && snap.peers) ? snap.peers : [];
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
    lastReachable = reachable;
    for (const p of reachable) {
      const label = (p.name || p.address) + " (" + p.address + ")";
      select.appendChild(h("option", { value: p.address }, label));
    }
    if (reachable.some((p) => p.address === prev)) select.value = prev;  // keep selection
    setEnabled(true);
  }

  async function create() {
    const rightAddr = select.value;
    if (!selfAddr || !rightAddr) return;
    const sub = roleSelect.value === "sub";
    saving = true;
    setEnabled(false);
    status.textContent = sub ? "Adding the subwoofer…" : "Creating the pair…";
    try {
      const picked = lastReachable.find((p) => p.address === rightAddr);
      const pickedName = (picked && picked.name) || "";
      // Subwoofer bond: the picked speaker plays only the low end (a clip-safe
      // mono sum, low-passed locally in jasper-outputd). The leader then plays
      // the WHOLE program full-range as channel "stereo" — not "left" — because
      // a single main box + sub is one full-range speaker plus lows, not half a
      // stereo pair. (Bass-management high-pass of the main is out of scope; it
      // needs an active leader.) crossover_hz is clamped server-side.
      const members = sub
        ? [
            { addr: selfAddr, role: "leader", channel: "stereo" },
            { addr: rightAddr, role: "follower", channel: "sub",
              crossover_hz: Number(crossoverInput.value),
              name: pickedName },
          ]
        : [
            { addr: selfAddr, role: "leader", channel: "left" },
            { addr: rightAddr, role: "follower", channel: "right",
              name: pickedName },
          ];
      const data = await postJSON("bond", { members });
      if (data && data.ok) {
        status.textContent = sub
          ? "Subwoofer added — both speakers are configuring (~10s)."
          : "Stereo pair created — both speakers are configuring (~10s).";
      }
    } catch (e) {
      console.error("rooms: bond create failed", e);
      status.textContent =
        (sub ? "Couldn't add the subwoofer — " : "Couldn't pair — ") +
        describeBondFailure(e);
    } finally {
      saving = false;
      setEnabled(true);
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

  createBtn.addEventListener("click", create);
  swapBtn.addEventListener("click", swap);
  dissolveBtn.addEventListener("click", dissolve);
  // Default to the create face until the first sync() proves we're bonded, so
  // both faces are never visible at once during the initial paint.
  showFace(false);
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
