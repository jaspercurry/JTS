// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// Shared browser-side audio measurement primitives for phone-mic flows.
// Domain policy stays in each page; this module owns the repeated
// getUserMedia / AudioWorklet / WAV-encoding plumbing and the invariant
// that the microphone graph is never connected to speaker output.

export const DEFAULT_SAMPLE_RATE = 48000;

export class MicCaptureUnsupportedError extends Error {
  constructor(support) {
    super(support && support.message ? support.message : 'Microphone capture is unavailable.');
    this.name = 'MicCaptureUnsupportedError';
    this.reason = support && support.reason || 'unsupported';
    this.support = support || micCaptureSupport();
  }
}

export function micCaptureSupport(env = globalThis) {
  const win = env && env.window ? env.window : env;
  const nav = env && env.navigator ? env.navigator :
    (typeof navigator === 'undefined' ? null : navigator);
  if (win && win.isSecureContext === false) {
    return {
      ok: false,
      reason: 'non_secure_context',
      message: 'Microphone capture needs HTTPS. Open this measurement page through the secure correction link, then try again.',
    };
  }
  if (!nav || !nav.mediaDevices || typeof nav.mediaDevices.getUserMedia !== 'function') {
    return {
      ok: false,
      reason: 'media_devices_unavailable',
      message: 'This browser does not expose microphone capture here. Use Safari or Chrome on the secure correction page.',
    };
  }
  const AudioContextCtor = win && (win.AudioContext || win.webkitAudioContext);
  if (typeof AudioContextCtor !== 'function') {
    return {
      ok: false,
      reason: 'audio_context_unavailable',
      message: 'This browser cannot open the audio engine needed for measurement.',
    };
  }
  if (win && typeof win.AudioWorkletNode !== 'function') {
    return {
      ok: false,
      reason: 'audio_worklet_unavailable',
      message: 'This browser does not support the low-latency audio capture path JTS uses for measurements.',
    };
  }
  return {ok: true, reason: 'supported', message: 'Microphone capture is available.'};
}

export function assertMicCaptureSupported(env = globalThis) {
  const support = micCaptureSupport(env);
  if (!support.ok) throw new MicCaptureUnsupportedError(support);
  return support;
}

export function monoMicConstraints(options = {}) {
  const sampleRate = options.sampleRate || DEFAULT_SAMPLE_RATE;
  const deviceId = options.deviceId || '';
  const audio = {
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: false,
    channelCount: 1,
    sampleRate: sampleRate,
  };
  if (deviceId) audio.deviceId = {exact: deviceId};
  return {audio: audio, video: false};
}

export async function openMonoMic(options = {}) {
  assertMicCaptureSupported();
  const sampleRate = options.sampleRate || DEFAULT_SAMPLE_RATE;
  const stream = await navigator.mediaDevices.getUserMedia(
    monoMicConstraints({
      sampleRate: sampleRate,
      deviceId: options.deviceId || '',
    })
  );
  const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
  const context = new AudioContextCtor({sampleRate: sampleRate});
  return {stream: stream, context: context};
}

export async function addInlineAudioWorklet(
  context,
  source,
  type = 'application/javascript',
) {
  const blobUrl = URL.createObjectURL(new Blob([source], {type: type}));
  try {
    await context.audioWorklet.addModule(blobUrl);
  } finally {
    URL.revokeObjectURL(blobUrl);
  }
}

export function rmsToDbfs(rms) {
  const value = Number(rms);
  return value > 0 ? 20 * Math.log10(value) : -120;
}

