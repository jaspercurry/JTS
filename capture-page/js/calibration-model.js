// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

/** Infer a Pi-registered calibration model from a browser device label. */

function normalizedLabelToken(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

export function inferCalibrationModel(models, deviceLabel) {
  const label = normalizedLabelToken(deviceLabel);
  if (!label || !Array.isArray(models)) return null;
  for (const model of models) {
    if (!model || typeof model.key !== "string" || !model.key) continue;
    const aliases = Array.isArray(model.aliases) ? model.aliases : [];
    if (aliases.some((alias) => {
      const token = normalizedLabelToken(alias);
      return token && label.includes(token);
    })) {
      return model;
    }
  }
  return null;
}
