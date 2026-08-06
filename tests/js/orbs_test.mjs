// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Runtime tests for the shared thinking-orb component.
//
// deploy/assets/shared/js/orbs.js is a port of upstream MIT draw math, and a
// port is exactly the kind of change that "looks right" while quietly
// producing NaN coordinates, dots outside the canvas, or a single flat colour.
// The static contracts (attribution, token ownership, state/mode coverage)
// live in tests/test_web_orbs_module.py; this harness actually RUNS the
// renderer.
//
// The engine half needs no DOM — orbs.js guards its document/window touches —
// so node can import it directly. createOrb() does need a DOM, and gets the
// minimal stub below, because the token plumbing it owns (--orb-ink /
// --orb-paper -> rgb) is the JTS-specific part most worth pinning.
import assert from "node:assert/strict";

import { runTestFunctions } from "./run_test_functions.mjs";

const modulePath = process.argv[2] || "../../deploy/assets/shared/js/orbs.js";

let passed = 0;
const tests = [];
function test(name, fn) {
  tests.push(Object.defineProperty(fn, "name", { value: name }));
}

// --- a canvas context that records what was asked of it --------------------

function recordingCtx() {
  const rec = { dots: [], lines: [], fills: [], strokes: [] };
  let pending = null;
  return {
    rec,
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 0,
    setTransform() {},
    clearRect() {},
    beginPath() {},
    moveTo(x, y) { pending = [x, y]; },
    lineTo(x, y) {
      rec.lines.push([...(pending || [0, 0]), x, y]);
      rec.strokes.push(this.strokeStyle);
    },
    stroke() {},
    arc(x, y, r) { pending = [x, y, r]; },
    fill() {
      if (pending && pending.length === 3) {
        rec.dots.push(pending);
        rec.fills.push(this.fillStyle);
      }
    },
    fillRect() {},
    getImageData: () => ({ data: [0, 0, 0, 255] }),
  };
}

function parseRGBA(s) {
  const m = /^rgba\((\d+),(\d+),(\d+),([\d.]+)\)$/.exec(s);
  assert.ok(m, `fill style is not the expected rgba form: ${s}`);
  return [Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4])];
}

const engine = await import(modulePath);
const { ORB_STATES, MODE_DRAWS, resolvePreset, createOrb } = engine;

// A ramp shaped like the one orbs.js builds internally, with endpoints chosen
// to be unmistakable if the renderer ignores them.
function testRamp(ink, paper) {
  const lut = [];
  for (let i = 0; i < 64; i++) {
    const f = i / 63;
    lut.push(
      `${Math.round(ink[0] + (paper[0] - ink[0]) * f)},` +
      `${Math.round(ink[1] + (paper[1] - ink[1]) * f)},` +
      `${Math.round(ink[2] + (paper[2] - ink[2]) * f)}`
    );
  }
  return { ink, paper, lut };
}

const RAMP = testRamp([10, 20, 30], [200, 210, 220]);
const SAMPLE_TIMES = [0, 0.37, 1.13, 2.9, 5.5, 11.7, 23.1];

function renderState(state, sizeKey, size, ramp = RAMP, times = SAMPLE_TIMES) {
  const preset = resolvePreset(state, sizeKey);
  const frames = [];
  for (const raw of times) {
    const ctx = recordingCtx();
    MODE_DRAWS[preset.mode](ctx, size, raw * preset.speed, ramp, preset.opts);
    frames.push(ctx.rec);
  }
  return frames;
}

// --- the engine ------------------------------------------------------------

test("module exports the documented surface", () => {
  assert.equal(typeof createOrb, "function");
  assert.equal(typeof resolvePreset, "function");
  assert.equal(ORB_STATES.length, 9);
  for (const state of ORB_STATES) {
    const preset = resolvePreset(state, 64);
    assert.equal(typeof MODE_DRAWS[preset.mode], "function", `no painter for ${state}`);
    assert.ok(preset.speed > 0, `${state} has a non-positive speed`);
  }
  passed++;
});

