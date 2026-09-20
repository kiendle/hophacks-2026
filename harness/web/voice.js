// Voice for the chat: press Talk, ask out loud, hear the answer. A plug-in, so it builds on the
// exports of /chat.js and inserts its own controls next to the composer without touching that file.
// If the server says voice is off (no key), nothing at all is added to the page.
import { onEvent, send, plainText, el } from '/chat.js';

export const SPEAK_LIMIT = 900;   // one request to the speak route; the server refuses anything much bigger
const REMEMBER = 'signal.voice.aloud';
const HOLD_MS = 500;              // held longer than this and letting go sends the question
const SHOW_MS = 500;              // the words sit in the box this long, so the person sees what we heard
const RECORD_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/wav'];

// ---------------------------------------------------------------- pure logic
// Exported so a test can run them under node, with no browser and no network.

// What must not be read out loud: web addresses, post addresses, long ids, tags, and the stars and
// backticks that are markup on the screen and noise in the ear. The first pass matters most: a post
// can carry a right-to-left override or a zero width space, which are invisible but reorder the line
// a person reads. Same rule as harness/voice.py, step for step, and a test compares the two.
export function speakable(text) {
  if (typeof text !== 'string') return '';
  return plainText(text
    .replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u2069\ufeff]/g, '')
    .replace(/<\/?[A-Za-z][^<>]{0,200}>/g, ' ')
    .replace(/\b(?:https?:\/\/|www\.)\S+/gi, ' ')
    .replace(/\bat:\/\/\S+/gi, ' ')
    .replace(/\b(?:[0-9a-f]{16,}|\d{12,})\b/gi, ' ')
    .replace(/^[ \t]*[-*][ \t]+/gm, '')
    .replace(/[*_`#><]+/g, '')
    .replace(/[([]\s*[,.]?\s*[)\]]/g, ' ')
    .replace(/\s+/g, ' ')).trim();
}

// Pieces of at most `limit` characters, cut between sentences and never inside a word.
export function splitForSpeech(text, limit = SPEAK_LIMIT) {
  const cap = Number.isFinite(Number(limit)) ? Math.max(1, Math.trunc(Number(limit))) : SPEAK_LIMIT;
  const parts = [];
  let current = '';
  for (let sentence of String(text ?? '').trim().split(/(?<=[.!?])\s+/)) {
    while (sentence.length > cap) {
      const space = sentence.lastIndexOf(' ', cap);
      const cut = space > cap / 2 ? space : cap;
      if (current) { parts.push(current); current = ''; }
      parts.push(sentence.slice(0, cut).trim());
      sentence = sentence.slice(cut).trim();
    }
    if (!sentence) continue;
    if (current && current.length + sentence.length + 1 > cap) { parts.push(current); current = ''; }
    current = `${current} ${sentence}`.trim();
  }
  return [...parts, current].filter(Boolean);
}

// -------------------------------------------------------------------- state
const state = {
  aloud: false,        // "Read answers aloud", remembered between visits
  recording: false,
  starting: false,     // the microphone has been asked for and not granted yet
  cancelled: false,
  recorder: null,
  media: null,         // the microphone stream, stopped again as soon as the clip is done
  pieces: [],
  pressedAt: 0,
  pointer: false,      // a mouse press already did the work, so ignore the click that follows it
  working: false,      // a turn is running
};

// One thing is spoken at a time. `mine` is bumped by stop(), so a fetch that was already in flight
// when the person pressed Stop throws its answer away instead of talking over the next one.
const player = { mine: 0, audio: null, finish: null, busy: false, step: '', answer: null };

const remembered = () => { try { return localStorage.getItem(REMEMBER) === 'yes'; } catch { return false; } };
const remember = (value) => { try { localStorage.setItem(REMEMBER, value ? 'yes' : 'no'); } catch { /* private mode */ } };
const $ = (id) => document.getElementById(id);
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function announce(words) {
  const box = $('voice-state');
  if (box) box.textContent = words;
}

function paint() {
  const mic = $('voice-mic');
  if (mic) {
    mic.textContent = state.recording ? 'Stop' : 'Talk';
    mic.setAttribute('aria-pressed', state.recording ? 'true' : 'false');
    mic.setAttribute('aria-label', state.recording ? 'Stop recording and send the question' : 'Ask out loud. Press to start, press again to stop');
  }
  const toggle = $('voice-aloud');
  if (toggle) toggle.setAttribute('aria-pressed', state.aloud ? 'true' : 'false');
  const stop = $('voice-stop');
  if (stop) stop.hidden = !player.busy;
  const bar = $('voice-bar');
  if (bar) bar.dataset.state = state.recording ? 'listening' : (player.busy ? 'speaking' : 'idle');
}

// ----------------------------------------------------------------- speaking
async function fetchSpeech(text, mine) {
  let response;
  try {
    response = await fetch('/api/voice/speak', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
    });
  } catch {
    return { problem: 'We could not reach the voice service from this laptop.' };
  }
  if (!response.ok) {
    let said = 'The answer could not be read out loud.';
    try {
      const data = await response.json();
      if (data && data.error && typeof data.error.message === 'string') said = data.error.message;
    } catch { /* not JSON */ }
    return { problem: said };
  }
  const blob = await response.blob();
  return mine === player.mine ? { blob } : {};
}

// Pausing a real Audio element fires nothing, so stop() has to be able to end this wait itself:
// without that, one press of Stop would leave the player busy and nothing would ever speak again.
function playBlob(blob, mine) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    const finish = () => {
      if (player.finish !== finish) return;
      player.finish = null;
      audio.onended = null;
      audio.onerror = null;
      try { audio.pause(); } catch { /* already gone */ }
      URL.revokeObjectURL(url);
      if (player.audio === audio) player.audio = null;
      resolve();
    };
    player.audio = audio;
    player.finish = finish;
    audio.onended = finish;
    audio.onerror = finish;
    if (mine !== player.mine) { finish(); return; }
    audio.play().catch(finish);  // a browser that wants a click first: stay quiet, never throw
  });
}

async function pump() {
  if (player.busy) return;
  player.busy = true;
  paint();
  const mine = player.mine;
  try {
    while (mine === player.mine) {
      let text = '';
      const answer = player.answer;
      if (answer && answer.at < answer.chunks.length) {
        text = answer.chunks[answer.at++];
      } else if (player.step) {
        text = player.step;
        player.step = '';
      } else break;
      announce('Speaking');
      const ready = answer && answer.next ? answer.next : fetchSpeech(text, mine);
      if (answer) answer.next = answer.at < answer.chunks.length ? fetchSpeech(answer.chunks[answer.at], mine) : null;
      const { blob, problem } = await ready;
      if (problem) { announce(problem); player.answer = null; player.step = ''; break; }
      if (blob) await playBlob(blob, mine);
    }
  } finally {
    player.busy = false;
    if (!state.recording) announce(state.working ? 'Thinking' : '');
    paint();
  }
  // Work that arrived while this loop was finishing, which a call to pump() then found busy: the new
  // answer of a new turn lands exactly there, so without this it would never be read out.
  if (player.step || (player.answer && player.answer.at < player.answer.chunks.length)) pump();
}

function stopSpeaking() {
  player.mine += 1;
  player.step = '';
  player.answer = null;
  if (player.audio) { try { player.audio.pause(); } catch { /* already gone */ } }
  if (player.finish) player.finish();  // ends the wait inside pump(), which pausing on its own never does
  player.audio = null;
  paint();
}

// ---------------------------------------------------------------- listening
function pickType() {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
  return RECORD_TYPES.find((type) => MediaRecorder.isTypeSupported(type)) || '';
}

async function startRecording() {
  if (state.recording || state.starting) return;  // asking for the microphone takes a moment: one press, one recording
  state.starting = true;
  stopSpeaking();
  let media;
  try {
    media = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    state.starting = false;
    announce('This browser would not give us the microphone. Check the microphone permission for this page.');
    return;
  }
  state.starting = false;
  const type = pickType();
  let recorder;
  try {
    recorder = new MediaRecorder(media, type ? { mimeType: type } : {});
  } catch {
    media.getTracks().forEach((track) => track.stop());  // hand the microphone back before giving up
    announce('This browser cannot record sound. Type the question instead.');
    return;
  }
  state.recording = true;
  state.cancelled = false;
  state.pieces = [];
  state.media = media;
  state.recorder = recorder;
  recorder.ondataavailable = (event) => { if (event.data && event.data.size) state.pieces.push(event.data); };
  recorder.onstop = () => finishRecording(type);
  recorder.start();
  announce('Listening');
  paint();
}

function stopRecording(cancelled) {
  if (!state.recording) return;
  state.cancelled = Boolean(cancelled);
  state.recording = false;
  try { state.recorder.stop(); } catch { /* already stopped */ }
  paint();
}

async function finishRecording(type) {
  const media = state.media;
  if (media) media.getTracks().forEach((track) => track.stop());
  state.media = null;
  const pieces = state.pieces;
  state.pieces = [];
  if (state.cancelled) { announce(''); return; }
  const blob = new Blob(pieces, { type: (pieces[0] && pieces[0].type) || type || 'audio/webm' });
  announce('Thinking');
  let response;
  try {
    response = await fetch('/api/voice/transcribe', { method: 'POST', headers: { 'Content-Type': blob.type || 'audio/webm' }, body: blob });
  } catch {
    announce('We could not reach the voice service from this laptop.');
    return;
  }
  if (!response.ok) {
    let said = 'We could not turn that recording into words.';
    try {
      const data = await response.json();
      if (data && data.error && typeof data.error.message === 'string') said = data.error.message;
    } catch { /* not JSON */ }
    announce(said);
    return;
  }
  const data = await response.json().catch(() => null);
  const text = data && typeof data.text === 'string' ? plainText(data.text).trim() : '';
  if (!text) { announce('We could not make out any words. Try again.'); return; }
  const input = $('chat-input');
  if (input) input.value = text;          // half a second of "this is what we heard", then it is sent
  announce(text);
  await wait(SHOW_MS);
  if (!state.aloud) { state.aloud = true; remember(true); paint(); }  // asked out loud, so answer out loud
  // chat.js drops a message sent while a turn is running, and the question would vanish without a
  // word. It is already sitting in the box, so say that instead of pretending it went.
  if (state.working) { announce('The assistant is still answering. Your question is in the box, ready to send.'); return; }
  send(text);
}

// ---------------------------------------------------------------- the turns
function watch() {
  onEvent((event) => {
    if (!event || typeof event !== 'object') return;
    if (event.type === 'turn_start') {
      state.working = true;
      stopSpeaking();
      announce(state.aloud ? 'Thinking' : '');
    } else if (event.type === 'step' && event.phase === 'start' && state.aloud) {
      player.step = plainText(String(event.title || ''));  // newest only: an older waiting line is dropped
      pump();
    } else if (event.type === 'message' && state.aloud && typeof event.text === 'string') {
      const words = speakable(event.text);
      player.step = '';
      player.answer = words ? { chunks: splitForSpeech(words), at: 0, next: null } : null;
      pump();
    } else if (event.type === 'error') {
      stopSpeaking();
    } else if (event.type === 'turn_end') {
      state.working = false;
      if (!player.busy && !state.recording) announce('');
    }
  });
}

// ---------------------------------------------------------------- the panel
function controls() {
  const composer = $('composer');
  if (!composer) return false;
  composer.insertBefore(el('button', {
    class: 'voice-mic', id: 'voice-mic', type: 'button', text: 'Talk', 'aria-pressed': 'false',
    'aria-label': 'Ask out loud. Press to start, press again to stop',
    onpointerdown: (event) => {
      if (event.button) return;
      state.pressedAt = Date.now();
      if (state.recording) stopRecording(false); else startRecording();
    },
    onpointerup: () => {
      state.pointer = true;  // the click that follows a real press is the same press, so swallow it
      setTimeout(() => { state.pointer = false; }, 400);
      if (state.recording && Date.now() - state.pressedAt > HOLD_MS) stopRecording(false);
    },
    onclick: () => {
      if (state.pointer) { state.pointer = false; return; }  // keyboard only: the mouse already acted
      if (state.recording) stopRecording(false); else startRecording();
    },
  }), $('chat-send'));

  composer.parentNode.insertBefore(el('div', { class: 'voice-bar', id: 'voice-bar', 'data-state': 'idle' },
    el('button', {
      class: 'voice-aloud', id: 'voice-aloud', type: 'button', text: 'Read answers aloud',
      'aria-pressed': state.aloud ? 'true' : 'false',
      onclick: () => {
        state.aloud = !state.aloud;
        remember(state.aloud);
        if (!state.aloud) stopSpeaking();
        paint();
      },
    }),
    el('button', { class: 'voice-stop', id: 'voice-stop', type: 'button', text: 'Stop', hidden: true, 'aria-label': 'Stop reading the answer out loud', onclick: () => { stopSpeaking(); announce(''); } }),
    el('span', { class: 'voice-state', id: 'voice-state', role: 'status', 'aria-live': 'polite' }),
  ), composer);
  return true;
}

function styles() {
  if (document.querySelector('link[href="/voice.css"]')) return;
  document.head.append(el('link', { rel: 'stylesheet', href: '/voice.css' }));
}

async function start() {
  let ready = false;
  try {
    const response = await fetch('/api/voice/status');
    ready = response.ok && Boolean((await response.json()).available);
  } catch { /* the bridge is not answering: the chat still works, the microphone does not exist */ }
  if (!ready || !navigator.mediaDevices || typeof MediaRecorder === 'undefined') return;
  state.aloud = remembered();
  styles();
  if (!controls()) return;
  paint();
  watch();
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (state.recording) stopRecording(true);
    else if (player.busy) { stopSpeaking(); announce(''); }
  });
}

if (typeof document !== 'undefined' && document.getElementById('composer')) start();
