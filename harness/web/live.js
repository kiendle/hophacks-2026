// Talk live: a hands free spoken conversation with the same assistant the typed chat talks to.
//
// The local Claude is the only brain in the room. This file is the ears and the mouth: it decides
// when the person has stopped speaking, has those seconds turned into words, and hands them to
// /chat.js's own send(), so a spoken question walks exactly the same path as a typed one, with the
// same steps and the same cards. The answer is cut into sentences as it streams and read out while
// it is still being written. Every word spoken was written by the assistant or by this file.
//
// A plug-in under the contract in harness/prompts/README.md: it builds on the five exports of
// /chat.js, adds its own controls next to the composer, and edits no shared file.
import { onEvent, appendCard, send, plainText, el } from '/chat.js';

export const SAY_LIMIT = 600;   // one request to /api/live/say; the server refuses more
export const FIRST_CUT = 60;    // the first sentence is cut early, so the first words start fast
const MIN_PIECE = 4;            // "A." is an initial, not a sentence: keep looking for the real end
const TIMESLICE = 250;          // ms of sound per recorder piece
const CALIBRATE_MS = 1000;      // the first second of a session is the room, not the person
const START_MS = 150;           // loud for this long and the person has started talking
const END_MS = 900;             // quiet for this long and their turn is over
const MIN_TURN_MS = 400;        // shorter than this was a cough
const MIN_FLOOR = 0.012;        // however quiet the room is, silence never counts as speech
const BARGE_BAR = 2;            // while it is speaking, the person has to be this much louder to cut in
const STEP_STALE_MS = 5000;     // a step line older than this is news nobody needs
const HIDDEN_MS = 60000;        // the tab out of sight this long hands the microphone back
const PRE_ROLL = 2;             // pieces of sound kept from just before speech was noticed
const SAMPLE_MS = 50;
const RECORD_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/wav'];
const TALK_HINT = 'Ask one question by voice.';
const LIVE_HINT = 'Have a spoken conversation. It answers as it goes and you can interrupt.';

// ---------------------------------------------------------------- pure logic
// Exported so a test can run them under node, with no browser and no network.

// What must never be read out loud: web addresses, post addresses, long ids, an id in brackets,
// code spans, tags, and the stars and backticks that are markup on the screen and noise in the ear.
// The first pass matters most: a post can carry a right-to-left override or a zero width space,
// which are invisible but reorder the line a person reads. Same rule as harness/voice.py, which the
// server applies again to whatever this sends it.
export function speakable(text) {
  if (typeof text !== 'string') return '';
  return plainText(text
    .replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f​-‏ -‮⁠-⁤⁦-⁩﻿]/g, '')
    .replace(/<\/?[A-Za-z][^<>]{0,200}>/g, ' ')
    .replace(/`[^`\n]{0,200}`/g, ' ')
    .replace(/\b(?:https?:\/\/|www\.)\S+/gi, ' ')
    .replace(/\bat:\/\/\S+/gi, ' ')
    // brackets holding one unbroken run with a digit, a colon or a slash in it: an id, never a word
    .replace(/[([]([A-Za-z0-9_:/.+-]{8,})[)\]]/g, (whole, inside) => (/[0-9_:/]/.test(inside) ? ' ' : whole))
    .replace(/\b(?:[0-9a-f]{16,}|\d{12,})\b/gi, ' ')
    .replace(/^[ \t]*[-*][ \t]+/gm, '')
    .replace(/[*_`#><]+/g, '')
    .replace(/[([]\s*[,.]?\s*[)\]]/g, ' ')
    .replace(/\s+/g, ' ')).trim();
}

// Where the sentence starting at `from` ends, or -1 while more text may still arrive. A full stop
// only ends a sentence when whitespace follows it, so "3.5" and "example.com" are left alone, and a
// stop at the very end of what we have so far waits: "1." may still become "1.5 million".
function endOf(text, from = 0) {
  for (let at = from; at < text.length; at += 1) {
    const mark = text[at];
    if (mark === '\n') return at + 1;
    if (mark !== '.' && mark !== '!' && mark !== '?') continue;
    let end = at + 1;
    while (end < text.length && '"\')]’”'.includes(text[end])) end += 1;
    if (end >= text.length) return -1;
    if (!/\s/.test(text[end])) continue;
    const before = text[at - 1] || '';
    const earlier = at >= 2 ? text[at - 2] : ' ';
    if (/[A-Za-z]/.test(before) && /\s/.test(earlier)) continue;  // "A." is an initial
    return end;
  }
  return -1;
}

