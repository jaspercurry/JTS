// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// views.js — build-once / update-on-fetch rendering for /chat/.
//
// The conversation store treats transcript text and data_json as untrusted.
// This module never uses innerHTML; all visible content is built with text
// nodes through dom.js.

import { h } from "./dom.js";
import { actionButton, badge, header, livePill, table, titledCard } from "./components.js";

const NO_USER_TRANSCRIPT = "No user transcript captured for this turn.";
const NO_ASSISTANT_TRANSCRIPT = "No transcript for this turn.";

export function buildPage(root, handlers, opts = {}) {
  const live = livePill();
  const filter = titledCard("Filter");
  const history = titledCard("Recent turns");
  const sinceInput = h("input", {
    id: "chat-since",
    type: "date",
    value: opts.initialDate || "",
  });
  const filterStatus = h("p.info-card__note", { "attr:aria-live": "polite" });
  const errorDetails = actionButton("Show error", {
    variant: "ghost",
    onClick: handlers.showErrorDetails,
  });
  errorDetails.hidden = true;

  filter.body.append(
    h("form.chat-filter", {
      onsubmit(e) {
        e.preventDefault();
        handlers.applyFilter(sinceInput.value);
      },
    },
      h("div.field.chat-filter__field", null,
        h("label", { for: "chat-since" }, "Since"),
        sinceInput),
      h("div.btn-row.chat-filter__actions", null,
        actionButton("Apply", {
          variant: "primary",
          onClick(e) {
            e.preventDefault();
            handlers.applyFilter(sinceInput.value);
          },
        }),
        actionButton("Clear filter", {
          variant: "ghost",
          onClick(e) {
            e.preventDefault();
            sinceInput.value = "";
            handlers.clearFilter();
          },
        })),
    ),
    filterStatus,
    errorDetails,
  );

  const historyBody = h("div");
  history.body.append(historyBody);

  root.replaceChildren(
    header({ title: "Chat", backHref: "/" }),
    h("main.app-main.chat-main", null, live.el, filter.section, history.section),
  );
  root.setAttribute("aria-busy", "false");

  return {
    staleness: live.label,
    filterStatus,
    historyBody,
    errorDetails,
    sinceInput,
    _memo: {},
  };
}

export function dateValueToSince(value) {
  const trimmed = String(value || "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) return "";
  const [year, month, day] = trimmed.split("-").map((part) => Number(part));
  const localMidnight = new Date(year, month - 1, day, 0, 0, 0, 0);
  if (
    localMidnight.getFullYear() !== year ||
    localMidnight.getMonth() !== month - 1 ||
    localMidnight.getDate() !== day
  ) {
    return "";
  }
  return isoNoMillis(localMidnight);
}

export function sinceToDateValue(value) {
  const raw = String(value || "").trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw;
  const parsed = new Date(raw);
  if (!Number.isNaN(parsed.getTime())) return localDateValue(parsed);
  const match = raw.match(/^(\d{4}-\d{2}-\d{2})/);
  return match ? match[1] : "";
}

export function normalizeSince(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) return dateValueToSince(trimmed);
  return trimmed;
}

export function updateError(refs, err, state) {
  document.body.classList.add("stale");
  refs.staleness.textContent = "Disconnected. Retrying...";
  refs.filterStatus.textContent = state.since
    ? `Could not load turns since ${state.since}.`
    : "Could not load conversation history.";
  refs.errorDetails.hidden = false;
  renderSection(refs, "history-error", refs.historyBody, String(err && err.message), () =>
    h("p.info-card__note", null, "Conversation history is temporarily unavailable."));
}

