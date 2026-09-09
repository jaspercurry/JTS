// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

import { h } from '/assets/shared/js/dom.js';
import { sliderToFreq } from '/assets/sound-profile/js/format.js';

export function frequencyWindow(container, onChange) {
  const rangeHz = () => sliders.map((slider) => Number(slider.value) === 1000
    ? 20000 : Math.round(sliderToFreq(slider.value, 20, 20000) * 10) / 10);
  const label = h('output.measurement-frequency__label');
  const selection = h('div.measurement-frequency__selection');
  const sliders = ['Lower frequency', 'Upper frequency'].map((name, index) => h('input', {
    type: 'range', min: 0, max: 1000, step: 1, value: index * 1000,
    'aria-label': name,
    oninput: () => {
      sliders[index].value = index === 0
        ? Math.min(Number(sliders[0].value), Number(sliders[1].value) - 1)
        : Math.max(Number(sliders[1].value), Number(sliders[0].value) + 1);
      update();
      onChange();
    },
  }));
  const reset = h('button.btn.btn--ghost', {
    type: 'button',
    onclick: () => {
      sliders[0].value = 0;
      sliders[1].value = 1000;
      update();
      onChange();
    },
  }, 'Reset range');

  function update() {
    const labels = rangeHz().map((hz) => `${hz.toLocaleString('en-US', { maximumFractionDigits: 1 })} Hz`);
    label.textContent = labels.join(' – ');
    sliders.forEach((slider, index) => slider.setAttribute('aria-valuetext', labels[index]));
    selection.style.left = `${Number(sliders[0].value) / 10}%`;
    selection.style.right = `${100 - Number(sliders[1].value) / 10}%`;
    reset.disabled = Number(sliders[0].value) === 0 && Number(sliders[1].value) === 1000;
  }

  container.replaceChildren(h('div.measurement-frequency', { role: 'group', 'aria-label': 'Frequency range' },
    h('div.measurement-frequency__heading', null, label, reset),
    h('div.measurement-frequency__slider', null,
      h('div.measurement-frequency__track', null, selection), sliders),
    h('div.measurement-frequency__limits', { 'aria-hidden': 'true' },
      h('span', null, '20 Hz'), h('span', null, '20 kHz')),
  ));
  update();
  return rangeHz;
}
