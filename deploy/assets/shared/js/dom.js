// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// dom.js — tiny hyperscript helper, zero dependencies. The shared owner of
// the text-node DOM builder, promoted out of the per-page module graphs
// (same shared-by-promotion path as dialog.js / escape.js / http.js). Import
// it by absolute path: `import { h, svg } from "/assets/shared/js/dom.js"`.
//
// h("div.foo", { onclick: fn }, "hello", child)
//   - tag may include `.class` and `#id` shortcuts.
//   - props: any DOM property, attr (via `attr:foo`), style object, or
//     dataset object.
//   - children: strings, numbers, Nodes, arrays, null/undefined (skipped).
//
// Text children become text nodes, so untrusted values (conversation
// transcripts, provider names, HA URLs, AirPlay event detail, Camilla config
// paths, service names, timestamps) are escaped by the DOM — there is no
// innerHTML path to forget to sanitise. This is the basis of the
// "untrusted strings never reach innerHTML" safety argument on every page
// built with it, so it lives in one place and a conventions test forbids any
// page module from re-declaring h()/svg().

const ATTR_PREFIX = "attr:";

export function h(tag, props, ...children) {
  const { tagName, classes, id } = parseTag(tag);
  const el = document.createElement(tagName);
  if (id) el.id = id;
  if (classes.length) el.className = classes.join(" ");

  if (props && typeof props === "object" && !isChildLike(props)) {
    applyProps(el, props);
  } else if (props !== undefined) {
    children.unshift(props);
  }

  appendChildren(el, children);
  return el;
}

function parseTag(tag) {
  let tagName = "div";
  const classes = [];
  let id = "";
  const match = tag.match(/^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)$/);
  if (match) {
    if (match[1]) tagName = match[1];
    if (match[2]) {
      for (const part of match[2].match(/[.#][\w-]+/g) || []) {
        if (part[0] === ".") classes.push(part.slice(1));
        else id = part.slice(1);
      }
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

function applyProps(el, props) {
  for (const key in props) {
    const value = props[key];
    if (value == null || value === false) continue;
    if (key === "class" || key === "className") {
      el.className = el.className ? `${el.className} ${value}` : value;
    } else if (key === "style" && typeof value === "object") {
      for (const prop in value) {
        // Dashed names (custom props like --tone, or kebab CSS properties)
        // must go through setProperty; camelCase props assign directly.
        if (prop.includes("-")) el.style.setProperty(prop, value[prop]);
        else el.style[prop] = value[prop];
      }
    } else if (key === "dataset" && typeof value === "object") {
      Object.assign(el.dataset, value);
    } else if (key.startsWith(ATTR_PREFIX)) {
      el.setAttribute(key.slice(ATTR_PREFIX.length), value);
    } else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key in el) {
      try { el[key] = value; } catch { el.setAttribute(key, value); }
    } else {
      el.setAttribute(key, value);
    }
  }
}

function appendChildren(el, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) appendChildren(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
}

// SVG helper (separate namespace; same API minus the property fast-path).
const SVG_NS = "http://www.w3.org/2000/svg";
export function svg(tag, props, ...children) {
  const { tagName, classes, id } = parseTag(tag);
  const el = document.createElementNS(SVG_NS, tagName);
  if (id) el.setAttribute("id", id);
  if (classes.length) el.setAttribute("class", classes.join(" "));
  if (props && typeof props === "object" && !isChildLike(props)) {
    for (const key in props) {
      const value = props[key];
      if (value == null || value === false) continue;
      if (key === "class" || key === "className") {
        el.setAttribute("class", value);
      } else if (key.startsWith("on") && typeof value === "function") {
        el.addEventListener(key.slice(2).toLowerCase(), value);
      } else {
        el.setAttribute(key, value);
      }
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
