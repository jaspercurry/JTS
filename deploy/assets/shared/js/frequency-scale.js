// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

export const FREQUENCY_SLIDER_STEPS = 1000;

export function freqToSlider(freq, min, max) {
  const bounded = Math.min(max, Math.max(min, Number(freq) || 0));
  return Math.round(Math.log(bounded / min) / Math.log(max / min) * FREQUENCY_SLIDER_STEPS);
}

export function sliderToFreq(pos, min, max) {
  const bounded = Math.min(FREQUENCY_SLIDER_STEPS, Math.max(0, Number(pos) || 0));
  return min * (max / min) ** (bounded / FREQUENCY_SLIDER_STEPS);
}
