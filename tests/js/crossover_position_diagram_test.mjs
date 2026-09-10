// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Pins #3629's per-position picture (#1941 R11): the speaker at the mark and
// an arrow to the prompt's own target, drawn straight off the SAME signed
// degrees/vertical_deg fields the wire payload's `position_pending` already
// carries (jasper.active_speaker.crossover_v2.position_gate) -- no new
// backend data, and the sign convention matches capture_plan.py's prompt
// copy: negative degrees is LEFT, positive is RIGHT; negative vertical_deg is
// BELOW mark height, positive is ABOVE.

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { loadEsm, repoPath } from "./_loader.mjs";

// A minimal SVG element double: setAttribute/getAttribute + a children
// array via appendChild. dom.js's svg() only reaches these two methods for a
// caller (like position-diagram.js) that never threads text children
// through svg() itself -- see tests/js/_dom.mjs's installFixedDocument,
// whose createElementNS does the same thing for the full main.js harness.
class FakeSvgElement {
  constructor(tag) {
    this.tag = tag;
    this.attributes = {};
    this.children = [];
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name]
      : null;
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
}
// dom.js's isChildLike() references the bare `Node` global even for a
// props-only call (the h(tag, child)-shorthand guard) -- it never receives
// one of these as a value here, but it must exist to be referenced.
globalThis.Node = class Node {};
globalThis.document = {
  createElementNS: (_ns, tag) => new FakeSvgElement(tag),
};

// The real svg() implementation, not a stub: position-diagram.js's only
// import is dom.js's absolute /assets/... specifier, which Node cannot
// resolve on its own, so the rewrite below points it at the real file
// instead of faking svg() out too.
const { positionDiagram, positionCaption } = await loadEsm(
  repoPath("deploy/assets/correction/js/crossover/position-diagram.js"),
  {
    rewrite: [[
      /from ["']\/assets\/shared\/js\/dom\.js["']/,
      `from "${pathToFileURL(repoPath("deploy/assets/shared/js/dom.js")).href}"`,
    ]],
  },
);

let passed = 0;
function check(condition, message) {
  assert.ok(condition, message);
  passed += 1;
}

// --- positionCaption: every sign combination, and the on-mark case --------

check(positionCaption(0, 0) === "On the mark", "no bearing at all reads as on the mark");
check(positionCaption(7, 0) === "Move right of the mark", "positive degrees is RIGHT");
check(positionCaption(-7, 0) === "Move left of the mark", "negative degrees is LEFT");
check(positionCaption(0, 5) === "Move above mark height", "positive vertical_deg is ABOVE");
check(positionCaption(0, -5) === "Move below mark height", "negative vertical_deg is BELOW");
check(
  positionCaption(-7, 5) === "Move left of the mark and above mark height",
  "a compound row names both axes, horizontal first",
);

// --- positionDiagram: structure and geometry -------------------------------

const onMark = positionDiagram(0, 0);
check(onMark.tag === "svg", "the root is an <svg>");
check(onMark.attributes.role === "img", "the picture is exposed as an image to assistive tech");
check(
  onMark.attributes["aria-label"] === positionCaption(0, 0),
  "the aria-label is the same caption the visible text uses",
);
check(
  onMark.children.length === 3,
  "on the mark, there is no arrow to draw: speaker + mark + mic only",
);

const moved = positionDiagram(7, 0);
check(
  moved.children.length === 5,
  "off the mark, the picture also draws the arrow line and its head",
);

const speakerEl = moved.children[0];
const markEl = moved.children[1];
const micEl = moved.children[moved.children.length - 1];
check(speakerEl.tag === "rect", "the speaker is the first shape drawn");
check(markEl.tag === "circle", "the mark is the second shape drawn");
check(micEl.tag === "circle", "the mic's target spot is the last shape drawn");

const markX = Number(markEl.attributes.cx);
const rightMicX = Number(micEl.attributes.cx);
check(rightMicX > markX, "a positive (RIGHT) bearing places the mic right of the mark");

const leftMic = positionDiagram(-7, 0).children.at(-1);
check(Number(leftMic.attributes.cx) < markX, "a negative (LEFT) bearing places the mic left of the mark");

const markY = Number(markEl.attributes.cy);
const aboveMic = positionDiagram(0, 12).children.at(-1);
const belowMic = positionDiagram(0, -12).children.at(-1);
check(Number(aboveMic.attributes.cy) < markY, "a positive (ABOVE) elevation raises the mic on the picture");
check(Number(belowMic.attributes.cy) > Number(aboveMic.attributes.cy), "a negative (BELOW) elevation is drawn lower than an ABOVE one");

// A real walk never asks for more than a desk-scale move (capture_plan.py's
// widest prompted row is 60 cm at ~1.2 m, well under a display clamp), but
// the picture must not crash or run the dot off its own canvas for a
// pathological input.
const farMic = positionDiagram(1000, 0).children.at(-1);
check(
  Number(farMic.attributes.cx) <= markX + 60,
  "an extreme bearing clamps rather than running the mic off the picture",
);
check(Number.isFinite(Number(farMic.attributes.cx)), "the clamp never produces NaN/Infinity coordinates");

console.log(JSON.stringify({ ok: true, passed }));