// The answer arrives a few characters at a time. feed() gives back every sentence that has become
// whole, in order; flush() gives back what is left when the answer ends.
export function createCutter(options = {}) {
  const first = Math.max(8, Number(options.firstCut) || FIRST_CUT);
  const limit = Math.max(first, Number(options.limit) || SAY_LIMIT);
  let buffer = '';
  let opened = false;  // the first sentence has been handed over, so the early cut is done with

  const take = (at) => {
    const piece = buffer.slice(0, at).trim();
    buffer = buffer.slice(at).replace(/^\s+/, '');
    opened = true;
    return piece;
  };
  const wordCut = (width) => {
    const space = buffer.lastIndexOf(' ', width);
    return space > width / 2 ? space : width;
  };
  const nextEnd = () => {
    for (let at = 0; ;) {
      const end = endOf(buffer, at);
      if (end < 0) return -1;
      if (end >= MIN_PIECE) return end;
      at = end;
    }
  };

  return {
    feed(text) {
      buffer += typeof text === 'string' ? text : '';
      const out = [];
      for (;;) {
        const end = nextEnd();
        if (end > 0 && end <= limit) out.push(take(end));
        else if (!opened && buffer.length >= first) out.push(take(wordCut(first)));
        else if (buffer.length > limit) out.push(take(wordCut(limit)));
        else break;
      }
      return out.filter(Boolean);
    },
    flush() {
      const out = [];
      while (buffer.length > limit) out.push(take(wordCut(limit)));
      const rest = buffer.trim();
      buffer = '';
      opened = true;
      if (rest) out.push(rest);
      return out.filter(Boolean);
    },
  };
}

// Is the person talking? feed(level, at) takes one loudness reading and the clock, and answers with
// "start" when a turn begins, "end" when it is over, "drop" when it was too short to be a question,
// and "" the rest of the time. The bar is the room's own noise, measured in the first second.
export function createDetector(options = {}) {
  const startMs = Number(options.startMs) || START_MS;
  const endMs = Number(options.endMs) || END_MS;
  const minMs = Number(options.minMs) || MIN_TURN_MS;
  const calibrateMs = Number(options.calibrateMs) || CALIBRATE_MS;
  const room = [];
  let began = null;
  let last = null;
  let loud = 0;
  let quiet = 0;
  let talking = false;
  let startedAt = 0;

  const feed = (level, at) => {
    const now = Number(at);
    const value = Number(level);
    if (!Number.isFinite(now) || !Number.isFinite(value)) return '';
    if (began === null) began = now;
    const step = last === null ? 0 : Math.max(0, Math.min(400, now - last));  // a stalled tab is not speech
    last = now;
    if (now - began < calibrateMs) {
      room.push(Math.max(0, value));
      return '';
    }
    if (!feed.floor) {
      const sorted = room.slice().sort((one, two) => one - two);
      const middle = sorted.length ? sorted[Math.floor(sorted.length / 2)] : 0;
      feed.floor = Math.max(MIN_FLOOR, middle * 3 + 0.004);
    }
    if (value >= feed.floor) {
      quiet = 0;
      loud += step;
      if (!talking && loud >= startMs) {
        talking = true;
        startedAt = now - loud;
        return 'start';
      }
    } else if (talking) {
      quiet += step;
      if (quiet >= endMs) {
        const spoke = now - quiet - startedAt;
        talking = false;
        loud = 0;
        quiet = 0;
        return spoke >= minMs ? 'end' : 'drop';
      }
    } else {
      loud = 0;
    }
    return '';
  };
  feed.floor = 0;
  return feed;
}

// -------------------------------------------------------------------- state
const live = {
  on: false, starting: false, muted: false, running: false,
  media: null, context: null, analyser: null, buffer: null, recorder: null, mime: '',
  pieces: [], header: null, turnAt: -1, detect: null, ticker: null,
  cutter: null, sawDelta: false, waiting: '', hidden: null,
  panel: null, stateNode: null, noteNode: null, meterFill: null, muteButton: null, button: null, bar: null,
  restoreAloud: false, level: 0,
};

