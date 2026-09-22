// SPDX-FileCopyrightText: 2026 Jasper Curry
// SPDX-License-Identifier: Apache-2.0

import { h } from '/assets/shared/js/dom.js';
import { getJSON, postJSON } from '/assets/shared/js/http.js';
import { copyText } from '/assets/shared/js/copy.js';
import { jtsConfirm } from '/assets/shared/js/dialog.js';

const root = document.getElementById('view-body');
const status = document.getElementById('status');
let view, layout, inputs, manual, busy = false;
const openSections = new Map();

function adopt(next) {
  openSections.clear();
  if (globalThis.location?.hash === '#driver-safety-issues') openSections.set('2. Driver details', true);
  view = next;
  layout = structuredClone(view.layout);
  inputs = structuredClone(view.draft.operator_inputs);
  inputs.target_models = Object.fromEntries(view.draft.targets.map(t => [t.target_id, t.model]));
  manual = structuredClone(view.draft.manual_settings);
  manual.drivers ||= [];
  render();
}

function message(text, error = false) {
  status.textContent = text;
  status.className = 'status-line' + (error ? ' status-line--err' : '');
}

async function run(operation, success = '') {
  if (busy) return;
  busy = true;
  root.setAttribute('aria-busy', 'true');
  message('Working…');
  try {
    const response = await operation();
    if (response.setup) adopt(response.setup);
    const outcome = response.result?.save || response.result?.reset;
    message(outcome?.message || success, outcome?.status === 'needs_attention');
    return response;
  } catch (error) {
    message(error.message || 'The operation failed. Please try again.', true);
  } finally {
    busy = false;
    root.removeAttribute('aria-busy');
  }
}

function button(label, action, primary = false) {
  return h('button', { type: 'button', className: 'btn' + (primary ? ' btn--primary' : ''), onclick: action }, label);
}

function field(label, value, change, { type = 'text', options, placeholder = '' } = {}) {
  const control = options
    ? h('select', { onchange: e => change(e.target.value) }, options.map(option =>
      h('option', { value: option.value, selected: String(option.value) === String(value) }, option.label)))
    : h('input', { type, value: value ?? '', placeholder, step: type === 'number' ? 'any' : undefined,
      oninput: e => change(type === 'number' && e.target.value !== '' ? Number(e.target.value) : e.target.value) });
  return h('div.field', {}, h('label', {}, label, control));
}

function section(title, open, ...body) {
  return h('details.disclosure', { open: openSections.get(title) ?? open,
    ontoggle: event => openSections.set(title, event.target.open) },
  h('summary', {}, title), h('div.disclosure__body.speaker-section', {}, ...body));
}

function edit(object, path, value) {
  const keys = path.split('.');
  const key = keys.pop();
  const parent = keys.reduce((record, part) => record[part] ||= {}, object);
  if (value === '') delete parent[key];
  else parent[key] = value;
}

function driverEdits(target) {
  let driver = manual.drivers.find(item => item.target_id === target.target_id);
  if (!driver) {
    driver = { target_id: target.target_id, role: target.role };
    manual.drivers.push(driver);
  }
  return driver;
}

async function changeLayout(key, value) {
  layout.choices[key] = value;
  const response = await run(() => postJSON('./setup/layout', layout.choices));
  if (response) { layout = response.layout; render(); }
}

function layoutCard() {
  const choices = layout.choices;
  const editor = h('textarea', { className: 'speaker-textarea', rows: 12, value: JSON.stringify(layout.topology, null, 2), 'aria-label': 'Custom output layout' });
  return section('1. Speaker layout', view.stage === 'layout',
    field('Crossover', choices.crossover, value => changeLayout('crossover', value), { options: [
      { value: 'passive', label: 'Passive — crossover inside the speaker' },
      { value: 'active', label: 'Active — one amplifier channel per driver' }] }),
    choices.crossover === 'active' && field('Amplifier channels per speaker', choices.channels,
      value => changeLayout('channels', Number(value)), { options: [
        { value: 2, label: 'Two' }, { value: 3, label: 'Three' }] }),
    choices.crossover === 'active' && choices.channels === 3 && field('Driver arrangement', choices.cardioid,
      value => changeLayout('cardioid', value === 'true'), { options: [
        { value: false, label: 'Woofer, midrange, tweeter' },
        { value: true, label: 'Front woofer, tweeter, rear woofer (cardioid)' }] }),
    field('Speakers', choices.layout, value => changeLayout('layout', value), { options: [
      { value: 'mono', label: 'One mono speaker' }, { value: 'stereo', label: 'Stereo pair on this device' }] }),
    layout.topology.speaker_groups.map(group => h('fieldset', {}, h('legend', {}, group.label),
      group.channels.map(channel => h('div.speaker-driver', {},
        field(channel.output_variant === 'rear' ? 'Rear woofer output' : `${channel.role.replaceAll('_', ' ')} output`,
          channel.physical_output_index, value => { channel.physical_output_index = Number(value); }, { options: layout.outputs }),
        channel.role === 'tweeter' && field('High-frequency driver type', channel.driver_style || '',
          value => { channel.driver_style = value; }, { options: [
            { value: '', label: 'Choose a type' }, ...layout.driver_styles] }))))),
    h('details', {}, h('summary', {}, 'Custom output layout'), editor,
      button('Save custom layout', () => run(() => postJSON('./setup/save-layout', { output_topology: JSON.parse(editor.value) }), 'Layout saved.'))),
    button('Save layout', () => run(async () => {
      if (!layout.topology.speaker_groups.length) {
        const response = await postJSON('./setup/layout', layout.choices);
        layout = response.layout;
      }
      return postJSON('./setup/save-layout', { output_topology: layout.topology });
    }, 'Layout saved.'), true));
}

