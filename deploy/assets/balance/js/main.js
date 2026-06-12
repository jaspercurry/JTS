// Pair-balance wizard (#23 P2). One continuous phone-mic capture
// brackets the left/right/left burst sequence the leader plays; the
// server (jasper/web/balance_flow.py) does all the math and the page
// only renders phases. Mic constraints + AudioWorklet capture +
// float32ToWav mirror the proven /correction/ patterns.

const csrf =
  (document.querySelector('meta[name="jts-csrf"]') || {}).content || '';
const statusEl = document.getElementById('status');
const membersEl = document.getElementById('members');
const verdictEl = document.getElementById('verdict');
const startBtn = document.getElementById('start');
const applyBtn = document.getElementById('apply');
const againBtn = document.getElementById('again');

const REQUIRED_SR = 48000;

const REASON_COPY = {
  no_alignment: 'Couldn’t hear the test sounds — raise the ' +
    'speaker volume a little and try again.',
  low_snr: 'Too much background noise for a reliable reading — ' +
    'try again, a bit louder or in a quieter moment.',
  clipped: 'Too loud for the phone microphone — turn the ' +
    'speakers down a touch and try again.',
  drift: 'The phone moved during the measurement — hold it ' +
    'still and try again.',
  capture_short: 'The recording was cut short — try again.',
};

let ctx = null;
let workletNode = null;
let micStream = null;
let pendingUpload = false;
let lastMembers = null;

function setStatus(text, tone) {
  statusEl.textContent = text || '';
  statusEl.dataset.tone = tone || '';
}

function jsonHeaders() {
  return { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf };
}

async function post(path, body, headers) {
  const resp = await fetch(path, {
    method: 'POST', headers: headers || jsonHeaders(), body: body,
  });
  let data = null;
  try { data = await resp.json(); } catch (e) { /* non-JSON error */ }
  if (!data) throw new Error(path + ' → HTTP ' + resp.status);
  return data;
}

function float32ToWav(samples, sampleRate) {
  const len = samples.length;
  const buf = new ArrayBuffer(44 + len * 2);
  const view = new DataView(buf);
  function w8s(off, str) {
    for (let i = 0; i < str.length; i++) {
      view.setUint8(off + i, str.charCodeAt(i));
    }
  }
  w8s(0, 'RIFF');
  view.setUint32(4, 36 + len * 2, true);
  w8s(8, 'WAVE');
  w8s(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);  // PCM
  view.setUint16(22, 1, true);  // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  w8s(36, 'data');
  view.setUint32(40, len * 2, true);
  let off = 44;
  for (let i = 0; i < len; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(off, s * 0x7FFF, true);
    off += 2;
  }
  return new Blob([buf], { type: 'audio/wav' });
}

async function openMic() {
  if (workletNode) return;
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
      sampleRate: REQUIRED_SR,
      channelCount: 1,
    },
    video: false,
  });
  ctx = new (window.AudioContext || window.webkitAudioContext)(
    { sampleRate: REQUIRED_SR });
  const workletSrc =
    'class M extends AudioWorkletProcessor {' +
      'constructor(){super();this.cap=false;this.buf=[];' +
        'this.port.onmessage=(e)=>{' +
          'if(e.data===\'startCapture\'){this.buf=[];this.cap=true;}' +
          'else if(e.data===\'stopCapture\'){' +
            'this.cap=false;' +
            'var total=0;' +
            'for(var i=0;i<this.buf.length;i++)total+=this.buf[i].length;' +
            'var out=new Float32Array(total);var pos=0;' +
            'for(var i=0;i<this.buf.length;i++){' +
              'out.set(this.buf[i],pos);pos+=this.buf[i].length;}' +
            'this.port.postMessage(' +
              '{type:\'capture\',buffer:out.buffer},[out.buffer]);' +
            'this.buf=[];' +
          '}};}' +
      'process(inp){' +
        'var ch=inp[0]&&inp[0][0];if(!ch)return true;' +
        'if(this.cap){' +
          'var copy=new Float32Array(ch.length);copy.set(ch);' +
          'this.buf.push(copy);' +
        '}' +
        'return true;' +
      '}}' +
    'registerProcessor("m",M);';
  const blobUrl = URL.createObjectURL(
    new Blob([workletSrc], { type: 'application/javascript' }));
  await ctx.audioWorklet.addModule(blobUrl);
  const src = ctx.createMediaStreamSource(micStream);
  workletNode = new AudioWorkletNode(ctx, 'm');
  workletNode.port.onmessage = (ev) => {
    if (ev.data && ev.data.type === 'capture') {
      onCapture(new Float32Array(ev.data.buffer));
    }
  };
  src.connect(workletNode);
  // No mic→destination connect: that would feed the speakers back.
}