test("every state renders finite, in-frame geometry at both sizes", () => {
  for (const [sizeKey, size] of [[64, 96], [20, 20]]) {
    for (const state of ORB_STATES) {
      const frames = renderState(state, sizeKey, size);
      const total = frames.reduce((n, f) => n + f.dots.length, 0);
      assert.ok(
        total / frames.length >= 5,
        `${state}@${sizeKey} drew ${total / frames.length} dots/frame — too few to be a real orb`
      );
      for (const frame of frames) {
        for (const [x, y, r] of frame.dots) {
          assert.ok(Number.isFinite(x) && Number.isFinite(y) && Number.isFinite(r),
            `${state}@${sizeKey} produced a non-finite dot: ${[x, y, r]}`);
          assert.ok(r > 0, `${state}@${sizeKey} produced a non-positive radius ${r}`);
          // Dots may sit slightly proud of the box; a projection sign error
          // sends them far outside it.
          assert.ok(x > -size * 0.25 && x < size * 1.25 && y > -size * 0.25 && y < size * 1.25,
            `${state}@${sizeKey} drew outside the canvas at ${[x, y]}`);
        }
      }
    }
  }
  passed++;
});

test("rendering is deterministic — the same t gives the same frame", () => {
  // No Math.random() survived the port; upstream's hashD/vnoise are seeded.
  for (const state of ORB_STATES) {
    const [a] = renderState(state, 64, 96, RAMP, [3.3]);
    const [b] = renderState(state, 64, 96, RAMP, [3.3]);
    assert.deepEqual(a.dots, b.dots, `${state} is not deterministic`);
    assert.deepEqual(a.fills, b.fills, `${state} colours are not deterministic`);
  }
  passed++;
});

test("states animate — consecutive frames differ", () => {
  for (const state of ORB_STATES) {
    const [a] = renderState(state, 64, 96, RAMP, [1.0]);
    const [b] = renderState(state, 64, 96, RAMP, [1.9]);
    assert.notDeepEqual(a.dots, b.dots, `${state} is frozen — the clock is not reaching it`);
  }
  passed++;
});

test("every painted colour comes from the supplied ramp", () => {
  // This is the JTS change: upstream hardcodes a grayscale ramp, the port
  // interpolates the caller's two endpoints. If a mode ever paints its own
  // colour, orbs stop tracking app.css and this fails.
  const allowed = new Set(RAMP.lut);
  for (const state of ORB_STATES) {
    for (const frame of renderState(state, 64, 96)) {
      for (const style of [...frame.fills, ...frame.strokes]) {
        const [r, g, b, a] = parseRGBA(style);
        assert.ok(allowed.has(`${r},${g},${b}`),
          `${state} painted ${style}, which is not on the supplied ink ramp`);
        // Alpha is only floored, not ceilinged: the radially-breathing modes
        // (braid, wave) push a dot's depth just past 1 when the surface swells
        // outside R, so upstream's `0.45 + 0.55 * depth` can reach ~1.02.
        // Canvas clamps that to 1. Asserting a <= 1 here would be asserting a
        // fork of upstream's math, so only the floor is checked.
        assert.ok(a > 0 && Number.isFinite(a), `${state} painted alpha ${a}`);
      }
    }
  }
  passed++;
});

test("swapping the ramp endpoints inverts the rendering, nothing else", () => {
  // A dark surface is expressed by swapping the tokens rather than by a
  // second code path, so geometry must be untouched by the swap.
  const inverted = testRamp(RAMP.paper, RAMP.ink);
  for (const state of ORB_STATES) {
    const [light] = renderState(state, 64, 96, RAMP, [2.4]);
    const [dark] = renderState(state, 64, 96, inverted, [2.4]);
    assert.deepEqual(light.dots, dark.dots, `${state} geometry changed with the ramp`);
    assert.notDeepEqual(light.fills, dark.fills, `${state} ignored the swapped ramp`);
  }
  passed++;
});

test("a stray upstream `dark` boolean falls back instead of painting nothing", () => {
  // Upstream's painters take a boolean in this position. A future re-sync that
  // pastes one in should degrade to the default ramp, not to invisible dots.
  for (const bogus of [true, false, undefined, null]) {
    const preset = resolvePreset("breathing", 64);
    const ctx = recordingCtx();
    MODE_DRAWS[preset.mode](ctx, 96, 2.0, bogus, preset.opts);
    assert.ok(ctx.rec.dots.length > 0, `passing ${bogus} drew nothing`);
    for (const style of ctx.rec.fills) parseRGBA(style);
  }
  passed++;
});