// One thing is spoken at a time. `token` is bumped whenever the person interrupts, so a fetch that
// was already in flight throws its answer away instead of talking over them.
const talk = { token: 0, queue: [], step: null, next: null, audio: null, finish: null, busy: false, aborts: new Set() };

const $ = (id) => document.getElementById(id);

function setState(words) {
  if (live.stateNode) live.stateNode.textContent = words;
  if (live.panel) live.panel.dataset.state = words.toLowerCase().split(' ')[0] || 'off';
}

function paint() {
  if (live.button) {
    live.button.textContent = live.on ? 'End live' : 'Talk live';
    live.button.setAttribute('aria-pressed', live.on ? 'true' : 'false');
  }
  if (live.muteButton) {
    live.muteButton.textContent = live.muted ? 'Unmute' : 'Mute';
    live.muteButton.setAttribute('aria-pressed', live.muted ? 'true' : 'false');
  }
  if (live.meterFill) live.meterFill.style.setProperty('--level', `${Math.round(Math.min(1, live.level) * 100)}%`);
}

// ----------------------------------------------------------------- speaking
async function fetchSay(text, mine) {
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  if (controller) talk.aborts.add(controller);
  let response;
  try {
    response = await fetch('/api/live/say', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }), signal: controller ? controller.signal : undefined,
    });
  } catch {
    return mine === talk.token ? { problem: 'We could not reach the voice service from this laptop.' } : {};
  } finally {
    if (controller) talk.aborts.delete(controller);
  }
  if (!response.ok) {
    let said = 'That sentence could not be read out loud.';
    try {
      const data = await response.json();
      if (data && data.error && typeof data.error.message === 'string') said = data.error.message;
    } catch { /* not JSON */ }
    return { problem: said };
  }
  const blob = await response.blob().catch(() => null);
  return mine === talk.token && blob ? { blob } : {};
}

// Pausing a real Audio element fires nothing, so stopSpeaking has to be able to end this wait
// itself: without that, one interruption would leave the player busy and nothing would speak again.
function playBlob(blob, mine) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    const finish = () => {
      if (talk.finish !== finish) return;
      talk.finish = null;
      audio.onended = null;
      audio.onerror = null;
      try { audio.pause(); } catch { /* already gone */ }
      URL.revokeObjectURL(url);
      if (talk.audio === audio) talk.audio = null;
      resolve();
    };
    talk.audio = audio;
    talk.finish = finish;
    audio.onended = finish;
    audio.onerror = finish;
    if (mine !== talk.token) { finish(); return; }
    audio.play().catch(finish);  // a browser that wants a click first: stay quiet, never throw
  });
}

async function pump() {
  if (talk.busy) return;
  talk.busy = true;
  const mine = talk.token;
  try {
    while (mine === talk.token) {
      let text = '';
      if (talk.queue.length) {
        text = talk.queue.shift();
      } else if (talk.step && Date.now() - talk.step.at <= STEP_STALE_MS) {
        text = talk.step.text;
        talk.step = null;
      } else {
        talk.step = null;  // older than five seconds: the step it was about is long finished
        break;
      }
      setState('Speaking');
      const ready = talk.next || fetchSay(text, mine);
      talk.next = talk.queue.length ? fetchSay(talk.queue[0], mine) : null;  // one ahead, never more
      const { blob, problem } = await ready;
      if (problem) {
        note(problem);
        talk.queue = [];
        talk.next = null;
        break;
      }
      if (blob) await playBlob(blob, mine);
    }
  } finally {
    talk.busy = false;
    if (mine === talk.token && live.on) setState(live.running ? 'Thinking' : 'Listening');
  }
  if (live.on && (talk.queue.length || talk.step)) pump();  // work that arrived while this was finishing
}

function stopSpeaking() {
  talk.token += 1;
  talk.queue = [];
  talk.step = null;
  talk.next = null;
  for (const controller of talk.aborts) { try { controller.abort(); } catch { /* already done */ } }
  talk.aborts.clear();
  if (talk.audio) { try { talk.audio.pause(); } catch { /* already gone */ } }
  if (talk.finish) talk.finish();  // ends the wait inside pump(), which pausing alone never does
  talk.audio = null;
}

function enqueue(sentences) {
  for (const sentence of sentences) {
    const words = speakable(sentence);
    if (words) talk.queue.push(words);
  }
  if (talk.queue.length) pump();
}