function driverCard(target) {
  const driver = driverEdits(target);
  const pad = driver.pad || { kind: 'none' };
  return h('fieldset', {}, h('legend', {}, target.label),
    field('Manufacturer and model', inputs.target_models[target.target_id], value => { inputs.target_models[target.target_id] = value; }),
    target.role !== 'tweeter' && field('Enclosure', driver.cabinet?.enclosure_kind || 'unknown',
      value => { edit(driver, 'cabinet.enclosure_kind', value); render(); }, { options: view.draft.enclosures }),
    target.driver_style === 'compression_driver' && field('Horn or waveguide (if known)', driver.installation?.horn_model,
      value => edit(driver, 'installation.horn_model', value)),
    field('Resistor or L-pad', pad.kind, value => { driver.pad = { kind: value }; render(); }, { options: view.draft.pads }),
    pad.kind !== 'none' && field('Nominal impedance (ohms)', driver.nominal_impedance_ohm,
      value => edit(driver, 'nominal_impedance_ohm', value), { type: 'number' }),
    ['series_resistor', 'l_pad'].includes(pad.kind) && field('Series resistor (ohms)', pad.series_ohm,
      value => edit(driver, 'pad.series_ohm', value), { type: 'number' }),
    pad.kind === 'l_pad' && field('Shunt resistor (ohms)', pad.shunt_ohm,
      value => edit(driver, 'pad.shunt_ohm', value), { type: 'number' }),
    pad.kind === 'direct_db' && field('Attenuation (dB)', pad.attenuation_db,
      value => edit(driver, 'pad.attenuation_db', value), { type: 'number' }),
    h('details', {}, h('summary', {}, 'More driver details'),
      Object.entries(view.draft.installation_fields).filter(([key, spec]) => key !== 'horn_model' &&
        (!spec.enclosure || spec.enclosure === driver.cabinet?.enclosure_kind)).map(([key, spec]) =>
        field(spec.label, driver.installation?.[key], value => edit(driver, `installation.${key}`, value), { type: spec.type })),
      Object.entries(view.draft.driver_fields).map(([key, label]) => field(label, driver[key], value => {
        edit(driver, key, value);
        if (key === 'gain_offset_db') delete driver.gain_offset_db_provenance;
      }, { type: 'number', placeholder: target.values[key] == null ? 'Not specified' : `Researched: ${target.values[key]}` }))));
}

function detailsCard() {
  const card = section('2. Driver details', view.stage === 'details',
    view.draft.targets.map(driverCard),
    field('Build notes (optional)', inputs.notes, value => { inputs.notes = value; }),
    button('Save details', () => run(() => postJSON('./setup/details', { operator_inputs: inputs, manual_settings: manual }), 'Details saved.'), true));
  card.id = 'driver-safety-issues';
  return card;
}

function promptCopy(id, prompt) {
  const text = h('textarea', { className: 'speaker-textarea', id, value: prompt, readOnly: true, rows: 8, 'aria-label': 'Prompt' });
  const details = h('details', {}, h('summary', {}, 'View prompt'), text);
  const copy = button('Copy prompt', async () => {
    details.open = true;
    const ok = await copyText(text);
    if (ok) details.open = false;
    copy.textContent = ok ? 'Copied' : 'Select and copy the prompt';
  }, true);
  return h('div', {}, copy, details);
}