export function delayMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function float32ToWavBlob(samples, sampleRate = DEFAULT_SAMPLE_RATE) {
  const input = samples instanceof Float32Array
    ? samples : new Float32Array(samples || []);
  const frameCount = input.length;
  const buf = new ArrayBuffer(44 + frameCount * 2);
  const view = new DataView(buf);
  const write = (offset, text) => {
    for (let i = 0; i < text.length; i++) {
      view.setUint8(offset + i, text.charCodeAt(i));
    }
  };

  write(0, 'RIFF');
  view.setUint32(4, 36 + frameCount * 2, true);
  write(8, 'WAVE');
  write(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  write(36, 'data');
  view.setUint32(40, frameCount * 2, true);

  let offset = 44;
  for (const sample of input) {
    const clamped = Math.max(-1, Math.min(1, sample));
    view.setInt16(
      offset,
      clamped < 0 ? clamped * 32768 : clamped * 32767,
      true,
    );
    offset += 2;
  }
  return new Blob([buf], {type: 'audio/wav'});
}

export async function closeAudioGraph(graph = {}) {
  for (const node of [graph.workletNode, graph.sourceNode]) {
    if (!node) continue;
    try {
      node.disconnect();
    } catch (e) {
      // It is fine if a node was already disconnected by the page.
    }
  }
  if (graph.stream) {
    graph.stream.getTracks().forEach((track) => track.stop());
  }
  if (graph.context && graph.context.state !== 'closed') {
    await graph.context.close();
  }
}

export async function createBandpassRmsMeter(options = {}) {
  const context = options.context;
  const stream = options.stream;
  if (!context || !stream) {
    throw new Error('audio context and mic stream are required');
  }
  const frequencyHz = options.frequencyHz || 1000;
  const q = options.q || 0.67;
  const frameSize = options.frameSize || 4800;
  const processorName = options.processorName || 'jts-bandpass-rms';
  const sampleRate = context.sampleRate || DEFAULT_SAMPLE_RATE;
  const w0 = 2 * Math.PI * frequencyHz / sampleRate;
  const alpha = Math.sin(w0) / (2 * q);
  const a0 = 1 + alpha;
  const coeffs = {
    b0: alpha / a0,
    b1: 0,
    b2: -alpha / a0,
    a1: -2 * Math.cos(w0) / a0,
    a2: (1 - alpha) / a0,
  };
  const workletSrc =
    'class JtsBandpassRms extends AudioWorkletProcessor {' +
      'constructor(){super();this.c=null;this.frameSize=4800;' +
        'this.x1=0;this.x2=0;this.y1=0;this.y2=0;this.acc=0;this.n=0;' +
        'this.port.onmessage=(e)=>{if(e.data&&e.data.coeffs){' +
          'this.c=e.data.coeffs;this.frameSize=e.data.frameSize||4800;' +
        '}};}' +
      'process(inp){' +
        'var ch=inp[0]&&inp[0][0];if(!ch||!this.c)return true;' +
        'var c=this.c;' +
        'for(var i=0;i<ch.length;i++){' +
          'var x=ch[i];' +
          'var y=c.b0*x+c.b1*this.x1+c.b2*this.x2' +
            '-c.a1*this.y1-c.a2*this.y2;' +
          'this.x2=this.x1;this.x1=x;this.y2=this.y1;this.y1=y;' +
          'this.acc+=y*y;this.n++;' +
        '}' +
        'if(this.n>=this.frameSize){' +
          'var rms=Math.sqrt(this.acc/this.n);' +
          'this.port.postMessage({type:"rms",value:rms});' +
          'this.acc=0;this.n=0;' +
        '}' +
        'return true;' +
      '}}' +
    'registerProcessor(' + JSON.stringify(processorName) + ',JtsBandpassRms);';

  await addInlineAudioWorklet(context, workletSrc);
  const sourceNode = context.createMediaStreamSource(stream);
  const workletNode = new AudioWorkletNode(context, processorName);
  workletNode.port.postMessage({coeffs: coeffs, frameSize: frameSize});
  sourceNode.connect(workletNode);
  return {sourceNode: sourceNode, workletNode: workletNode};
}

export async function createMonoRecorder(options = {}) {
  const sampleRate = options.sampleRate || DEFAULT_SAMPLE_RATE;
  const processorName = options.processorName || 'jts-mono-recorder';
  const opened = await openMonoMic({sampleRate: sampleRate});
  let sourceNode = null;
  let workletNode = null;
  let stopState = null;

  try {
    const workletSrc =
      'class JtsMonoRecorder extends AudioWorkletProcessor {' +
        'constructor(){super();this.recording=false;this.buf=[];' +
          'this.port.onmessage=(e)=>{' +
            'if(e.data&&e.data.type==="start"){' +
              'this.buf=[];this.recording=true;' +
            '}else if(e.data&&e.data.type==="stop"){' +
              'this.recording=false;' +
              'var total=0;for(var i=0;i<this.buf.length;i++)total+=this.buf[i].length;' +
              'var out=new Float32Array(total);var pos=0;' +
              'for(var j=0;j<this.buf.length;j++){' +
                'out.set(this.buf[j],pos);pos+=this.buf[j].length;' +
              '}' +
              'this.buf=[];' +
              'this.port.postMessage({type:"capture",buffer:out.buffer},[out.buffer]);' +
            '}};}' +
        'process(inp){' +
          'var ch=inp[0]&&inp[0][0];' +
          'if(ch&&this.recording){' +
            'var copy=new Float32Array(ch.length);copy.set(ch);this.buf.push(copy);' +
          '}' +
          'return true;' +
        '}}' +
      'registerProcessor(' + JSON.stringify(processorName) + ',JtsMonoRecorder);';

    await addInlineAudioWorklet(opened.context, workletSrc);
    sourceNode = opened.context.createMediaStreamSource(opened.stream);
    workletNode = new AudioWorkletNode(opened.context, processorName);
    workletNode.port.onmessage = (ev) => {
      if (ev.data && ev.data.type === 'capture' && stopState) {
        const state = stopState;
        stopState = null;
        clearTimeout(state.timer);
        state.resolve(new Float32Array(ev.data.buffer));
      }
    };
    sourceNode.connect(workletNode);

    return {
      context: opened.context,
      stream: opened.stream,
      sourceNode: sourceNode,
      workletNode: workletNode,
      start() {
        workletNode.port.postMessage({type: 'start'});
      },
      stop(stopOptions = {}) {
        if (stopState) return stopState.promise;
        const timeoutMs = stopOptions.timeoutMs || 1200;
        const state = {};
        const promise = new Promise((resolve, reject) => {
          const timer = setTimeout(() => {
            if (stopState === state) stopState = null;
            reject(new Error('recording timed out'));
          }, timeoutMs);
          Object.assign(state, {
            resolve: resolve,
            reject: reject,
            timer: timer,
          });
        });
        state.promise = promise;
        stopState = state;
        workletNode.port.postMessage({type: 'stop'});
        return promise;
      },
      async close() {
        if (stopState) {
          const state = stopState;
          stopState = null;
          clearTimeout(state.timer);
          state.reject(new Error('recording closed'));
        }
        await closeAudioGraph({
          stream: opened.stream,
          context: opened.context,
          sourceNode: sourceNode,
          workletNode: workletNode,
        });
      },
    };
  } catch (e) {
    await closeAudioGraph({
      stream: opened.stream,
      context: opened.context,
      sourceNode: sourceNode,
      workletNode: workletNode,
    });
    throw e;
  }
}