// ---------------------------------------------------------------- listening
function pickType() {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
  return RECORD_TYPES.find((type) => MediaRecorder.isTypeSupported(type)) || '';
}

function loudness() {
  if (!live.analyser || !live.buffer) return 0;
  live.analyser.getByteTimeDomainData(live.buffer);
  let sum = 0;
  for (let at = 0; at < live.buffer.length; at += 1) {
    const away = (live.buffer[at] - 128) / 128;
    sum += away * away;
  }
  return Math.sqrt(sum / live.buffer.length);
}

function beginTurn() {
  stopSpeaking();  // barge in: whatever is playing stops at once and the queue goes with it
  live.turnAt = Math.max(0, live.pieces.length - PRE_ROLL);
  setState('Listening');
}

// Only the sound of this turn is sent. The first piece the recorder ever made carries the file's
// header, so it goes in front of every turn but the first, or there is nothing to decode.
function turnBlob() {
  const parts = live.pieces.slice(live.turnAt);
  const whole = parts[0] === live.header ? parts : [live.header, ...parts];
  return new Blob(whole.filter(Boolean), { type: live.mime || 'audio/webm' });
}

function trimPieces() {
  live.pieces = live.header ? [live.header] : [];
  live.turnAt = -1;
}

async function endTurn(keep) {
  const blob = live.turnAt >= 0 ? turnBlob() : null;
  trimPieces();
  if (!keep || live.muted || !blob || blob.size < 512) return;  // never send silence
  setState('Heard you');
  let response;
  try {
    response = await fetch('/api/live/listen', { method: 'POST', headers: { 'Content-Type': blob.type || 'audio/webm' }, body: blob });
  } catch {
    stop('We lost the voice service, so the live talk has ended.');
    return;
  }
  if (!response.ok) {
    let said = 'We could not turn that into words.';
    try {
      const data = await response.json();
      if (data && data.error && typeof data.error.message === 'string') said = data.error.message;
    } catch { /* not JSON */ }
    note(said);
    if (live.on) setState('Listening');
    return;
  }
  const data = await response.json().catch(() => null);
  const text = data && typeof data.text === 'string' ? plainText(data.text).trim() : '';
  if (!text) {
    if (live.on) setState('Listening');
    return;
  }
  if (live.running) {  // chat.js drops a message sent mid turn, so it waits here instead of vanishing
    live.waiting = text;
    note('Heard you. It answers the last question first.');
    return;
  }
  send(text);
}

function sample() {
  if (!live.on) return;
  const raw = live.muted ? 0 : loudness();
  live.level = raw;
  paint();
  // While it is talking, the microphone also hears the answer coming out of the speakers, so the
  // person has to be clearly louder than that before we treat it as an interruption.
  const move = live.detect(talk.busy ? raw / BARGE_BAR : raw, Date.now());
  if (move === 'start') beginTurn();
  else if (move === 'end' || move === 'drop') endTurn(move === 'end').catch(() => {});
}

async function openMicrophone() {
  const media = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  const Sound = window.AudioContext || window.webkitAudioContext;
  const context = new Sound();
  const analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  context.createMediaStreamSource(media).connect(analyser);
  const type = pickType();
  const recorder = new MediaRecorder(media, type ? { mimeType: type } : {});
  live.media = media;
  live.context = context;
  live.analyser = analyser;
  live.buffer = new Uint8Array(analyser.fftSize);
  live.recorder = recorder;
  live.mime = type || 'audio/webm';
  live.pieces = [];
  live.header = null;
  live.turnAt = -1;
  recorder.ondataavailable = (event) => {
    if (!event.data || !event.data.size) return;
    if (!live.header) live.header = event.data;
    live.pieces.push(event.data);
    if (live.turnAt < 0 && live.pieces.length > PRE_ROLL + 1) live.pieces.splice(1, 1);  // idle: keep the header and a little pre-roll
  };
  recorder.start(TIMESLICE);
}

