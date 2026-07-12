// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Pure label ownership for the wake-corpus recorder. DOM consumers remain
// responsible for assigning these file/runtime-provided strings as text.

export function createLegLabels(config = {}) {
  const source = config.leg_labels &&
    typeof config.leg_labels === "object" &&
    !Array.isArray(config.leg_labels)
    ? config.leg_labels
    : {};
  const labels = { ...source };
  const usbSweepBaseline = typeof config.usb_aec3_sweep_baseline_label === "string"
    ? config.usb_aec3_sweep_baseline_label
    : "";

  function planLegLabel(plan, leg) {
    for (const detail of plan?.legs || []) {
      if (detail?.token === leg && detail?.label) return detail.label;
    }
    return "";
  }

  function legLabel(leg, session) {
    const planned = planLegLabel(session?.capture_plan, leg);
    if (planned) return planned;
    if (
      leg === "usb_webrtc" &&
      session?.include_aec3_sweep &&
      session?.aec3_sweep_source === "usb"
    ) {
      return usbSweepBaseline || leg;
    }
    return typeof labels[leg] === "string" && labels[leg] ? labels[leg] : leg;
  }

  function applyAec3SweepVariants(variants) {
    for (const variant of variants || []) {
      if (variant?.leg && variant?.label) labels[variant.leg] = variant.label;
    }
  }

  return { legLabel, planLegLabel, applyAec3SweepVariants };
}
