// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// capture.js — the measurement capture stack's WAV encoder. The mic/worklet
// half of that stack still lives in main.js; converging either onto
// /assets/shared/js/measurement-audio.js is the measurement loop's call.

export function float32ToWav(samples, sampleRate) {
  var len = samples.length;
  var buf = new ArrayBuffer(44 + len * 2);
  var view = new DataView(buf);
  function w8s(off, str) {
    for (var i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i));
  }
  w8s(0, 'RIFF');
  view.setUint32(4, 36 + len * 2, true);
  w8s(8, 'WAVE');
  w8s(12, 'fmt ');
  view.setUint32(16, 16, true);          // fmt chunk size
  view.setUint16(20, 1, true);           // PCM
  view.setUint16(22, 1, true);           // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);  // byte rate (mono * 2 bytes)
  view.setUint16(32, 2, true);           // block align
  view.setUint16(34, 16, true);          // 16-bit
  w8s(36, 'data');
  view.setUint32(40, len * 2, true);
  var off = 44;
  for (var i = 0; i < len; i++) {
    var s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(off, s * 0x7FFF, true);
    off += 2;
  }
  return new Blob([buf], {type: 'audio/wav'});
}
