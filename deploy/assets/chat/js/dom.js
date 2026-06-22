// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// dom.js — tiny DOM builder copied from the /system/ static module graph.
//
// String children become text nodes, so untrusted conversation transcripts,
// provider names, timestamps, and metadata never pass through innerHTML.

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
      try { el[key] = value; } catch (_) { el.setAttribute(key, value); }
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