test("the 64 and 20 presets are genuinely different designs", () => {
  // Upstream is explicit that they are separate tunings, not a scale factor.
  for (const state of ORB_STATES) {
    const big = resolvePreset(state, 64);
    const small = resolvePreset(state, 20);
    assert.equal(big.mode, small.mode);
    assert.notDeepEqual(big.opts, small.opts, `${state} has identical opts at both sizes`);
  }
  passed++;
});

test("resolvePreset rejects an unknown state instead of drawing nothing", () => {
  assert.throws(() => resolvePreset("pondering", 64), /unknown orb state/);
  passed++;
});

test("breathing stays face-on — the ring is a circle, not a tilted ellipse", () => {
  // breathing and composing share drawRibbon; only the faceOn profile flag
  // separates them, by cancelling the camera tilt so the band reads as a true
  // circle. Drop that flag and the ring silhouette squashes vertically.
  // Measured on the shipped code the aspect ratio stays within 0.027 of 1
  // across the cycle; with faceOn removed it never comes closer than 0.084.
  // 0.05 sits between the two with margin on both sides.
  //
  // Only breathing is measurable this way: composing keeps a 150-dot ghost
  // sphere whose bounding box hides the band's own shape.
  for (const t of [0.6, 1.4, 2.2, 3.7, 5.1]) {
    const [frame] = renderState("breathing", 64, 96, RAMP, [t]);
    const xs = frame.dots.map((d) => d[0]);
    const ys = frame.dots.map((d) => d[1]);
    const ratio =
      (Math.max(...xs) - Math.min(...xs)) / (Math.max(...ys) - Math.min(...ys));
    assert.ok(Math.abs(ratio - 1) <= 0.05,
      `breathing is not face-on at t=${t}: aspect ratio ${ratio.toFixed(3)}`);
  }
  passed++;
});

// --- createOrb + the CSS token plumbing ------------------------------------

function installDOM({ ink = "rgb(32, 43, 31)", paper = "rgb(247, 241, 232)" } = {}) {
  // The module rasterises a resolved colour string through a 1x1 canvas to
  // normalise whatever colour syntax the browser handed back. The stub parses
  // the rgb() form the same way a browser would, and remembers the result so
  // the matching getImageData() can return it.
  let lastFill = [0, 0, 0];
  const rasterise = (text) => {
    const m = /rgb\((\d+),\s*(\d+),\s*(\d+)\)/.exec(String(text));
    lastFill = m ? [Number(m[1]), Number(m[2]), Number(m[3])] : [255, 0, 255];
    return text;
  };

  const doc = { hidden: false, addEventListener() {} };

  const makeEl = (tag) => {
    const el = {
      tagName: tag.toUpperCase(),
      // resolveToken() refuses to probe a node with no ownerDocument, so the
      // stub must supply one or every lookup silently takes the fallback.
      ownerDocument: doc,
      style: {
        cssText: "",
        _color: "",
        set color(v) { this._color = v; },
        get color() { return this._color; },
      },
      attrs: {},
      parentNode: null,
      children: [],
      setAttribute(k, v) { this.attrs[k] = v; },
      getAttribute(k) { return this.attrs[k] ?? null; },
      hasAttribute(k) { return k in this.attrs; },
      appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
      remove() { this.parentNode = null; },
      getContext() {
        const ctx = recordingCtx();
        // Object.assign would copy the VALUE of an accessor, not the accessor,
        // so the setter has to be installed explicitly.
        let fill = "";
        Object.defineProperty(ctx, "fillStyle", {
          configurable: true,
          get: () => fill,
          set: (v) => { fill = rasterise(v); },
        });
        ctx.getImageData = () => ({ data: [...lastFill, 255] });
        return ctx;
      },
    };
    return el;
  };

  doc.createElement = makeEl;
  globalThis.document = doc;
  globalThis.window = {
    devicePixelRatio: 2,
    matchMedia: () => ({ matches: false, addEventListener() {} }),
  };
  globalThis.getComputedStyle = (el) => ({
    color: el.style.color.includes("--orb-ink") ? ink : paper,
  });
  globalThis.performance = { now: () => 1234 };
  globalThis.requestAnimationFrame = () => 1;
  globalThis.cancelAnimationFrame = () => {};
  return makeEl;
}