function researchForm() {
  const input = h('textarea', { className: 'speaker-textarea', rows: 6, 'aria-label': 'Paste research result', placeholder: 'Paste the JSON result here' });
  return h('div', {}, h('p', {}, 'Copy the prompt into your research assistant, then paste its result below.'),
    promptCopy('driver-prompt', view.draft.prompt), input,
    button('Load values', () => run(() => postJSON('./setup/research', { text: input.value }), 'Starting values loaded.'), true));
}

function baseSummary() {
  return h('div', {},
    view.base_preview.crossovers.map(c => h('p', {},
      `${(c.between_roles || []).join(' / ')}: ${c.proposed_frequency_hz ?? '—'} Hz`)),
    view.base_preview.trims.map(trim => h('p', {}, `${trim.role}: ${trim.gain_db} dB · ${trim.source}`)),
    view.base_preview.rear_muted && h('p.form-hint', {}, 'Rear output stays muted until cardioid tuning is applied.'));
}

function advancedSettings() {
  const editor = h('textarea', { className: 'speaker-textarea', rows: 12, value: JSON.stringify(manual, null, 2), 'aria-label': 'Custom driver and crossover settings' });
  return h('details', {}, h('summary', {}, 'Details and custom settings'),
    h('p.form-hint', {}, 'Blank driver fields use research. Custom values take priority. Remove a custom value to use research again.'),
    h('details', {}, h('summary', {}, 'All resolved values'), h('pre', {}, JSON.stringify(view.draft.resolved, null, 2))),
    editor, button('Save custom settings', () => run(() => postJSON('./setup/details', {
      operator_inputs: inputs, manual_settings: JSON.parse(editor.value)
    }), 'Custom settings saved.')));
}

function startingCard() {
  return section('3. Starting configuration', ['research', 'apply'].includes(view.stage),
    !view.draft.prompt ? h('p', {}, 'Save the driver model names to prepare the research prompt.') :
      view.stage === 'research' ? researchForm() : h('details', {}, h('summary', {}, 'Research driver values'), researchForm()),
    ['apply', 'tune'].includes(view.stage) && baseSummary(),
    view.issues.map(issue => h('p.form-hint', {}, issue.message)),
    advancedSettings(),
    ['apply', 'tune'].includes(view.stage) && button('Save to speaker', () => run(async () => {
      const response = await postJSON('./setup/apply', {});
      if (response.result?.status !== 'applied') {
        if (response.setup) adopt(response.setup);
        throw new Error('The configuration could not be applied. Check the details and try again.');
      }
      return response;
    }, 'Base setup is active.'), true));
}

function tuningCard() {
  return section('4. Tuning', view.stage === 'tune', h('p.form-hint', {}, 'Optional. Start with driver linearization, then refine the rear output, bass, and room.'),
    view.programs.map(program => {
      const holder = h('div');
      return h('div.speaker-program', {}, h('h3', {}, program.title), h('p', {}, program.description),
        program.applied && h('p.form-hint', {}, 'Correction applied'),
        button('Copy prompt', async () => {
          const response = await run(() => getJSON(`./active-speaker/tuning-handoff?program=${program.id}`));
          if (response?.prompt) {
            const text = h('textarea', { className: 'speaker-textarea', value: response.prompt, readOnly: true, rows: 8, 'aria-label': `${program.title} prompt` });
            const details = h('details', { open: true }, h('summary', {}, 'View prompt'), text);
            holder.replaceChildren(details);
            const ok = await copyText(text);
            details.open = !ok;
            message(ok ? 'Prompt copied.' : 'Select and copy the prompt below.');
          }
        }, program.id === 'speaker'), holder,
        h('a.btn', { href: `./crossover/?program=${program.id}` }, 'Open tuning'));
    }));
}

function render() {
  root.replaceChildren(...[
    h('p', {}, view.stage === 'tune' ? 'Speaker setup is active. Tuning is optional.' : 'Set up the speaker, load starting values, then save.'),
    layoutCard(),
    view.draft.targets.length > 0 && detailsCard(),
    view.draft.targets.length > 0 && startingCard(),
    view.stage === 'tune' && tuningCard(),
    h('details', {}, h('summary', {}, 'Reset setup'), h('p.form-hint', {}, 'Clear the speaker layout and tune. Audio will stop.'),
      button('Reset speaker setup', async () => {
        if (await jtsConfirm('This clears the layout and tune and stops audio.', { title: 'Reset speaker setup?', confirmLabel: 'Reset setup', danger: true })) {
          await run(() => postJSON('./setup/reset', {}), 'Speaker setup cleared.');
        }
      }))].filter(Boolean));
}

message('Loading speaker setup…');
getJSON('./setup').then(next => { adopt(next); message(''); }).catch(error => message(error.message, true));