// ------------------------------------------------------------------ the turns
function watch() {
  onEvent((event) => {
    if (!event || typeof event !== 'object' || !live.on) return;
    if (event.type === 'turn_start') {
      live.running = true;
      live.cutter = createCutter();
      live.sawDelta = false;
      talk.step = null;
      setState('Thinking');
    } else if (event.type === 'delta' && typeof event.text === 'string') {
      live.sawDelta = true;
      if (live.cutter) enqueue(live.cutter.feed(event.text));
    } else if (event.type === 'step' && event.phase === 'start') {
      talk.step = { text: plainText(String(event.title || '')), at: Date.now() };  // newest only
      pump();
    } else if (event.type === 'message' && typeof event.text === 'string') {
      if (!live.cutter) live.cutter = createCutter();
      if (!live.sawDelta) live.cutter.feed(event.text);  // nothing streamed: the whole answer arrives at once
      enqueue(live.cutter.flush());
    } else if (event.type === 'error') {
      stopSpeaking();
      if (typeof event.text === 'string' && event.text) note(plainText(event.text).slice(0, 200));
    } else if (event.type === 'turn_end') {
      live.running = false;
      live.cutter = null;
      if (!talk.busy) setState('Listening');
      const asked = live.waiting;
      live.waiting = '';
      if (asked) send(asked);
    }
  });
}

// ------------------------------------------------------------------ the panel
function note(words) {
  if (!live.noteNode) return;
  live.noteNode.textContent = plainText(String(words || ''));
}

function buildPanel() {
  live.stateNode = el('span', { class: 'live-state', role: 'status', 'aria-live': 'polite', text: 'Starting' });
  live.meterFill = el('i', { class: 'live-level-fill' });
  live.muteButton = el('button', {
    class: 'live-action', type: 'button', text: 'Mute', 'aria-pressed': 'false',
    onclick: () => {
      live.muted = !live.muted;
      if (live.muted) { live.turnAt = -1; trimPieces(); }
      note(live.muted ? 'The microphone is off. Press Unmute to talk again.' : '');
      setState(live.muted ? 'Muted' : 'Listening');
      paint();
    },
  });
  live.noteNode = el('p', { class: 'live-note', role: 'status', 'aria-live': 'polite' });
  live.panel = el('section', { class: 'card live-panel', 'aria-label': 'Talking live', 'data-state': 'starting' },
    el('p', { class: 'card-title', text: 'Talking live' }),
    el('div', { class: 'live-row' },
      live.stateNode,
      el('span', { class: 'live-level', 'aria-hidden': 'true' }, live.meterFill),
      live.muteButton,
      el('button', { class: 'live-action live-end', type: 'button', text: 'End', onclick: () => stop('The live talk has ended.') })),
    live.noteNode,
    el('p', { class: 'live-help', text: 'Speak when you are ready. Talk over it and it stops to listen.' }));
  appendCard(live.panel);
}

// ------------------------------------------------------------- start and stop
function silenceTheOtherVoice(on) {
  const bar = live.bar;
  if (bar) {
    if (on) bar.dataset.live = 'true';
    else delete bar.dataset.live;
  }
  const mic = $('voice-mic');
  if (mic) mic.disabled = on;
  if (!on) {
    if (live.restoreAloud) {
      const aloud = $('voice-aloud');
      if (aloud && aloud.getAttribute('aria-pressed') !== 'true') aloud.click();
      live.restoreAloud = false;
    }
    return;
  }
  try { document.dispatchEvent(new CustomEvent('signal:voice-stop')); } catch { /* an old browser */ }
  const stopButton = $('voice-stop');
  if (stopButton && typeof stopButton.click === 'function') stopButton.click();  // today's voice.js listens for the press
  const aloud = $('voice-aloud');
  if (aloud && aloud.getAttribute('aria-pressed') === 'true') {  // two voices reading one answer is nobody's idea of live
    aloud.click();
    live.restoreAloud = true;
  }
}

async function begin() {
  if (live.on || live.starting) return;  // asking for the microphone takes a moment: one press, one session
  live.starting = true;
  buildPanel();
  setState('Starting');
  try {
    await openMicrophone();
  } catch {
    live.starting = false;
    setState('Off');
    note('This browser would not give us the microphone. Check the microphone permission for this page.');
    return;
  }
  live.starting = false;
  live.on = true;
  live.muted = false;
  live.waiting = '';
  live.detect = createDetector();
  silenceTheOtherVoice(true);
  setState('Listening');
  note('');
  paint();
  live.ticker = setInterval(sample, SAMPLE_MS);
}

