// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// components.js — composable UI pieces built on the shared design system
// (app.css). Everything returns DOM nodes via dom.js, so text content is
// escaped by construction — there is no innerHTML path.

import { h, svg } from "./dom.js";

// A titled section: a cased card title above a card body. Returns the section
// plus the (empty) body container, so the poll loop can re-render just the
// body without rebuilding the title. `.section` / `.info-card` live in app.css.
export function titledCard(title, opts = {}) {
  const body = h(`div.info-card${opts.accent ? ".info-card--accent" : ""}`);
  const section = h("section.section", null,
    h("div.section__head", null, h("h2.section__title", null, title)),
    body,
  );
  return { section, body };
}

// Vital-stat card: status dot + headline value + optional sub + optional
// chart (or pill) slot. Tone colours the dot via the inline --tone prop.
export function statCard({ label, value, sub, tone = "ok", chart }) {
  const card = h("div.stat-card", null,
    h("div.stat-card__head", null,
      h("span.stat-card__dot", { "attr:aria-hidden": "true" }),
      h("p.eyebrow", null, label),
    ),
    h("p.stat-card__value", null, value),
    sub ? h("p.stat-card__sub", null, sub) : null,
    chart ? h("div.stat-card__chart", null, chart) : null,
  );
  card.style.setProperty("--tone", `var(--status-${tone})`);
  return card;
}

// Key/value description list. `rows` is [[key, value], …]; value may be a
// string or a Node.
export function defList(rows) {
  return h("dl.deflist", null,
    rows.flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)]),
  );
}

// Status pill. Tone drives the colour through the inline --tone prop.
export function badge(text, tone = "ok") {
  const el = h("span.badge", null, text);
  el.style.setProperty("--tone", `var(--status-${tone})`);
  return el;
}

// Table. columns: {key, label, align?}[]; rows: object[]; renderCell:
// optional (row, col) => Node | string.
export function table({ columns, rows, modifier = "", renderCell }) {
  const head = h("thead", null,
    h("tr", null, columns.map((c) =>
      h("th", { class: c.align === "right" ? "num" : "" }, c.label))),
  );
  const body = h("tbody", null,
    rows.map((row) =>
      h("tr", null, columns.map((c) => {
        const cellClass = c.align === "right" ? "num" : "";
        const value = renderCell ? renderCell(row, c) : row[c.key];
        return h("td", { class: cellClass }, value);
      }))),
  );
  const cls = ["table", modifier ? `table--${modifier}` : ""].filter(Boolean).join(" ");
  return h("div.table-wrap", null, h("table", { class: cls }, head, body));
}

// Choice card (the Medium/Best audio-conversion toggle). aria-pressed marks
// the active option; onClick fires the apply.
export function choiceCard({ title, body, active, onClick }) {
  return h("button.choice", {
    type: "button",
    onclick: onClick,
    "attr:aria-pressed": active ? "true" : "false",
  },
    h("p.choice__title", null, title),
    h("p.choice__body", null, body),
  );
}

export function actionButton(label, opts = {}) {
  const { variant = "default", onClick } = opts;
  return h(`button.btn.btn--${variant}`, { type: "button", onclick: onClick }, label);
}

// Collapsible section. The open state lives on the element's dataset, so a
// poll that re-renders the body never reopens/closes it.
export function collapsible({ title, open = true, body }) {
  const root = h("section.collapsible", null,
    h("button.collapsible__toggle", {
      type: "button",
      "attr:aria-expanded": String(open),
      onclick() {
        const host = this.closest(".collapsible");
        const next = host.dataset.open !== "true";
        host.dataset.open = String(next);
        this.setAttribute("aria-expanded", String(next));
      },
    },
      svg("svg", {
        viewBox: "0 0 16 16", width: "14", height: "14", fill: "none",
        stroke: "currentColor", "stroke-width": "1.75",
        "stroke-linecap": "round", "stroke-linejoin": "round",
      }, svg("polyline", { points: "5 3 11 8 5 13" })),
      h("h2.eyebrow", null, title),
    ),
    h("div.collapsible__body", null, body),
  );
  root.dataset.open = String(open);
  return root;
}

// Sticky page header: back affordance + centred title. Uses the shared icon
// sprite (#icon-back) that canonical_page() emits into the document.
export function header({ title = "System", backHref = "/" } = {}) {
  return h("header.app-header", null,
    h("div.app-header__row", null,
      h("a.icon-button", { href: backHref, "attr:aria-label": "Home" },
        svg("svg.ico", { "aria-hidden": "true" },
          svg("use", { href: "#icon-back" }))),
      h("h1.app-header__title", null, title),
      h("span"),
    ),
  );
}

// Pulsing "Live · …" indicator. Returns the element plus its label node so
// the staleness text can be updated in place each poll.
export function livePill(initial = "Loading…") {
  const label = h("p.eyebrow", null, initial);
  const el = h("div.live-pill", null,
    h("span.live-pill__dot", { "attr:aria-hidden": "true" }),
    label,
  );
  return { el, label };
}
