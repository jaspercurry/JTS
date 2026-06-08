// controls.js — state rules for /wake-corpus/ capture options.
//
// Kept separate from main.js so the chip-profile invariants can be tested
// directly without loading the full page module and its network/dialog imports.

function optionRow(input) {
  return input?.closest?.('.capture-option') || null;
}

function setOptionVisible(input, visible) {
  const row = optionRow(input);
  if (row) row.hidden = !visible;
}

function checked(input) {
  return Boolean(input?.checked);
}

export function syncCorpusProfileControls(elements, sessionLoaded = false) {
  const chipProfile = checked(elements.chipProfile);
  if (chipProfile) {
    if (elements.rawMic0) elements.rawMic0.checked = true;
    if (elements.dtln) elements.dtln.checked = false;
    if (elements.aec3Sweep) elements.aec3Sweep.checked = false;
  }

  setOptionVisible(elements.rawMic0, !chipProfile);
  setOptionVisible(elements.dtln, !chipProfile);
  setOptionVisible(elements.aec3Sweep, !chipProfile);

  if (elements.rawMic0) elements.rawMic0.disabled = sessionLoaded || chipProfile;
  if (elements.dtln) elements.dtln.disabled = sessionLoaded || chipProfile;
  if (elements.usbMic) elements.usbMic.disabled = sessionLoaded;
  if (elements.aec3Sweep) elements.aec3Sweep.disabled = sessionLoaded || chipProfile;
}

export function currentSessionPayload(elements) {
  const includeAec3Sweep = checked(elements.aec3Sweep);
  const chipProfile = checked(elements.chipProfile);
  return {
    member: (elements.member?.value || '').trim(),
    corpus_profile: chipProfile
      ? 'chip_aec_comparison_v1'
      : 'standard',
    include_raw_mic_0: checked(elements.rawMic0),
    include_dtln: chipProfile ? false : checked(elements.dtln),
    include_usb_mic: checked(elements.usbMic) || includeAec3Sweep,
    include_usb_dtln: checked(elements.usbDtln),
    include_xvf_raw0_dtln: checked(elements.xvfRaw0Dtln),
    include_aec3_sweep: includeAec3Sweep,
    aec3_sweep_source: includeAec3Sweep ? 'usb' : 'xvf',
  };
}
