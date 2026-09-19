// Observation assistant — chat widget for the harness bridge (port 5195).
// No frameworks and no HTML strings: model and post text only ever becomes a text node.

// ---------------------------------------------------------------- pure logic
// Both functions below are exported so they can be tested without a browser.

// Server-sent-events reader. feed(chunk) returns the JSON payloads that became
// complete with this chunk: frames may be split anywhere, a chunk may hold
// several, comment lines (": ...") and unparsable payloads are dropped.
export function createFrameParser() {
  let buffer = '';
  return function feed(chunk) {
    buffer += chunk;
    const events = [];
    for (;;) {
      buffer = buffer.replace(/\r\n/g, '\n');
      const end = buffer.indexOf('\n\n');
      if (end === -1) break;
      const frame = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      for (const raw of frame.split('\n')) {
        const line = raw.replace(/\r$/, '');
        if (!line || line.startsWith(':') || !line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try {
          events.push(JSON.parse(payload));
        } catch {
          // a half-written frame that still ended in a blank line: ignore it
        }
      }
    }
    return events;
  };
}

// Assistant text -> blocks. Paragraphs split on blank lines, `- ` lines become
// list items, `**bold**` becomes a bold span. Nothing else is markup: every
// other character, including `<script>`, stays literal text.
export function formatBlocks(text) {
  const blocks = [];
  for (const chunk of String(text ?? '').split(/\n[ \t]*\n+/)) {
    const lines = chunk.split('\n').filter((line) => line.trim() !== '');
    let list = null;
    let paragraph = null;
    for (const line of lines) {
      const bullet = /^[ \t]*-[ \t]+(.*)$/.exec(line);
      if (bullet) {
        paragraph = null;
        if (!list) blocks.push((list = { type: 'ul', items: [] }));
        list.items.push(spans(bullet[1]));
      } else {
        list = null;
        if (!paragraph) blocks.push((paragraph = { type: 'p', lines: [] }));
        paragraph.lines.push(spans(line.trim()));
      }
    }
  }
  return blocks;
}

function spans(line) {
  const out = [];
  let rest = line;
  for (;;) {
    const match = /\*\*([^\n]+?)\*\*/.exec(rest);
    if (!match) break;
    if (match.index > 0) out.push({ text: rest.slice(0, match.index), bold: false });
    out.push({ text: match[1], bold: true });
    rest = rest.slice(match.index + match[0].length);
  }
  if (rest) out.push({ text: rest, bold: false });
  return out.length ? out : [{ text: '', bold: false }];
}

// ------------------------------------------------------------- DOM plumbing
const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'style') Object.assign(node.style, value);  // CSSOM, never a style attribute
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? '' : value);
  }
  node.append(...children.filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

const spanNodes = (parts) => parts.map((part) => (part.bold ? el('strong', { text: part.text }) : document.createTextNode(part.text)));

function blockNodes(text) {
  return formatBlocks(text).map((block) => {
    if (block.type === 'ul') return el('ul', { class: 'msg-list' }, ...block.items.map((item) => el('li', {}, ...spanNodes(item))));
    const lines = [];
    block.lines.forEach((line, index) => {
      if (index) lines.push(el('br'));
      lines.push(...spanNodes(line));
    });
    return el('p', {}, ...lines);
  });
}

function withBreaks(text) {
  const nodes = [];
  String(text ?? '').split('\n').forEach((line, index) => {
    if (index) nodes.push(el('br'));
    nodes.push(document.createTextNode(line));
  });
  return nodes;
}

const number = (value) => Number(value || 0).toLocaleString();

// ------------------------------------------------------------------- state
const SESSION_KEY = 'signal.observe.session';
const OFFLINE = 'Cannot reach the assistant on this laptop. Check that the bridge is running, then send the message again.';
const CHIPS = [
  'What data can I observe here?',
  'How did people react to the PlayStation cancellation around Sep 10?',
  'What did people say about Anthropic on Sep 9?',
];

const state = {
  running: false,
  open: false,
  pinned: true,        // false once the reader scrolls up: stop auto-scrolling
  bubble: null,        // { node, body, text } of the assistant bubble being streamed
  sawDelta: false,
  turnText: '',        // every delta of this turn, to compare with the final `message`
  turnBubbles: [],     // the bubbles this turn's deltas went into (a card can start a new one)
  progress: null,      // the progress line under the streaming bubble
  draft: null,         // the one "Project draft" card, reused by later spec events
};

const readSession = () => {
  try { return sessionStorage.getItem(SESSION_KEY); } catch { return null; }
};
const writeSession = (id) => {
  try { if (id) sessionStorage.setItem(SESSION_KEY, id); else sessionStorage.removeItem(SESSION_KEY); } catch { /* private mode */ }
};

function setStatus(text, stateName) {
  $('chat-status').textContent = text;
  $('chat-status').dataset.state = stateName;
}

function setRunning(running) {
  state.running = running;
  $('chat-send').disabled = running;
  if (running) setStatus('Thinking…', 'thinking');
  else if ($('chat-status').dataset.state !== 'offline') setStatus('Ready', 'ready');
}

// ------------------------------------------------------------ message list
function add(node) {
  $('chat-log').append(node);
  if (!state.open) $('launcher-dot').hidden = false;
  scroll();
  return node;
}

function scroll() {
  const log = $('chat-log');
  if (state.pinned) log.scrollTop = log.scrollHeight;
}

function dropChips() {
  const chips = $('chat-log').querySelector('.chips');
  if (chips) chips.remove();
}

function showChips() {
  if ($('chat-log').querySelector('.chips')) return;
  add(el('div', { class: 'chips' },
    el('p', { class: 'chips-lede', text: 'Ask it anything about what can be observed — or start from one of these.' }),
    ...CHIPS.map((chip) => el('button', {
      class: 'chip', type: 'button', text: chip,
      onclick: () => { if (!state.running) send(chip); },
    })),
  ));
}

function userBubble(text) {
  add(el('div', { class: 'msg msg-user' }, el('div', { class: 'bubble' }, ...withBreaks(text))));
}

function assistantBubble(text = '') {
  const body = el('div', { class: 'bubble' }, ...blockNodes(text));
  const node = add(el('div', { class: 'msg msg-assistant' }, body));
  return { node, body, text };
}

function setBubbleText(bubble, text) {
  bubble.text = text;
  bubble.body.replaceChildren(...blockNodes(text));
  scroll();
}

function setProgress(text) {
  if (text === null || text === undefined || text === '') {
    if (state.progress) { state.progress.remove(); state.progress = null; }
    return;
  }
  if (!state.progress) state.progress = add(el('div', { class: 'progress' }, el('i', { class: 'progress-dot' }), el('span', { class: 'progress-text' })));
  state.progress.querySelector('.progress-text').textContent = text;
  scroll();
}

function showError(text) {
  add(el('div', { class: 'notice', role: 'alert' }, ...withBreaks(text)));
}

// ------------------------------------------------------------------- cards
function card(title, ...body) {
  return el('section', { class: 'card' }, el('h3', { class: 'card-title', text: title }), ...body);
}

function previewCard(event) {
  const days = Array.isArray(event.per_day) ? event.per_day : [];
  const peak = days.reduce((max, day) => Math.max(max, Number(day.count) || 0), 0) || 1;
  const examples = (Array.isArray(event.examples) ? event.examples : []).slice(0, 6);
  const parts = [el('p', { class: 'card-total' }, el('strong', { text: number(event.total) }), document.createTextNode(' posts already collected'))];

  if (days.length) {
    parts.push(el('div', { class: 'bars' }, ...days.map((day) => el('div', { class: 'bar-row' },
      el('span', { class: 'bar-day', text: String(day.day ?? '') }),
      el('span', { class: 'bar-track' }, el('i', { class: 'bar-fill', style: { width: `${Math.max(2, ((Number(day.count) || 0) / peak) * 100)}%` } })),
      el('span', { class: 'bar-count', text: number(day.count) }),
    ))));
  }

  if (examples.length) {
    parts.push(el('ul', { class: 'examples' }, ...examples.map((post) => {
      const body = [
        el('p', { class: 'example-text' }, ...withBreaks(post.body ?? '')),
        el('p', { class: 'example-meta' }, document.createTextNode([post.lang, `${number(post.like_count)} likes`, post.day].filter(Boolean).join(' · '))),
      ];
      // A post's own text is never a link; only a url our own tools built, and only over https.
      const href = typeof post.url === 'string' && /^https:\/\//.test(post.url) ? post.url : null;
      return el('li', { class: 'example' }, href
        ? el('a', { class: 'example-link', href, target: '_blank', rel: 'noopener noreferrer' }, ...body)
        : el('div', {}, ...body));
    })));
  }

  if (typeof event.note === 'string' && event.note) parts.push(el('p', { class: 'card-note', text: event.note }));

  if (event.exact !== undefined || event.seconds !== undefined) {
    const bits = [];
    if (event.exact !== undefined) bits.push(event.exact ? 'exact count' : 'approximate count');
    if (typeof event.seconds === 'number') bits.push(`${event.seconds.toFixed(1)} s`);
    parts.push(el('p', { class: 'card-foot', text: bits.join(' · ') }));
  }
  return card(typeof event.title === 'string' && event.title ? event.title : 'Preview — real posts', ...parts);
}

function specCard(event) {
  const short = String(event.spec_hash ?? '').slice(0, 8);
  const json = (() => {
    try { return JSON.stringify(event.spec ?? {}, null, 2); } catch { return String(event.spec); }
  })();
  if (state.draft) {
    state.draft.summary.textContent = `Project draft${short ? ` · ${short}` : ''}`;
    state.draft.pre.textContent = json;
    scroll();
    return null;
  }
  const summary = el('summary', { class: 'card-title', text: `Project draft${short ? ` · ${short}` : ''}` });
  const pre = el('pre', { class: 'spec-json', text: json });
  state.draft = { summary, pre };
  return el('details', { class: 'card card-draft' }, summary, pre);
}

function confirmCard(event) {
  const countdown = el('p', { class: 'countdown' });
  const raw = Number(event.expires_ms) || 0;
  const deadline = raw > 1e12 ? raw : Date.now() + raw;  // absolute epoch ms, or a duration
  let timer = null;

  const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
  const answer = (approved) => {
    stop();
    confirmButton.disabled = true;
    cancelButton.disabled = true;
    countdown.textContent = approved ? 'Confirmed.' : 'Cancelled.';
    runTurn(`/api/sessions/${encodeURIComponent(readSession())}/confirm`, { confirmation_id: event.confirmation_id, approved });
  };

  const confirmButton = el('button', { class: 'primary', type: 'button', text: 'Confirm', onclick: () => answer(true) });
  const cancelButton = el('button', { class: 'secondary', type: 'button', text: 'Cancel', onclick: () => answer(false) });

  const tick = () => {
    const left = Math.max(0, deadline - Date.now());
    if (left <= 0) {
      stop();
      confirmButton.disabled = true;
      cancelButton.disabled = true;
      countdown.textContent = 'Expired — ask me to request confirmation again.';
      return;
    }
    const seconds = Math.ceil(left / 1000);
    countdown.textContent = `Expires in ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  };
  tick();
  timer = setInterval(tick, 250);

  return card('Confirm before it runs',
    el('p', { class: 'confirm-summary' }, ...withBreaks(event.summary ?? '')),
    countdown,
    el('div', { class: 'confirm-actions' }, confirmButton, cancelButton),
  );
}

// ------------------------------------------------------------------ events
function handleEvent(event) {
  if (!event || typeof event !== 'object') return;
  switch (event.type) {
    case 'delta': {
      if (typeof event.text !== 'string' || !event.text) return;
      state.sawDelta = true;
      state.turnText += event.text;
      if (!state.bubble) {
        state.bubble = assistantBubble('');
        state.turnBubbles.push(state.bubble);
      }
      setBubbleText(state.bubble, state.bubble.text + event.text);
      if (state.progress) state.bubble.node.after(state.progress);
      if (!state.open) $('launcher-dot').hidden = false;
      break;
    }
    case 'progress':
      setProgress(event.text);
      if (state.progress && state.bubble) state.bubble.node.after(state.progress);
      break;
    case 'preview':
      add(previewCard(event));
      state.bubble = null;  // later deltas start a new bubble below the card
      break;
    case 'spec': {
      const node = specCard(event);
      if (node) add(node);
      state.bubble = null;
      break;
    }
    case 'confirm_request':
      add(confirmCard(event));
      state.bubble = null;
      break;
    case 'message': {
      if (typeof event.text !== 'string') return;
      if (!state.sawDelta || !state.turnBubbles.length) {
        // no deltas arrived: the final text is all we have
        if (event.text.trim()) {
          state.bubble = assistantBubble(event.text);
          state.turnBubbles.push(state.bubble);
        }
      } else if (event.text !== state.turnText) {
        // the final text differs from what was streamed: keep the first bubble's
        // place in the list, put the whole text in it, drop any later ones
        const first = state.turnBubbles[0];
        for (const extra of state.turnBubbles.slice(1)) extra.node.remove();
        state.turnBubbles = [first];
        state.bubble = first;
        setBubbleText(first, event.text);
      }
      state.turnText = event.text;
      break;
    }
    case 'error':
      setProgress(null);
      showError(event.text || 'Something went wrong.');
      setRunning(false);
      break;
    case 'done':
      setProgress(null);
      setRunning(false);
      break;
    default:
      break;  // unknown event types are ignored on purpose
  }
}

// ------------------------------------------------------------------ network
async function ensureSession() {
  const existing = readSession();
  if (existing) return existing;
  let response;
  try {
    response = await fetch('/api/sessions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  } catch {
    throw new Error(OFFLINE);  // the bridge is not listening
  }
  if (!response.ok) throw new Error(await errorText(response, 'Could not start a session.'));
  const data = await response.json();
  if (!data.session_id) throw new Error('The server did not return a session id.');
  writeSession(data.session_id);
  return data.session_id;
}

async function errorText(response, fallback) {
  try {
    const data = await response.json();
    if (data && typeof data.error === 'string' && data.error) return data.error;
  } catch { /* not JSON */ }
  return `${fallback} (HTTP ${response.status})`;
}

async function runTurn(url, body) {
  setRunning(true);
  state.bubble = null;
  state.sawDelta = false;
  state.turnText = '';
  state.turnBubbles = [];
  setProgress(null);
  const parse = createFrameParser();
  try {
    const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!response.ok || !response.body) {
      if (response.status === 409) showError('Still working on the previous message.');
      else showError(await errorText(response, 'The assistant could not answer.'));
      setRunning(false);
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const event of parse(decoder.decode(value, { stream: true }))) handleEvent(event);
    }
    for (const event of parse(decoder.decode())) handleEvent(event);
  } catch {
    setProgress(null);
    setStatus('Offline', 'offline');
    showError(OFFLINE);
  }
  if (state.running) setRunning(false);
}

async function send(text) {
  const message = text.trim();
  if (!message || state.running) return;
  dropChips();
  userBubble(message);
  $('chat-input').value = '';
  grow();
  setRunning(true);
  let id;
  try {
    id = await ensureSession();
  } catch (error) {
    setStatus('Offline', 'offline');
    showError(error.message || OFFLINE);
    setRunning(false);
    return;
  }
  setStatus('Thinking…', 'thinking');
  await runTurn(`/api/sessions/${encodeURIComponent(id)}/messages`, { text: message });
}

// ------------------------------------------------------------------- widget
function grow() {
  const input = $('chat-input');
  input.style.height = 'auto';
  input.style.height = `${Math.min(140, input.scrollHeight)}px`;
}

function openPanel(prefill) {
  state.open = true;
  $('chat-panel').hidden = false;
  $('launcher').setAttribute('aria-expanded', 'true');
  $('launcher-dot').hidden = true;
  if (prefill) { $('chat-input').value = prefill; grow(); }
  $('chat-input').focus();
  state.pinned = true;
  scroll();
}

function closePanel() {
  state.open = false;
  $('chat-panel').hidden = true;
  $('launcher').setAttribute('aria-expanded', 'false');
  $('launcher').focus();
}

function newChat() {
  writeSession(null);
  state.bubble = null;
  state.draft = null;
  state.progress = null;
  state.sawDelta = false;
  state.turnText = '';
  state.turnBubbles = [];
  state.pinned = true;
  $('chat-log').replaceChildren();
  setRunning(false);
  setStatus('Ready', 'ready');
  showChips();
  $('chat-input').focus();
}

function start() {
  showChips();
  setStatus('Ready', 'ready');

  $('launcher').addEventListener('click', () => (state.open ? closePanel() : openPanel()));
  $('chat-close').addEventListener('click', closePanel);
  $('chat-new').addEventListener('click', newChat);
  $('open-chat').addEventListener('click', () => openPanel());
  $('open-chat-data').addEventListener('click', () => openPanel(CHIPS[0]));

  $('composer').addEventListener('submit', (event) => {
    event.preventDefault();
    send($('chat-input').value);
  });

  $('chat-input').addEventListener('input', grow);
  $('chat-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send($('chat-input').value);
    }
  });

  $('chat-log').addEventListener('scroll', () => {
    const log = $('chat-log');
    state.pinned = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && state.open) closePanel();
  });
}

if (typeof document !== 'undefined' && document.getElementById('chat-panel')) start();
