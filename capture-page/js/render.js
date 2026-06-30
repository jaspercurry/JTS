// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// The fixed, trusted, DATA-ONLY renderer for the capture page (build step 3).
//
// THIS IS A SECURITY BOUNDARY, NOT A STYLE CHOICE (docs/phone-mic-relay-plan.md
// §8). The page holds the microphone AND the E2E content_key (in its URL
// fragment, readable by its own JS). The capture_spec it renders arrives across
// the UNTRUSTED relay. So the renderer treats the spec as pure DATA:
//
//   - It maps a CLOSED vocabulary of component types to a FIXED set of element
//     tags (h1/p/ol/li/div/button). A `type` it does not know renders NOTHING.
//     The spec can never choose the tag, so it can never make a <script>,
//     <iframe>, <img onerror>, etc.
//   - All text is written via `textContent` — never `innerHTML`. `<script>...`
//     in a heading becomes inert text.
//   - Button actions are an ALLOWLISTED string that SELECTS a host-provided
//     handler; the spec never carries the handler. (host-mediated indirection —
//     see docs/extensibility.md §1.)
//   - Theme is a token mapped to a fixed CSS value by theme.js; the spec never
//     provides raw CSS.
//
// A hostile payload's worst case is "wrong text on screen" — never code
// execution. Pinned by tests/js/capture_render_test.mjs.

import { resolveTheme } from "./theme.js";

const COMPONENT_TYPES = new Set(["heading", "steps", "level_meter", "button", "note"]);
const BUTTON_ACTIONS = new Set(["begin_capture", "retry"]);

function el(doc, tag, className) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  return node;
}

// The ONLY way text from the spec reaches the DOM. textContent never parses
// markup, so any HTML/JS in the string is inert.
function setText(node, text) {
  node.textContent = typeof text === "string" ? text : "";
}

function renderComponent(component, doc, handlers, refs) {
  if (!component || typeof component !== "object") return null;
  const type = component.type;
  if (!COMPONENT_TYPES.has(type)) return null; // unknown type -> render nothing

  if (type === "heading") {
    const h = el(doc, "h1", "cap-heading");
    setText(h, component.text);
    return h;
  }
  if (type === "note") {
    const p = el(doc, "p", "cap-note");
    setText(p, component.text);
    return p;
  }
  if (type === "steps") {
    const ol = el(doc, "ol", "cap-steps");
    const items = Array.isArray(component.items) ? component.items : [];
    for (const item of items) {
      const li = el(doc, "li");
      setText(li, item);
      ol.appendChild(li);
    }
    return ol;
  }
  if (type === "level_meter") {
    const wrap = el(doc, "div", "cap-meter");
    wrap.setAttribute("role", "meter");
    const bar = el(doc, "div", "cap-meter-bar");
    wrap.appendChild(bar);
    refs.levelMeters.push(bar);
    return wrap;
  }
  if (type === "button") {
    const b = el(doc, "button", "cap-button");
    b.type = "button";
    setText(b, component.label);
    // The action is a SELECTOR into host-provided handlers, never a handler.
    const action = BUTTON_ACTIONS.has(component.action) ? component.action : null;
    if (action) {
      b.setAttribute("data-action", action);
      const handler = handlers && handlers[action];
      if (typeof handler === "function") b.addEventListener("click", handler);
    }
    refs.buttons.push({ action, el: b });
    return b;
  }
  return null;
}

// Render spec.ui into rootEl as data. Returns refs to interactive nodes so the
// orchestrator can wire the level meter / read button state. `handlers` maps an
// allowlisted action name to the page's own click handler.
export function renderScreen(rootEl, spec, options = {}) {
  const doc = options.doc || (rootEl && rootEl.ownerDocument) || globalThis.document;
  const handlers = options.handlers || {};

  while (rootEl.firstChild) rootEl.removeChild(rootEl.firstChild);

  const ui = (spec && typeof spec === "object" && spec.ui) || {};
  const theme = resolveTheme(ui.theme);
  // Theme is applied as CSS variables from FIXED values — never raw CSS.
  rootEl.style.setProperty("--cap-accent", theme.accentVar);
  rootEl.style.setProperty("--cap-font", theme.fontVar);

  const refs = { buttons: [], levelMeters: [] };
  const screen = Array.isArray(ui.screen) ? ui.screen : [];
  for (const component of screen) {
    const node = renderComponent(component, doc, handlers, refs);
    if (node) rootEl.appendChild(node);
  }
  return refs;
}

export const _internal = { COMPONENT_TYPES, BUTTON_ACTIONS };