// Every way out of a live session comes through here: the End button, the button in the bar, a
// microphone we lost, a connection we lost, the chat panel closing, New chat, and the tab being
// out of sight for a minute. The microphone is handed back on all of them.
function stop(reason) {
  const was = live.on || live.starting;
  live.on = false;
  live.starting = false;
  stopSpeaking();
  if (live.ticker) { clearInterval(live.ticker); live.ticker = null; }
  if (live.hidden) { clearTimeout(live.hidden); live.hidden = null; }
  try { if (live.recorder && live.recorder.state !== 'inactive') live.recorder.stop(); } catch { /* already stopped */ }
  if (live.media) { try { live.media.getTracks().forEach((track) => track.stop()); } catch { /* already gone */ } }
  if (live.context) { try { live.context.close(); } catch { /* already closed */ } }
  live.media = null;
  live.context = null;
  live.analyser = null;
  live.recorder = null;
  live.pieces = [];
  live.header = null;
  live.turnAt = -1;
  live.level = 0;
  live.waiting = '';
  live.cutter = null;
  silenceTheOtherVoice(false);
  if (was) {
    setState('Off');
    if (reason) note(reason);
    if (live.muteButton) live.muteButton.disabled = true;
  }
  paint();
}

function controls(bar) {
  live.bar = bar;
  const mic = $('voice-mic');
  if (mic) {
    mic.setAttribute('title', TALK_HINT);
    mic.setAttribute('aria-describedby', 'live-hint-talk');
  }
  live.button = el('button', {
    class: 'live-talk', id: 'live-talk', type: 'button', text: 'Talk live', title: LIVE_HINT,
    'aria-pressed': 'false', 'aria-describedby': 'live-hint-live',
    onclick: () => { if (live.on || live.starting) stop('The live talk has ended.'); else begin(); },
  });
  const where = $('voice-state');  // the bar's last child stretches to the right: the buttons stay together
  if (where && where.parentNode === bar) bar.insertBefore(live.button, where);
  else bar.append(live.button);
  bar.append(el('span', { class: 'live-hidden', id: 'live-hint-talk', text: TALK_HINT }),
    el('span', { class: 'live-hidden', id: 'live-hint-live', text: LIVE_HINT }));
}

// voice.js builds the bar after its own status call, so it may not be there yet. If it never comes,
// one of ours goes in the same place and is styled to match.
function findBar(ready) {
  const found = $('voice-bar');
  if (found) { ready(found); return; }
  const composer = $('composer');
  if (!composer || !composer.parentNode) return;
  let done = false;
  const observer = typeof MutationObserver === 'function'
    ? new MutationObserver(() => {
      const bar = $('voice-bar');
      if (!bar || done) return;
      done = true;
      observer.disconnect();
      ready(bar);
    })
    : null;
  if (observer) observer.observe(composer.parentNode, { childList: true, subtree: true });
  setTimeout(() => {
    if (done) return;
    done = true;
    if (observer) observer.disconnect();
    const bar = $('voice-bar') || el('div', { class: 'voice-bar live-bar', id: 'live-bar', 'data-state': 'idle' });
    if (!bar.parentNode) composer.parentNode.insertBefore(bar, composer);
    ready(bar);
  }, 2500);
}

function exits() {
  const panel = $('chat-panel');
  if (panel && typeof MutationObserver === 'function') {
    new MutationObserver(() => { if (panel.hidden && live.on) stop(''); })
      .observe(panel, { attributes: true, attributeFilter: ['hidden'] });
  }
  const fresh = $('chat-new');
  if (fresh) fresh.addEventListener('click', () => stop(''));
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (live.on && !live.hidden) live.hidden = setTimeout(() => stop('The live talk ended while the page was away.'), HIDDEN_MS);
    } else if (live.hidden) {
      clearTimeout(live.hidden);
      live.hidden = null;
    }
  });
  window.addEventListener('pagehide', () => stop(''));
}

function styles() {
  if (document.querySelector('link[href="/live.css"]')) return;
  document.head.append(el('link', { rel: 'stylesheet', href: '/live.css' }));
}

async function start() {
  let ready = false;
  try {
    const response = await fetch('/api/live/status');
    ready = response.ok && Boolean((await response.json()).available);
  } catch { /* the bridge is not answering: the typed chat still works, live talk does not exist */ }
  if (!ready || !navigator.mediaDevices || typeof MediaRecorder === 'undefined'
      || !(window.AudioContext || window.webkitAudioContext)) return;
  styles();
  findBar((bar) => {
    controls(bar);
    paint();
  });
  watch();
  exits();
}

if (typeof document !== 'undefined' && document.getElementById('composer')) start();