function channelLine(ch, m) {
  const row = document.createElement('div');
  row.className = 'row';
  const name = document.createElement('span');
  name.textContent = (ch === 'left' ? 'Left — ' : 'Right — ')
    + m.label;
  const lvl = document.createElement('span');
  lvl.className = 'lvl';
  lvl.textContent = m.trim_db.toFixed(1) + ' dB trim';
  row.append(name, lvl);
  return row;
}

function renderAnalyzed(data) {
  lastMembers = data.members;
  const r = data.result;
  const rec = data.recommendation;
  membersEl.textContent = '';
  membersEl.append(
    channelLine('left', {
      label: data.members.left.label, trim_db: rec.left_trim_db,
    }),
    channelLine('right', {
      label: data.members.right.label, trim_db: rec.right_trim_db,
    }),
  );
  membersEl.style.display = 'block';
  const louder = r.delta_db >= 0 ? 'left' : 'right';
  const diff = Math.abs(r.delta_db);
  let text;
  if (diff < 0.5) {
    text = 'Already balanced (difference '
      + diff.toFixed(1) + ' dB).';
  } else {
    text = 'The ' + louder + ' speaker ('
      + data.members[louder].label + ') is '
      + diff.toFixed(1) + ' dB louder. Apply sets the trims above.';
  }
  if (rec.clamped) {
    text += ' Note: the difference exceeds the −24 dB trim '
      + 'range, so this is the closest possible match.';
  }
  verdictEl.textContent = text;
  setStatus('Measurement complete.', 'ok');
  applyBtn.hidden = diff < 0.5 && !rec.clamped;
  againBtn.hidden = false;
}

async function onCapture(samples) {
  if (!pendingUpload) return;
  pendingUpload = false;
  try {
    setStatus('Analyzing…');
    const wav = float32ToWav(samples, ctx.sampleRate);
    const data = await post('upload-capture', wav,
      { 'Content-Type': 'audio/wav', 'X-CSRF-Token': csrf });
    if (data.ok) {
      renderAnalyzed(data);
    } else if (data.rejected && data.result) {
      setStatus(REASON_COPY[data.result.reason]
        || ('Measurement rejected: ' + data.result.reason), 'bad');
    } else {
      setStatus(data.error || 'Analysis failed.', 'bad');
    }
  } catch (e) {
    setStatus(e.message, 'bad');
  }
  startBtn.disabled = false;
  startBtn.textContent = 'Measure again';
}

startBtn.addEventListener('click', async () => {
  startBtn.disabled = true;
  applyBtn.hidden = true;
  againBtn.hidden = true;
  verdictEl.textContent = '';
  membersEl.style.display = 'none';
  try {
    setStatus('Asking for the microphone…');
    await openMic();
    workletNode.port.postMessage('startCapture');
    setStatus('Playing test sounds — hold the phone still…');
    const play = await post('play', JSON.stringify({}));
    if (!play.ok) {
      workletNode.port.postMessage('stopCapture');  // discarded
      setStatus(play.error || 'Could not start.', 'bad');
      startBtn.disabled = false;
      return;
    }
    setStatus('Finishing the recording…');
    await new Promise((r) => setTimeout(r, 600));
    pendingUpload = true;
    workletNode.port.postMessage('stopCapture');
  } catch (e) {
    setStatus(e.message, 'bad');
    startBtn.disabled = false;
  }
});

applyBtn.addEventListener('click', async () => {
  applyBtn.disabled = true;
  try {
    const data = await post('apply', JSON.stringify({}));
    if (data.ok) {
      setStatus('Trims applied — measure again to verify.', 'ok');
      applyBtn.hidden = true;
    } else {
      const failed = Object.values(data.writes || {})
        .filter((w) => !w.ok).map((w) => w.label).join(', ');
      setStatus('Apply failed for: ' + (failed || 'unknown')
        + ' — check the pair and try again.', 'bad');
    }
  } catch (e) {
    setStatus(e.message, 'bad');
  }
  applyBtn.disabled = false;
});

againBtn.addEventListener('click', async () => {
  againBtn.disabled = true;
  try { await post('reset', JSON.stringify({})); } catch (e) { /* soft */ }
  againBtn.disabled = false;
  againBtn.hidden = true;
  applyBtn.hidden = true;
  verdictEl.textContent = '';
  membersEl.style.display = 'none';
  setStatus('');
});

(async function init() {
  try {
    const resp = await fetch('status');
    const st = await resp.json();
    if (!st.bonded) {
      setStatus('No stereo pair is bonded — set one up at '
        + 'jts.local/rooms first.', 'bad');
      startBtn.disabled = true;
    } else if (st.role !== 'leader') {
      setStatus('Open this page on the pair leader.', 'bad');
      startBtn.disabled = true;
    }
  } catch (e) {
    setStatus('Could not reach the speaker: ' + e.message, 'bad');
    startBtn.disabled = true;
  }
})();