export function update(refs, payload, state) {
  document.body.classList.remove("stale");
  refs.errorDetails.hidden = true;
  const snap = payload || {};
  const turns = newestFirst(Array.isArray(snap.turns) ? snap.turns : []);
  const limit = Number.isFinite(snap.limit) ? snap.limit : turns.length;

  if (snap.available === false) {
    refs.staleness.textContent = "History store unavailable.";
  } else {
    refs.staleness.textContent = `Live · ${turns.length} ${plural(turns.length, "turn")}`;
  }
  refs.filterStatus.textContent = state.since
    ? `Showing turns since ${state.since}.`
    : `Showing the latest ${limit} ${plural(limit, "turn")}.`;

  renderSection(
    refs,
    "history",
    refs.historyBody,
    { available: snap.available, turns, since: state.since, limit },
    () => historyContent(snap, turns),
  );
}

function historyContent(snap, turns) {
  if (snap.available === false) {
    return h("p.info-card__note", null,
      "The conversation-history store is not available on this speaker yet.");
  }
  if (!turns.length) {
    return h("p.info-card__note", null, "No conversation turns match this filter.");
  }
  return table({
    columns: [
      { key: "time", label: "Time" },
      { key: "provider", label: "Provider" },
      { key: "turn", label: "User -> Assistant" },
    ],
    rows: turns,
    modifier: "chat",
    renderCell: renderTurnCell,
  });
}

function renderTurnCell(turn, col) {
  if (col.key === "time") {
    return h("time", { dateTime: textOrEmpty(turn.ts_utc) },
      formatTimestamp(turn.ts_utc));
  }
  if (col.key === "provider") {
    const parts = [h("span.chat-provider__name", null, providerLabel(turn.provider))];
    if (isResearchTurn(turn)) parts.push(badge("Research", "warn"));
    return h("div.chat-provider", null, parts);
  }
  return h("div.chat-pair", null,
    transcriptBlock("User", turn.user_text, NO_USER_TRANSCRIPT),
    h("div.chat-pair__connector", { "attr:aria-hidden": "true" }, "->"),
    transcriptBlock("Assistant", turn.assistant_text, NO_ASSISTANT_TRANSCRIPT),
  );
}

function transcriptBlock(label, text, missingText) {
  const missing = text == null;
  return h(`div.chat-transcript${missing ? ".chat-transcript--missing" : ""}`, null,
    h("p.chat-transcript__label", null, label),
    h("p.chat-transcript__text", null, missing ? missingText : String(text)),
  );
}

function isResearchTurn(turn) {
  const parsed = parseDataJson(turn && turn.data_json);
  return !!(parsed && parsed.kind === "research");
}

function parseDataJson(raw) {
  if (typeof raw !== "string" || raw.trim() === "") return null;
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    return parsed;
  } catch (_) {
    return null;
  }
}

function newestFirst(turns) {
  return turns.slice().sort((a, b) => {
    const ts = textOrEmpty(b.ts_utc).localeCompare(textOrEmpty(a.ts_utc));
    if (ts !== 0) return ts;
    return textOrEmpty(b.id).localeCompare(textOrEmpty(a.id));
  });
}

function renderSection(refs, key, container, data, build) {
  let memo = null;
  try { memo = JSON.stringify(data); } catch (_) { /* render every time */ }
  if (memo !== null && refs._memo[key] === memo) return;
  try {
    const out = build();
    container.replaceChildren(...(Array.isArray(out) ? out : [out]));
    refs._memo[key] = memo;
  } catch (e) {
    console.error(`chat: rendering section '${key}' failed`, e);
    refs._memo[key] = null;
    container.replaceChildren(
      h("p.info-card__note", null, "Couldn't render this section; see the console."));
  }
}

function formatTimestamp(value) {
  const raw = textOrEmpty(value);
  if (!raw) return "Unknown";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function providerLabel(value) {
  const raw = textOrEmpty(value);
  return raw || "Unknown";
}

function textOrEmpty(value) {
  return value == null ? "" : String(value);
}

function plural(count, noun) {
  return count === 1 ? noun : `${noun}s`;
}

function localDateValue(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function isoNoMillis(date) {
  return date.toISOString().replace(".000Z", "Z");
}
