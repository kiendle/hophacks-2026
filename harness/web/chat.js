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

// A semicolon, or a run of them (";;", "; ;"), is ONE full stop, and the word after it gets a capital.
// The whole run goes in one match, which is what makes the rule idempotent: matched one at a time,
// "a;;b" became "a. ;b" and only a second pass finished the job. A sentence that had already ended
// ("e.g.; x") or a line that opens with one does not get a second stop.
function unsemicolon(whole, tail, at, all) {
  const before = all.slice(0, at);
  const opensLine = before === '' || before.endsWith('\n');
  const stop = opensLine || /[.!?]$/.test(before) ? '' : '.';
  if (!tail) return stop;
  if ('.,!?'.includes(tail)) return stop ? tail : '';  // the mark that follows does the semicolon's job
  return (opensLine ? '' : `${stop} `) + tail.toUpperCase();
}

// Plain punctuation, the same rule steps.py enforces on the server: a person reads commas, full
// stops, "and" and "to", never a dash, a middle dot, an arrow or a semicolon. A plain hyphen with a
// space on each side counts as a dash too ("posts - most on Sep 10"), which is what a model reaches
// for once it has been told not to type an em dash, while a hyphen inside a word (well-known), a date
// (2026-09-10), a negative number (-5), a web address and a line that opens with "- " are left exactly
// as written. Idempotent, and never throws. steps.py's plain() is the same rule, pattern for pattern.
export function plainText(value) {
  if (typeof value !== 'string') return '';
  return value
    .replace(/[^\S\n]{2,}/g, ' ')  // first: each pattern below reads the spaces around its own mark,
    .replace(/[^\S\n]*(?:[←→↔⇒⇨➡]+|-{1,2}>|=>)[^\S\n]*/g, ' to ')  // so a long run would be rescanned by every one
    .replace(/[^\S\n]*[‒–—―]+[^\S\n]*/g, ', ')
    .replace(/[^\S\n]*[·•‣▪・]+[^\S\n]*/g, ', ')
    .replace(/(?<=\S)[^\S\n]+(?:-{1,2}[^\S\n]+)+(?=\S)/g, ', ')  // "a - b", and "a - - b" in one pass
    .replace(/[^\S\n]*(?:;[^\S\n]*)+(\S?)/g, unsemicolon)
    .replace(/[^\S\n]{2,}/g, ' ')
    .replace(/[^\S\n]+([,.])/g, '$1')
    .replace(/(?:,[^\S\n]*){2,}/g, ', ')
    .replace(/,[^\S\n]*\./g, '.')
    .replace(/^[^\S\n]*,[^\S\n]*|[^\S\n]*,[^\S\n]*$/gm, '');
}

// A step's own duration, one decimal under ten seconds: "3.5 seconds", "28 seconds", "1 minute 5 seconds".
export function durationWords(ms) {
  const value = ms === null || ms === undefined || ms === '' ? NaN : Number(ms);
  if (!Number.isFinite(value) || value < 0) return '';
  if (value >= 9950) return formatDuration(value);
  const seconds = Math.round(value / 100) / 10;
  return `${seconds} ${seconds === 1 ? 'second' : 'seconds'}`;
}

// A duration in words: "9 seconds", "1 minute 5 seconds", "2 minutes".
export function formatDuration(ms) {
  const value = ms === null || ms === undefined || ms === '' ? NaN : Number(ms);
  if (!Number.isFinite(value) || value < 0) return '';
  const total = Math.round(value / 1000);
  if (total < 60) return plural(total, 'second');
  const seconds = total % 60;
  return plural(Math.floor(total / 60), 'minute') + (seconds ? ` ${plural(seconds, 'second')}` : '');
}

// The one line a finished activity list collapses into: "4 steps, 28 seconds".
export function summarizeSteps(steps, totalMs) {
  const list = Array.isArray(steps) ? steps : [];
  if (!list.length) return 'No steps';
  const failed = list.filter((step) => step && step.ok === false).length;
  const parts = [plural(list.length, 'step')];
  if (failed) parts.push(`${failed} did not finish`);
  const duration = formatDuration(totalMs);
  if (duration) parts.push(duration);
  return parts.join(', ');
}

