// RBJ Audio EQ Cookbook biquad magnitude response.
//
// Pure, DOM-free module shared by the live /sound/ graph (main.js) and the
// node parity check (scripts/check-peq-parity.mjs). It MUST stay
// byte-for-byte equivalent to the Python reference in
// jasper/sound/profile.py (_biquad_coeffs / _filter_response_db). Both are
// checked against tests/fixtures/peq_response_fixture.json — drift is a test
// failure, not a field bug.
//
// https://www.w3.org/TR/audio-eq-cookbook/ — the same digital biquad family
// CamillaDSP realises, so the drawn magnitude matches the speaker's actual
// output for the Q-parameterised types (Peaking/Highpass/Lowpass/Notch).

// Must match CamillaDSP's runtime rate (DEFAULT_SAMPLE_RATE = 48000) so the
// preview curve matches the speaker's actual output.
export var RESPONSE_SAMPLE_RATE_HZ = 48000;

// Cut/notch types shape the response without a user gain term.
export var GAINLESS_TYPES = ['Highpass', 'Lowpass', 'Notch'];

// Advanced shelves are realised at a fixed 6 dB/oct slope; we draw a
// Butterworth (non-resonant) shelf, so Q is not a user control for shelves.
var SHELF_Q = 1.0 / Math.sqrt(2.0);

// RBJ biquad coefficients (un-normalised). Returns [b0,b1,b2,a0,a1,a2].
export function biquadCoeffs(type, freq, gainDb, q) {
  var w0 = 2.0 * Math.PI * Math.max(freq, 1e-6) / RESPONSE_SAMPLE_RATE_HZ;
  var cw = Math.cos(w0), sw = Math.sin(w0);
  var effQ = (type === 'Lowshelf' || type === 'Highshelf') ? SHELF_Q : Math.max(q, 1e-4);
  var alpha = sw / (2.0 * effQ);
  if (type === 'Lowpass') {
    return [(1 - cw) / 2, 1 - cw, (1 - cw) / 2, 1 + alpha, -2 * cw, 1 - alpha];
  }
  if (type === 'Highpass') {
    return [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2, 1 + alpha, -2 * cw, 1 - alpha];
  }
  if (type === 'Notch') {
    return [1.0, -2 * cw, 1.0, 1 + alpha, -2 * cw, 1 - alpha];
  }
  var amp = Math.pow(10.0, gainDb / 40.0);
  if (type === 'Lowshelf') {
    var bl = 2.0 * Math.sqrt(amp) * alpha;
    return [
      amp * ((amp + 1) - (amp - 1) * cw + bl),
      2 * amp * ((amp - 1) - (amp + 1) * cw),
      amp * ((amp + 1) - (amp - 1) * cw - bl),
      (amp + 1) + (amp - 1) * cw + bl,
      -2 * ((amp - 1) + (amp + 1) * cw),
      (amp + 1) + (amp - 1) * cw - bl
    ];
  }
  if (type === 'Highshelf') {
    var bh = 2.0 * Math.sqrt(amp) * alpha;
    return [
      amp * ((amp + 1) + (amp - 1) * cw + bh),
      -2 * amp * ((amp - 1) + (amp + 1) * cw),
      amp * ((amp + 1) + (amp - 1) * cw - bh),
      (amp + 1) - (amp - 1) * cw + bh,
      2 * ((amp - 1) - (amp + 1) * cw),
      (amp + 1) - (amp - 1) * cw - bh
    ];
  }
  // Peaking (default).
  return [1 + alpha * amp, -2 * cw, 1 - alpha * amp,
          1 + alpha / amp, -2 * cw, 1 - alpha / amp];
}

// Magnitude in dB of one biquad at one frequency. Cascading is exact in dB
// (|H1·H2| = |H1|·|H2|), so callers sum per-band results.
export function magnitudeDb(type, freq, gainDb, q, atFreq) {
  var c = biquadCoeffs(type, freq, gainDb, q);
  var w = 2.0 * Math.PI * Math.max(atFreq, 1e-6) / RESPONSE_SAMPLE_RATE_HZ;
  var c1 = Math.cos(w), s1 = Math.sin(w);
  var c2 = Math.cos(2.0 * w), s2 = Math.sin(2.0 * w);
  var nre = c[0] + c[1] * c1 + c[2] * c2;
  var nim = -(c[1] * s1 + c[2] * s2);
  var dre = c[3] + c[4] * c1 + c[5] * c2;
  var dim = -(c[4] * s1 + c[5] * s2);
  var num = nre * nre + nim * nim;
  var den = dre * dre + dim * dim;
  return den > 0.0 ? 10.0 * Math.log10(Math.max(num / den, 1e-12)) : 0.0;
}
