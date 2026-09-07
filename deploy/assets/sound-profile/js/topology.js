// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Sound profile — output-topology model.
//
// Answers what the setup flow asks of the topology record (groups, channels,
// observed hardware, subwoofer and crossover pairing). No rendering, no IO.

import { humanRole } from "/assets/sound-profile/js/active-speaker-ui.js";
import { clone } from "/assets/sound-profile/js/format.js";
import {
  driverResearch,
  outputTopology
} from "/assets/sound-profile/js/state.js";

function activeCommissionRoles(group) {
  var seen = {};
  var order = ['woofer', 'mid', 'tweeter'];
  (group && Array.isArray(group.channels) ? group.channels : []).forEach(function(ch) {
    if (ch && ch.role) seen[ch.role] = true;
  });
  return order.filter(function(r) { return seen[r]; });
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
function physicalOutputOptions(topology) {
  var hardware = outputHardware(topology) || {};
  var outputs = Array.isArray(hardware.outputs) ? hardware.outputs : [];
  if (outputs.length) {
    return outputs.map(function(output) {
      var index = Number(output.index);
      return {
        index: index,
        label: output.human_label || ('Output ' + (index + 1))
      };
    }).filter(function(output) {
      return isFinite(output.index);
    });
  }
  var count = Number(hardware.physical_output_count) || 0;
  var fallback = [];
  for (var i = 0; i < count; i += 1) {
    fallback.push({index: i, label: 'Output ' + (i + 1)});
  }
  return fallback;
}
function physicalOutputLabel(topology, index) {
  var wanted = Number(index);
  var options = physicalOutputOptions(topology);
  for (var i = 0; i < options.length; i += 1) {
    if (Number(options[i].index) === wanted) return options[i].label;
  }
  return isFinite(wanted) ? 'Output ' + (wanted + 1) : 'No output assigned';
}
function observedOutputHardware() {
  return outputTopology.observedHardware || null;
}
function hardwareOutputCount(hardware) {
  return Number(hardware && hardware.physical_output_count) || 0;
}
function outputHardwareMismatch(topology) {
  // The declared-vs-detected comparison is computed once, server-side, in
  // jasper.output_topology.declared_hardware_mismatch and published as
  // payload.hardware_mismatch (jasper/web/sound_active_speaker.py's
  // _output_topology_payload) -- the same rule jasper.control.audio_health's
  // #2812 setup hint reads, since that detector runs in a different daemon
  // and cannot see this page's HTTP response. `topology` is accepted but
  // unused so existing call sites are unchanged.
  return outputTopology.hardwareMismatch;
}
function outputEvaluation(topology) {
  return topology && topology.evaluation ? topology.evaluation : {};
}

function outputClockDomainReport() {
  return outputTopology.clockDomain || null;
}
function outputActiveRoute() {
  return outputTopology.activeRoute || null;
}

function outputAssignedToOtherMap(topology, groupId, role) {
  var out = {};
  outputGroups(topology).forEach(function(group) {
    (group.channels || []).forEach(function(channel) {
      if (channel.physical_output_index == null) return;
      if ((group.id || '') === groupId && (channel.role || '') === role) return;
      out[String(channel.physical_output_index)] =
        (group.label || group.id) + ' · ' + humanRole(channel.role);
    });
  });
  return out;
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

function activeCrossoverPairs(topology) {
  var pairs = [];
  var seen = {};
  outputGroups(topology).forEach(function(group) {
    var groupPairs = group.mode === 'active_3_way'
      ? [['woofer', 'mid'], ['mid', 'tweeter']]
      : (group.mode === 'active_2_way' ? [['woofer', 'tweeter']] : []);
    groupPairs.forEach(function(pair) {
      var key = pair.join(':');
      if (!seen[key]) {
        seen[key] = true;
        pairs.push(pair);
      }
    });
  });
  return pairs;
}
function crossoverSettingKey(pair) {
  return String(pair[0] || '') + ':' + String(pair[1] || '');
}
function driverSetting(targetId) {
  if (!driverResearch.settings.drivers) driverResearch.settings.drivers = {};
  var drivers = driverResearch.settings.drivers;
  if (!drivers[targetId]) drivers[targetId] = {};
  return drivers[targetId];
}
function crossoverSetting(pair) {
  if (!driverResearch.settings.crossovers) driverResearch.settings.crossovers = {};
  var crossovers = driverResearch.settings.crossovers;
  var key = crossoverSettingKey(pair);
  if (!crossovers[key]) crossovers[key] = {};
  return crossovers[key];
}

function pairRoleKey(pair) {
  return (pair || []).map(String).sort().join(':');
}

function outputChannelGuardReady(channel) {
  var statusValue = channel && channel.protection_status || 'unknown';
  return !channel || !channel.protection_required ||
    statusValue === 'present' ||
    statusValue === 'software_guard_requested';
}

function activeOutputGroups(topology) {
  return outputGroups(topology).filter(function(group) {
    return group && (group.mode === 'active_2_way' || group.mode === 'active_3_way');
  });
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
function outputTemplateIsActive(template) {
  return !!(template && template.id && template.id.indexOf('_active_') >= 0);
}
function outputTemplateActiveOutputNeed(template, hasSubwoofer) {
  return outputTemplateIsActive(template)
    ? Number(template.minOutputs || 0) + (hasSubwoofer ? 1 : 0)
    : 0;
}
function outputTemplateUnavailableReason(template, topology, hasSubwoofer) {
  if (!template) return 'Choose a supported speaker layout.';
  var mismatch = outputHardwareMismatch(topology);
  if (mismatch) {
    return mismatch.message + ' Reconnect the saved hardware or refresh after the attached hardware is stable.';
  }
  var hardware = outputHardware(topology);
  var physicalCount = Number(hardware && hardware.physical_output_count) || 0;
  if (physicalCount < template.minOutputs) {
    return template.label + ' needs at least ' + template.minOutputs +
      ' physical output' + (template.minOutputs === 1 ? '.' : 's.');
  }
  if (!outputTemplateIsActive(template)) return '';
  var route = outputActiveRoute() || {};
  var routeCount = Number(route.transport_channel_count) || 0;
  var needed = outputTemplateActiveOutputNeed(template, hasSubwoofer);
  if (routeCount > 0 && needed > routeCount) {
    return 'This install can test and apply up to ' + routeCount +
      ' active outputs right now; ' + template.label + ' needs ' + needed + '.';
  }
  if (hasSubwoofer && route.subwoofer_supported !== true) {
    return 'Subwoofer active profiles are not available on this install yet.';
  }
  return '';
}

// One speaker's drivers landing on two different child DACs of a composite
// output device. The backend names it — output_topology.CROSS_CHILD_GROUP_CODE
// / cross_child_group_verdicts — at WARNING severity, never as a blocker: the
// layout drives every lane, so the cost is fidelity (an uncorrected clock seam
// sitting inside a crossover), not damage. JTS therefore discloses it here and
// lets the household decide, rather than refusing the save.
var CROSS_CHILD_GROUP_CODE = 'speaker_group_spans_child_devices';
function crossChildGroupVerdicts(topology) {
  var warnings = outputEvaluation(topology).warnings;
  return (Array.isArray(warnings) ? warnings : []).filter(function(issue) {
    return issue && issue.code === CROSS_CHILD_GROUP_CODE;
  });
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

export {
  activeCommissionRoles,
  activeCrossoverPairs,
  activeOutputGroups,
  baseOutputDraft,
  crossChildGroupVerdicts,
  crossoverSetting,
  crossoverSettingKey,
  currentOutputTopology,
  driverSetting,
  firstUnusedOutputIndex,
  hardwareOutputCount,
  nextSubwooferGroupId,
  observedOutputHardware,
  outputAssignedToOtherMap,
  outputChannel,
  outputChannelGuardReady,
  outputClockDomainReport,
  outputGroups,
  outputHardware,
  outputHardwareMismatch,
  outputHasSubwoofer,
  outputRoleSummary,
  outputTemplateKindFromAxes,
  outputTemplateUnavailableReason,
  pairRoleKey,
  physicalOutputLabel,
  physicalOutputOptions,
  removeSubwooferFromTopology,
};
