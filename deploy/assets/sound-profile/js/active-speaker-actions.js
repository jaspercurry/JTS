export function createActiveSpeakerActions(deps) {
  var store = deps.store;
  var api = deps.api;
  var views = deps.views;
  var activeSpeaker = store.activeSpeaker;
  var outputTopology = store.outputTopology;
  var driverResearch = store.driverResearch;
  var crossoverPreview = store.crossoverPreview;
  var patchActiveSpeaker = store.patchActiveSpeaker;
  var patchActiveSpeakerMicObservation = store.patchActiveSpeakerMicObservation;
  var el = deps.el;
  var jsonHeaders = deps.jsonHeaders;
  var clone = deps.clone;
  var clamp = deps.clamp;
  var fmtDbfs = deps.fmtDbfs;
  var status = deps.status;
  var render = deps.render;
  var jtsConfirm = deps.jtsConfirm;
  var renderActiveSpeakerSetup = views.renderActiveSpeakerSetup;
  var currentOutputTopology = views.currentOutputTopology;
  var outputGroups = views.outputGroups;
  var outputHardware = views.outputHardware;
  var outputEvaluation = views.outputEvaluation;
  var outputIdentityReport = views.outputIdentityReport;
  var outputClockDomainReport = views.outputClockDomainReport;
  var identityTargetFor = views.identityTargetFor;
  var outputAssignedMap = views.outputAssignedMap;
  var outputStatusClass = views.outputStatusClass;
  var humanMode = views.humanMode;
  var humanRole = views.humanRole;
  var outputRoleSummary = views.outputRoleSummary;
  var assignedOutputIndices = views.assignedOutputIndices;
  var firstUnusedOutputIndex = views.firstUnusedOutputIndex;
  var outputSubwooferGroup = views.outputSubwooferGroup;
  var outputHasSubwoofer = views.outputHasSubwoofer;
  var nextSubwooferGroupId = views.nextSubwooferGroupId;
  var addSubwooferToTopology = views.addSubwooferToTopology;
  var removeSubwooferFromTopology = views.removeSubwooferFromTopology;
  var driverResearchRoleLabel = views.driverResearchRoleLabel;
  var driverResearchPrompt = views.driverResearchPrompt;
  var summarizeDriverResearchPayload = views.summarizeDriverResearchPayload;
  var driverResearchDraftSaved = views.driverResearchDraftSaved;
  var driverResearchCanPreparePreview = views.driverResearchCanPreparePreview;
  var driverResearchStepSatisfied = views.driverResearchStepSatisfied;
  var ingestDesignDraft = views.ingestDesignDraft;
  var fetchDesignDraft = views.fetchDesignDraft;
  var ingestCrossoverPreview = views.ingestCrossoverPreview;
  var fetchCrossoverPreview = views.fetchCrossoverPreview;
  var toneSummary = views.toneSummary;
  var renderOutputTopologySetup = views.renderOutputTopologySetup;
  var renderOutputHardwareRefresh = views.renderOutputHardwareRefresh;
  var outputIdentityComplete = views.outputIdentityComplete;
  var outputStartupLoaded = views.outputStartupLoaded;
  var outputStepState = views.outputStepState;
  var defaultOutputStep = views.defaultOutputStep;
  var outputStepIsOpen = views.outputStepIsOpen;
  var outputStepTitle = views.outputStepTitle;
  var outputStepCanOpen = views.outputStepCanOpen;
  var openOutputStep = views.openOutputStep;
  var renderOutputStepCard = views.renderOutputStepCard;
  var renderOutputStepButton = views.renderOutputStepButton;
  var outputTemplateKindFromAxes = views.outputTemplateKindFromAxes;
  var outputTemplateAxesForTopology = views.outputTemplateAxesForTopology;
  var outputTemplateAxisButton = views.outputTemplateAxisButton;
  var renderOutputSetupTemplates = views.renderOutputSetupTemplates;
  var renderOutputSubwooferCard = views.renderOutputSubwooferCard;
  var renderDriverResearchSummary = views.renderDriverResearchSummary;
  var renderDriverResearchCard = views.renderDriverResearchCard;
  var previewStatusClass = views.previewStatusClass;
  var renderPreviewIssues = views.renderPreviewIssues;
  var renderCrossoverPreviewRows = views.renderCrossoverPreviewRows;
  var renderCrossoverPreviewCard = views.renderCrossoverPreviewCard;
  var renderOutputTopologyBody = views.renderOutputTopologyBody;
  var renderOutputHardwareCard = views.renderOutputHardwareCard;
  var outputGroupPoint = views.outputGroupPoint;
  var outputGroupInitial = views.outputGroupInitial;
  var renderOutputStageCard = views.renderOutputStageCard;
  var renderOutputGroupsCard = views.renderOutputGroupsCard;
  var renderOutputGroup = views.renderOutputGroup;
  var renderOutputIdentityCard = views.renderOutputIdentityCard;
  var sequenceStep = views.sequenceStep;
  var renderOutputBringupSequence = views.renderOutputBringupSequence;
  var renderOutputCommissioningRehearsal = views.renderOutputCommissioningRehearsal;
  var outputCurrentLevelAtFloor = views.outputCurrentLevelAtFloor;
  var outputTargetSignature = views.outputTargetSignature;
  var outputSameTarget = views.outputSameTarget;
  var outputFloorAudioConfirmedForReadiness = views.outputFloorAudioConfirmedForReadiness;
  var outputFloorAudioPendingForPlayback = views.outputFloorAudioPendingForPlayback;
  var renderOutputFloorAudioResultActions = views.renderOutputFloorAudioResultActions;
  var quietStartTargetLabel = views.quietStartTargetLabel;
  var quietStartLabel = views.quietStartLabel;
  var readinessTargetLockReason = views.readinessTargetLockReason;
  var readinessBlockedReasons = views.readinessBlockedReasons;
  var renderOutputReadinessSummary = views.renderOutputReadinessSummary;
  var renderOutputReadinessBlockers = views.renderOutputReadinessBlockers;
  var renderOutputHighFrequencyReadiness = views.renderOutputHighFrequencyReadiness;
  var renderOutputReadinessCard = views.renderOutputReadinessCard;
  var renderOutputReadinessActions = views.renderOutputReadinessActions;
  var renderOutputReadinessPlayback = views.renderOutputReadinessPlayback;
  var renderOutputSafetyCard = views.renderOutputSafetyCard;
  var renderActiveSpeakerStatus = views.renderActiveSpeakerStatus;
  var activeSpeakerLevelConfig = views.activeSpeakerLevelConfig;
  var activeSpeakerMicRecommendation = views.activeSpeakerMicRecommendation;
  var activeSpeakerSelectedReadinessTarget = views.activeSpeakerSelectedReadinessTarget;
  var activeSpeakerTargetLabel = views.activeSpeakerTargetLabel;
  var activeSpeakerAutoLevelLabel = views.activeSpeakerAutoLevelLabel;
  var renderActiveSpeakerLevel = views.renderActiveSpeakerLevel;
  var renderActiveSpeakerIssues = views.renderActiveSpeakerIssues;
  var renderActiveSpeakerStagedConfig = views.renderActiveSpeakerStagedConfig;
  var modeStatusLabel = views.modeStatusLabel;
  var renderActiveSpeakerBringup = views.renderActiveSpeakerBringup;
  var renderActiveSpeakerStartupLoad = views.renderActiveSpeakerStartupLoad;
  var renderActiveSpeakerActions = views.renderActiveSpeakerActions;
  var outputChannel = views.outputChannel;
  var baseOutputDraft = views.baseOutputDraft;
  var outputTemplateDefinition = views.outputTemplateDefinition;

  function ingestOutputTopology(payload) {
    var topology = payload && (payload.output_topology || payload);
    outputTopology.payload = topology || null;
    outputTopology.draft = topology ? clone(topology) : null;
    outputTopology.identity = payload && payload.channel_identity || topology && topology.channel_identity || null;
    outputTopology.clockDomain = payload && payload.clock_domain || topology && topology.clock_domain || null;
    outputTopology.error = '';
    outputTopology.dirty = false;
    outputTopology.saving = false;
    outputTopology.loading = false;
    outputTopology.identitySaving = '';
    outputTopology.protectionSaving = '';
    outputTopology.readiness = null;
    outputTopology.readinessChecking = '';
    outputTopology.readinessError = '';
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
  }
  async function refreshOutputTopology(options) {
    options = options || {};
    if (!options.silent && outputTopology.dirty &&
        !await jtsConfirm('Refresh hardware and lose the unsaved speaker layout draft?')) {
      return;
    }
    if (!options.silent) outputTopology.touched = true;
    outputTopology.loading = true;
    outputTopology.error = '';
    if (!options.silent) render();
    try {
      var payload = await api.get('./output-topology', 'speaker layout load failed');
      ingestOutputTopology(payload);
      try {
        await fetchDesignDraft();
      } catch (draftError) {
        driverResearch.designDraft = {
          status: 'unreadable',
          summary: {},
          issues: [{message: draftError.message}]
        };
      }
      try {
        await fetchCrossoverPreview();
      } catch (previewError) {
        crossoverPreview.payload = null;
        crossoverPreview.error = previewError.message;
      }
    } catch (e) {
      outputTopology.loading = false;
      outputTopology.error = e.message;
    }
    render();
  }
  function setOutputDraft(next) {
    outputTopology.draft = next;
    outputTopology.dirty = true;
    outputTopology.touched = true;
    outputTopology.error = '';
    outputTopology.readiness = null;
    outputTopology.readinessChecking = '';
    outputTopology.readinessError = '';
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
    driverResearch.dirty = true;
    crossoverPreview.payload = null;
    crossoverPreview.error = '';
    patchActiveSpeaker({stagedConfig: null});
    render();
  }
  async function setOutputTemplate(kind, options) {
    options = options || {};
    if (outputTopology.dirty && !options.skipDirtyConfirm &&
        !await jtsConfirm('Replace the unsaved speaker layout draft?')) {
      return;
    }
    var next = baseOutputDraft();
    if (!next || !next.hardware) {
      status('Load output hardware before creating a speaker layout.', true);
      return;
    }
    var keepSubwoofer = outputHasSubwoofer(next);
    var count = Number(next.hardware.physical_output_count) || 0;
    var template = outputTemplateDefinition(kind);
    if (!template) {
      status('Choose a supported speaker layout template.', true);
      return;
    }
    if (count < template.minOutputs) {
      status(template.name + ' needs at least ' + template.minOutputs +
        ' physical output' + (template.minOutputs === 1 ? '.' : 's.'), true);
      return;
    }
    next.name = template.name;
    next.speaker_groups = template.groups;
    next.routing = {
      main_left_group_id: template.routing.main_left_group_id || null,
      main_right_group_id: template.routing.main_right_group_id || null,
      mono_group_id: template.routing.mono_group_id || null,
      subwoofer_group_ids: template.routing.subwoofer_group_ids || []
    };
    if (keepSubwoofer) {
      next = addSubwooferToTopology(next) || next;
    }
    setOutputDraft(next);
    status(
      keepSubwoofer && !outputHasSubwoofer(next)
        ? 'Speaker layout draft updated. Subwoofer was removed because no spare output remains.'
        : 'Speaker layout is a draft. Save to validate; no sound will play.'
    );
  }
  async function setOutputTemplateAxis(axis, value) {
    var topology = currentOutputTopology();
    if (!topology) {
      status('Load output hardware before creating a speaker layout.', true);
      return;
    }
    var axes = outputTemplateAxesForTopology(topology);
    var layout = axis === 'layout' ? value : axes.layout;
    var speakerMode = axis === 'speaker-mode' ? value : axes.speakerMode;
    var kind = outputTemplateKindFromAxes(layout, speakerMode);
    if (!kind) {
      status('Choose passive, active 2-way, or active 3-way to continue.', true);
      return;
    }
    await setOutputTemplate(kind, {skipDirtyConfirm: true});
  }
  function toggleOutputSubwoofer(modeValue) {
    var topology = currentOutputTopology();
    if (!topology) {
      status('Load output hardware before editing the speaker layout.', true);
      return;
    }
    var next = modeValue === 'remove'
      ? removeSubwooferFromTopology(topology)
      : addSubwooferToTopology(topology);
    if (!next) {
      status('Could not update subwoofer draft.', true);
      return;
    }
    if (modeValue !== 'remove' && !outputHasSubwoofer(next)) {
      status('No unused physical output is available for a subwoofer.', true);
      return;
    }
    setOutputDraft(next);
    status(modeValue === 'remove' ?
      'Removed subwoofer from the speaker layout draft.' :
      'Added subwoofer to the speaker layout draft. Save before verification.');
  }
  function updateDriverResearchPromptPreview() {
    var prompt = el('driver-research-prompt');
    if (prompt) prompt.value = driverResearchPrompt(currentOutputTopology());
  }
  function updateDriverResearchImportSummary() {
    var summary = el('driver-research-summary');
    if (summary) summary.innerHTML = renderDriverResearchSummary();
  }
  async function copyDriverResearchPrompt() {
    var prompt = el('driver-research-prompt');
    if (!prompt) return;
    var copied = false;
    try {
      await navigator.clipboard.writeText(prompt.value);
      copied = true;
    } catch (e) {
      try {
        prompt.select();
        copied = document.execCommand('copy');
      } catch (fallbackError) {
        copied = false;
      }
    }
    status(copied ? 'Copied driver research prompt.' :
      'Could not copy automatically. Select the prompt text and copy it manually.', !copied);
  }
  function parseDriverResearchImport() {
    try {
      var payload = JSON.parse(driverResearch.importText || '');
      driverResearch.parsed = summarizeDriverResearchPayload(payload);
      driverResearch.error = '';
      driverResearch.dirty = true;
      status('Driver research JSON parsed. Review it before using any values.');
    } catch (e) {
      driverResearch.parsed = null;
      driverResearch.error = e.message;
      status('Driver research JSON needs review: ' + e.message, true);
    }
    render();
  }
  async function saveDriverResearchDraft(options) {
    options = options || {};
    if (!driverResearch.dirty && driverResearchStepSatisfied() && options.nextStep) {
      store.outputStepOverride = options.nextStep;
      status(driverResearchCanPreparePreview() ?
        'Driver research is already saved. Continue with output mapping.' :
        'Driver research is skipped for now. Continue with output mapping.');
      render();
      return true;
    }
    if (outputTopology.dirty) {
      status('Save the speaker layout before saving driver research.', true);
      return false;
    }
    if (!currentOutputTopology()) {
      status('Load output hardware before saving a speaker design draft.', true);
      return false;
    }
    var researchPayload = null;
    if ((driverResearch.importText || '').trim()) {
      try {
        researchPayload = JSON.parse(driverResearch.importText);
        driverResearch.parsed = summarizeDriverResearchPayload(researchPayload);
        driverResearch.error = '';
      } catch (e) {
        driverResearch.parsed = null;
        driverResearch.error = e.message;
        status('Driver research JSON needs review: ' + e.message, true);
        render();
        return false;
      }
    }
    driverResearch.saving = true;
    driverResearch.error = '';
    render();
    try {
      var resp = await fetch('./active-speaker/design-draft', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          operator_inputs: driverResearch.inputs,
          driver_research: researchPayload
        })
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'speaker design draft save failed');
      ingestDesignDraft(payload, {force: true});
      crossoverPreview.payload = null;
      crossoverPreview.error = '';
      if (options.nextStep) store.outputStepOverride = options.nextStep;
      status('Saved speaker design draft. No filters were applied and no sound was played.');
      render();
      return true;
    } catch (e) {
      driverResearch.saving = false;
      driverResearch.error = e.message;
      status('Could not save speaker design draft: ' + e.message, true);
      render();
      return false;
    }
  }
  async function prepareCrossoverPreview() {
    if (!driverResearchDraftSaved()) {
      status('Save a ready speaker design draft before preparing the crossover preview.', true);
      return false;
    }
    crossoverPreview.preparing = true;
    crossoverPreview.error = '';
    render();
    try {
      var resp = await fetch('./active-speaker/crossover-preview', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({})
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'crossover preview failed');
      ingestCrossoverPreview(payload);
      status('Prepared crossover preview. No YAML was emitted, no filters were applied, and no sound was played.');
      render();
      return true;
    } catch (e) {
      crossoverPreview.preparing = false;
      crossoverPreview.error = e.message;
      status('Could not prepare crossover preview: ' + e.message, true);
      render();
      return false;
    }
  }
  async function advanceOutputStep(step) {
    var topology = currentOutputTopology();
    if (step === 'layout') {
      if (!topology || !outputGroups(topology).length) {
        store.outputStepOverride = 'layout';
        status('Choose a speaker layout before continuing.', true);
        render();
        return;
      }
      if (outputTopology.dirty) {
        await saveOutputTopology({nextStep: 'research'});
        return;
      }
      openOutputStep('research');
      status('Speaker layout is already saved. Continue with driver research or skip ahead.');
      return;
    }
    if (step === 'research') {
      if (!await saveDriverResearchDraft({nextStep: 'map'})) return;
      return;
    }
    if (step === 'map') {
      if (outputTopology.dirty) {
        store.outputStepOverride = 'map';
        status('Save the speaker layout before confirming outputs.', true);
        render();
        return;
      }
      if (!outputIdentityComplete()) {
        var report = outputIdentityReport();
        var assigned = Number(report && report.assigned_channel_count || 0);
        store.outputStepOverride = 'map';
        status(assigned > 0 ?
          'Confirm every assigned output before continuing.' :
          'Save a speaker layout with assigned outputs before continuing.', true);
        render();
        return;
      }
      openOutputStep('safety');
      status('Outputs are confirmed. Continue with the first quiet test.');
      return;
    }
    if (step === 'safety') {
      store.outputStepOverride = 'safety';
      status('Use this card to run safety preflight, stage the protected config, load only when allowed, and start quiet.');
      render();
    }
  }
  async function saveOutputTopology(options) {
    options = options || {};
    if (!outputTopology.draft) return;
    outputTopology.saving = true;
    outputTopology.touched = true;
    outputTopology.error = '';
    patchActiveSpeaker({stagedConfig: null});
    render();
    try {
      var resp = await fetch('./output-topology', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({output_topology: outputTopology.draft})
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'speaker layout save failed');
      ingestOutputTopology(payload);
      if (options.nextStep) store.outputStepOverride = options.nextStep;
      status('Saved speaker layout. No sound was played.');
    } catch (e) {
      outputTopology.saving = false;
      outputTopology.error = e.message;
      status('Could not save speaker layout: ' + e.message, true);
    }
    render();
  }
  async function updateOutputChannelIdentity(button) {
    if (outputTopology.dirty) {
      status('Save the speaker layout before changing output confirmation.', true);
      return;
    }
    var groupId = button.getAttribute('data-group-id') || '';
    var role = button.getAttribute('data-role') || '';
    var verified = button.getAttribute('data-verified') !== 'false';
    var label = button.getAttribute('data-label') || (groupId + ' ' + role);
    var message = verified
      ? 'Confirm that "' + label + '" is wired to the driver shown here? No sound will play.'
      : 'Mark "' + label + '" as not confirmed?';
    if (!await jtsConfirm(message, {danger: false})) return;

    outputTopology.identitySaving = groupId + ':' + role;
    outputTopology.error = '';
    outputTopology.readinessError = '';
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
    outputTopology.touched = true;
    render();
    try {
      var resp = await fetch('./active-speaker/channel-identity', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          speaker_group_id: groupId,
          role: role,
          identity_verified: verified
        })
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'channel identity update failed');
      ingestOutputTopology(payload);
      status((verified ? 'Confirmed output: ' : 'Cleared output confirmation: ') + label + '.');
    } catch (e) {
      outputTopology.identitySaving = '';
      outputTopology.error = e.message;
      status('Could not update channel identity: ' + e.message, true);
    }
    render();
  }
  async function saveOutputChannelProtectionState(groupId, role, nextStatus) {
    var resp = await fetch('./active-speaker/channel-protection', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify({
        speaker_group_id: groupId,
        role: role,
        protection_status: nextStatus
      })
    });
    var payload = await resp.json();
    if (!resp.ok) throw new Error(payload.error || 'channel quiet test setup update failed');
    return payload;
  }
  async function fetchOutputPlaybackReadiness(groupId, role) {
    var resp = await fetch('./active-speaker/playback-readiness', {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify({
        speaker_group_id: groupId,
        role: role
      })
    });
    var payload = await resp.json();
    if (!resp.ok) throw new Error(payload.error || 'playback readiness failed');
    return payload;
  }
  async function checkOutputPlaybackReadiness(button) {
    if (outputTopology.dirty) {
      status('Save the speaker layout before choosing the first quiet test channel.', true);
      return;
    }
    var groupId = button.getAttribute('data-group-id') || '';
    var role = button.getAttribute('data-role') || '';
    var label = button.getAttribute('data-label') || (groupId + ' ' + role);
    var protectionRequired = button.getAttribute('data-protection-required') === 'true';
    var protectionStatus = button.getAttribute('data-protection-status') || 'unknown';
    var targetId = groupId + ':' + role;
    var needsSoftwareGuard = protectionRequired &&
      protectionStatus !== 'present' &&
      protectionStatus !== 'software_guard_requested';
    outputTopology.readinessChecking = targetId;
    outputTopology.protectionSaving = needsSoftwareGuard ? targetId : '';
    outputTopology.error = '';
    outputTopology.readinessError = '';
    outputTopology.readiness = null;
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
    outputTopology.touched = true;
    activeSpeaker.stagedConfig = null;
    if (needsSoftwareGuard) {
      status('Preparing ' + label + ' for a quiet first test. No sound will play.');
    }
    render();
    try {
      if (needsSoftwareGuard) {
        ingestOutputTopology(await saveOutputChannelProtectionState(
          groupId,
          role,
          'software_guard_requested'
        ));
        outputTopology.protectionSaving = '';
      }
      outputTopology.readiness = await fetchOutputPlaybackReadiness(groupId, role);
      outputTopology.readinessChecking = '';
      status('Selected ' + label + ' for the first quiet test. No sound was played.');
    } catch (e) {
      outputTopology.protectionSaving = '';
      outputTopology.readinessChecking = '';
      outputTopology.readinessError = e.message;
      status('Could not prepare that driver for a quiet test: ' + e.message, true);
    }
    render();
  }
  async function playOutputReadinessTone(button) {
    var groupId = button.getAttribute('data-group-id') || '';
    var role = button.getAttribute('data-role') || '';
    var label = button.getAttribute('data-label') || (groupId + ' ' + role);
    var audio = button.getAttribute('data-audio') === 'true';
    if (audio && !await jtsConfirm(
      'Play one short quiet ' + humanRole(role).toLowerCase() + ' test on "' +
        label + '"? JTS will use the quiet test setup, bounded test level, and Stop gate for this target.',
      {danger: true}
    )) {
      return;
    }
    outputTopology.readinessPlaybackChecking = audio ? 'audio' : 'artifact';
    outputTopology.readinessError = '';
    outputTopology.readinessPlayback = null;
    outputTopology.touched = true;
    render();
    try {
      var resp = await fetch('./active-speaker/play-tone', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          speaker_group_id: groupId,
          role: role,
          audio: audio
        })
      });
      var result = await resp.json();
      if (!resp.ok) throw new Error(result.error || 'channel test failed');
      outputTopology.readinessPlayback = result.playback || null;
      outputTopology.readinessPlaybackChecking = '';
      patchActiveSpeaker({
        loading: false, action: '',
        session: result.session || activeSpeaker.session,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      status(audio ? 'Played quiet channel test.' : 'Verified channel test artifact. No sound was played.');
    } catch (e) {
      outputTopology.readinessPlaybackChecking = '';
      outputTopology.readinessError = e.message;
      status('Could not run channel test: ' + e.message, true);
    }
    render();
  }
  async function recordFloorAudioResult(button) {
    var outcome = button.getAttribute('data-outcome') || '';
    var playbackId = button.getAttribute('data-playback-id') || '';
    if (!outcome || !playbackId) {
      status('Floor-test result is missing playback evidence.', true);
      return;
    }
    var target = outputTopology.readiness && outputTopology.readiness.target || null;
    patchActiveSpeaker({
      loading: false, action: 'Recording floor result',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/floor-audio-result', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({
          outcome: outcome,
          playback_id: playbackId
        })
      });
      var result = await resp.json();
      if (!resp.ok) throw new Error(result.error || 'quiet-test result failed');
      patchActiveSpeaker({
        loading: false, action: '',
        session: result,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      if (target && target.speaker_group_id && target.role) {
        try {
          outputTopology.readiness = await fetchOutputPlaybackReadiness(
            target.speaker_group_id,
            target.role
          );
          outputTopology.readinessError = '';
        } catch (refreshErr) {
          outputTopology.readinessError = refreshErr.message;
        }
      }
      await refreshActiveSpeakerRehearsal();
      var quiet = result.quiet_start || {};
      status(quiet.floor_audio_confirmed ?
        'Floor audio confirmed for this target.' :
        'Floor audio was not confirmed; stay at the floor before continuing.');
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not record quiet-test result: ' + e.message, true);
    }
    render();
  }
  async function stageActiveSpeakerConfig() {
    if (outputTopology.dirty) {
      status('Save the speaker layout before preparing quiet test mode.', true);
      return;
    }
    if (!await jtsConfirm(
      'Prepare the first quiet test from the saved speaker layout? JTS will build the quiet test setup, but it will not load CamillaDSP or play sound yet.',
      {danger: false}
    )) {
      return;
    }
    patchActiveSpeaker({
      loading: false, action: 'Preparing quiet test mode',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/stage-config', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'protected config staging failed');
      var startupLoad = await fetchActiveSpeakerStartupLoad();
      patchActiveSpeaker({
        loading: false, action: '',
        stagedConfig: payload,
        startupLoad: startupLoad,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      status(payload.status === 'staged' ?
        'Quiet test mode is prepared. Continue when you are ready; no sound was played.' :
        'Quiet test mode needs one more setup step before it can continue.',
        payload.status !== 'staged');
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not prepare quiet test mode: ' + e.message, true);
    }
    render();
  }
  async function updateActiveSpeakerLevel(action, requestedLevel) {
    var seq = store.nextActiveSpeakerLevelSeq();
    var cfg = activeSpeakerLevelConfig();
    var body = {
      action: action || 'set'
    };
    if (requestedLevel != null) body.level_dbfs = requestedLevel;
    patchActiveSpeaker({
      loading: false, action: 'Updating level',
      error: '',
      levelDbfs: cfg.value
    });
    render();
    try {
      var resp = await fetch('./active-speaker/calibration-level', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(body)
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'calibration level update failed');
      if (seq !== store.activeSpeakerLevelSeq) return;
      var accepted = payload && payload.test_signal ?
        Number(payload.test_signal.requested_level_dbfs) : cfg.value;
      var startupLoad = await fetchActiveSpeakerStartupLoad();
      if (seq !== store.activeSpeakerLevelSeq) return;
      patchActiveSpeaker({
        loading: false, action: '',
        calibrationLevel: payload,
        startupLoad: startupLoad,
        error: '',
        levelDbfs: isFinite(accepted) ? accepted : cfg.value
      });
      status(payload.issues && payload.issues.length ?
        'Level raised one guarded step; larger upward move was limited.' :
        'Calibration level updated.');
    } catch (e) {
      if (seq !== store.activeSpeakerLevelSeq) return;
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not update calibration level: ' + e.message, true);
    }
    render();
  }
  async function recordActiveSpeakerMicObservation() {
    var input = el('active-speaker-mic-dbfs');
    var clippingInput = el('active-speaker-mic-clipping');
    var raw = input ? String(input.value || '').trim() : '';
    var clipping = !!(clippingInput && clippingInput.checked);
    var observed = raw === '' ? null : Number(raw);
    if (raw !== '' && !isFinite(observed)) {
      status('Enter the observed mic level as dBFS, for example -35.0.', true);
      return;
    }
    if (raw === '' && !clipping) {
      status('Enter a mic reading or mark clipping before recording an observation.', true);
      return;
    }
    store.nextActiveSpeakerLevelSeq();
    patchActiveSpeakerMicObservation({observedDbfs: raw, clipping: clipping});
    patchActiveSpeaker({
      loading: false, action: 'Recording mic reading',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var body = {action: 'observe', mic_clipping: clipping};
      if (observed != null) body.observed_mic_dbfs = observed;
      var resp = await fetch('./active-speaker/calibration-level', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(body)
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'mic observation update failed');
      var meter = payload.mic_meter || {};
      var accepted = payload && payload.test_signal ?
        Number(payload.test_signal.requested_level_dbfs) : activeSpeaker.levelDbfs;
      var bringupResp = await fetch('./active-speaker/bringup-preflight', {cache: 'no-store'});
      if (!bringupResp.ok) throw new Error('bring-up preflight failed');
      var startupLoad = await fetchActiveSpeakerStartupLoad();
      patchActiveSpeakerMicObservation({
        observedDbfs: meter.observed_dbfs != null ? String(meter.observed_dbfs) : raw,
        clipping: meter.status === 'clipping'
      });
      patchActiveSpeaker({
        loading: false, action: '',
        calibrationLevel: payload,
        bringup: await bringupResp.json(),
        startupLoad: startupLoad,
        error: '',
        levelDbfs: isFinite(accepted) ? accepted : activeSpeaker.levelDbfs
      });
      await refreshActiveSpeakerRehearsal();
      status(meter.status === 'clipping' ?
        'Mic clipping recorded; calibration level reset to the floor.' :
        'Mic observation recorded.');
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not record mic observation: ' + e.message, true);
    }
    render();
  }
  async function applyActiveSpeakerAutoLevel() {
    var target = activeSpeakerSelectedReadinessTarget();
    if (!target) {
      status('Choose a saved driver before applying a guided level step.', true);
      return;
    }
    var input = el('active-speaker-mic-dbfs');
    var clippingInput = el('active-speaker-mic-clipping');
    var raw = input ? String(input.value || '').trim() : '';
    var observed = raw === '' ? null : Number(raw);
    var clipping = !!(clippingInput && clippingInput.checked);
    if (raw !== '' && !isFinite(observed)) {
      status('Enter the observed mic level as dBFS, for example -35.0.', true);
      return;
    }
    store.nextActiveSpeakerLevelSeq();
    patchActiveSpeakerMicObservation({observedDbfs: raw, clipping: clipping});
    patchActiveSpeaker({
      loading: false, action: 'Applying guided level',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    outputTopology.readinessPlayback = null;
    outputTopology.readinessPlaybackChecking = '';
    render();
    try {
      var body = {
        action: 'auto_step',
        speaker_group_id: target.speaker_group_id,
        role: target.role,
        mic_clipping: clipping
      };
      if (observed != null) body.observed_mic_dbfs = observed;
      var resp = await fetch('./active-speaker/calibration-level', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify(body)
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'guided level step failed');
      var accepted = payload && payload.test_signal ?
        Number(payload.test_signal.requested_level_dbfs) : activeSpeaker.levelDbfs;
      var meter = payload.mic_meter || {};
      var decision = payload.auto_level || {};
      patchActiveSpeakerMicObservation({
        observedDbfs: meter.observed_dbfs != null ? String(meter.observed_dbfs) : raw,
        clipping: meter.status === 'clipping'
      });
      var startupLoad = activeSpeaker.startupLoad;
      var refreshWarning = '';
      try {
        startupLoad = await fetchActiveSpeakerStartupLoad();
      } catch (refreshErr) {
        refreshWarning = refreshErr.message || 'startup status refresh failed';
      }
      try {
        outputTopology.readiness = await fetchOutputPlaybackReadiness(
          target.speaker_group_id,
          target.role
        );
        outputTopology.readinessError = '';
      } catch (refreshErr2) {
        outputTopology.readinessError = refreshErr2.message;
        refreshWarning = refreshWarning || refreshErr2.message;
      }
      patchActiveSpeaker({
        loading: false, action: '',
        calibrationLevel: payload,
        startupLoad: startupLoad,
        error: '',
        levelDbfs: isFinite(accepted) ? accepted : activeSpeaker.levelDbfs
      });
      await refreshActiveSpeakerRehearsal();
      status('Guided level step: ' + (decision.reason || decision.status || 'level held') +
        (refreshWarning ? ' Refresh warning: ' + refreshWarning + '.' : '.'));
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not apply guided level step: ' + e.message, true);
    }
    render();
  }
  async function fetchActiveSpeakerStartupLoad() {
    return await api.get('./active-speaker/startup-load', 'startup load status failed');
  }
  async function fetchActiveSpeakerCommissioningRehearsal() {
    return await api.get(
      './active-speaker/commissioning-rehearsal',
      'commissioning rehearsal failed'
    );
  }
  async function refreshActiveSpeakerRehearsal() {
    try {
      patchActiveSpeaker({rehearsal: await fetchActiveSpeakerCommissioningRehearsal()});
    } catch (e) {
      patchActiveSpeaker({rehearsal: activeSpeaker.rehearsal || null});
    }
  }
  async function fetchActiveSpeakerEnvironment() {
    return await api.get('./active-speaker/environment', 'environment probe failed');
  }
  async function checkActivePathSafety() {
    patchActiveSpeaker({
      loading: false, action: 'Checking protected path',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/check-path-safety', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'quiet-test path check failed');
      var ready = payload.report && payload.report.ok_to_load_active_config;
      var environment = await fetchActiveSpeakerEnvironment();
      patchActiveSpeaker({
        loading: false, action: '',
        payload: environment,
        startupLoad: payload.startup_load || activeSpeaker.startupLoad,
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      status(ready ?
        'Protected path check passed. No sound was played.' :
        'Protected path check found blockers. No sound was played.',
        !ready);
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not check protected path: ' + e.message, true);
    }
    render();
  }
  async function loadActiveStartupConfig() {
    if (!await jtsConfirm(
      'Continue preparing the first quiet test? This reloads the quiet test setup but does not play sound or change the test level.',
      {danger: false}
    )) {
      return;
    }
    patchActiveSpeaker({
      loading: false, action: 'Loading quiet test mode',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/load-startup-config', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'startup load failed');
      patchActiveSpeaker({
        loading: false, action: '',
        startupLoad: {
          state: payload.load || {},
          preflight: payload.preflight || {}
        },
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      var loaded = payload.load && payload.load.status === 'loaded';
      status(loaded ?
        'Quiet test mode is loaded. No sound was played.' :
        'Quiet test mode needs one more setup step before it can continue.',
        !loaded);
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not load quiet test mode: ' + e.message, true);
    }
    render();
  }
  async function rollbackActiveStartupConfig() {
    if (!await jtsConfirm(
      'Exit quiet test mode and restore the previous DSP setup? This reloads CamillaDSP but does not play sound.',
      {danger: false}
    )) {
      return;
    }
    patchActiveSpeaker({
      loading: false, action: 'Exiting quiet test mode',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch('./active-speaker/rollback-startup-config', {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      var payload = await resp.json();
      if (!resp.ok) throw new Error(payload.error || 'startup rollback failed');
      var startupLoad = await fetchActiveSpeakerStartupLoad();
      patchActiveSpeaker({
        loading: false, action: '',
        startupLoad: startupLoad.state ? startupLoad : {
          state: payload.rollback || {},
          preflight: activeSpeaker.startupLoad && activeSpeaker.startupLoad.preflight || {}
        },
        error: '',
        levelDbfs: activeSpeaker.levelDbfs
      });
      var rolledBack = payload.rollback && payload.rollback.status === 'rolled_back';
      status(rolledBack ?
        'Exited quiet test mode. No sound was played.' :
        'Could not exit quiet test mode yet; review the setup state.',
        !rolledBack);
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
      status('Could not exit quiet test mode: ' + e.message, true);
    }
    render();
  }
  async function refreshActiveSpeakerStatus() {
    patchActiveSpeaker({
      loading: true, action: '',
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    var probes = [
      ['payload', function() { return fetch('./active-speaker/environment', {cache: 'no-store'}); },
        'environment probe failed'],
      ['session', function() { return fetch('./active-speaker/safe-playback', {cache: 'no-store'}); },
        'safe playback status failed'],
      ['stagedConfig', function() { return fetch('./active-speaker/staged-config', {cache: 'no-store'}); },
        'staged config status failed'],
      ['calibrationLevel', function() { return fetch('./active-speaker/calibration-level', {cache: 'no-store'}); },
        'calibration level status failed'],
      ['bringup', function() { return fetch('./active-speaker/bringup-preflight', {cache: 'no-store'}); },
        'bring-up preflight failed'],
      ['startupLoad', function() { return fetch('./active-speaker/startup-load', {cache: 'no-store'}); },
        'startup load status failed'],
      ['rehearsal', function() { return fetch('./active-speaker/commissioning-rehearsal', {cache: 'no-store'}); },
        'commissioning rehearsal failed'],
      ['targets', function() { return fetch('./active-speaker/tone-targets', {cache: 'no-store'}); },
        'tone targets failed']
    ];
    var results = await Promise.allSettled(probes.map(function(probe) {
      return probe[1]().then(function(resp) {
        return api.jsonFromResponse(resp, probe[2]);
      });
    }));
    var patch = {loading: false, action: '', error: ''};
    var errors = [];
    results.forEach(function(result, index) {
      var key = probes[index][0];
      if (result.status === 'fulfilled') {
        patch[key] = result.value;
        return;
      }
      errors.push(result.reason && result.reason.message || probes[index][2]);
    });
    var nextLevel = patch.calibrationLevel || activeSpeaker.calibrationLevel;
    if (nextLevel && nextLevel.test_signal) {
      patch.levelDbfs = Number(nextLevel.test_signal.requested_level_dbfs);
    }
    if (errors.length) patch.error = 'Partial refresh: ' + errors.join('; ');
    patchActiveSpeaker(patch);
    render();
  }
  async function activeSpeakerPost(path, actionLabel) {
    patchActiveSpeaker({
      loading: false, action: actionLabel,
      error: '',
      levelDbfs: activeSpeaker.levelDbfs
    });
    render();
    try {
      var resp = await fetch(path, {
        method: 'POST',
        headers: jsonHeaders(),
        body: '{}'
      });
      if (!resp.ok) throw new Error(actionLabel + ' failed');
      var nextSession = await resp.json();
      var nextLevel = nextSession.calibration_level || activeSpeaker.calibrationLevel;
      if (path.indexOf('/stop') >= 0 &&
          !(nextLevel && nextLevel.status === 'reset_failed')) {
        var levelResp = await fetch('./active-speaker/calibration-level', {cache: 'no-store'});
        if (levelResp.ok) nextLevel = await levelResp.json();
      }
      var startupLoad = await fetchActiveSpeakerStartupLoad();
      patchActiveSpeaker({
        loading: false, action: '',
        session: nextSession,
        calibrationLevel: nextLevel,
        startupLoad: startupLoad,
        error: '',
        levelDbfs: nextLevel && nextLevel.test_signal ?
          Number(nextLevel.test_signal.requested_level_dbfs) : activeSpeaker.levelDbfs
      });
      await refreshActiveSpeakerRehearsal();
      outputTopology.readiness = null;
      outputTopology.readinessChecking = '';
      outputTopology.readinessError = '';
      outputTopology.readinessPlayback = null;
      outputTopology.readinessPlaybackChecking = '';
    } catch (e) {
      patchActiveSpeaker({
        loading: false, action: '',
        error: e.message,
        levelDbfs: activeSpeaker.levelDbfs
      });
    }
    render();
  }
  function handleActiveSpeakerLevelInput(value) {
    var cfg = activeSpeakerLevelConfig();
    patchActiveSpeaker({levelDbfs: clamp(value, cfg.min, cfg.max)});
    var levelReadout = el('active-speaker-level-readout');
    if (levelReadout) levelReadout.textContent = fmtDbfs(activeSpeaker.levelDbfs);
  }

  function setActiveSpeakerMicClipping(checked) {
    patchActiveSpeakerMicObservation({clipping: checked});
  }

  function handleDriverResearchFieldInput(driverField, value) {
    driverResearch.inputs[driverField] = value;
    driverResearch.error = '';
    driverResearch.dirty = true;
    updateDriverResearchPromptPreview();
  }

  function handleDriverResearchImportInput(value) {
    driverResearch.importText = value;
    driverResearch.error = '';
    driverResearch.parsed = null;
    driverResearch.dirty = true;
    updateDriverResearchImportSummary();
  }

  function handleOutputStepToggle(target) {
    if (target && target.matches && target.matches('[data-active-speaker-setup]')) {
      store.activeSpeakerSetupOpen = !!target.open;
      return;
    }
    if (target && target.classList && target.classList.contains('output-step') &&
        target.open) {
      var step = target.getAttribute('data-output-step') || store.outputStepOverride;
      var topology = currentOutputTopology();
      if (!outputStepCanOpen(step, topology)) {
        target.open = false;
        store.outputStepOverride = defaultOutputStep();
        status('Finish the current card before opening ' + outputStepTitle(step) + '.', true);
        render();
        return;
      }
      store.outputStepOverride = step;
      el('view-body').querySelectorAll('.output-step[open]').forEach(function(stepEl) {
        if (stepEl !== target) stepEl.open = false;
      });
    }
  }

  return {
    ingestOutputTopology: ingestOutputTopology,
    refreshOutputTopology: refreshOutputTopology,
    setOutputDraft: setOutputDraft,
    setOutputTemplate: setOutputTemplate,
    setOutputTemplateAxis: setOutputTemplateAxis,
    toggleOutputSubwoofer: toggleOutputSubwoofer,
    updateDriverResearchPromptPreview: updateDriverResearchPromptPreview,
    updateDriverResearchImportSummary: updateDriverResearchImportSummary,
    copyDriverResearchPrompt: copyDriverResearchPrompt,
    parseDriverResearchImport: parseDriverResearchImport,
    saveDriverResearchDraft: saveDriverResearchDraft,
    prepareCrossoverPreview: prepareCrossoverPreview,
    advanceOutputStep: advanceOutputStep,
    saveOutputTopology: saveOutputTopology,
    updateOutputChannelIdentity: updateOutputChannelIdentity,
    fetchOutputPlaybackReadiness: fetchOutputPlaybackReadiness,
    checkOutputPlaybackReadiness: checkOutputPlaybackReadiness,
    playOutputReadinessTone: playOutputReadinessTone,
    recordFloorAudioResult: recordFloorAudioResult,
    stageActiveSpeakerConfig: stageActiveSpeakerConfig,
    updateActiveSpeakerLevel: updateActiveSpeakerLevel,
    recordActiveSpeakerMicObservation: recordActiveSpeakerMicObservation,
    applyActiveSpeakerAutoLevel: applyActiveSpeakerAutoLevel,
    fetchActiveSpeakerStartupLoad: fetchActiveSpeakerStartupLoad,
    fetchActiveSpeakerCommissioningRehearsal: fetchActiveSpeakerCommissioningRehearsal,
    refreshActiveSpeakerRehearsal: refreshActiveSpeakerRehearsal,
    fetchActiveSpeakerEnvironment: fetchActiveSpeakerEnvironment,
    checkActivePathSafety: checkActivePathSafety,
    loadActiveStartupConfig: loadActiveStartupConfig,
    rollbackActiveStartupConfig: rollbackActiveStartupConfig,
    refreshActiveSpeakerStatus: refreshActiveSpeakerStatus,
    activeSpeakerPost: activeSpeakerPost,
    handleActiveSpeakerLevelInput: handleActiveSpeakerLevelInput,
    setActiveSpeakerMicClipping: setActiveSpeakerMicClipping,
    handleDriverResearchFieldInput: handleDriverResearchFieldInput,
    handleDriverResearchImportInput: handleDriverResearchImportInput,
    handleOutputStepToggle: handleOutputStepToggle
  };
}