const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const DAYS_IN = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
const LANGUAGE_NAMES = {
  en: 'English', ja: 'Japanese', es: 'Spanish', pt: 'Portuguese', ko: 'Korean', fr: 'French',
  de: 'German', tr: 'Turkish', ar: 'Arabic', it: 'Italian', zh: 'Chinese', ru: 'Russian', nl: 'Dutch', hi: 'Hindi',
};
const SOURCE_NAMES = { bluesky_live: 'live Bluesky', twitter_firehose: 'the X/Twitter archive', congress: 'US Congress posts' };

const oneLine = (value, limit = 160) => (typeof value === 'string' || typeof value === 'number'
  ? plainText(String(value).replace(/\s+/g, ' ').trim()).slice(0, limit) : '');
const asObject = (value) => (value && typeof value === 'object' && !Array.isArray(value) ? value : {});

function dayParts(value, shift = 0) {
  const found = /^(\d{4})-(\d{2})-(\d{2})/.exec(typeof value === 'string' ? value.trim() : '');
  if (!found) return null;
  let [year, month, day] = [Number(found[1]), Number(found[2]), Number(found[3]) + shift];
  while (day < 1) {
    month -= 1;
    if (month < 1) { month = 12; year -= 1; }
    day += DAYS_IN[month - 1] + (month === 2 && year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 1 : 0);
  }
  if (month < 1 || month > 12 || day > 31) return null;
  return { year, month, day };
}

const dayWords = (value, shift = 0) => {
  const parts = dayParts(value, shift);
  return parts ? `${MONTHS[parts.month - 1]} ${parts.day}` : '';
};

// A bucket's label on the preview card: a date reads as Sep 10, the same as everywhere else on the
// page, while a live bucket, which is a clock time like 19:05 and not a date at all, is left alone.
const dayLabel = (value) => dayWords(value) || oneLine(value, 24);

// The project's end date is the first day NOT observed, so the last day shown is the day before it.
export function windowWords(from, to) {
  const low = dayWords(from);
  const high = dayWords(to, -1);
  const span = low && high && low !== high ? `${low} to ${high}` : (low || high);
  if (!span) return '';
  const parts = dayParts(to, -1) || dayParts(from);
  return parts ? `${span}, ${parts.year}` : span;
}

export function liveWords(lookbackHours, runHours) {
  const back = Number(lookbackHours);
  const run = Number(runHours);
  const said = ['From now'];
  if (Number.isFinite(back) && back > 0) said.push(`also looking back ${plural(back, 'hour')}`);
  if (Number.isFinite(run) && run > 0) said.push(`and it keeps running for ${plural(run, 'hour')}`);
  return said.join(', ');
}

function groupsOf(spec) {
  const raw = Array.isArray(spec.categories) ? spec.categories : (Array.isArray(spec.classification) ? spec.classification : []);
  return raw.slice(0, 12).map((group) => ({
    name: oneLine(group && typeof group === 'object' ? group.name : group, 60),
    description: oneLine(group && typeof group === 'object' ? (group.description ?? group.about) : '', 160),
  })).filter((group) => group.name);
}

function languageWords(value) {
  const codes = (Array.isArray(value) ? value : [value]).map((code) => oneLine(code, 16)).filter(Boolean);
  const names = codes.slice(0, 6).map((code) => LANGUAGE_NAMES[code.toLowerCase()] || code);
  return names.length ? names.join(', ') : 'Any language';
}

// The project card's own content: labelled plain lines, built from the saved draft and nothing else.
// The draft is written by the model, so every key may be spelled either way and every value is text.
export function projectLines(spec) {
  const root = asObject(spec);
  const observation = asObject(root.observation);
  const span = asObject(observation.window);
  const filter = asObject(root.filter);
  const source = oneLine(observation.source ?? root.source, 40);
  const words = (Array.isArray(root.keywords) ? root.keywords : (Array.isArray(filter.any_terms) ? filter.any_terms : []))
    .map((word) => oneLine(word, 40)).filter(Boolean).slice(0, 20);
  const live = source === 'bluesky_live' || span.mode === 'live';
  const groups = groupsOf(root);
  const lines = [
    { label: 'Name', value: oneLine(root.name, 120) },
    { label: 'What we are watching', value: oneLine(observation.intent ?? root.intent, 240) },
    { label: 'Where', value: SOURCE_NAMES[source] || oneLine(source.replace(/_/g, ' '), 40) },
    { label: 'When', value: live ? liveWords(span.lookback_hours, span.run_hours)
      : windowWords(span.from ?? root.date_from, span.to ?? root.date_to) },
    { label: 'Language', value: languageWords(root.language ?? observation.language ?? filter.languages) },
    { label: 'Words we search for', value: words.map((word) => `"${word}"`).join(', ') },
  ];
  if (groups.length) {
    lines.push({ label: 'Groups we sort posts into', groups,
      value: groups.map((group) => (group.description ? `${group.name}: ${group.description}` : group.name)).join('. ') });
  }
  lines.push({ label: 'Feeling question', value: oneLine(root.sentiment_question ?? asObject(root.sentiment).instructions, 240) });
  const said = lines.filter((line) => line.value);
  // "Language: Any language" on its own says nothing, so an empty draft returns no lines at all and the
  // card shows its own "nothing has been written down yet" instead of one meaningless row. The confirm
  // summary in demo_mcp_server.py makes exactly the same judgement.
  return said.some((line) => line.label !== 'Language') ? said : [];
}

