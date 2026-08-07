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
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { runTestFunctions } from "./run_test_functions.mjs";

// Resolve against the CWD, not this file's directory. Every sibling harness is
// invoked with a repo-root-relative path (`deploy/assets/...`); a bare import()
// specifier would instead resolve relative to tests/js/ and crash on exactly
// that convention.
const modulePath = pathToFileURL(
  resolve(process.argv[2] || "deploy/assets/shared/js/orbs.js")
).href;

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
        // Alpha is only floored, not ceilinged: braid's radial breathing pushes
        // a dot's depth just past 1 when the strand swells outside R, so
        // upstream's `0.45 + 0.55 * depth` reaches up to ~1.021 (about 0.6% of
        // its fills, at both sizes). That is a measured bound, not an exact
        // value: a handful of samples understates it (this test's own
        // SAMPLE_TIMES only reaches 1.015) and it keeps creeping up as the
        // sweep gets finer — 1.0199 at step 0.1, 1.0206 at step 0.001 over
        // [0, 40) — so treat ~1.021 as "at least this high", not a ceiling.
        // Braid is the only state that does this — the other radially-
        // deformed mode, wave, emits no alpha at all. CSS Color 4 clamps
        // out-of-range alpha, so this renders correctly; asserting a <= 1
        // would be asserting a fork of upstream's math.
        assert.ok(a > 0 && Number.isFinite(a), `${state} painted alpha ${a}`);
      }
    }
  }
  passed++;
});

test("the ink ramp keeps its full resolution", () => {
  // The ramp is quantised into LUT_STEPS levels so ~2,000 dots a frame don't
  // each rebuild a colour string. Coarsen that and the orbs band visibly while
  // every other assertion here still passes: the colours all remain ON the
  // supplied ramp, there are just far fewer of them.
  //
  // A Proxy records which lut indices the renderer actually reaches, swept over
  // all nine states at both sizes. Measured: shipped touches 0..49; dropping to
  // 16 levels touches 0..12. (The top of the range is 49 rather than 63 because
  // the palest dot any mode asks for is white 0.78, in the braid and ribbon
  // ghost spheres.) 40 sits well clear of both.
  let maxIndex = -1;
  const probe = new Proxy(RAMP.lut, {
    get(target, prop) {
      if (typeof prop === "string" && /^\d+$/.test(prop)) {
        maxIndex = Math.max(maxIndex, Number(prop));
      }
      return Reflect.get(target, prop);
    }
  });
  const probeRamp = { ink: RAMP.ink, paper: RAMP.paper, lut: probe };
  for (const state of ORB_STATES) {
    for (const [key, size] of [[64, 96], [20, 20]]) {
      for (let t = 0; t < 12; t += 0.25) renderState(state, key, size, probeRamp, [t]);
    }
  }
  assert.ok(maxIndex >= 40,
    `ink ramp resolution collapsed: highest lut index reached was ${maxIndex}, ` +
    "expected ~49. LUT_STEPS was probably lowered.");
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
  //
  // Bounds swept at 0.002 over t in [0, 20) — one full beat period of the two
  // undulation frequencies (1.7 and 1.1, scaled by the 3.24 preset speed), so
  // these are the real extremes rather than the luckiest of a few samples:
  //
  //             shipped worst    faceOn removed, closest
  //   size 64      0.0394               0.1083
  //   size 20      0.0666               0.1238
  //
  // The margin is NOT symmetric: shipped worst sits only 0.0106 below the
  // 0.05 threshold, while the mutant's closest approach still clears it by
  // 0.058. Nearly all the risk is on the shipped side — a future change
  // that nudges wobMul (0.368) or the two beat frequencies should re-check
  // the shipped-worst number specifically, since that is the number with no
  // room to spare. The threshold is SIZE-64-SPECIFIC regardless: the
  // shipped 20px ring legitimately reaches 0.0666 and would fail it, which
  // is why this samples only the 64 tuning.
  //
  // Only breathing is measurable this way: composing keeps a 150-dot ghost
  // sphere whose bounding box hides the band's own shape.
  //
  // 200 samples across the period, not a handful: a sparse sample can miss the
  // worst case and would let a partial regression through.
  let worst = 0;
  let worstT = 0;
  for (let i = 0; i < 200; i++) {
    const t = (i / 200) * 20;
    const [frame] = renderState("breathing", 64, 96, RAMP, [t]);
    const xs = frame.dots.map((d) => d[0]);
    const ys = frame.dots.map((d) => d[1]);
    const dev = Math.abs(
      (Math.max(...xs) - Math.min(...xs)) / (Math.max(...ys) - Math.min(...ys)) - 1
    );
    if (dev > worst) { worst = dev; worstT = t; }
  }
  assert.ok(worst <= 0.05,
    `breathing is not face-on: worst aspect deviation ${worst.toFixed(4)} at t=${worstT.toFixed(2)}`);
  passed++;
});

