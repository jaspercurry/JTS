// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";

import {
  inferCalibrationModel,
} from "../../capture-page/js/calibration-model.js";

let passed = 0;
const models = [
  { key: "future_mic", label: "Future mic", aliases: ["Acme X-7", "X7 USB"] },
  { key: "other_mic", label: "Other mic", aliases: ["Other 2"] },
];

assert.equal(
  inferCalibrationModel(models, "Microphone (ACME X 7 USB Audio)")?.key,
  "future_mic",
);
passed += 1;

assert.equal(
  inferCalibrationModel(models, "x7-usb measurement interface")?.key,
  "future_mic",
);
passed += 1;

assert.equal(inferCalibrationModel(models, "Built-in microphone"), null);
passed += 1;

assert.equal(
  inferCalibrationModel(
    [{ key: "broken", label: "Broken", aliases: "not-a-list" }],
    "not-a-list",
  ),
  null,
);
passed += 1;

console.log(JSON.stringify({ ok: true, passed }));