const LIVE_DEFAULTS = { minutes: 15, seconds: 20 };
const span = (value, fallback) => (Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : fallback);

// The plain-words part of what the "details" link opens: the same facts the step was built from.
// A person who wants to know what was searched reads this; the exact request is shown below it.
// How long the step took is the last of them, once the step has ended. It is only ever read by
// someone who opened details: the row itself never shows a duration.
export function stepFacts(detail, ms) {
  const info = asObject(detail);
  const input = asObject(info.input);
  const tool = oneLine(info.tool, 60).replace('mcp__harness__', '');
  const searching = tool === 'preview_keywords' || tool === 'bluesky_recent' || tool === 'bluesky_listen';
  const words = (Array.isArray(input.keywords) ? input.keywords : []).map((word) => oneLine(word, 40)).filter(Boolean).slice(0, 20);
  const lines = [];
  if (words.length) lines.push({ label: 'Words searched', value: words.map((word) => `"${word}"`).join(', ') });
  if (tool === 'preview_keywords') {
    const low = dayWords(input.date_from);
    const high = dayWords(input.date_to, -1);
    const dates = low && high && low !== high ? `${low} to ${high}` : (low || high);
    if (dates) lines.push({ label: 'Dates', value: dates });
  }
  if (tool === 'bluesky_recent') lines.push({ label: 'Time covered', value: `the last ${plural(span(input.minutes, LIVE_DEFAULTS.minutes), 'minute')}` });
  if (tool === 'bluesky_listen') lines.push({ label: 'Time covered', value: `${plural(span(input.seconds, LIVE_DEFAULTS.seconds), 'second')} of live posts` });
  if (searching) lines.push({ label: 'Language', value: languageWords(input.language) });
  if (tool === 'save_draft') {
    let name = '';
    try { name = oneLine(asObject(JSON.parse(String(input.spec_json ?? ''))).name, 120); } catch { name = ''; }
    if (name) lines.push({ label: 'Project name', value: name });
  }
  const took = durationWords(ms);
  if (took) lines.push({ label: '', value: `Took ${took}` });
  return lines.filter((line) => line.value);
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

// ------------------------------------------------------------ example posts
// A post on the "What we found" card is someone else's words. It is never reworded (no plainText),
// never markup (text nodes only), and the only things in it that become links are addresses that
// pass the checks below.

// A web address inside a post. It stops at whitespace and at the characters that never belong to one.
const URL_IN_TEXT = /https?:\/\/[^\s<>"'`]+/gi;

// "See https://t.co/abc." ends a sentence, not an address: closing punctuation goes back to the text.
// A closing bracket stays only when the address opened one itself, as in .../Kojima_(band).
function trimAddress(raw) {
  let address = raw;
  for (;;) {
    const last = address.at(-1);
    if (!last) break;
    const opens = { ')': '(', ']': '[', '}': '{' }[last];
    if (opens ? address.split(opens).length >= address.split(last).length : !/[.,!?:;'"…»”’]/.test(last)) break;
    address = address.slice(0, -1);
  }
  return address;
}

// An address a person may click inside a post: http or https, a real host, nothing hidden before an
// "@". The label is built from the PARSED address (host and the first part of the path), so what is
// shown is where the link really goes, and a look-alike host reads as what it is.
export function webLink(raw) {
  if (typeof raw !== 'string' || raw.length > 2000) return null;
  let url;
  try { url = new URL(raw); } catch { return null; }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
  if (url.username || url.password || !url.hostname.includes('.')) return null;
  const host = url.hostname.replace(/^www\./, '');
  const path = url.pathname.split('/').filter(Boolean);
  const first = (path[0] || '').slice(0, 24);
  const more = path.length > 1 || (path[0] || '').length > 24 || url.search !== '' || url.hash !== '';
  return { href: url.href, label: `${host}${first ? `/${first}` : ''}${more ? '...' : ''}` };
}

// The link to the post itself. Only a url our own tools built is ever passed here, and even so only
// https to the three hosts a post can live on is let through: a look-alike host (x.com.evil.example),
// a port, a name before an "@", plain http, or any other scheme (javascript:, data:) gives no link.
const POST_HOSTS = { 'x.com': 'Open on X', 'twitter.com': 'Open on X', 'bsky.app': 'Open on Bluesky' };
export function postLink(value) {
  if (typeof value !== 'string' || value.length > 500) return null;
  let url;
  try { url = new URL(value); } catch { return null; }
  if (url.protocol !== 'https:' || url.username || url.password || url.port) return null;
  return Object.hasOwn(POST_HOSTS, url.hostname) ? { href: url.href, label: POST_HOSTS[url.hostname] } : null;
}

// A post split into what the page draws: plain text, and the addresses inside it. `cutTail` says the
// text was cut short by someone else, so an address that runs to its very end may be half an address
// and is left as text.
export function textParts(text, cutTail = false) {
  const value = typeof text === 'string' ? text : '';
  const parts = [];
  let at = 0;
  for (const match of value.matchAll(URL_IN_TEXT)) {
    const address = trimAddress(match[0]);
    const link = cutTail && match.index + match[0].length === value.length ? null : webLink(address);
    if (!link) continue;
    if (match.index > at) parts.push({ text: value.slice(at, match.index) });
    parts.push(link);
    at = match.index + address.length;
  }
  if (at < value.length) parts.push({ text: value.slice(at) });
  return parts;
}

// The short form of a post: never cut inside an address, inside a word when a space is near, or
// inside one emoji, and it ends in "..." whenever anything was left out. Nothing is ever hidden
// without saying so, which is why the card has no CSS line clamp.
export const SHORT_POST = 240;
export function excerpt(text, limit = SHORT_POST) {
  const value = typeof text === 'string' ? text : '';
  if (value.length <= limit) return value;
  let cut = limit;
  for (const match of value.matchAll(URL_IN_TEXT)) {
    if (match.index >= cut) break;
    const end = match.index + match[0].length;
    if (end > cut) { cut = match.index > 0 ? match.index : end; break; }
  }
  if (cut === limit && /\S/.test(value[cut] ?? ' ') && /\S/.test(value[cut - 1])) {
    const space = value.slice(0, cut).search(/\s\S*$/);
    if (space >= limit * 0.6) cut = space;
  }
  if (/[\uD800-\uDBFF]/.test(value[cut - 1] ?? '')) cut -= 1;
  const head = value.slice(0, cut).trimEnd();
  return head.length < value.trimEnd().length ? `${head}...` : value;
}

// ------------------------------------------------------------- DOM plumbing
// ?dev=1 adds one thing only: the Copy JSON button on the project card.
const DEV = (() => { try { return /(^|[?&])dev=1(&|$)/.test(String(location.search || '')); } catch { return false; } })();
const $ = (id) => document.getElementById(id);

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'style') Object.assign(node.style, value);  // CSSOM, never a style attribute
    else if (key === 'hidden') node.hidden = Boolean(value);     // the property, so toggling it later reads back
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
  'What are people saying about AI on Bluesky right now?',
  'How did people react to the Anthropic resignation post around Sep 9?',
  'What was said about GPT-6 Astra in early September?',
];
const DATA_QUESTION = 'What data can I observe here?';  // the landing page's own button

const state = {
  running: false,
  open: false,
  pinned: true,        // false once the reader scrolls up: stop auto-scrolling
  bubble: null,        // { node, body, text } of the assistant bubble being streamed
  sawDelta: false,
  turnText: '',        // every delta of this turn, to compare with the final `message`
  turnBubbles: [],     // the bubbles this turn's deltas went into (a card can start a new one)
  progress: null,      // the progress line under the streaming bubble
  activity: null,      // this turn's list of real steps, which replaces the progress line while it exists
  project: null,       // the one "Your project so far" card, rewritten in place by later spec events
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
    el('p', { class: 'chips-lede', text: 'Ask anything about how people are talking about AI, or start from one of these.' }),
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
  // even a message written by the server obeys the page's punctuation rule before a person reads it
  add(el('div', { class: 'notice', role: 'alert' }, ...withBreaks(plainText(String(text ?? '')))));
}

// ---------------------------------------------------------------- activity
// Every row is one real tool call. Closed, a row is its state mark, its title and a "details" link
// and nothing else, so a normal reader never meets a tool name or a piece of JSON. "details" opens,
// in this order: why the assistant did it, what came back (written by the server from the tool's
// real result), the same facts in plain words, and the exact request that was sent. The panel is
// built when it is opened and emptied when it is closed, so an unopened row holds none of it.
function detailText(detail) {
  try {
    const name = detail && typeof detail.tool === 'string' ? detail.tool : '(unknown tool)';
    return `${name}\n${JSON.stringify(detail && detail.input !== undefined ? detail.input : null, null, 2)}`;
  } catch {
    return '(the raw call could not be shown)';
  }
}

// `given` are the plain lines a tool module wrote on the server (steps.register_tool), for a tool this
// file knows nothing about. They come first; the facts computed here, and the duration, follow.
function factNodes(detail, ms, given) {
  const sent = (Array.isArray(given) ? given : [])
    .map((line) => ({ label: oneLine(asObject(line).label, 60), value: oneLine(asObject(line).value, 240) }))
    .filter((line) => line.value);
  return [...sent, ...stepFacts(detail, ms)].map((line) => el('p', { class: 'step-fact' },
    line.label ? el('span', { class: 'step-fact-label', text: `${line.label}: ` }) : null,
    document.createTextNode(plainText(line.value))));
}

// The two parts of an open panel that change when the step ends: the result and the facts.
function fillStepPanel(row) {
  if (!row.outcomeNode || !row.factsNode) return;  // closed
  row.outcomeNode.textContent = row.outcome === null ? 'Still working...' : row.outcome;
  row.outcomeNode.dataset.waiting = row.outcome === null ? 'true' : 'false';
  const facts = factNodes(row.detail, row.ms, row.facts);
  row.factsNode.replaceChildren(...facts);
  row.factsNode.hidden = !facts.length;
}

function openStepPanel(row) {
  row.outcomeNode = el('p', { class: 'step-outcome' });
  row.factsNode = el('div', { class: 'step-facts' });
  row.panel.replaceChildren(...[
    row.why ? el('p', { class: 'step-why' }, el('span', { class: 'step-why-label', text: 'Why: ' }), document.createTextNode(row.why)) : null,
    row.outcomeNode,
    row.factsNode,
    el('p', { class: 'step-raw-label', text: 'Exact request' }),
    el('pre', { class: 'step-raw', text: detailText(row.detail) }),  // textContent: the input is never markup
  ].filter(Boolean));
  fillStepPanel(row);
}

function closeStepPanel(row) {
  row.outcomeNode = null;
  row.factsNode = null;
  row.panel.replaceChildren();
}

let panelCount = 0;  // every panel needs its own id for aria-controls

// A step that worked says so with its mark and keeps the row to one line. A step that did NOT work
// has something a person needs to read, so its short result comes out onto the row, under the title,
// rather than waiting inside a panel nobody opened. While that panel is open it already carries the
// same sentence, so the row's copy steps aside and the reader never sees it twice.
function showFailure(row) {
  row.failure.textContent = row.ok === false ? row.outcome : '';
  row.failure.hidden = !(row.ok === false && row.panel.hidden);
}

function stepRow(event) {
  panelCount += 1;
  const panel = el('div', { class: 'step-panel', id: `step-panel-${panelCount}`, hidden: true });
  const failure = el('p', { class: 'step-failed', hidden: true });
  const row = {
    node: null, panel, failure, outcomeNode: null, factsNode: null, detail: event.detail, ok: null, ms: null,
    facts: Array.isArray(event.facts) ? event.facts : null,  // written by a tool module on the server
    outcome: null,  // null until the step ends
    why: typeof event.why === 'string' ? plainText(event.why.trim()) : '',
  };
  const details = el('button', {
    class: 'link-button step-details', type: 'button', text: 'details',
    'aria-expanded': 'false', 'aria-controls': `step-panel-${panelCount}`,
    onclick: () => {
      const open = panel.hidden;
      if (open) openStepPanel(row); else closeStepPanel(row);
      panel.hidden = !open;
      details.setAttribute('aria-expanded', open ? 'true' : 'false');
      showFailure(row);
      scroll();
    },
  });
  row.node = el('div', { class: 'step' },
    el('div', { class: 'step-head' },
      el('i', { class: 'step-mark', 'aria-hidden': 'true' }),
      el('span', { class: 'step-title', text: plainText(String(event.title || 'Working on it')) }),
      details),
    failure,
    panel);
  row.node.dataset.state = 'run';  // the mark is drawn from this, and it changes when the step ends
  return row;
}

function endStepRow(row, event) {
  row.ok = event.ok !== false;
  row.node.dataset.state = row.ok ? 'ok' : 'warn';
  row.outcome = plainText(String(event.outcome || 'Done.'));
  row.ms = event.ms;  // only known now
  showFailure(row);   // a closed row still tells the reader when its step did not work
  fillStepPanel(row); // an open panel is updated in place and stays open, a closed one stays empty
}

function startActivity() {
  const rows = el('div', { class: 'activity-rows' });
  const summary = el('button', {
    class: 'link-button activity-summary', type: 'button', 'aria-expanded': 'true', hidden: true,
    onclick: () => {
      const open = rows.hidden;
      rows.hidden = !open;
      summary.setAttribute('aria-expanded', open ? 'true' : 'false');
      scroll();
    },
  });
  const node = add(el('section', { class: 'activity', 'aria-label': 'What the assistant is doing' }, rows, summary));
  return { node, rows, summary, steps: [], byId: new Map(), started: Date.now() };
}

function collapseActivity(totalMs) {
  const activity = state.activity;
  if (!activity) return;
  const total = Number(totalMs);
  activity.summary.textContent = summarizeSteps(activity.steps, Number.isFinite(total) && total > 0 ? total : Date.now() - activity.started);
  activity.summary.hidden = false;
  activity.summary.setAttribute('aria-expanded', 'false');
  activity.rows.hidden = true;
  state.activity = null;
  scroll();
}

// ------------------------------------------------------------------- cards
function card(title, ...body) {
  return el('section', { class: 'card' }, el('h3', { class: 'card-title', text: title }), ...body);
}

function prettyJson(value) {
  try { return JSON.stringify(value ?? {}, null, 2); } catch { return String(value); }
}

// Developer view only: the project really is a JSON file on this laptop, so hand over the exact text.
function copyButton(text) {
  const note = el('span', { class: 'copy-note', role: 'status' });
  let timer = null;
  const button = el('button', {
    class: 'link-button', type: 'button', text: 'Copy JSON',
    onclick: async () => {
      if (timer) { clearTimeout(timer); timer = null; }
      try {
        if (!navigator || !navigator.clipboard || !navigator.clipboard.writeText) throw new Error('no clipboard');
        await navigator.clipboard.writeText(text());
        note.textContent = 'Copied';
        timer = setTimeout(() => { note.textContent = ''; }, 2000);
      } catch {
        note.textContent = 'Copying is not available here.';
      }
    },
  });
  return { button, note };
}

// Every link the card makes opens in a new tab and tells the other site nothing about this one.
function outsideLink(className, link, title) {
  return el('a', { class: className, href: link.href, target: '_blank', rel: 'noopener noreferrer', title, text: link.label });
}

// A post as nodes: its own words as text nodes (line breaks kept), its addresses as links.
function postNodes(text, cutTail) {
  return textParts(text, cutTail).flatMap((part) => (part.href ? [outsideLink('example-inline', part, part.href)] : withBreaks(part.text)));
}

let postCount = 0;  // every expandable text needs its own id for aria-controls

// One example post: the short text, then a row with likes and day, "Show full post" when there is
// more to show, and "Open on X" or "Open on Bluesky" when the post has an address we trust.
function examplePost(post) {
  const item = post && typeof post === 'object' ? post : {};
  const body = typeof item.body === 'string' ? item.body : '';
  const sent = typeof item.full_text === 'string' && item.full_text !== '';
  const whole = (sent ? item.full_text : body).slice(0, 2000);
  // no full text came with it, and the short one is as long as a cut one would be: its end may be half an address
  const cutTail = !sent && body.length >= SHORT_POST;
  const short = excerpt(whole);
  postCount += 1;
  const text = el('p', { class: 'example-text', id: `example-text-${postCount}` }, ...postNodes(short, cutTail));
  const row = [el('span', { class: 'example-meta', text: [`${number(item.like_count)} likes`, dayLabel(item.day)].filter(Boolean).join(', ') })];
  if (short !== whole) {
    const more = el('button', {
      class: 'link-button example-more', type: 'button', text: 'Show full post', 'aria-expanded': 'false', 'aria-controls': text.id,
      onclick: () => {
        const open = more.getAttribute('aria-expanded') !== 'true';
        text.replaceChildren(...postNodes(open ? whole : short, false));  // in place: the same paragraph
        more.textContent = open ? 'Show less' : 'Show full post';
        more.setAttribute('aria-expanded', open ? 'true' : 'false');
        // no scroll() here: the reader is looking at this post, so the page must not move under them
      },
    });
    row.push(more);
  }
  const link = postLink(item.url);
  if (link) row.push(outsideLink('example-open', link));
  return el('li', { class: 'example' }, text, el('div', { class: 'example-row' }, ...row));
}

function previewCard(event) {
  const days = Array.isArray(event.per_day) ? event.per_day : [];
  const peak = days.reduce((max, day) => Math.max(max, Number(day.count) || 0), 0) || 1;
  const examples = (Array.isArray(event.examples) ? event.examples : []).slice(0, 6);
  const parts = [el('p', { class: 'card-total' }, el('strong', { text: number(event.total) }),
    document.createTextNode(Number(event.total) === 1 ? ' post' : ' posts'))];

  if (days.length) {
    parts.push(el('div', { class: 'bars' }, ...days.map((day) => el('div', { class: 'bar-row' },
      el('span', { class: 'bar-day', text: dayLabel(day.day) }),
      el('span', { class: 'bar-track' }, el('i', { class: 'bar-fill', style: { width: `${Math.max(2, ((Number(day.count) || 0) / peak) * 100)}%` } })),
      el('span', { class: 'bar-count', text: number(day.count) }),
    ))));
  }

  if (examples.length) parts.push(el('ul', { class: 'examples' }, ...examples.map(examplePost)));

  if (typeof event.note === 'string' && event.note) parts.push(el('p', { class: 'card-note', text: plainText(event.note) }));

  if (event.exact !== undefined) {
    parts.push(el('p', { class: 'card-foot', text: event.exact ? 'We counted every matching post.' : 'This is an estimate.' }));
  }
  return card(plainText(typeof event.title === 'string' && event.title ? event.title : 'What we found'), ...parts);
}

// The project the assistant has built so far, in the reader's own words. One card per turn-set:
// a newer spec event rewrites these lines in place rather than stacking another card on the log.
function projectLineNode(line) {
  if (Array.isArray(line.groups)) {
    return el('div', { class: 'project-groups' },
      el('p', { class: 'project-line' }, el('span', { class: 'project-label', text: `${line.label}:` })),
      ...line.groups.map((group) => el('p', { class: 'project-group' },
        el('strong', { text: group.name }),
        document.createTextNode(group.description ? ` ${group.description}` : ''))));
  }
  return el('p', { class: 'project-line' },
    el('span', { class: 'project-label', text: `${line.label}: ` }),
    document.createTextNode(line.value));
}

function fillProject(body, spec) {
  const lines = projectLines(spec);
  body.replaceChildren(...(lines.length ? lines.map(projectLineNode)
    : [el('p', { class: 'project-line', text: 'Nothing has been written down yet.' })]));
}

function specCard(event) {
  if (state.project) {
    state.project.spec = event.spec;
    fillProject(state.project.body, event.spec);
    scroll();
    return null;
  }
  const body = el('div', { class: 'project-body' });
  state.project = { body, spec: event.spec };
  fillProject(body, event.spec);
  const copy = DEV ? copyButton(() => prettyJson(state.project.spec)) : null;
  return card('Your project so far', body,
    copy ? el('div', { class: 'card-tools' }, copy.button, copy.note) : null);
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
    countdown.textContent = approved ? 'You confirmed it.' : 'You cancelled it.';
    runTurn(`/api/sessions/${encodeURIComponent(readSession())}/confirm`, { confirmation_id: event.confirmation_id, approved }, 'confirm');
  };

  const confirmButton = el('button', { class: 'primary', type: 'button', text: 'Confirm', onclick: () => answer(true) });
  const cancelButton = el('button', { class: 'secondary', type: 'button', text: 'Cancel', onclick: () => answer(false) });

  const tick = () => {
    const left = Math.max(0, deadline - Date.now());
    if (left <= 0) {
      stop();
      confirmButton.disabled = true;
      cancelButton.disabled = true;
      countdown.textContent = 'This button has expired. Ask me to set it up again.';
      return;
    }
    countdown.textContent = `This button works for ${countdownWords(Math.ceil(left / 1000))}.`;
  };
  tick();
  timer = setInterval(tick, 250);

  return card('Confirm before it runs',
    el('p', { class: 'confirm-summary' }, ...withBreaks(plainText(event.summary ?? ''))),
    countdown,
    el('div', { class: 'confirm-actions' }, confirmButton, cancelButton),
  );
}

// "4 minutes 12 seconds", "12 seconds": the same words as everywhere else on the page.
function countdownWords(seconds) {
  if (seconds < 60) return plural(seconds, 'second');
  const rest = seconds % 60;
  return plural(Math.floor(seconds / 60), 'minute') + (rest ? ` ${plural(rest, 'second')}` : '');
}

// --------------------------------------------------------------- plug-in points
// An optional module (/voice.js, /analysis.js, /brief.js) is imported after start-up and builds on
// these four exports and nothing else: it watches the events of every turn, adds its own card to the
// current answer, and can send a message as if the person had typed it. A listener that throws is
// ignored, because a plug-in must never cost the chat a turn.
const listeners = [];

export function onEvent(fn) {
  if (typeof fn !== 'function') return () => {};
  listeners.push(fn);
  return () => {
    const at = listeners.indexOf(fn);
    if (at >= 0) listeners.splice(at, 1);
  };
}

function emit(event) {
  for (const fn of listeners.slice()) {
    try { fn(event); } catch { /* a broken plug-in is its own problem */ }
  }
}

// One card in the answer being written, in the order the cards arrived, exactly where the preview and
// project cards go. Later text starts a new bubble underneath it, as it does for every other card.
export function appendCard(node) {
  if (!node) return null;
  add(node);
  state.bubble = null;
  return node;
}

const PLUGINS = ['/voice.js', '/analysis.js', '/brief.js', '/realtime.js'];  // each one optional: a missing file is not an error

function loadPlugins() {
  for (const path of PLUGINS) {
    try { import(path).catch(() => {}); } catch { /* an environment without dynamic import */ }
  }
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
      if (state.activity) break;  // this turn shows its real steps instead of one line
      setProgress(event.text);
      if (state.progress && state.bubble) state.bubble.node.after(state.progress);
      break;
    case 'step': {
      if (event.phase === 'start') {
        if (!state.activity) {
          setProgress(null);
          state.activity = startActivity();
          state.bubble = null;  // the list sits above this turn's text: later deltas start a new bubble
        }
        const row = stepRow(event);
        state.activity.byId.set(String(event.id ?? ''), row);
        state.activity.steps.push(row);
        state.activity.rows.append(row.node);
        if (!state.open) $('launcher-dot').hidden = false;
        scroll();
      } else if (state.activity) {
        const row = state.activity.byId.get(String(event.id ?? ''));
        if (row) endStepRow(row, event);  // by id: parallel calls come back out of order
        scroll();
      }
      break;
    }
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
    case 'card':
      break;  // a plug-in draws its own kind of card with appendCard; this file draws none
    case 'error':
      setProgress(null);
      collapseActivity(event.duration_ms);
      showError(event.text || 'Something went wrong.');
      setRunning(false);
      break;
    case 'done':
      setProgress(null);
      collapseActivity(event.duration_ms);
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

async function runTurn(url, body, source = 'message') {
  emit({ type: 'turn_start', source });
  setRunning(true);
  state.bubble = null;
  state.sawDelta = false;
  state.turnText = '';
  state.turnBubbles = [];
  state.activity = null;  // a new turn starts a new list; the finished one stays where it is
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
      for (const event of parse(decoder.decode(value, { stream: true }))) receive(event);
    }
    for (const event of parse(decoder.decode())) receive(event);
  } catch {
    setProgress(null);
    setStatus('Offline', 'offline');
    showError(OFFLINE);
  }
  if (state.running) setRunning(false);
  emit({ type: 'turn_end' });
}

// The page acts on the event first, so a plug-in's card lands after what the event itself drew.
function receive(event) {
  handleEvent(event);
  emit(event);
}

export async function send(text) {
  const message = String(text ?? '').trim();
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
  state.project = null;
  state.progress = null;
  state.activity = null;
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
  $('open-chat-data').addEventListener('click', () => openPanel(DATA_QUESTION));

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

  loadPlugins();  // last: the page works whether or not any of them are there
}

if (typeof document !== 'undefined' && document.getElementById('chat-panel')) start();
