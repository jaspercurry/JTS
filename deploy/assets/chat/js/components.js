// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// components.js — small chat-page UI primitives.
//
// These mirror the /system/ module graph and build everything with dom.js so
// untrusted transcript strings remain text nodes.

import { h, svg } from "./dom.js";

export function titledCard(title, opts = {}) {
  const body = h(`div.info-card${opts.accent ? ".info-card--accent" : ""}`);
  const section = h("section.section", null,
    h("div.section__head", null, h("h2.section__title", null, title)),
    body,
  );
  return { section, body };
}

export function badge(text, tone = "ok") {
  const el = h("span.badge", null, text);
  el.style.setProperty("--tone", `var(--status-${tone})`);
  return el;
}

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

export function actionButton(label, opts = {}) {
  const { variant = "default", onClick } = opts;
  return h(`button.btn.btn--${variant}`, { type: "button", onclick: onClick }, label);
}

export function header({ title = "Chat", backHref = "/" } = {}) {
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

export function livePill(initial = "Loading...") {
  const label = h("p.eyebrow", null, initial);
  const el = h("div.live-pill", null,
    h("span.live-pill__dot", { "attr:aria-hidden": "true" }),
    label,
  );
  return { el, label };
}
