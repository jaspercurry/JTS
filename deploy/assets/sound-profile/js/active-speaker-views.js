export function createActiveSpeakerViews(deps) {
  var store = deps.store;
  var api = deps.api;
  var activeSpeaker = store.activeSpeaker;
  var activeSpeakerMicObservation = store.activeSpeakerMicObservation;
  var outputTopology = store.outputTopology;
  var driverResearch = store.driverResearch;
  var crossoverPreview = store.crossoverPreview;
  var escapeHtml = deps.escapeHtml;
  var ico = deps.ico;
  var fmtDb = deps.fmtDb;
  var fmtFreq = deps.fmtFreq;
  var fmtDbfs = deps.fmtDbfs;
  var clamp = deps.clamp;
  var clone = deps.clone;
  var status = deps.status;
  var render = deps.render;

  function renderActiveSpeakerSetup() {
    var open = activeSpeaker.loading || activeSpeaker.payload ||
      activeSpeaker.session || activeSpeaker.stagedConfig ||
      activeSpeaker.plan || activeSpeaker.playback ||
      activeSpeaker.error || outputTopology.loading || outputTopology.saving ||
      outputTopology.identitySaving || outputTopology.protectionSaving || outputTopology.error ||
      outputTopology.readinessChecking || outputTopology.readinessPlaybackChecking ||
      outputTopology.dirty || outputTopology.touched;
    return '<section class="active-speaker-setup">' +
      '<details class="advanced"' + (open ? ' open' : '') + '>' +
        '<summary>Advanced speaker setup</summary>' +
        renderOutputTopologySetup() +
      '</details>' +
    '</section>';
  }
  function currentOutputTopology() {
    return outputTopology.draft || outputTopology.payload || null;
  }
  function outputGroups(topology) {
    return topology && Array.isArray(topology.speaker_groups) ? topology.speaker_groups : [];
  }
  function outputHardware(topology) {
    return topology && topology.hardware ? topology.hardware : null;
  }
  function outputEvaluation(topology) {
    return topology && topology.evaluation ? topology.evaluation : {};
  }
  function outputIdentityReport() {
    return outputTopology.identity || null;
  }
  function outputClockDomainReport() {
    return outputTopology.clockDomain || null;
  }
  function identityTargetFor(groupId, role) {
    var report = outputIdentityReport();
    var targets = report && Array.isArray(report.targets) ? report.targets : [];
    return targets.find(function(target) {
      return target.speaker_group_id === groupId && target.role === role;
    }) || null;
  }
  function outputAssignedMap(topology) {
    var out = {};
    outputGroups(topology).forEach(function(group) {
      (group.channels || []).forEach(function(channel) {
        if (channel.physical_output_index == null) return;
        out[String(channel.physical_output_index)] = {
          group: group.label || group.id,
          role: channel.role || 'channel'
        };
      });
    });
    return out;
  }
  function outputStatusClass(statusValue) {
    if (statusValue === 'verified' || statusValue === 'valid') return ' status-pill--ready';
    if (statusValue === 'blocked') return ' status-pill--blocked';
    return ' status-pill--planned';
  }
  function humanMode(modeValue) {
    return {
      full_range_passive: 'Passive/full range',
      active_2_way: 'Active 2-way',
      active_3_way: 'Active 3-way',
      subwoofer: 'Subwoofer'
    }[modeValue] || modeValue || 'Unknown';
  }
  function humanRole(role) {
    return {
      full_range: 'Full range',
      woofer: 'Woofer',
      mid: 'Mid',
      tweeter: 'Tweeter',
      subwoofer: 'Subwoofer'
    }[role] || role || 'Channel';
  }
  function outputRoleSummary(topology) {
    var roles = [];
    outputGroups(topology).forEach(function(group) {
      (group.channels || []).forEach(function(channel) {
        var role = channel.role || '';
        if (role && roles.indexOf(role) < 0) roles.push(role);
      });
    });
    if (!roles.length) roles = ['woofer', 'tweeter'];
    return roles.sort(function(a, b) {
      var order = {full_range: 0, woofer: 1, mid: 2, tweeter: 3, subwoofer: 4};
      return (order[a] || 99) - (order[b] || 99);
    });
  }
  function assignedOutputIndices(topology) {
    var used = {};
    outputGroups(topology).forEach(function(group) {
      (group.channels || []).forEach(function(channel) {
        if (channel.physical_output_index != null) {
          used[String(channel.physical_output_index)] = true;
        }
      });
    });
    return used;
  }
  function firstUnusedOutputIndex(topology) {
    var hardware = outputHardware(topology) || {};
    var count = Number(hardware.physical_output_count || 0);
    var used = assignedOutputIndices(topology);
    for (var index = 0; index < count; index += 1) {
      if (!used[String(index)]) return index;
    }
    return null;
  }
  function outputSubwooferGroup(topology) {
    return outputGroups(topology).find(function(group) {
      return group.kind === 'subwoofer' || group.mode === 'subwoofer';
    }) || null;
  }
  function outputHasSubwoofer(topology) {
    return !!outputSubwooferGroup(topology);
  }
  function nextSubwooferGroupId(topology) {
    var existing = {};
    outputGroups(topology).forEach(function(group) { existing[group.id] = true; });
    if (!existing.sub) return 'sub';
    var i = 2;
    while (existing['sub_' + i]) i += 1;
    return 'sub_' + i;
  }
  function addSubwooferToTopology(topology) {
    var next = baseOutputDraft(topology);
    if (!next || outputHasSubwoofer(next)) return next;
    var outputIndex = firstUnusedOutputIndex(next);
    if (outputIndex == null) return next;
    var groupId = nextSubwooferGroupId(next);
    next.speaker_groups = (next.speaker_groups || []).concat([{
      id: groupId,
      label: 'Subwoofer',
      kind: 'subwoofer',
      mode: 'subwoofer',
      position: {x: 0, y: -0.72, rotation_degrees: 0},
      channels: [outputChannel('subwoofer', outputIndex)]
    }]);
    next.routing = Object.assign({}, next.routing || {}, {
      subwoofer_group_ids: (next.routing && next.routing.subwoofer_group_ids || []).concat([groupId])
    });
    return next;
  }
  function removeSubwooferFromTopology(topology) {
    var next = baseOutputDraft(topology);
    if (!next) return next;
    var subIds = {};
    next.speaker_groups = (next.speaker_groups || []).filter(function(group) {
      var isSub = group.kind === 'subwoofer' || group.mode === 'subwoofer';
      if (isSub) subIds[group.id] = true;
      return !isSub;
    });
    next.routing = Object.assign({}, next.routing || {}, {
      subwoofer_group_ids: (next.routing && next.routing.subwoofer_group_ids || [])
        .filter(function(id) { return !subIds[id]; })
    });
    return next;
  }
  function driverResearchRoleLabel(role) {
    return {
      woofer: 'Woofer / midbass',
      mid: 'Midrange',
      tweeter: 'Tweeter / high-frequency driver',
      subwoofer: 'Subwoofer'
    }[role] || humanRole(role);
  }
  function driverResearchPrompt(topology) {
    var roles = outputRoleSummary(topology);
    var lines = roles.map(function(role) {
      var name = (driverResearch.inputs[role] || '').trim();
      return '- ' + role + ': ' + (name || '[user has not entered a model yet]');
    });
    var notes = (driverResearch.inputs.notes || '').trim();
    return [
      'You are helping configure a safe active crossover for a JTS Raspberry Pi speaker.',
      '',
      'Driver list:',
      lines.join('\n'),
      notes ? '\nUser notes:\n' + notes : '',
      '',
      'Research manufacturer datasheets and reputable measurements. Prefer primary manufacturer data.',
      'Do not invent missing facts. Use null when a value is unknown. Include source URLs for every non-obvious claim.',
      'Focus on safe starting points, not final tuning. Assume JTS will start test signals extremely quiet and the human must approve any listening result.',
      '',
      'Return only JSON with this shape:',
      '{',
      '  "artifact_schema_version": 1,',
      '  "kind": "jts_active_crossover_driver_research",',
      '  "drivers": [',
      '    {',
      '      "role": "full_range|woofer|mid|tweeter|subwoofer",',
      '      "model": "string",',
      '      "manufacturer": "string|null",',
      '      "nominal_impedance_ohm": 8,',
      '      "sensitivity_db_2v83_1m": 90,',
      '      "usable_frequency_range_hz": [80, 5000],',
      '      "recommended_highpass_hz": 80,',
      '      "recommended_lowpass_hz": 2200,',
      '      "do_not_test_below_hz": 1200,',
      '      "notes": "short safety-relevant notes",',
      '      "sources": ["https://..."]',
      '    }',
      '  ],',
      '  "crossover_candidates": [',
      '    {',
      '      "between_roles": ["woofer", "tweeter"],',
      '      "frequency_hz": 1800,',
      '      "filter_type": "Linkwitz-Riley",',
      '      "slope_db_per_octave": 24,',
      '      "confidence": "low|medium|high",',
      '      "rationale": "why this is a safe starting point",',
      '      "warnings": ["short warnings"]',
      '    }',
      '  ],',
      '  "human_review": {',
      '    "must_verify_wiring": true,',
      '    "must_start_quiet": true,',
      '    "needs_measurement_before_final": true',
      '  }',
      '}'
    ].filter(Boolean).join('\n');
  }
  function summarizeDriverResearchPayload(payload) {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new Error('Driver research must be a JSON object.');
    }
    if (payload.kind !== 'jts_active_crossover_driver_research') {
      throw new Error('Driver research kind must be jts_active_crossover_driver_research.');
    }
    if (Number(payload.artifact_schema_version) !== 1) {
      throw new Error('Driver research artifact_schema_version must be 1.');
    }
    var drivers = Array.isArray(payload.drivers) ? payload.drivers : [];
    var candidates = Array.isArray(payload.crossover_candidates) ? payload.crossover_candidates : [];
    if (!drivers.length) throw new Error('Driver research must include at least one driver.');
    return {
      driverCount: drivers.length,
      candidateCount: candidates.length,
      roles: drivers.map(function(driver) { return driver.role || 'unknown'; })
        .filter(function(role, index, arr) { return arr.indexOf(role) === index; }),
      warnings: candidates.reduce(function(out, candidate) {
        return out.concat(Array.isArray(candidate.warnings) ? candidate.warnings : []);
      }, []).slice(0, 4)
    };
  }
  function driverResearchDraftSaved() {
    var draftPayload = driverResearch.designDraft || {};
    var savedStatus = draftPayload.status || '';
    return savedStatus === 'ready_for_review' && !driverResearch.dirty;
  }
  function ingestDesignDraft(payload, options) {
    options = options || {};
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return;
    driverResearch.designDraft = payload;
    driverResearch.saving = false;
    if (!options.force && driverResearch.dirty) return;
    var inputs = payload.operator_inputs || {};
    ['full_range', 'woofer', 'mid', 'tweeter', 'subwoofer', 'notes'].forEach(function(key) {
      driverResearch.inputs[key] = inputs[key] || '';
    });
    if (payload.driver_research) {
      driverResearch.importText = JSON.stringify(payload.driver_research, null, 2);
      try {
        driverResearch.parsed = summarizeDriverResearchPayload(payload.driver_research);
        driverResearch.error = '';
      } catch (e) {
        driverResearch.parsed = null;
        driverResearch.error = e.message;
      }
    } else {
      driverResearch.importText = '';
      driverResearch.parsed = null;
      driverResearch.error = '';
    }
    driverResearch.dirty = false;
  }
  async function fetchDesignDraft() {
    var payload = await api.get('./active-speaker/design-draft', 'speaker design draft failed');
    ingestDesignDraft(payload);
    return payload;
  }
  function ingestCrossoverPreview(payload) {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return;
    crossoverPreview.payload = payload;
    crossoverPreview.preparing = false;
    crossoverPreview.error = '';
  }
  async function fetchCrossoverPreview() {
    var payload = await api.get('./active-speaker/crossover-preview', 'crossover preview failed');
    ingestCrossoverPreview(payload);
    return payload;
  }
  function toneSummary(tone) {
    var frequency = Number(tone.frequency_hz);
    var level = Number(tone.level_dbfs);
    var duration = Number(tone.duration_ms);
    if (!isFinite(frequency) || !isFinite(level) || !isFinite(duration)) {
      return 'unknown';
    }
    return Math.round(frequency) + ' Hz at ' + level.toFixed(1) + ' dBFS for ' +
      Math.round(duration) + ' ms';
  }
  function humanProtectionStatus(value) {
    return {
      not_required: 'not needed',
      required_missing: 'not set',
      present: 'physical protection',
      software_guard_requested: 'software guard requested',
      unknown: 'unknown'
    }[value] || value || 'unknown';
  }
  function renderOutputTopologySetup() {
    return '<div class="setting-row setting-row--stack output-setup">' +
      '<div class="output-setup__head">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Active crossover setup</p>' +
          '<p class="setting-row__hint">Build the speaker layout, add driver info, map DAC outputs, then prepare safe test mode.</p>' +
        '</div></div>' +
      renderOutputTopologyBody() +
    '</div>';
  }
  function renderOutputHardwareRefresh() {
    var topology = currentOutputTopology();
    return '<div class="output-setup__actions">' +
      '<button type="button" class="btn btn--ghost" data-act="refresh-output-topology"' +
        (outputTopology.loading ? ' disabled' : '') + '>' + (topology ? 'Refresh hardware' : 'Find hardware') + '</button>' +
    '</div>';
  }
  function outputIdentityComplete() {
    if (outputTopology.dirty) return false;
    var report = outputIdentityReport();
    if (!report) return false;
    return Number(report.assigned_channel_count || 0) > 0 &&
      Number(report.unverified_channel_count || 0) === 0;
  }
  function outputStartupLoaded() {
    var startup = activeSpeaker.startupLoad || {};
    var state = startup.state || {};
    return state.status === 'loaded';
  }
  function outputStepState(step, topology) {
    var groups = outputGroups(topology);
    var hasLayout = groups.length > 0;
    if (step === 'layout') return hasLayout && !outputTopology.dirty ? 'done' : 'active';
    if (step === 'research') return hasLayout && !outputTopology.dirty ?
      (driverResearchDraftSaved() || driverResearch.parsed ? 'done' : 'active') : 'todo';
    if (step === 'map') return outputIdentityComplete() ? 'done' :
      (hasLayout && !outputTopology.dirty ? 'active' : 'todo');
    if (step === 'safety') return outputStartupLoaded() ? 'done' :
      (outputIdentityComplete() ? 'active' : 'todo');
    return 'todo';
  }
  function defaultOutputStep() {
    return 'layout';
  }
  function outputStepIsOpen(step, topology) {
    return (store.outputStepOverride || defaultOutputStep()) === step;
  }
  function outputStepTitle(step) {
    return {
      layout: 'Choose speaker layout',
      research: 'Research drivers',
      map: 'Map and verify outputs',
      safety: 'Prepare safe test mode'
    }[step] || 'this card';
  }
  function outputStepCanOpen(step, topology) {
    return outputStepState(step, topology) !== 'todo';
  }
  function openOutputStep(step) {
    store.outputStepOverride = step;
    render();
  }
  function renderOutputStepCard(step, title, hint, topology, bodyHtml, footerHtml) {
    var state = outputStepState(step, topology);
    var open = outputStepIsOpen(step, topology);
    var done = state === 'done';
    return '<details class="output-step output-step--' + escapeHtml(state) + '"' +
      ' data-output-step="' + escapeHtml(step) + '"' +
      (open ? ' open' : '') + '>' +
      '<summary class="output-step__summary">' +
        '<span class="output-step__marker" aria-hidden="true">' + (done ? '&#10003;' : '') + '</span>' +
        '<span class="output-step__text"><strong>' + escapeHtml(title) + '</strong>' +
          '<span>' + escapeHtml(hint) + '</span></span>' +
        '<span class="output-step__chevron" aria-hidden="true"></span>' +
      '</summary>' +
      '<div class="output-step__body">' + bodyHtml +
        (footerHtml ? '<div class="output-step__footer">' + footerHtml + '</div>' : '') +
      '</div>' +
    '</details>';
  }
  function renderOutputStepButton(step, label, primary) {
    return '<button type="button" class="btn ' + escapeHtml(primary ? 'btn--primary' : 'btn--ghost') +
      '" data-act="output-step-next" data-step="' + escapeHtml(step) + '">' +
      escapeHtml(label) + '</button>';
  }
  function outputTemplateKindFromAxes(layout, speakerMode) {
    if (layout !== 'mono' && layout !== 'stereo') return '';
    if (speakerMode !== 'passive' &&
        speakerMode !== 'active_2way' &&
        speakerMode !== 'active_3way') {
      return '';
    }
    return layout + '_' + speakerMode;
  }
  function outputTemplateAxesForTopology(topology) {
    var mainGroups = outputGroups(topology).filter(function(group) {
      return group.kind !== 'subwoofer' && group.mode !== 'subwoofer';
    });
    var kinds = mainGroups.map(function(group) { return group.kind; });
    var layout = (kinds.indexOf('left') >= 0 || kinds.indexOf('right') >= 0)
      ? 'stereo'
      : 'mono';
    var mode = mainGroups.length ? mainGroups[0].mode : 'full_range_passive';
    var speakerMode = {
      full_range_passive: 'passive',
      active_2_way: 'active_2way',
      active_3_way: 'active_3way'
    }[mode] || 'passive';
    return {layout: layout, speakerMode: speakerMode};
  }
  function outputTemplateAxisButton(axis, value, label, hint, selected, disabled) {
    return '<button type="button" class="output-template-option" data-act="output-template-axis" ' +
      'data-axis="' + escapeHtml(axis) + '" data-value="' + escapeHtml(value) + '" ' +
      'aria-pressed="' + (selected ? 'true' : 'false') + '"' +
      (disabled ? ' disabled' : '') + '>' +
        '<strong>' + escapeHtml(label) + '</strong>' +
        '<span>' + escapeHtml(hint) + '</span>' +
      '</button>';
  }
  function renderOutputSetupTemplates(topology) {
    var hardware = outputHardware(topology);
    var count = Number(hardware && hardware.physical_output_count) || 0;
    var axes = outputTemplateAxesForTopology(topology);
    var selectedTemplate = outputTemplateDefinition(
      outputTemplateKindFromAxes(axes.layout, axes.speakerMode)
    );
    var hasSub = outputHasSubwoofer(topology);
    var selectedLabel = selectedTemplate
      ? selectedTemplate.label + (hasSub ? ' + subwoofer' : '')
      : 'Choose a layout';
    var outputCount = selectedTemplate
      ? selectedTemplate.minOutputs + (hasSub ? 1 : 0)
      : 0;
    var layoutChoices = [
      {value: 'mono', label: 'Mono', hint: 'One speaker or cabinet'},
      {value: 'stereo', label: 'Stereo', hint: 'Left and right speakers'}
    ];
    var speakerChoices = [
      {value: 'passive', label: 'Passive', hint: 'Full-range output per speaker'},
      {value: 'active_2way', label: 'Active 2-way', hint: 'Woofer + tweeter'},
      {value: 'active_3way', label: 'Active 3-way', hint: 'Woofer + mid + tweeter'}
    ];
    return '<div class="output-card output-card--templates">' +
      '<div class="output-card__head"><div><p class="output-card__title">Setup template</p>' +
        '<p class="setting-row__hint">Choose the speaker layout you are wiring. This only edits the saved draft.</p></div></div>' +
      '<div class="output-template-axes">' +
        '<div class="output-template-axis">' +
          '<p class="output-template-axis__label">Speaker count</p>' +
          '<div class="output-template-options output-template-options--layout">' +
            layoutChoices.map(function(choice) {
              var template = outputTemplateDefinition(
                outputTemplateKindFromAxes(choice.value, axes.speakerMode)
              );
              return outputTemplateAxisButton(
                'layout',
                choice.value,
                choice.label,
                choice.hint,
                axes.layout === choice.value,
                !template || count < template.minOutputs
              );
            }).join('') +
          '</div>' +
        '</div>' +
        '<div class="output-template-axis">' +
          '<p class="output-template-axis__label">Speaker type</p>' +
          '<div class="output-template-options output-template-options--mode">' +
            speakerChoices.map(function(choice) {
              var template = outputTemplateDefinition(
                outputTemplateKindFromAxes(axes.layout, choice.value)
              );
              return outputTemplateAxisButton(
                'speaker-mode',
                choice.value,
                choice.label,
                choice.hint,
                axes.speakerMode === choice.value,
                !template || count < template.minOutputs
              );
            }).join('') +
          '</div>' +
        '</div>' +
      '</div>' +
      '<dl class="active-speaker-facts output-facts output-template-summary">' +
        '<div><dt>Draft map</dt><dd>' + escapeHtml(selectedLabel) + '</dd></div>' +
        '<div><dt>Physical outputs</dt><dd>' + escapeHtml(
          outputCount ? String(outputCount) + ' of ' + String(count || 0) + ' assigned' : 'Choose a map'
        ) + '</dd></div>' +
      '</dl>' +
    '</div>';
  }
  function renderOutputSubwooferCard(topology) {
    var hasLayout = outputGroups(topology).length > 0;
    var hasSub = outputHasSubwoofer(topology);
    var nextOutput = firstUnusedOutputIndex(topology);
    var disabled = !hasLayout || (!hasSub && nextOutput == null);
    var nextOutputLabel = null;
    var outputs = outputHardware(topology) && Array.isArray(outputHardware(topology).outputs)
      ? outputHardware(topology).outputs : [];
    outputs.forEach(function(output) {
      if (Number(output.index) === Number(nextOutput)) nextOutputLabel = output.human_label;
    });
    var hint = hasSub
      ? 'Subwoofer is included in this draft. Remove it to free that output lane.'
      : (!hasLayout
        ? 'Choose a speaker layout first, then add a subwoofer if you have a spare amplifier channel.'
        : (nextOutput == null
        ? 'No unused physical output is available for a subwoofer in this layout.'
        : 'Adds one subwoofer group on ' + (nextOutputLabel || ('DAC output ' + (Number(nextOutput) + 1)))));
    return '<div class="output-card output-card--subwoofer">' +
      '<div class="output-card__head"><div><p class="output-card__title">Subwoofer add-on</p>' +
        '<p class="setting-row__hint">Optional. This composes with any mono or stereo layout instead of duplicating templates.</p></div>' +
        '<span class="status-pill' + (hasSub ? ' status-pill--ready' : '') + '">' + escapeHtml(hasSub ? 'added' : 'optional') + '</span></div>' +
      '<p class="setting-row__hint">' + escapeHtml(hint) + '</p>' +
      '<button type="button" class="btn btn--ghost" data-act="toggle-output-subwoofer" data-mode="' +
        escapeHtml(hasSub ? 'remove' : 'add') + '"' + (disabled ? ' disabled' : '') + '>' +
        escapeHtml(hasSub ? 'Remove subwoofer' : 'Add subwoofer') + '</button>' +
    '</div>';
  }
  function renderDriverResearchSummary() {
    var saved = driverResearch.designDraft || {};
    var savedStatus = saved.status || '';
    var savedSummary = saved.summary || {};
    var savedHtml = savedStatus && savedStatus !== 'not_saved' ? (
      '<div class="driver-research__summary driver-research__summary--saved">' +
        '<span class="status-pill' + (savedStatus === 'ready_for_review' ? ' status-pill--ready' : '') + '">' +
          escapeHtml('saved draft: ' + savedStatus.replace(/_/g, ' ')) + '</span>' +
        '<p class="setting-row__hint">' + escapeHtml(
          String(savedSummary.driver_count || 0) + ' saved driver' +
          (Number(savedSummary.driver_count || 0) === 1 ? '' : 's') +
          ', ' + String(savedSummary.crossover_candidate_count || 0) +
          ' crossover candidate' +
          (Number(savedSummary.crossover_candidate_count || 0) === 1 ? '' : 's') +
          '. No filters are applied.'
        ) + '</p>' +
      '</div>'
    ) : '';
    if (driverResearch.error) {
      return savedHtml +
        '<p class="setting-row__hint driver-research__error">' +
        escapeHtml(driverResearch.error) + '</p>';
    }
    if (!driverResearch.parsed) {
      return savedHtml +
        '<p class="setting-row__hint">Paste JSON from the assistant to sanity-check the shape. JTS will not apply it automatically.</p>';
    }
    var summary = driverResearch.parsed;
    return savedHtml + '<div class="driver-research__summary">' +
      '<span class="status-pill status-pill--ready">import parsed</span>' +
      '<p class="setting-row__hint">' + escapeHtml(
        summary.driverCount + ' driver' + (summary.driverCount === 1 ? '' : 's') +
        ', ' + summary.candidateCount + ' crossover candidate' + (summary.candidateCount === 1 ? '' : 's') +
        '. Roles: ' + summary.roles.join(', ')
      ) + '</p>' +
      (summary.warnings.length ? '<ul class="active-speaker-issues">' + summary.warnings.map(function(warning) {
        return '<li>' + escapeHtml(String(warning)) + '</li>';
      }).join('') + '</ul>' : '') +
    '</div>';
  }
  function renderDriverResearchCard(topology) {
    var roles = outputRoleSummary(topology);
    var saveDisabled = driverResearch.saving || outputTopology.dirty ||
      !currentOutputTopology();
    var fields = roles.map(function(role) {
      return '<label class="driver-research__field">' +
        '<span>' + escapeHtml(driverResearchRoleLabel(role)) + '</span>' +
        '<input type="text" data-driver-field="' + escapeHtml(role) + '" value="' +
          escapeHtml(driverResearch.inputs[role] || '') + '" placeholder="Manufacturer and model">' +
      '</label>';
    }).join('');
    return '<div class="output-card output-card--driver-research">' +
      '<div class="output-card__head"><div><p class="output-card__title">Driver research helper</p>' +
        '<p class="setting-row__hint">Generate a precise prompt for an external assistant, then paste bounded JSON back for review.</p></div></div>' +
      '<div class="driver-research__grid">' +
        '<div class="driver-research__fields">' +
          fields +
          '<label class="driver-research__field driver-research__field--wide">' +
            '<span>Build notes</span>' +
            '<textarea rows="3" data-driver-field="notes" placeholder="Waveguide, baffle, enclosure, amplifier, measurement constraints">' +
              escapeHtml(driverResearch.inputs.notes || '') + '</textarea>' +
          '</label>' +
        '</div>' +
        '<div class="driver-research__panel">' +
          '<div class="row-between active-speaker-level__head">' +
            '<p class="setting-row__title">Research prompt</p>' +
            '<button type="button" class="btn btn--ghost" data-act="copy-driver-research-prompt">Copy prompt</button>' +
          '</div>' +
          '<textarea id="driver-research-prompt" class="driver-research__textarea" readonly rows="10" ' +
            'aria-label="Driver research prompt">' +
            escapeHtml(driverResearchPrompt(topology)) + '</textarea>' +
        '</div>' +
        '<div class="driver-research__panel">' +
          '<div class="row-between active-speaker-level__head">' +
            '<p class="setting-row__title">Paste JSON result</p>' +
            '<div class="driver-research__actions">' +
              '<button type="button" class="btn btn--ghost" data-act="parse-driver-research">Check JSON</button>' +
              '<button type="button" class="btn btn--primary" data-act="save-driver-design"' +
                (saveDisabled ? ' disabled' : '') + '>' +
                escapeHtml(driverResearch.saving ? 'Saving' : 'Save design draft') +
              '</button>' +
            '</div>' +
          '</div>' +
          '<textarea id="driver-research-import" class="driver-research__textarea" data-driver-import ' +
            'rows="7" placeholder="{...}" aria-label="Driver research JSON result">' +
            escapeHtml(driverResearch.importText || '') + '</textarea>' +
          '<div id="driver-research-summary">' + renderDriverResearchSummary() + '</div>' +
        '</div>' +
      '</div>' +
    '</div>';
  }
  function previewStatusClass(value) {
    if (value === 'ready_for_protected_staging') return ' status-pill--ready';
    if (value === 'blocked' || value === 'stale' || value === 'unreadable') return ' status-pill--blocked';
    return '';
  }
  function renderPreviewIssues(issues) {
    issues = Array.isArray(issues) ? issues : [];
    if (!issues.length) return '';
    return '<ul class="active-speaker-issues">' + issues.slice(0, 5).map(function(issue) {
      return '<li>' + escapeHtml(issue.message || issue.code || 'review required') + '</li>';
    }).join('') + '</ul>';
  }
  function renderCrossoverPreviewRows(payload) {
    var groups = Array.isArray(payload.groups) ? payload.groups : [];
    var rows = [];
    groups.forEach(function(group) {
      (Array.isArray(group.crossovers) ? group.crossovers : []).forEach(function(crossover) {
        var roles = Array.isArray(crossover.between_roles) ? crossover.between_roles : [];
        var filter = (Array.isArray(crossover.filters) && crossover.filters[0]) || {};
        var label = (group.label || group.group_id || 'Speaker') + ': ' + roles.join(' / ');
        var detail = crossover.proposed_frequency_hz ?
          fmtFreq(crossover.proposed_frequency_hz) + ', ' +
          (filter.filter_type || 'filter') + ', ' +
          String(filter.slope_db_per_octave || 24) + ' dB/oct' :
          'needs research';
        rows.push('<div><dt>' + escapeHtml(label) + '</dt><dd>' + escapeHtml(detail) + '</dd></div>');
      });
    });
    if (!rows.length) {
      rows.push('<div><dt>Preview</dt><dd>No active crossover candidate prepared yet.</dd></div>');
    }
    return '<dl class="active-speaker-facts output-facts">' + rows.join('') + '</dl>';
  }
  function renderCrossoverPreviewCard() {
    var payload = crossoverPreview.payload || {};
    var value = payload.status || 'not_prepared';
    var label = value.replace(/_/g, ' ');
    var summary = payload.summary || {};
    var canPrepare = driverResearchDraftSaved() && !outputTopology.dirty;
    var disabled = crossoverPreview.preparing || !canPrepare;
    var hint = canPrepare ?
      'Builds bounded filter intent from the saved draft. No YAML, no Camilla load, no sound.' :
      'Save the speaker layout and design draft before preparing a crossover preview.';
    if (crossoverPreview.error) hint = crossoverPreview.error;
    return '<div class="output-card output-card--crossover-preview">' +
      '<div class="output-card__head"><div><p class="output-card__title">Crossover preview</p>' +
        '<p class="setting-row__hint">' + escapeHtml(hint) + '</p></div>' +
        '<span class="status-pill' + previewStatusClass(value) + '">' + escapeHtml(label) + '</span></div>' +
      renderCrossoverPreviewRows(payload) +
      '<p class="setting-row__hint">' + escapeHtml(
        String(summary.ready_crossover_count || 0) + ' ready, ' +
        String(summary.blocker_count || 0) + ' blocker' +
        (Number(summary.blocker_count || 0) === 1 ? '' : 's') +
        '. Measurement is still required before final tuning.'
      ) + '</p>' +
      renderPreviewIssues(payload.issues) +
      '<button type="button" class="btn btn--primary" data-act="prepare-crossover-preview"' +
        (disabled ? ' disabled' : '') + '>' +
        escapeHtml(crossoverPreview.preparing ? 'Preparing' : 'Prepare crossover preview') +
      '</button>' +
    '</div>';
  }
  function renderOutputTopologyBody() {
    if (outputTopology.loading && !currentOutputTopology()) {
      return '<p class="setting-row__hint">Loading output topology…</p>';
    }
    if (outputTopology.error) {
      return '<div class="output-error">' +
        '<span class="status-pill status-pill--blocked">Active crossover setup unavailable</span>' +
        '<p class="setting-row__hint">' + escapeHtml(outputTopology.error) + '</p>' +
        renderOutputHardwareRefresh() +
      '</div>';
    }
    var topology = currentOutputTopology();
    if (!topology) {
      return '<div class="output-empty">' +
        '<p class="setting-row__hint">Refresh hardware to start a speaker layout.</p>' +
        renderOutputHardwareRefresh() +
      '</div>';
    }
    var evaluation = outputEvaluation(topology);
    var statusValue = outputTopology.dirty ? 'draft' : (evaluation.status || topology.status || 'draft');
    return '<div class="output-layout">' +
      renderOutputStepCard(
        'layout',
        'Choose speaker layout',
        'Pick mono or stereo, active or passive, then optionally add a subwoofer.',
        topology,
        renderOutputSetupTemplates(topology) +
          renderOutputSubwooferCard(topology) +
          renderOutputHardwareCard(topology, statusValue),
        renderOutputHardwareRefresh() +
          renderOutputStepButton('layout',
          outputTopology.dirty ? 'Save and continue' : 'Next: research drivers',
          true)
      ) +
      renderOutputStepCard(
        'research',
        'Research drivers',
        'Generate a precise external-assistant prompt, then paste bounded JSON back for review.',
        topology,
        renderDriverResearchCard(topology) +
          renderCrossoverPreviewCard(),
        renderOutputStepButton('research', 'Next: map outputs', true)
      ) +
      renderOutputStepCard(
        'map',
        'Map and verify outputs',
        'Review speaker groups, DAC lanes, and physical verification evidence.',
        topology,
        renderOutputStageCard(topology) +
          renderOutputGroupsCard(topology) +
          renderOutputIdentityCard(),
        renderOutputStepButton('map', 'Next: safety checks', true)
      ) +
      renderOutputStepCard(
        'safety',
        'Prepare safe test mode',
        'Run safety preflight, stage protected startup, then start from the quiet floor.',
        topology,
        renderOutputCommissioningRehearsal() +
        '<div class="output-card output-card--active-status">' +
          '<div class="output-card__head"><div><p class="output-card__title">Safety preflight</p>' +
          '<p class="setting-row__hint">Checks DAC, DSP config, volume limits, and rollback. This does not play sound.</p></div></div>' +
          renderOutputBringupSequence() +
          renderActiveSpeakerStatus() +
        '</div>' +
        renderOutputReadinessCard() +
        renderOutputSafetyCard(topology, statusValue),
        ''
      ) +
    '</div>';
  }
  function renderOutputHardwareCard(topology, statusValue) {
    var hardware = outputHardware(topology) || {};
    var clock = outputClockDomainReport();
    var clockStatus = clock && clock.status || '';
    var compositeClock = clockStatus.indexOf('dual_apple_composite_clock') === 0;
    var clockSupportLabel = compositeClock ? 'Composite clock' : 'Multi-DAC aggregate';
    var clockSupportValue = compositeClock
      ? (clock && clock.composite_clock_supported ? 'supported' : 'needs attention')
      : (clock && clock.multi_device_aggregate_supported ? 'supported' : 'not enabled');
    var rows = [
      ['Device', hardware.device_id || 'unknown'],
      ['Outputs', String(hardware.physical_output_count || 0) + ' physical'],
      ['Route', hardware.route || 'default'],
      ['Clock domain', clock && clock.clock_domain_label ||
        hardware.clock_domain_label || 'Single output device clock'],
      [clockSupportLabel, clockSupportValue],
      ['Topology', topology.name || topology.topology_id || 'Speaker outputs']
    ];
    return '<div class="output-card output-card--hardware">' +
      '<div class="output-card__head">' +
        '<div><p class="output-card__title">' + escapeHtml(hardware.device_label || 'Unknown output device') + '</p>' +
        '<p class="setting-row__hint">Detected output hardware</p></div>' +
        '<span class="status-pill' + outputStatusClass(statusValue) + '">' + escapeHtml(statusValue) + '</span>' +
      '</div>' +
      '<dl class="active-speaker-facts output-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
    '</div>';
  }
  function outputGroupPoint(group, index, total) {
    var pos = group.position || {};
    var hasPos = isFinite(Number(pos.x)) || isFinite(Number(pos.y));
    var x = hasPos ? Number(pos.x || 0) : (group.kind === 'left' ? -0.65 : (group.kind === 'right' ? 0.65 : 0));
    var y = hasPos ? Number(pos.y || 0) : (group.kind === 'subwoofer' ? -0.72 : 0.45);
    if (!hasPos && group.kind === 'mono' && total > 1) x = (index - (total - 1) / 2) * 0.55;
    return {
      x: 120 + clamp(x, -1.2, 1.2) * 70,
      y: 92 - clamp(y, -1.0, 1.0) * 52
    };
  }
  function outputGroupInitial(group) {
    if (group.kind === 'left') return 'L';
    if (group.kind === 'right') return 'R';
    if (group.kind === 'subwoofer') return 'S';
    return 'M';
  }
  function renderOutputStageCard(topology) {
    var groups = outputGroups(topology);
    var assigned = outputAssignedMap(topology);
    var outputs = outputHardware(topology) && Array.isArray(outputHardware(topology).outputs)
      ? outputHardware(topology).outputs : [];
    var markers = groups.map(function(group, index) {
      var p = outputGroupPoint(group, index, groups.length);
      return '<g class="output-stage__speaker" data-kind="' + escapeHtml(group.kind || '') + '">' +
        '<circle cx="' + p.x.toFixed(1) + '" cy="' + p.y.toFixed(1) + '" r="18"></circle>' +
        '<text x="' + p.x.toFixed(1) + '" y="' + (p.y + 4).toFixed(1) + '">' +
          escapeHtml(outputGroupInitial(group)) + '</text>' +
      '</g>';
    }).join('');
    var lane = outputs.length ? outputs.map(function(output) {
      var hit = assigned[String(output.index)];
      return '<span class="output-chip' + (hit ? ' output-chip--assigned' : '') + '">' +
        escapeHtml(output.human_label || ('Output ' + (Number(output.index) + 1))) +
        (hit ? '<small>' + escapeHtml(hit.group + ' · ' + humanRole(hit.role)) + '</small>' : '<small>Unassigned</small>') +
      '</span>';
    }).join('') : '<p class="setting-row__hint">No physical outputs detected.</p>';
    return '<div class="output-card output-card--stage">' +
      '<div class="output-card__head"><div><p class="output-card__title">Speaker layout</p>' +
        '<p class="setting-row__hint">Top-down sketch for routing context only.</p></div></div>' +
      '<svg class="output-stage" viewBox="0 0 240 150" role="img" aria-label="Speaker output layout">' +
        '<rect x="16" y="18" width="208" height="114" rx="8"></rect>' +
        '<path d="M60 112 C88 92 152 92 180 112"></path>' +
        '<text x="120" y="122" class="output-stage__seat">Listening area</text>' +
        (markers || '<text x="120" y="78" class="output-stage__empty">No groups yet</text>') +
      '</svg>' +
      '<div class="output-lane">' + lane + '</div>' +
    '</div>';
  }
  function renderOutputGroupsCard(topology) {
    var groups = outputGroups(topology);
    if (!groups.length) {
      return '<div class="output-card output-card--groups">' +
        '<p class="output-card__title">Speaker groups</p>' +
        '<p class="setting-row__hint">Choose a setup template above. JTS keeps it as a draft until you verify channels safely.</p>' +
      '</div>';
    }
    return '<div class="output-card output-card--groups">' +
      '<div class="output-card__head"><div><p class="output-card__title">Speaker groups</p>' +
        '<p class="setting-row__hint">Driver roles and assigned DAC outputs.</p></div></div>' +
      '<div class="output-groups">' + groups.map(renderOutputGroup).join('') + '</div>' +
    '</div>';
  }
  function renderOutputGroup(group) {
    var channels = Array.isArray(group.channels) ? group.channels : [];
    return '<div class="output-group">' +
      '<div class="output-group__head">' +
        '<div><p class="output-group__title">' + escapeHtml(group.label || group.id) + '</p>' +
        '<p class="setting-row__hint">' + escapeHtml(humanMode(group.mode)) + '</p></div>' +
        '<span class="output-group__badge">' + escapeHtml(group.kind || 'speaker') + '</span>' +
      '</div>' +
      '<div class="output-roles">' + channels.map(function(channel) {
        var protectionStatus = channel.protection_status || 'unknown';
        var protection = channel.protection_required
          ? ' · guard ' + humanProtectionStatus(protectionStatus) : '';
        var label = channel.human_output_label ||
          (channel.physical_output_index == null ? 'No output assigned' : 'Output ' + (Number(channel.physical_output_index) + 1));
        var target = identityTargetFor(group.id, channel.role) || {};
        var targetId = target.id || (group.id + ':' + channel.role);
        var busy = outputTopology.identitySaving === targetId;
        var protectionBusy = outputTopology.protectionSaving === targetId;
        var readinessBusy = outputTopology.readinessChecking === targetId;
        var action = channel.identity_verified ? 'Clear' : 'Mark verified';
        var protectionPresent = protectionStatus === 'present';
        var softwareGuard = protectionStatus === 'software_guard_requested';
        var disabled = outputTopology.dirty || busy || protectionBusy ||
          readinessBusy || channel.physical_output_index == null;
        return '<div class="output-role">' +
          '<div class="output-role__text">' +
            '<span>' + escapeHtml(humanRole(channel.role)) + '</span>' +
            '<strong>' + escapeHtml(label) + '</strong>' +
            '<small>' + escapeHtml((channel.identity_verified ? 'identity verified' : 'identity unverified') + protection) + '</small>' +
          '</div>' +
          '<div class="output-role__actions">' +
            '<button type="button" class="btn btn--ghost output-role__action" ' +
              'data-act="mark-output-identity" ' +
              'data-group-id="' + escapeHtml(group.id) + '" ' +
              'data-role="' + escapeHtml(channel.role) + '" ' +
              'data-verified="' + (channel.identity_verified ? 'false' : 'true') + '" ' +
              'data-label="' + escapeHtml((group.label || group.id) + ' ' + humanRole(channel.role) + ' on ' + label) + '"' +
              (disabled ? ' disabled' : '') + '>' +
              escapeHtml(busy ? 'Saving' : action) + '</button>' +
            (channel.protection_required ? (
              (protectionPresent || softwareGuard ? '<button type="button" class="btn btn--ghost output-role__action" ' +
                'data-act="mark-output-protection" ' +
                'data-group-id="' + escapeHtml(group.id) + '" ' +
                'data-role="' + escapeHtml(channel.role) + '" ' +
                'data-status="required_missing" ' +
                'data-label="' + escapeHtml((group.label || group.id) + ' ' + humanRole(channel.role) + ' on ' + label) + '"' +
                (disabled ? ' disabled' : '') + '>' +
                escapeHtml(protectionBusy ? 'Saving' : 'Clear guard') + '</button>' : '') +
              (!protectionPresent ? '<button type="button" class="btn btn--ghost output-role__action" ' +
                'data-act="mark-output-protection" ' +
                'data-group-id="' + escapeHtml(group.id) + '" ' +
                'data-role="' + escapeHtml(channel.role) + '" ' +
                'data-status="present" ' +
                'data-label="' + escapeHtml((group.label || group.id) + ' ' + humanRole(channel.role) + ' on ' + label) + '"' +
                (disabled ? ' disabled' : '') + '>' +
                escapeHtml(protectionBusy ? 'Saving' : 'Hardware protected') + '</button>' : '') +
              (!softwareGuard ? '<button type="button" class="btn btn--ghost output-role__action" ' +
                'data-act="mark-output-protection" ' +
                'data-group-id="' + escapeHtml(group.id) + '" ' +
                'data-role="' + escapeHtml(channel.role) + '" ' +
                'data-status="software_guard_requested" ' +
                'data-label="' + escapeHtml((group.label || group.id) + ' ' + humanRole(channel.role) + ' on ' + label) + '"' +
                (disabled ? ' disabled' : '') + '>' +
                escapeHtml(protectionBusy ? 'Saving' : 'Use software guard') + '</button>' : '')
            ) : '') +
            '<button type="button" class="btn btn--ghost output-role__action" ' +
              'data-act="check-output-readiness" ' +
              'data-group-id="' + escapeHtml(group.id) + '" ' +
              'data-role="' + escapeHtml(channel.role) + '" ' +
              'data-label="' + escapeHtml((group.label || group.id) + ' ' + humanRole(channel.role) + ' on ' + label) + '"' +
              (disabled ? ' disabled' : '') + '>' +
              escapeHtml(readinessBusy ? 'Checking' : 'Check readiness') + '</button>' +
          '</div>' +
        '</div>';
      }).join('') + '</div>' +
    '</div>';
  }
  function renderOutputIdentityCard() {
    if (outputTopology.dirty) {
      return '<div class="output-card output-card--identity">' +
        '<div class="output-card__head"><div><p class="output-card__title">Channel identity</p>' +
        '<p class="setting-row__hint">Save this speaker layout draft before recording physical verification evidence.</p></div>' +
        '<span class="status-pill">draft</span></div>' +
        '<p class="setting-row__hint">JTS will re-run backend validation after save, then you can mark assigned channels as physically verified.</p>' +
      '</div>';
    }
    var report = outputIdentityReport();
    if (!report) {
      return '<div class="output-card output-card--identity">' +
        '<div class="output-card__head"><div><p class="output-card__title">Channel identity</p>' +
        '<p class="setting-row__hint">Load or save the speaker layout to see verification progress.</p></div></div>' +
      '</div>';
    }
    var assigned = Number(report.assigned_channel_count || 0);
    var verified = Number(report.verified_channel_count || 0);
    var unverified = Number(report.unverified_channel_count || 0);
    var targets = Array.isArray(report.targets) ? report.targets : [];
    var rows = targets.length ? targets.map(function(target) {
      var blockers = Array.isArray(target.sound_test_blockers) ? target.sound_test_blockers : [];
      return '<li class="output-identity-row">' +
        '<span>' + escapeHtml(target.speaker_label || target.speaker_group_id || 'Speaker') +
          ' · ' + escapeHtml(humanRole(target.role)) + '</span>' +
        '<strong>' + escapeHtml(target.identity_verified ? 'Verified' :
          (target.assigned ? 'Needs check' : 'Unassigned')) + '</strong>' +
        (blockers.length ? '<small>' + escapeHtml(blockers.slice(0, 2).join(', ')) + '</small>' : '') +
      '</li>';
    }).join('') : '<li class="output-identity-row"><span>No channels configured</span><strong>Draft</strong></li>';
    return '<div class="output-card output-card--identity">' +
      '<div class="output-card__head"><div><p class="output-card__title">Channel identity</p>' +
        '<p class="setting-row__hint">Physical verification is operator evidence. It does not authorize playback by itself.</p></div>' +
        '<span class="status-pill' + outputStatusClass(report.status || 'draft') + '">' +
          escapeHtml(verified + '/' + assigned + ' verified') + '</span></div>' +
      (outputTopology.dirty ? '<p class="setting-row__hint">Save the draft before changing identity evidence.</p>' : '') +
      '<ul class="output-identity-list">' + rows + '</ul>' +
      '<p class="setting-row__hint">' + escapeHtml(report.next_step || 'Verify assigned channels before sound tests.') + '</p>' +
      (unverified > 0 ? '<p class="setting-row__hint">Use this only after wiring inspection, dummy-load/DMM checks, or a future low-level channel test confirms the driver.</p>' : '') +
    '</div>';
  }
  function sequenceStep(label, done, active, blocked, detail) {
    var state = done ? 'done' : (blocked ? 'blocked' : (active ? 'active' : 'todo'));
    var marker = done ? 'Done' : (blocked ? 'Blocked' : (active ? 'Now' : 'Next'));
    return '<li class="output-sequence__item output-sequence__item--' + escapeHtml(state) + '">' +
      '<span class="output-sequence__marker">' + escapeHtml(marker) + '</span>' +
      '<span class="output-sequence__text"><strong>' + escapeHtml(label) + '</strong>' +
        '<small>' + escapeHtml(detail || '') + '</small></span>' +
    '</li>';
  }
  function renderOutputBringupSequence() {
    var env = activeSpeaker.payload || {};
    var staged = activeSpeaker.stagedConfig || {};
    var startup = activeSpeaker.startupLoad || {};
    var startupState = startup.state || {};
    var startupPreflight = startup.preflight || {};
    var pathSafety = startupPreflight.path_safety || {};
    var session = activeSpeaker.session || {};
    var readiness = outputTopology.readiness || {};
    var envChecked = !!activeSpeaker.payload;
    var envReady = !!env.ok_to_load_active_config;
    var stagedReady = staged.status === 'staged';
    var pathReady = pathSafety.load_gate === 'ready';
    var startupReady = startupState.status === 'loaded' &&
      !!startupState.rollback_available &&
      !!startupState.current_config_matches_loaded;
    var armed = session.status === 'armed';
    var readinessChecked = !!outputTopology.readiness;
    var readinessReady = !!readiness.preconditions_passed;
    var atFloor = outputCurrentLevelAtFloor();
    var floorConfirmed = outputFloorAudioConfirmedForReadiness(readiness);
    var artifactReady = !!(outputTopology.readinessPlayback && outputTopology.readinessPlayback.artifact);
    var rows = [
      sequenceStep(
        'Run safety preflight',
        envReady,
        !envChecked,
        envChecked && !envReady,
        envReady ? 'DAC, DSP config, volume limit, and rollback look ready.' :
          (envChecked ? 'Resolve safety blockers before loading DSP.' : 'Run the safety preflight first.')
      ),
      sequenceStep(
        'Stage protected startup',
        stagedReady,
        envReady && !stagedReady,
        false,
        stagedReady ? 'Muted, limited startup config is staged.' : 'Build a protected startup config for review.'
      ),
      sequenceStep(
        'Check protected path',
        pathReady,
        stagedReady && !pathReady,
        startupPreflight.status === 'blocked',
        pathReady ? 'Path-safety evidence is bound to this startup load.' : 'Verify renderer/cue paths before loading.'
      ),
      sequenceStep(
        'Load protected startup',
        startupReady,
        pathReady && !startupReady,
        startupState.status === 'blocked',
        startupReady ? 'CamillaDSP is on the protected graph with rollback.' : 'Reloads DSP only; it does not play sound.'
      ),
      sequenceStep(
        'Arm safe session',
        armed,
        startupReady && !armed,
        false,
        armed ? 'Stop is available and the session is time-bounded.' : 'Arming records safety state only.'
      ),
      sequenceStep(
        'Select target and check readiness',
        readinessReady,
        armed && !readinessChecked,
        readinessChecked && !readinessReady,
        readinessChecked ? (readiness.next_step || 'Review target readiness below.') : 'Use Check readiness on a saved output lane.'
      ),
      sequenceStep(
        'Start at the floor',
        atFloor || floorConfirmed,
        readinessReady && !atFloor && !floorConfirmed,
        false,
        floorConfirmed ? 'Floor audio is confirmed for this target.' :
          (atFloor ? 'Calibration level is at the quiet floor.' : 'Reset or lower test level before preparing tone.')
      ),
      sequenceStep(
        'Verify artifact before audio',
        artifactReady && (atFloor || floorConfirmed),
        readinessReady && (atFloor || floorConfirmed) && !artifactReady,
        false,
        'Artifact-only verification remains the default; audible playback needs explicit lab enablement.'
      ),
      sequenceStep(
        'Confirm floor audio',
        floorConfirmed,
        readinessReady && atFloor && !floorConfirmed,
        false,
        floorConfirmed ? 'Raised tests are unlocked for this target/session.' : 'First audible test must succeed at the floor.'
      ),
      sequenceStep(
        'Raise slowly',
        floorConfirmed && !atFloor,
        floorConfirmed && atFloor,
        false,
        'After floor audio succeeds, raised audible tests remain bounded by 1 dB steps.'
      )
    ];
    return '<div class="output-sequence">' +
      '<p class="setting-row__title">Safe bring-up sequence</p>' +
      '<ol class="output-sequence__list">' + rows.join('') + '</ol>' +
    '</div>';
  }
  function renderOutputCommissioningRehearsal() {
    var rehearsal = activeSpeaker.rehearsal || {};
    var steps = Array.isArray(rehearsal.steps) ? rehearsal.steps : [];
    var statusValue = rehearsal.status || 'not_checked';
    if (!steps.length) {
      return '<div class="output-card output-card--rehearsal">' +
        '<div class="output-card__head"><div><p class="output-card__title">Commissioning rehearsal</p>' +
        '<p class="setting-row__hint">Refresh active-speaker status to rehearse the durable safety sequence without sound.</p></div>' +
        '<span class="status-pill">not checked</span></div>' +
      '</div>';
    }
    return '<div class="output-card output-card--rehearsal">' +
      '<div class="output-card__head"><div><p class="output-card__title">Commissioning rehearsal</p>' +
        '<p class="setting-row__hint">' + escapeHtml(rehearsal.next_step || 'No sound is played by this rehearsal.') + '</p></div>' +
        '<span class="status-pill' + outputStatusClass(statusValue === 'blocked' ? 'blocked' : 'valid') + '">' +
          escapeHtml(statusValue.replace(/_/g, ' ')) + '</span></div>' +
      '<ol class="output-sequence__list">' + steps.map(function(step) {
        var stepStatus = step.status || 'pending';
        return '<li class="output-sequence__item output-sequence__item--' + escapeHtml(stepStatus) + '">' +
          '<span>' + escapeHtml(stepStatus.replace(/_/g, ' ')) + '</span>' +
          '<strong>' + escapeHtml(step.label || step.id || 'Step') + '</strong>' +
          '<p>' + escapeHtml(step.message || '') + '</p>' +
        '</li>';
      }).join('') + '</ol>' +
    '</div>';
  }
  function outputCurrentLevelAtFloor() {
    var level = activeSpeakerLevelConfig();
    return Math.abs(Number(level.value) - Number(level.min)) < 0.001;
  }
  function outputTargetSignature(raw) {
    raw = raw || {};
    var output = raw.output_index != null ? raw.output_index : raw.physical_output_index;
    output = output == null ? null : Number(output);
    return {
      speaker_group_id: raw.speaker_group_id || null,
      role: String(raw.driver_role || raw.role || '').trim().toLowerCase() || null,
      output_index: isFinite(output) && output >= 0 ? output : null
    };
  }
  function outputSameTarget(a, b) {
    return !!a && !!b &&
      (a.speaker_group_id || null) === (b.speaker_group_id || null) &&
      (a.role || null) === (b.role || null) &&
      (a.output_index == null ? null : Number(a.output_index)) ===
        (b.output_index == null ? null : Number(b.output_index));
  }
  function outputFloorAudioConfirmedForReadiness(readiness) {
    var session = activeSpeaker.session || {};
    var quiet = session.quiet_start || {};
    return session.status === 'armed' &&
      quiet.status === 'floor_confirmed' &&
      quiet.floor_audio_confirmed === true &&
      outputSameTarget(quiet.current_target, outputTargetSignature(readiness && readiness.target));
  }
  function outputFloorAudioPendingForPlayback(playback) {
    var session = activeSpeaker.session || {};
    var quiet = session.quiet_start || {};
    var pendingId = quiet.pending_playback_id || null;
    var playbackId = playback && playback.playback_id || null;
    return session.status === 'armed' &&
      quiet.status === 'floor_pending_operator' &&
      pendingId && playbackId && pendingId === playbackId &&
      playback.audio_emitted === true;
  }
  function renderOutputFloorAudioResultActions(playback) {
    var session = activeSpeaker.session || {};
    var quiet = session.quiet_start || {};
    var lastResult = quiet.last_operator_result || null;
    var playbackId = playback && playback.playback_id || '';
    if (!outputFloorAudioPendingForPlayback(playback)) {
      if (!lastResult || !lastResult.outcome || lastResult.playback_id !== playbackId) return '';
      return '<p class="setting-row__hint">Last floor-test result: ' +
        escapeHtml(String(lastResult.outcome).replace(/_/g, ' ')) + '</p>';
    }
    var buttons = [
      ['heard_correct_driver', 'Heard correct driver', 'btn--primary'],
      ['heard_wrong_driver', 'Wrong driver', 'btn--ghost'],
      ['silent', 'Silent', 'btn--ghost'],
      ['too_loud', 'Too loud', 'btn--danger']
    ];
    return '<div class="output-floor-result">' +
      '<p class="setting-row__title">What did you hear?</p>' +
      '<p class="setting-row__hint">Raised tests unlock only after the correct physical driver is heard at the floor level.</p>' +
      '<div class="active-speaker-actions">' + buttons.map(function(item) {
        return '<button type="button" class="btn ' + escapeHtml(item[2]) +
          '" data-act="active-floor-result" data-outcome="' + escapeHtml(item[0]) +
          '" data-playback-id="' + escapeHtml(playbackId) + '">' +
          escapeHtml(item[1]) + '</button>';
      }).join('') + '</div>' +
    '</div>';
  }
  function quietStartTargetLabel(target) {
    target = target || {};
    var pieces = [];
    if (target.speaker_group_id) pieces.push(String(target.speaker_group_id));
    if (target.role) pieces.push(humanRole(target.role).toLowerCase());
    if (target.output_index != null && isFinite(Number(target.output_index))) {
      pieces.push('output ' + (Number(target.output_index) + 1));
    }
    return pieces.join(' ');
  }
  function quietStartLabel(session) {
    var quiet = session && session.quiet_start || {};
    if (session && session.status !== 'armed') return 'Not armed';
    if (quiet.status === 'floor_confirmed' && quiet.floor_audio_confirmed) {
      var targetLabel = quietStartTargetLabel(quiet.current_target);
      return targetLabel ? 'Floor confirmed for ' + targetLabel : 'Floor confirmed for last target';
    }
    if (quiet.status === 'floor_pending_operator') {
      var pendingTargetLabel = quietStartTargetLabel(quiet.current_target);
      return pendingTargetLabel ? 'Floor test pending for ' + pendingTargetLabel : 'Floor test pending';
    }
    return 'Floor required';
  }
  function readinessTargetLockReason(readiness) {
    var target = readiness && readiness.target || {};
    var audible = readiness && readiness.audible_test || {};
    if (target.role === 'tweeter') {
      return audible.target_role_allowed === false ?
        'High-frequency playback requires a valid protection profile.' : '';
    }
    if (audible.target_role_allowed === false) {
      return 'Audible tests are limited to woofer, mid, and subwoofer targets in this slice.';
    }
    return '';
  }
  function readinessBlockedReasons(readiness) {
    var reasons = [];
    var gates = Array.isArray(readiness.required_gates) ? readiness.required_gates : [];
    var issues = Array.isArray(readiness.issues) ? readiness.issues : [];
    gates.forEach(function(gate) {
      if (!gate || gate.passed) return;
      reasons.push(gate.message || gate.label || gate.id || 'Readiness gate is blocked.');
    });
    issues.forEach(function(issue) {
      if (!issue) return;
      reasons.push(issue.message || issue.code || 'Readiness issue requires review.');
    });
    var lockReason = readinessTargetLockReason(readiness);
    if (lockReason) {
      reasons.unshift(lockReason);
    }
    return reasons.filter(function(reason, index, arr) {
      return reason && arr.indexOf(reason) === index;
    }).slice(0, 6);
  }
  function renderOutputReadinessSummary(readiness) {
    var target = readiness.target || {};
    var startup = readiness.startup_load || {};
    var backend = readiness.tone_backend || {};
    var level = readiness.calibration_level && readiness.calibration_level.test_signal || {};
    var audible = readiness.audible_test || {};
    var rows = [
      ['Target', target.label || target.role || 'unknown'],
      ['DAC output', target.physical_output_index == null ? 'unknown' : 'Output ' + (Number(target.physical_output_index) + 1)],
      ['Role policy', audible.target_role_allowed === false ? 'Locked in this slice' : 'Eligible after gates'],
      ['Backend', (backend.audio_enabled ? 'audible lab backend' : 'artifact-only') + (backend.test_pcm ? ' · ' + backend.test_pcm : '')],
      ['Quiet start', quietStartLabel(activeSpeaker.session)],
      ['Rollback', startup.rollback_available ? 'available' : 'not ready'],
      ['Test level', level.requested_level_dbfs == null ? fmtDbfs(activeSpeakerLevelConfig().value) : fmtDbfs(level.requested_level_dbfs)]
    ];
    return '<dl class="active-speaker-facts output-readiness-summary">' + rows.map(function(row) {
      return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
    }).join('') + '</dl>';
  }
  function renderOutputReadinessBlockers(readiness) {
    var reasons = readinessBlockedReasons(readiness);
    if (!reasons.length) {
      return '<p class="setting-row__hint">' + escapeHtml(readiness.playback_allowed ?
        'No blocking readiness reasons; the audible lab backend is enabled for this target.' :
        'No blocking readiness reasons for artifact verification. Audible playback still requires explicit backend enablement.') + '</p>';
    }
    return '<div class="output-readiness-blockers">' +
      '<p class="setting-row__title">Why sound is blocked</p>' +
      '<ul class="active-speaker-issues">' + reasons.map(function(reason) {
        return '<li>' + escapeHtml(reason) + '</li>';
      }).join('') + '</ul>' +
    '</div>';
  }
  function renderOutputHighFrequencyReadiness(readiness) {
    var hf = readiness && readiness.high_frequency_driver;
    if (!hf || !hf.applies) return '';
    var mic = hf.microphone || {};
    var preview = hf.floor_test_preview || {};
    var previewTone = preview.tone || {};
    var gates = Array.isArray(hf.required_gates) ? hf.required_gates : [];
    var autoLevel = hf.auto_level || {};
    var statusLabel = {
      guided_ready: 'Guided evidence ready',
      manual_ready: 'Manual evidence ready',
      blocked: 'Blocked'
    }[hf.status] || hf.status || 'Unknown';
    var rows = [
      ['Audio allowed', hf.audio_allowed ? 'Yes' : 'No'],
      ['Protection path', hf.protection_mode || 'unknown'],
      ['Manual floor test', hf.manual_floor_test_candidate ? 'candidate' : 'blocked'],
      ['Guided level', hf.guided_floor_test_candidate ? 'candidate' : 'blocked'],
      ['Mic status', mic.status || 'unknown'],
      ['Mic reading', mic.observed_dbfs == null ? 'none' : fmtDbfs(Number(mic.observed_dbfs))],
      ['Auto-level', autoLevel.status || 'not checked'],
      ['Floor-test preview', previewTone.frequency_hz ?
        fmtFreq(previewTone.frequency_hz) + ' at ' + fmtDbfs(Number(previewTone.level_dbfs)) :
        'not ready']
    ];
    return '<div class="active-speaker-plan output-high-frequency-readiness">' +
      '<div class="row-between active-speaker-level__head">' +
        '<div><p class="setting-row__title">High-frequency bring-up readiness</p>' +
        '<p class="setting-row__hint">Summarizes protection, mic, and auto-level evidence before any tweeter-style output.</p></div>' +
        '<span class="status-pill' + (hf.status === 'blocked' ? ' status-pill--blocked' : ' status-pill--ready') + '">' +
          escapeHtml(statusLabel) + '</span></div>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      '<ul class="output-safety-list">' + gates.slice(0, 10).map(function(gate) {
        return '<li class="output-safety-list__item output-safety-list__item--' + escapeHtml(gate.passed ? 'info' : 'blocker') + '">' +
          '<span>' + escapeHtml(gate.label || gate.id || 'gate') + '</span>' +
          '<p>' + escapeHtml(gate.message || (gate.passed ? 'Passed' : 'Blocked')) + '</p>' +
        '</li>';
      }).join('') + '</ul>' +
      (preview.kind ? '<p class="setting-row__hint">Preview only: ' +
        escapeHtml(preview.next_step || 'No audio will play from this preview.') + '</p>' : '') +
      '<p class="setting-row__hint">' + escapeHtml(hf.next_step || 'High-frequency audio remains gated.') + '</p>' +
    '</div>';
  }
  function renderOutputReadinessCard() {
    if (outputTopology.dirty) {
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">Playback readiness</p>' +
        '<p class="setting-row__hint">Save the speaker layout before checking a channel.</p></div>' +
        '<span class="status-pill">draft</span></div>' +
      '</div>';
    }
    if (outputTopology.readinessChecking) {
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">Playback readiness</p>' +
        '<p class="setting-row__hint">Checking the selected saved channel. No sound will play.</p></div>' +
        '<span class="status-pill">checking</span></div>' +
      '</div>';
    }
    if (outputTopology.readinessError) {
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">Playback readiness</p>' +
        '<p class="setting-row__hint">The last readiness check failed. The saved speaker layout is still available.</p></div>' +
        '<span class="status-pill status-pill--blocked">check failed</span></div>' +
        '<p class="setting-row__hint">' + escapeHtml(outputTopology.readinessError) + '</p>' +
      '</div>';
    }
    var readiness = outputTopology.readiness;
    if (!readiness) {
      return '<div class="output-card output-card--readiness">' +
        '<div class="output-card__head"><div><p class="output-card__title">Playback readiness</p>' +
        '<p class="setting-row__hint">Choose Check readiness on one saved channel to see the no-audio safety checklist.</p></div>' +
        '<span class="status-pill">not checked</span></div>' +
      '</div>';
    }
    var target = readiness.target || {};
    var gates = Array.isArray(readiness.required_gates) ? readiness.required_gates : [];
    var rows = gates.map(function(gate) {
      return [
        gate.passed ? 'info' : 'blocker',
        gate.label || gate.id || 'gate',
        gate.message || (gate.passed ? 'Passed' : 'Blocked')
      ];
    });
    var statusValue = readiness.preconditions_passed ? 'Preconditions passed' : 'Blocked';
    return '<div class="output-card output-card--readiness">' +
      '<div class="output-card__head"><div><p class="output-card__title">Playback readiness</p>' +
        '<p class="setting-row__hint">' + escapeHtml(target.label || 'Selected channel') + '</p></div>' +
        '<span class="status-pill' + (readiness.preconditions_passed ? ' status-pill--ready' : ' status-pill--blocked') + '">' +
          escapeHtml(statusValue) + '</span></div>' +
      renderOutputReadinessSummary(readiness) +
      renderOutputReadinessBlockers(readiness) +
      renderOutputHighFrequencyReadiness(readiness) +
      '<ul class="output-safety-list">' + rows.slice(0, 10).map(function(row) {
        return '<li class="output-safety-list__item output-safety-list__item--' + escapeHtml(row[0]) + '">' +
          '<span>' + escapeHtml(row[1]) + '</span>' +
          '<p>' + escapeHtml(row[2]) + '</p>' +
        '</li>';
      }).join('') + '</ul>' +
      '<p class="setting-row__hint">' + escapeHtml(readiness.next_step || 'No audio was emitted.') + '</p>' +
      '<p class="setting-row__hint">Playback allowed: ' + escapeHtml(readiness.playback_allowed ? 'yes' : 'no') + '. This checklist does not play sound.</p>' +
      renderOutputReadinessActions(readiness) +
      renderOutputReadinessPlayback(outputTopology.readinessPlayback) +
    '</div>';
  }
  function renderOutputReadinessActions(readiness) {
    var target = readiness && readiness.target || {};
    var lockReason = readinessTargetLockReason(readiness);
    var atFloor = outputCurrentLevelAtFloor();
    var floorConfirmed = outputFloorAudioConfirmedForReadiness(readiness);
    var disabled = !readiness || !readiness.preconditions_passed || !!lockReason ||
      (!atFloor && !floorConfirmed) || outputTopology.readinessPlaybackChecking;
    var attrs = 'data-group-id="' + escapeHtml(target.speaker_group_id || '') + '" ' +
      'data-role="' + escapeHtml(target.role || '') + '" ' +
      'data-label="' + escapeHtml(target.label || '') + '"';
    var artifactLabel = outputTopology.readinessPlaybackChecking === 'artifact' ?
      'Verifying' : 'Verify artifact';
    var roleLabel = humanRole(target.role || 'channel').toLowerCase();
    var playLabel = outputTopology.readinessPlaybackChecking === 'audio' ?
      'Playing' : (atFloor ? 'Play floor-level ' : 'Play quiet ') + roleLabel + ' test';
    var hints = [];
    if (lockReason) hints.push(lockReason + ' Artifact verification stays locked too so the UI cannot imply this target is ready for sound.');
    if (readiness && readiness.preconditions_passed && !atFloor && !floorConfirmed) {
      hints.push('Reset calibration level to the quiet floor before verifying an artifact or playing a test.');
    }
    if (readiness && readiness.preconditions_passed && floorConfirmed && !atFloor) {
      hints.push('Floor audio is confirmed for this target/session; raised tests remain bounded by the calibration level guard.');
    }
    return hints.map(function(hint) {
      return '<p class="setting-row__hint">' + escapeHtml(hint) + '</p>';
    }).join('') +
      '<div class="active-speaker-actions">' +
      '<button type="button" class="btn btn--ghost" data-act="play-output-readiness-tone" ' +
        attrs + ' data-audio="false"' + (disabled ? ' disabled' : '') + '>' +
        escapeHtml(artifactLabel) + '</button>' +
      (readiness && readiness.playback_allowed ? '<button type="button" class="btn btn--danger" ' +
        'data-act="play-output-readiness-tone" ' + attrs + ' data-audio="true"' +
        (disabled ? ' disabled' : '') + '>' + escapeHtml(playLabel) + '</button>' : '') +
    '</div>';
  }
  function renderOutputReadinessPlayback(playback) {
    if (!playback) return '';
    var artifact = playback.artifact || {};
    var target = playback.target || {};
    var rows = [
      ['Playback status', playback.status || 'unknown'],
      ['Backend', playback.backend || 'none'],
      ['Target', target.label || target.driver_role || target.role || 'unknown'],
      ['Tone', toneSummary(playback.tone || {})],
      ['Artifact', artifact.wav_basename || 'none'],
      ['Audio emitted', playback.audio_emitted ? 'Yes' : 'No']
    ];
    var issues = Array.isArray(playback.issues) ? playback.issues.slice(0, 4) : [];
    return '<div class="active-speaker-plan output-readiness-playback">' +
      '<p class="setting-row__title">Channel test result</p>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.map(function(issue) {
        return '<li>' + escapeHtml('Playback: ' + (issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
      renderOutputFloorAudioResultActions(playback) +
    '</div>';
  }
  function renderOutputSafetyCard(topology, statusValue) {
    var evaluation = outputEvaluation(topology);
    var clock = outputClockDomainReport();
    var blockers = Array.isArray(evaluation.blockers) ? evaluation.blockers : [];
    var warnings = Array.isArray(evaluation.warnings) ? evaluation.warnings : [];
    var safety = topology.safety || evaluation.safety || {};
    var rows = [];
    if (outputTopology.dirty) {
      rows.push(['warning', 'unsaved_draft', 'Save to run backend validation on this draft.']);
    }
    blockers.forEach(function(issue) { rows.push(['blocker', issue.code, issue.message]); });
    warnings.forEach(function(issue) { rows.push(['warning', issue.code, issue.message]); });
    if (clock && clock.status && clock.status !== 'single_device_clock') {
      rows.push([
        clock.composite_clock_supported ? 'info' : 'warning',
        'clock_domain',
        clock.recommendation || 'Output clocking needs review.'
      ]);
    }
    rows.push([
      safety.sound_tests_allowed ? 'warning' : 'info',
      'sound_tests_allowed',
      safety.sound_tests_allowed ? 'Sound tests are enabled.' : 'Sound tests remain disabled for this setup surface.'
    ]);
    return '<div class="output-card output-card--safety">' +
      '<div class="output-card__head"><div><p class="output-card__title">Safety evidence</p>' +
        '<p class="setting-row__hint">Backend validation owns the final decision.</p></div>' +
        '<span class="status-pill' + outputStatusClass(statusValue) + '">' + escapeHtml(statusValue) + '</span></div>' +
      '<ul class="output-safety-list">' + rows.slice(0, 8).map(function(row) {
        return '<li class="output-safety-list__item output-safety-list__item--' + escapeHtml(row[0]) + '">' +
          '<span>' + escapeHtml(row[1]) + '</span>' +
          '<p>' + escapeHtml(row[2]) + '</p>' +
        '</li>';
      }).join('') + '</ul>' +
    '</div>';
  }
  function renderActiveSpeakerStatus() {
    if (activeSpeaker.loading) {
      return '<div class="row-between active-speaker-status__head">' +
        '<span class="status-pill">Checking safety</span>' +
        '<button type="button" class="btn btn--ghost" data-act="refresh-active-speaker" disabled>Refresh</button>' +
      '</div>';
    }
    if (activeSpeaker.error && !activeSpeaker.payload) {
      return '<div class="active-speaker-status__stack">' +
        '<div class="row-between active-speaker-status__head">' +
          '<span class="status-pill status-pill--blocked">Probe failed</span>' +
          '<button type="button" class="btn btn--ghost" data-act="refresh-active-speaker">Retry</button>' +
        '</div>' +
        '<p class="setting-row__hint">' + escapeHtml(activeSpeaker.error) + '</p>' +
      '</div>';
    }
    if (!activeSpeaker.payload) {
      return '<div class="row-between active-speaker-status__head">' +
        '<span class="status-pill status-pill--planned">Preflight needed</span>' +
        '<button type="button" class="btn btn--ghost" data-act="refresh-active-speaker">Run safety preflight</button>' +
      '</div>';
    }
    var p = activeSpeaker.payload || {};
    var cfg = p.camilla_config || {};
    var alsa = p.alsa || {};
    var validation = p.camilla_validation || {};
    var safe = p.safe_playback || {};
    var session = activeSpeaker.session || {};
    var staged = activeSpeaker.stagedConfig || {};
    var ok = !!p.ok_to_load_active_config;
    var devices = Array.isArray(alsa.devices) ? alsa.devices.length : 0;
    var rows = [
      ['Camilla config', cfg.label || cfg.classification || 'Unknown'],
      ['Playback lane', (cfg.playback_device || 'Unknown') + (cfg.playback_channels ? ' · ' + cfg.playback_channels + ' ch' : '')],
      ['Volume ceiling', cfg.volume_limit_db == null ? 'Missing' : fmtDb(cfg.volume_limit_db) + ' dB'],
      ['ALSA playback devices', String(devices)],
      ['Config validation', validation.status || 'unknown'],
      ['Staged startup', staged.status || 'not staged'],
      ['Startup load', activeSpeaker.startupLoad && activeSpeaker.startupLoad.state ?
        (activeSpeaker.startupLoad.state.status || 'idle') : 'idle'],
      ['Safe playback', safe.playback_allowed ? 'Allowed' : 'Not allowed yet'],
      ['Safety session', session.status || 'Not armed'],
      ['Calibration level', fmtDbfs(activeSpeakerLevelConfig().value)]
    ];
    var envIssues = Array.isArray(p.issues) ? p.issues.slice(0, 4) : [];
    var sessionIssues = Array.isArray(session.issues) ? session.issues.slice(0, 4) : [];
    return '<div class="active-speaker-status__stack">' +
      '<div class="row-between active-speaker-status__head">' +
        '<span class="status-pill ' + (activeSpeaker.error ? 'status-pill--planned' :
          (ok ? 'status-pill--ready' : 'status-pill--blocked')) + '">' +
          escapeHtml(activeSpeaker.error ? 'Partial refresh' :
            (ok ? 'Load gate ready' : 'Load gate blocked')) + '</span>' +
        '<button type="button" class="btn btn--ghost" data-act="refresh-active-speaker">Refresh</button>' +
      '</div>' +
      (activeSpeaker.error ? '<p class="setting-row__hint">' + escapeHtml(activeSpeaker.error) + '</p>' : '') +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      renderActiveSpeakerIssues(envIssues, sessionIssues) +
      renderActiveSpeakerStagedConfig(activeSpeaker.stagedConfig) +
      renderActiveSpeakerBringup(activeSpeaker.bringup) +
      renderActiveSpeakerStartupLoad(activeSpeaker.startupLoad) +
      renderActiveSpeakerLevel() +
      renderActiveSpeakerActions(ok, session) +
      renderActiveSpeakerPlan(activeSpeaker.plan) +
      renderActiveSpeakerPlayback(activeSpeaker.playback) +
      '<p class="setting-row__hint">' + escapeHtml(safe.warning || 'Playback remains disabled until the safe tone path is implemented.') + '</p>' +
    '</div>';
  }
  function activeSpeakerLevelConfig() {
    var contract = activeSpeaker.calibrationLevel ||
      (activeSpeaker.targets && activeSpeaker.targets.calibration_level) || {};
    var raw = contract.test_signal || {};
    var min = Number(raw.min_level_dbfs);
    var max = Number(raw.max_level_dbfs);
    var step = Number(raw.step_db);
    var def = Number(raw.default_level_dbfs);
    if (!isFinite(min)) min = -80;
    if (!isFinite(max)) max = -45;
    if (!isFinite(step) || step <= 0) step = 1;
    if (!isFinite(def)) def = min;
    var value = Number(activeSpeaker.levelDbfs);
    if (!isFinite(value)) value = def;
    value = clamp(value, min, max);
    return {min: min, max: max, step: step, def: def, value: value};
  }
  function activeSpeakerMicRecommendation(code) {
    return {
      start_at_minimum: 'Start at the minimum level.',
      raise_slowly: 'Raise slowly, one backend-approved step at a time.',
      hold_level: 'Hold this level for the next check.',
      lower_level: 'Lower the level before continuing.',
      stop_or_lower: 'Stop or lower; clipping resets the level to the floor.'
    }[code] || 'Record a mic reading before treating this as guided calibration.';
  }
  function activeSpeakerSelectedReadinessTarget() {
    var target = outputTopology.readiness && outputTopology.readiness.target || null;
    if (!target || !target.speaker_group_id || !target.role) return null;
    return target;
  }
  function activeSpeakerTargetLabel(target) {
    if (!target) return 'Check readiness on a saved channel first';
    return target.label || [
      target.speaker_label || target.speaker_group_id || 'Speaker',
      humanRole(target.role || target.driver_role || 'channel'),
      target.physical_output_index == null && target.output_index == null ? '' :
        'Output ' + (Number(target.physical_output_index != null ?
          target.physical_output_index : target.output_index) + 1)
    ].filter(Boolean).join(' · ');
  }
  function activeSpeakerAutoLevelLabel(autoLevel) {
    if (!autoLevel || !autoLevel.kind) return 'No guided step yet';
    return (autoLevel.status || 'hold').replace(/_/g, ' ') + ': ' +
      (autoLevel.reason || 'level held');
  }
  function renderActiveSpeakerLevel() {
    var cfg = activeSpeakerLevelConfig();
    var contract = activeSpeaker.calibrationLevel ||
      activeSpeaker.plan && activeSpeaker.plan.calibration_level ||
      activeSpeaker.targets && activeSpeaker.targets.calibration_level || {};
    var meter = contract.mic_meter || {};
    var guard = contract.software_gain_guard || {};
    var issues = Array.isArray(contract.issues) ? contract.issues : [];
    var label = {
      unmeasured: 'Mic unmeasured',
      too_quiet: 'Too quiet',
      low: 'Low',
      usable: 'Usable',
      too_loud: 'Too loud',
      clipping: 'Clipping'
    }[meter.status] || 'Mic unmeasured';
    var toneClass = meter.tone === 'danger' ? ' status-pill--blocked' :
      (meter.tone === 'ok' ? ' status-pill--ready' : '');
    var observedInput = activeSpeakerMicObservation.observedDbfs;
    if (!observedInput && meter.observed_dbfs != null) {
      observedInput = String(meter.observed_dbfs);
    }
    var clippingChecked = activeSpeakerMicObservation.clipping ||
      meter.status === 'clipping';
    var selectedTarget = activeSpeakerSelectedReadinessTarget();
    var autoLevel = contract.auto_level || {};
    var autoBusy = activeSpeaker.action === 'Applying guided level';
    var autoDisabled = !selectedTarget || autoBusy;
    return '<div class="active-speaker-level">' +
      '<div class="row-between active-speaker-level__head">' +
        '<div class="setting-row__text">' +
          '<p class="setting-row__title">Calibration level</p>' +
          '<p class="setting-row__hint">Test-signal level only. Normal listening volume is untouched.</p>' +
        '</div>' +
        '<span class="active-speaker-level__readout" id="active-speaker-level-readout">' +
          escapeHtml(fmtDbfs(cfg.value)) + '</span>' +
      '</div>' +
      '<input type="range" class="active-speaker-level__range" id="active-speaker-level" ' +
        'min="' + cfg.min + '" max="' + cfg.max + '" step="' + cfg.step + '" value="' + cfg.value + '" ' +
        'aria-label="Calibration test signal level">' +
      '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--ghost" data-act="active-level" data-level-action="lower">Lower</button>' +
        '<button type="button" class="btn btn--ghost" data-act="active-level" data-level-action="reset">Reset</button>' +
        '<button type="button" class="btn btn--ghost" data-act="active-level" data-level-action="raise">Raise 1 dB</button>' +
      '</div>' +
      '<div class="active-speaker-meter">' +
        '<span class="active-speaker-meter__label">Quiet</span>' +
        '<span class="active-speaker-meter__label">Usable</span>' +
        '<span class="active-speaker-meter__label">High</span>' +
      '</div>' +
      '<div class="row-between active-speaker-level__meter">' +
        '<span class="status-pill' + toneClass + '">' + escapeHtml(label) + '</span>' +
        '<span class="setting-row__hint">JTS caps this at ' + escapeHtml(fmtDbfs(cfg.max)) +
          ' and limits upward moves to ' + escapeHtml(fmtDb(Number(guard.upward_step_limit_db) || cfg.step)) + ' dB.</span>' +
      '</div>' +
      '<div class="active-speaker-mic-observation">' +
        '<div class="active-speaker-mic-observation__fields">' +
          '<label for="active-speaker-mic-dbfs">Mic reading dBFS' +
            '<input type="number" id="active-speaker-mic-dbfs" inputmode="decimal" ' +
              'min="-120" max="0" step="0.1" value="' + escapeHtml(observedInput) + '" ' +
              'placeholder="-35.0"></label>' +
          '<label class="active-speaker-mic-observation__check">' +
            '<input type="checkbox" id="active-speaker-mic-clipping"' +
              (clippingChecked ? ' checked' : '') + '> Clipping observed</label>' +
          '<button type="button" class="btn btn--ghost" data-act="active-mic-observation">Record reading</button>' +
        '</div>' +
        '<p class="setting-row__hint">' + escapeHtml(activeSpeakerMicRecommendation(meter.recommendation)) + '</p>' +
        '<p class="setting-row__hint">This records operator-observed capture level only. It does not play sound or claim calibrated SPL.</p>' +
      '</div>' +
      '<div class="active-speaker-guided-step">' +
        '<dl class="active-speaker-facts">' +
          '<div><dt>Guided target</dt><dd>' + escapeHtml(activeSpeakerTargetLabel(selectedTarget)) + '</dd></div>' +
          '<div><dt>Recommendation</dt><dd>' + escapeHtml(activeSpeakerAutoLevelLabel(autoLevel)) + '</dd></div>' +
        '</dl>' +
        '<div class="active-speaker-actions">' +
          '<button type="button" class="btn btn--primary" data-act="active-auto-level"' +
            (autoDisabled ? ' disabled' : '') + '>' +
            escapeHtml(autoBusy ? 'Applying' : 'Apply recommended step') + '</button>' +
        '</div>' +
        '<p class="setting-row__hint">Uses the selected saved-channel readiness target, latest mic observation, driver protection policy, and floor-first safe-session evidence.</p>' +
      '</div>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.slice(0, 3).map(function(issue) {
        return '<li>' + escapeHtml('Level guard: ' + (issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
    '</div>';
  }
  function renderActiveSpeakerIssues(envIssues, sessionIssues) {
    var rows = [];
    envIssues.forEach(function(issue) {
      rows.push(['Preflight', issue]);
    });
    sessionIssues.forEach(function(issue) {
      rows.push(['Session', issue]);
    });
    if (!rows.length) {
      return '<p class="setting-row__hint">No active blockers in the safety preflight.</p>';
    }
    return '<ul class="active-speaker-issues">' + rows.slice(0, 6).map(function(row) {
      var issue = row[1] || {};
      return '<li>' + escapeHtml(row[0] + ': ' + (issue.code || 'issue')) + '</li>';
    }).join('') + '</ul>';
  }
  function renderActiveSpeakerStagedConfig(staged) {
    if (!staged || staged.status === 'not_staged') return '';
    var cfg = staged.config || {};
    var preset = staged.preset || {};
    var load = staged.load || {};
    var rows = [
      ['Stage status', staged.status || 'unknown'],
      ['Preset', preset.name || preset.preset_id || 'unknown'],
      ['Config', cfg.basename || 'none'],
      ['Playback device', cfg.playback_device || 'missing'],
      ['Channels', cfg.playback_channels == null ? 'unknown' : String(cfg.playback_channels)],
      ['Validation', cfg.validation && cfg.validation.status || 'unknown'],
      ['Protective HP', cfg.tweeter_protective_highpass_hz ?
        String(cfg.tweeter_protective_highpass_hz) + ' Hz' : 'unknown'],
      ['Load gate', load.load_gate || 'startup load not checked']
    ];
    var issues = Array.isArray(staged.issues) ? staged.issues.slice(0, 5) : [];
    return '<div class="active-speaker-plan active-speaker-stage">' +
      '<p class="setting-row__title">Protected startup config</p>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.map(function(issue) {
        return '<li>' + escapeHtml('Stage: ' + (issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
      '<p class="setting-row__hint">' + escapeHtml(staged.next_step || 'Staged only; no DSP graph was loaded.') + '</p>' +
    '</div>';
  }
  function modeStatusLabel(mode) {
    return {
      ready: 'Ready',
      ready_to_arm: 'Ready to arm',
      armed: 'Armed',
      ready_relative: 'Mic relative',
      ready_calibrated: 'Mic calibrated',
      blocked: 'Blocked'
    }[mode && mode.status] || (mode && mode.status) || 'Unknown';
  }
  function renderActiveSpeakerBringup(preflight) {
    if (!preflight) return '';
    var modes = preflight.modes || {};
    var manual = modes.manual_guarded_bringup || {};
    var guided = modes.guided_calibration || {};
    var mic = preflight.microphone || {};
    var guard = preflight.software_guard || {};
    var level = preflight.calibration_level || {};
    var failed = []
      .concat(Array.isArray(manual.required_gates) ? manual.required_gates : [])
      .concat(Array.isArray(guided.required_gates) ? guided.required_gates : [])
      .filter(function(gate, index, arr) {
        if (!gate || gate.passed) return false;
        var messageKey = String(gate.message || '').trim().toLowerCase();
        return arr.findIndex(function(item) {
          if (!item || item.passed) return false;
          if (item.id && gate.id && item.id === gate.id) return true;
          return messageKey && String(item.message || '').trim().toLowerCase() === messageKey;
        }) === index;
      })
      .slice(0, 5);
    var rows = [
      ['Manual guarded', modeStatusLabel(manual)],
      ['Guided calibration', modeStatusLabel(guided)],
      ['Microphone', mic.status || 'not checked'],
      ['Guard', guard.status || 'unknown'],
      ['Start level', level.at_floor ? 'At floor' : 'Reset needed']
    ];
    return '<div class="active-speaker-plan active-speaker-stage">' +
      '<p class="setting-row__title">Bring-up preflight</p>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      (failed.length ? '<ul class="active-speaker-issues">' + failed.map(function(gate) {
        return '<li>' + escapeHtml('Preflight: ' + (gate.message || gate.id || 'gate blocked')) + '</li>';
      }).join('') + '</ul>' : '') +
      '<p class="setting-row__hint">' + escapeHtml(preflight.next_step || 'Choose guided calibration when a mic is working; manual guarded bring-up stays available for known plans.') + '</p>' +
    '</div>';
  }
  function renderActiveSpeakerStartupLoad(startupLoad) {
    if (!startupLoad) return '';
    var state = startupLoad.state || {};
    var preflight = startupLoad.preflight || {};
    var candidate = preflight.candidate || {};
    var canLoad = !!preflight.load_allowed;
    var canRollback = !!state.rollback_available;
    var busy = !!activeSpeaker.action;
    var rows = [
      ['Load state', state.status || 'idle'],
      ['Preflight', preflight.status || 'unknown'],
      ['Candidate', candidate.basename || 'none'],
      ['Path safety', preflight.path_safety && preflight.path_safety.load_gate || 'unknown'],
      ['Rollback target', state.previous_config_path ? state.previous_config_path.split('/').pop() : 'none']
    ];
    var issues = []
      .concat(Array.isArray(preflight.issues) ? preflight.issues : [])
      .concat(Array.isArray(state.issues) ? state.issues : [])
      .filter(function(issue, index, arr) {
        if (!issue) return false;
        var code = issue.code || '';
        return arr.findIndex(function(item) {
          return item && item.code === code;
        }) === index;
      })
      .slice(0, 5);
    return '<div class="active-speaker-plan active-speaker-stage">' +
      '<p class="setting-row__title">Startup load</p>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.map(function(issue) {
        return '<li>' + escapeHtml('Load: ' + (issue.message || issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
      '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--ghost" data-act="check-active-path-safety"' +
          (busy ? ' disabled' : '') + '>Check protected path</button>' +
        '<button type="button" class="btn btn--ghost" data-act="load-active-startup"' +
          (busy || !canLoad ? ' disabled' : '') + '>Load protected config</button>' +
        '<button type="button" class="btn btn--ghost" data-act="rollback-active-startup"' +
          (busy || !canRollback ? ' disabled' : '') + '>Rollback to prior config</button>' +
      '</div>' +
      '<p class="setting-row__hint">' + escapeHtml(preflight.next_step || 'Loading reloads CamillaDSP but does not play sound.') + '</p>' +
    '</div>';
  }
  function renderActiveSpeakerActions(ok, session) {
    var busy = !!activeSpeaker.action;
    var state = session || {};
    var targets = activeSpeaker.targets && Array.isArray(activeSpeaker.targets.targets)
      ? activeSpeaker.targets.targets : [];
    var stageDisabled = outputTopology.dirty ? ' disabled' : '';
    if (busy) {
      return '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--ghost" disabled>' + escapeHtml(activeSpeaker.action) + '</button>' +
        '<span class="setting-row__hint">No audio is emitted by this step.</span>' +
      '</div>';
    }
    if (state.status === 'armed') {
      var targetButtons = targets.length ? targets.slice(0, 6).map(function(target) {
        var label = target.label || ((target.side || '') + ' ' + (target.driver_role || 'channel'));
        return '<button type="button" class="btn btn--ghost" data-act="prepare-active-tone" ' +
          'data-side="' + escapeHtml(target.side || '') + '" ' +
          'data-driver-role="' + escapeHtml(target.driver_role || '') + '">' +
          'Prepare ' + escapeHtml(label) + '</button>';
      }).join('') : '<span class="setting-row__hint">No preset channel targets available.</span>';
      return '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--danger" data-act="stop-active-speaker">Stop</button>' +
        '<button type="button" class="btn btn--ghost" data-act="stage-active-config"' + stageDisabled + '>Stage protected config</button>' +
        '<span class="setting-row__hint">Armed safety session. Artifact checks are available; audible tests require saved output readiness and explicit lab enablement.</span>' +
      '</div>' +
      '<div class="active-speaker-actions active-speaker-actions--targets">' +
        targetButtons +
      '</div>';
    }
    return '<div class="active-speaker-actions">' +
      '<button type="button" class="btn btn--ghost" data-act="stage-active-config"' + stageDisabled + '>Stage protected config</button>' +
      '<button type="button" class="btn btn--ghost" data-act="arm-active-speaker"' + (ok ? '' : ' disabled') + '>Arm safe session</button>' +
      '<span class="setting-row__hint">Arming records the safety state only; it does not play sound.</span>' +
    '</div>';
  }
  function renderActiveSpeakerPlan(plan) {
    if (!plan) return '';
    var tone = plan.tone || {};
    var target = plan.target || {};
    var rows = [
      ['Plan status', plan.status || 'unknown'],
      ['Target', target.label || target.driver_role || 'unknown'],
      ['Tone', (tone.frequency_hz || '?') + ' Hz at ' + fmtDbfs(tone.level_dbfs)],
      ['Duration', String(tone.duration_ms || '?') + ' ms'],
      ['Would play', plan.would_play ? 'Yes' : 'No']
    ];
    var issues = Array.isArray(plan.issues) ? plan.issues.slice(0, 4) : [];
    return '<div class="active-speaker-plan">' +
      '<p class="setting-row__title">Prepared channel test</p>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.map(function(issue) {
        return '<li>' + escapeHtml('Plan: ' + (issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
      (plan.status === 'ready' ? '<div class="active-speaker-actions">' +
        '<button type="button" class="btn btn--ghost" data-act="verify-active-tone">Verify tone artifact</button>' +
        '<span class="setting-row__hint">Generates a no-audio multi-channel WAV for inspection.</span>' +
      '</div>' : '') +
      '<p class="setting-row__hint">' + escapeHtml(plan.next_step || 'Prepared only; no sound was emitted.') + '</p>' +
    '</div>';
  }
  function renderActiveSpeakerPlayback(playback) {
    if (!playback) return '';
    var artifact = playback.artifact || {};
    var target = playback.target || {};
    var rows = [
      ['Playback status', playback.status || 'unknown'],
      ['Backend', playback.backend || 'none'],
      ['Target', target.label || target.driver_role || 'unknown'],
      ['Artifact', artifact.wav_basename || 'none'],
      ['Channels', artifact.channel_count == null ? 'unknown' : String(artifact.channel_count)],
      ['Audio emitted', playback.audio_emitted ? 'Yes' : 'No']
    ];
    var issues = Array.isArray(playback.issues) ? playback.issues.slice(0, 4) : [];
    return '<div class="active-speaker-plan">' +
      '<p class="setting-row__title">Tone artifact check</p>' +
      '<dl class="active-speaker-facts">' + rows.map(function(row) {
        return '<div><dt>' + escapeHtml(row[0]) + '</dt><dd>' + escapeHtml(row[1]) + '</dd></div>';
      }).join('') + '</dl>' +
      (issues.length ? '<ul class="active-speaker-issues">' + issues.map(function(issue) {
        return '<li>' + escapeHtml('Playback: ' + (issue.code || 'issue')) + '</li>';
      }).join('') + '</ul>' : '') +
      '<p class="setting-row__hint">' + escapeHtml(playback.audio_emitted ?
        'Audio was emitted by the explicitly enabled lab backend.' :
        'No audio was emitted by this backend.') + '</p>' +
    '</div>';
  }

  function outputChannel(role, index) {
    var tweeter = role === 'tweeter';
    return {
      role: role,
      physical_output_index: index,
      identity_verified: false,
      startup_muted: true,
      protection_required: tweeter,
      protection_status: tweeter ? 'required_missing' : 'not_required'
    };
  }
  function baseOutputDraft(source) {
    var topology = source || currentOutputTopology();
    if (!topology) return null;
    var next = clone(topology);
    next.status = 'draft';
    delete next.evaluation;
    if (next.safety) next.safety.sound_tests_allowed = false;
    return next;
  }
  function outputTemplateDefinition(kind) {
    return {
      mono_passive: {
        id: 'mono_passive',
        label: 'Mono passive',
        hint: 'One full-range channel',
        minOutputs: 1,
        name: 'Mono passive output',
        groups: [{
          id: 'main', label: 'Main speaker', kind: 'mono',
          mode: 'full_range_passive',
          position: {x: 0, y: 0.42, rotation_degrees: 0},
          channels: [outputChannel('full_range', 0)]
        }],
        routing: {mono_group_id: 'main'}
      },
      mono_active_2way: {
        id: 'mono_active_2way',
        label: 'Mono active 2-way',
        hint: 'Woofer + tweeter',
        minOutputs: 2,
        name: 'Mono active 2-way output',
        groups: [{
          id: 'main', label: 'Main speaker', kind: 'mono',
          mode: 'active_2_way',
          position: {x: 0, y: 0.42, rotation_degrees: 0},
          channels: [outputChannel('woofer', 0), outputChannel('tweeter', 1)]
        }],
        routing: {mono_group_id: 'main'}
      },
      mono_active_3way: {
        id: 'mono_active_3way',
        label: 'Mono active 3-way',
        hint: 'Woofer + mid + tweeter',
        minOutputs: 3,
        name: 'Mono active 3-way output',
        groups: [{
          id: 'main', label: 'Main speaker', kind: 'mono',
          mode: 'active_3_way',
          position: {x: 0, y: 0.42, rotation_degrees: 0},
          channels: [
            outputChannel('woofer', 0),
            outputChannel('mid', 1),
            outputChannel('tweeter', 2)
          ]
        }],
        routing: {mono_group_id: 'main'}
      },
      stereo_passive: {
        id: 'stereo_passive',
        label: 'Stereo passive',
        hint: 'Left + right full-range',
        minOutputs: 2,
        name: 'Stereo passive outputs',
        groups: [
          {
            id: 'left', label: 'Left speaker', kind: 'left',
            mode: 'full_range_passive',
            position: {x: -0.65, y: 0.42, rotation_degrees: 0},
            channels: [outputChannel('full_range', 0)]
          },
          {
            id: 'right', label: 'Right speaker', kind: 'right',
            mode: 'full_range_passive',
            position: {x: 0.65, y: 0.42, rotation_degrees: 0},
            channels: [outputChannel('full_range', 1)]
          }
        ],
        routing: {main_left_group_id: 'left', main_right_group_id: 'right'}
      },
      stereo_active_2way: {
        id: 'stereo_active_2way',
        label: 'Stereo active 2-way',
        hint: 'Two channels per speaker',
        minOutputs: 4,
        name: 'Stereo active 2-way outputs',
        groups: [
          {
            id: 'left', label: 'Left speaker', kind: 'left',
            mode: 'active_2_way',
            position: {x: -0.65, y: 0.42, rotation_degrees: 0},
            channels: [outputChannel('woofer', 0), outputChannel('tweeter', 1)]
          },
          {
            id: 'right', label: 'Right speaker', kind: 'right',
            mode: 'active_2_way',
            position: {x: 0.65, y: 0.42, rotation_degrees: 0},
            channels: [outputChannel('woofer', 2), outputChannel('tweeter', 3)]
          }
        ],
        routing: {main_left_group_id: 'left', main_right_group_id: 'right'}
      },
      stereo_active_3way: {
        id: 'stereo_active_3way',
        label: 'Stereo active 3-way',
        hint: 'Three channels per speaker',
        minOutputs: 6,
        name: 'Stereo active 3-way outputs',
        groups: [
          {
            id: 'left', label: 'Left speaker', kind: 'left',
            mode: 'active_3_way',
            position: {x: -0.65, y: 0.42, rotation_degrees: 0},
            channels: [
              outputChannel('woofer', 0),
              outputChannel('mid', 1),
              outputChannel('tweeter', 2)
            ]
          },
          {
            id: 'right', label: 'Right speaker', kind: 'right',
            mode: 'active_3_way',
            position: {x: 0.65, y: 0.42, rotation_degrees: 0},
            channels: [
              outputChannel('woofer', 3),
              outputChannel('mid', 4),
              outputChannel('tweeter', 5)
            ]
          }
        ],
        routing: {main_left_group_id: 'left', main_right_group_id: 'right'}
      }
    }[kind] || null;
  }

  return {
    renderActiveSpeakerSetup: renderActiveSpeakerSetup,
    currentOutputTopology: currentOutputTopology,
    outputGroups: outputGroups,
    outputHardware: outputHardware,
    outputEvaluation: outputEvaluation,
    outputIdentityReport: outputIdentityReport,
    outputClockDomainReport: outputClockDomainReport,
    identityTargetFor: identityTargetFor,
    outputAssignedMap: outputAssignedMap,
    outputStatusClass: outputStatusClass,
    humanMode: humanMode,
    humanRole: humanRole,
    outputRoleSummary: outputRoleSummary,
    assignedOutputIndices: assignedOutputIndices,
    firstUnusedOutputIndex: firstUnusedOutputIndex,
    outputSubwooferGroup: outputSubwooferGroup,
    outputHasSubwoofer: outputHasSubwoofer,
    nextSubwooferGroupId: nextSubwooferGroupId,
    addSubwooferToTopology: addSubwooferToTopology,
    removeSubwooferFromTopology: removeSubwooferFromTopology,
    driverResearchRoleLabel: driverResearchRoleLabel,
    driverResearchPrompt: driverResearchPrompt,
    summarizeDriverResearchPayload: summarizeDriverResearchPayload,
    driverResearchDraftSaved: driverResearchDraftSaved,
    ingestDesignDraft: ingestDesignDraft,
    fetchDesignDraft: fetchDesignDraft,
    ingestCrossoverPreview: ingestCrossoverPreview,
    fetchCrossoverPreview: fetchCrossoverPreview,
    toneSummary: toneSummary,
    humanProtectionStatus: humanProtectionStatus,
    renderOutputTopologySetup: renderOutputTopologySetup,
    renderOutputHardwareRefresh: renderOutputHardwareRefresh,
    outputIdentityComplete: outputIdentityComplete,
    outputStartupLoaded: outputStartupLoaded,
    outputStepState: outputStepState,
    defaultOutputStep: defaultOutputStep,
    outputStepIsOpen: outputStepIsOpen,
    outputStepTitle: outputStepTitle,
    outputStepCanOpen: outputStepCanOpen,
    openOutputStep: openOutputStep,
    renderOutputStepCard: renderOutputStepCard,
    renderOutputStepButton: renderOutputStepButton,
    outputTemplateKindFromAxes: outputTemplateKindFromAxes,
    outputTemplateAxesForTopology: outputTemplateAxesForTopology,
    outputTemplateAxisButton: outputTemplateAxisButton,
    renderOutputSetupTemplates: renderOutputSetupTemplates,
    renderOutputSubwooferCard: renderOutputSubwooferCard,
    renderDriverResearchSummary: renderDriverResearchSummary,
    renderDriverResearchCard: renderDriverResearchCard,
    previewStatusClass: previewStatusClass,
    renderPreviewIssues: renderPreviewIssues,
    renderCrossoverPreviewRows: renderCrossoverPreviewRows,
    renderCrossoverPreviewCard: renderCrossoverPreviewCard,
    renderOutputTopologyBody: renderOutputTopologyBody,
    renderOutputHardwareCard: renderOutputHardwareCard,
    outputGroupPoint: outputGroupPoint,
    outputGroupInitial: outputGroupInitial,
    renderOutputStageCard: renderOutputStageCard,
    renderOutputGroupsCard: renderOutputGroupsCard,
    renderOutputGroup: renderOutputGroup,
    renderOutputIdentityCard: renderOutputIdentityCard,
    sequenceStep: sequenceStep,
    renderOutputBringupSequence: renderOutputBringupSequence,
    renderOutputCommissioningRehearsal: renderOutputCommissioningRehearsal,
    outputCurrentLevelAtFloor: outputCurrentLevelAtFloor,
    outputTargetSignature: outputTargetSignature,
    outputSameTarget: outputSameTarget,
    outputFloorAudioConfirmedForReadiness: outputFloorAudioConfirmedForReadiness,
    outputFloorAudioPendingForPlayback: outputFloorAudioPendingForPlayback,
    renderOutputFloorAudioResultActions: renderOutputFloorAudioResultActions,
    quietStartTargetLabel: quietStartTargetLabel,
    quietStartLabel: quietStartLabel,
    readinessTargetLockReason: readinessTargetLockReason,
    readinessBlockedReasons: readinessBlockedReasons,
    renderOutputReadinessSummary: renderOutputReadinessSummary,
    renderOutputReadinessBlockers: renderOutputReadinessBlockers,
    renderOutputHighFrequencyReadiness: renderOutputHighFrequencyReadiness,
    renderOutputReadinessCard: renderOutputReadinessCard,
    renderOutputReadinessActions: renderOutputReadinessActions,
    renderOutputReadinessPlayback: renderOutputReadinessPlayback,
    renderOutputSafetyCard: renderOutputSafetyCard,
    renderActiveSpeakerStatus: renderActiveSpeakerStatus,
    activeSpeakerLevelConfig: activeSpeakerLevelConfig,
    activeSpeakerMicRecommendation: activeSpeakerMicRecommendation,
    activeSpeakerSelectedReadinessTarget: activeSpeakerSelectedReadinessTarget,
    activeSpeakerTargetLabel: activeSpeakerTargetLabel,
    activeSpeakerAutoLevelLabel: activeSpeakerAutoLevelLabel,
    renderActiveSpeakerLevel: renderActiveSpeakerLevel,
    renderActiveSpeakerIssues: renderActiveSpeakerIssues,
    renderActiveSpeakerStagedConfig: renderActiveSpeakerStagedConfig,
    modeStatusLabel: modeStatusLabel,
    renderActiveSpeakerBringup: renderActiveSpeakerBringup,
    renderActiveSpeakerStartupLoad: renderActiveSpeakerStartupLoad,
    renderActiveSpeakerActions: renderActiveSpeakerActions,
    renderActiveSpeakerPlan: renderActiveSpeakerPlan,
    renderActiveSpeakerPlayback: renderActiveSpeakerPlayback,
    outputChannel: outputChannel,
    baseOutputDraft: baseOutputDraft,
    outputTemplateDefinition: outputTemplateDefinition
  };
}