test("createOrb sizes the canvas for devicePixelRatio and labels it", async () => {
  const makeEl = installDOM();
  // Re-import under the DOM stub: the module installs its listeners at import.
  const dom = await import(`${modulePath}?dom=1`);
  const canvas = makeEl("canvas");
  makeEl("div").appendChild(canvas);

  const orb = dom.createOrb(canvas, { state: "breathing", size: 64 });
  assert.equal(canvas.width, 128, "canvas backing store should be size x dpr");
  assert.equal(canvas.style.width, "64px");
  assert.equal(canvas.getAttribute("role"), "img");
  assert.ok(canvas.getAttribute("aria-label"), "orb should carry an aria-label");

  orb.setState("listening");
  assert.equal(orb.state, "listening");
  assert.throws(() => orb.setState("pondering"), /unknown orb state/);
  orb.destroy();
  passed++;
});

test("createOrb resolves its ink ramp from the CSS custom properties", async () => {
  const makeEl = installDOM({ ink: "rgb(11, 22, 33)", paper: "rgb(222, 233, 244)" });
  const dom = await import(`${modulePath}?dom=2`);
  const canvas = makeEl("canvas");
  makeEl("div").appendChild(canvas);

  const orb = dom.createOrb(canvas, { state: "breathing", size: 64 });
  const internal = canvas.__jtsOrb;
  assert.deepEqual(internal.ramp.ink, [11, 22, 33],
    "--orb-ink did not reach the renderer");
  assert.deepEqual(internal.ramp.paper, [222, 233, 244],
    "--orb-paper did not reach the renderer");
  orb.destroy();
  assert.equal(canvas.__jtsOrb, undefined, "destroy() should detach the orb");
  passed++;
});

test("the render loop passes its clock through and picks the preset by size", async () => {
  const makeEl = installDOM();
  const dom = await import(`${modulePath}?dom=3`);

  // The clock: two different timestamps must produce two different frames.
  // Testing MODE_DRAWS directly cannot catch a render() that drops `nowMs`.
  const big = makeEl("canvas");
  makeEl("div").appendChild(big);
  const large = dom.createOrb(big, { state: "listening", size: 96 });
  const bigRec = big.__jtsOrb.ctx.rec;
  bigRec.dots.length = 0;
  big.__jtsOrb.render(1000);
  const firstFrame = bigRec.dots.slice();
  bigRec.dots.length = 0;
  big.__jtsOrb.render(2600);
  assert.notDeepEqual(bigRec.dots, firstFrame,
    "render() ignored its timestamp — every orb on the page would be frozen");
  large.destroy();

  // The size -> preset mapping: 64 and 20 are separate designs, so a 20px orb
  // drawn with the 64px tuning is wrong even though it still renders.
  const countWith = (key, size) => {
    const preset = dom.resolvePreset("breathing", key);
    const ctx = recordingCtx();
    dom.MODE_DRAWS[preset.mode](ctx, size, preset.speed, RAMP, preset.opts);
    return ctx.rec.dots.length;
  };
  assert.notEqual(countWith(20, 20), countWith(64, 20),
    "the two tunings are indistinguishable at 20px — this check proves nothing");

  const small = makeEl("canvas");
  makeEl("div").appendChild(small);
  const inline = dom.createOrb(small, { state: "breathing", size: 20 });
  const smallRec = small.__jtsOrb.ctx.rec;
  smallRec.dots.length = 0;
  small.__jtsOrb.render(1000);
  assert.equal(smallRec.dots.length, countWith(20, 20),
    "a 20px orb must render with the 20px tuning, not the 64px one");
  inline.destroy();
  passed++;
});

await runTestFunctions(tests, () => passed);