// Signatures captured from the shipped port, which was verified frame-for-frame
// against upstream at commit e94f207e. Pinning them turns "the port still draws
// what upstream draws" into something a test can check: a dropped profile flag,
// a transposed constant, or a re-sync that silently changes geometry all move
// these numbers. Recompute deliberately (and say why) if upstream is re-synced.
const SIGNATURES = {
  working:    { dots: 516, strokes: 0,  w: 71.7, h: 71.3, cx: 48,    cy: 48,    rsum: 254.4, order: -647886155 },
  searching:  { dots: 204, strokes: 0,  w: 77.8, h: 78,   cx: 48,    cy: 48,    rsum: 172.9, order: -442994526 },
  solving:    { dots: 138, strokes: 0,  w: 78.6, h: 78.5, cx: 46.76, cy: 47.66, rsum: 111,   order: 1746532862 },
  listening:  { dots: 134, strokes: 0,  w: 75.6, h: 74.8, cx: 48,    cy: 47.52, rsum: 110.2, order: -1076943972 },
  connecting: { dots: 48,  strokes: 84, w: 76.1, h: 74.6, cx: 52.72, cy: 50.75, rsum: 55.3,  order: 850901439 },
  weaving:    { dots: 153, strokes: 0,  w: 73.4, h: 72.6, cx: 48.03, cy: 47.66, rsum: 113.2, order: -567446713 },
  composing:  { dots: 566, strokes: 0,  w: 74.8, h: 73.2, cx: 48.01, cy: 48,    rsum: 396.8, order: -1870940383 },
  breathing:  { dots: 484, strokes: 0,  w: 70.8, h: 70.9, cx: 48,    cy: 48,    rsum: 393.3, order: 1491625137 },
  shaping:    { dots: 24,  strokes: 0,  w: 54.6, h: 54.6, cx: 48,    cy: 48,    rsum: 37.4,  order: 1368395520 }
};
const SIGNATURE_T = 2.5;

function signatureOf(state) {
  const [frame] = renderState(state, 64, 96, RAMP, [SIGNATURE_T]);
  const xs = frame.dots.map((d) => d[0]);
  const ys = frame.dots.map((d) => d[1]);
  const round = (v, n) => Number(v.toFixed(n));
  // Order-sensitive on purpose. paint() z-sorts far-to-near before filling, and
  // with no depth buffer that ORDER is the whole depth illusion: a near dot
  // drawn before a far one is simply overpainted. Reversing or dropping the
  // sort leaves every aggregate below untouched, so the checksum is what pins
  // the painter's algorithm.
  let order = 0;
  for (const [x, y, r] of frame.dots) {
    order = (Math.imul(order, 31) +
      Math.round(x * 100) + Math.round(y * 100) * 7 + Math.round(r * 100) * 13) | 0;
  }
  return {
    dots: frame.dots.length,
    strokes: frame.lines.length,
    w: round(Math.max(...xs) - Math.min(...xs), 1),
    h: round(Math.max(...ys) - Math.min(...ys), 1),
    cx: round(xs.reduce((a, b) => a + b, 0) / xs.length, 2),
    cy: round(ys.reduce((a, b) => a + b, 0) / ys.length, 2),
    rsum: round(frame.dots.reduce((a, d) => a + d[2], 0), 1),
    order
  };
}

test("each state still draws its own shape", () => {
  for (const state of ORB_STATES) {
    assert.deepEqual(signatureOf(state), SIGNATURES[state],
      `${state}'s rendering changed. If this was an intentional upstream ` +
      "re-sync, re-derive SIGNATURES and say so in the commit; otherwise the " +
      "port has drifted from upstream.");
  }
  passed++;
});

test("no two states render the same animation", () => {
  // Nine distinct states is the module's headline promise, and it is carried
  // entirely by STATE_TO_MODE plus each mode's profile. Collapsing several
  // states onto one painter breaks nothing loudly — every orb just renders the
  // same thing — so it needs its own assertion.
  const seen = new Map();
  for (const state of ORB_STATES) {
    const key = JSON.stringify(signatureOf(state));
    assert.ok(!seen.has(key),
      `${state} renders identically to ${seen.get(key)} — the state -> mode ` +
      "mapping or a mode profile has collapsed");
    seen.set(key, state);
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
