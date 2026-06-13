export function createActiveSpeakerStore() {
  var store = {
    activeSpeaker: {
      loading: false, action: '', payload: null, session: null, targets: null,
      stagedConfig: null, calibrationLevel: null,
      bringup: null, startupLoad: null, rehearsal: null, error: '', levelDbfs: null
    },
    activeSpeakerLevelSeq: 0,
    activeSpeakerMicObservation: {observedDbfs: '', clipping: false},
    activeSpeakerSetupOpen: false,
    outputTopology: {
      loading: false, saving: false, payload: null, draft: null,
      identity: null, clockDomain: null, identitySaving: '', protectionSaving: '',
      readiness: null, readinessChecking: '', readinessError: '',
      readinessPlayback: null, readinessPlaybackChecking: '',
      error: '', dirty: false, touched: false
    },
    outputStepOverride: '',
    driverResearch: {
      inputs: {full_range: '', woofer: '', mid: '', tweeter: '', subwoofer: '', notes: ''},
      importText: '',
      parsed: null,
      designDraft: null,
      error: '',
      dirty: false,
      saving: false
    },
    crossoverPreview: {payload: null, preparing: false, error: ''}
  };
  store.patchActiveSpeaker = function(patch) {
    Object.assign(store.activeSpeaker, patch || {});
    return store.activeSpeaker;
  };
  store.patchActiveSpeakerMicObservation = function(patch) {
    Object.assign(store.activeSpeakerMicObservation, patch || {});
    return store.activeSpeakerMicObservation;
  };
  store.getActiveSpeakerMicObservation = function() {
    return store.activeSpeakerMicObservation;
  };
  store.nextActiveSpeakerLevelSeq = function() {
    store.activeSpeakerLevelSeq += 1;
    return store.activeSpeakerLevelSeq;
  };
  return store;
}
