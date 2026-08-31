// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { registerHooks } from "node:module";
import { pathToFileURL } from "node:url";

import * as calibrationModel from "../../capture-page/js/calibration-model.js";
import * as captureIntegrity from "../../capture-page/js/capture-integrity.js";
import * as captureProtocol from "../../capture-page/js/capture-protocol.js";
import * as config from "../../capture-page/js/config.js";
import * as constraints from "../../capture-page/js/constraints.js";
import * as crypto from "../../capture-page/js/crypto.js";
import * as fragment from "../../capture-page/js/fragment.js";
import * as relayClient from "../../capture-page/js/relay-client.js";
import * as render from "../../capture-page/js/render.js";
import * as returnUrl from "../../capture-page/js/return-url.js";
import * as setupStore from "../../capture-page/js/setup-store.js";
import * as transportIntegrity from "../../capture-page/js/transport-integrity.js";
import * as wakelock from "../../capture-page/js/wakelock.js";
import * as measurementAudio from "../../deploy/assets/shared/js/measurement-audio.js";

import { repoPath } from "./_loader.mjs";

const MAIN_URL = pathToFileURL(repoPath("capture-page/js/main.js")).href;
const MODULE_DEPENDENCIES = new Map([
  ["./ambient-stats.js", { buildAmbientStatsEvent: undefined }],
  ["./calibration-model.js", calibrationModel],
  ["./capture-integrity.js", captureIntegrity],
  ["./capture-protocol.js", captureProtocol],
  ["./config.js", config],
  ["./constraints.js", constraints],
  ["./crypto.js", crypto],
  ["./fragment.js", fragment],
  ["./level-events.js", { runLevelRampProtocol: undefined }],
  ["./measurement-audio.js", measurementAudio],
  ["./relay-client.js", relayClient],
  ["./render.js", render],
  ["./return-url.js", returnUrl],
  ["./setup-store.js", setupStore],
  ["./transport-integrity.js", transportIntegrity],
  ["./wakelock.js", wakelock],
]);

const DEPENDENCY_NAMES = [
  ...new Set(
    [...MODULE_DEPENDENCIES.values()].flatMap((namespace) => Object.keys(namespace)),
  ),
];

const REAL_DEPENDENCIES = Object.assign(
  {},
  ...MODULE_DEPENDENCIES.values(),
);

const dependencySets = new Map();
let loadCount = 0;

globalThis.__capturePageDependencySets = dependencySets;

registerHooks({
  resolve(specifier, context, nextResolve) {
    const parent = context.parentURL ? new URL(context.parentURL) : null;
    const token = parent && parent.searchParams.get("capture-test");
    if (token && dependencySets.has(token) && specifier.startsWith("./")) {
      const modulePath = specifier.split("?", 1)[0];
      if (!MODULE_DEPENDENCIES.has(modulePath)) {
        throw new Error(`capture-page test has no dependency owner for ${modulePath}`);
      }
      return {
        shortCircuit: true,
        url: `capture-page-test:${token}/${encodeURIComponent(modulePath)}`,
      };
    }
    return nextResolve(specifier, context);
  },
  load(url, context, nextLoad) {
    if (!url.startsWith("capture-page-test:")) return nextLoad(url, context);
    const [token, encodedModulePath] = url.slice("capture-page-test:".length).split("/", 2);
    const modulePath = decodeURIComponent(encodedModulePath);
    const exports = Object.keys(MODULE_DEPENDENCIES.get(modulePath)).map(
      (name) => `export const ${name} = deps[${JSON.stringify(name)}];`,
    ).join("\n");
    return {
      format: "module",
      shortCircuit: true,
      source: `const deps = globalThis.__capturePageDependencySets.get(${JSON.stringify(token)});\n${exports}\n`,
    };
  },
});

function dependenciesFromSource(source) {
  if (!source) return {};
  const returned = DEPENDENCY_NAMES.map(
    (name) => `${name}: typeof ${name} === "undefined" ? undefined : ${name}`,
  ).join(",\n");
  const values = new Function(`${source}\nreturn {${returned}};`)();
  return Object.fromEntries(
    Object.entries(values).filter(([, value]) => value !== undefined),
  );
}

export function loadCapturePage({
  dependencySource = "",
  dependencies = {},
  mainUrl = MAIN_URL,
} = {}) {
  const token = String(++loadCount);
  dependencySets.set(token, {
    ...REAL_DEPENDENCIES,
    ...dependenciesFromSource(dependencySource),
    ...dependencies,
  });
  const url = new URL(mainUrl);
  url.searchParams.set("capture-test", token);
  return import(url.href);
}
