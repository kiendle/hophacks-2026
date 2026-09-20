// The audio brief, inside the chat. A tool result carrying {"_card": {"kind": "brief", ...}} arrives
// here as a card event; this file draws one card per brief, asks the product's own server how the brief
// is doing every three seconds in plain words, and shows the player and the story lines when it is
// ready. It never plays by itself: a browser blocks sound the person did not ask for, so the person
// presses play. The same brief seen twice (make_brief, then get_brief) updates the first card instead
// of adding another one.
//
// The brief API belongs to the Morning Brief half of the product, which shares this origin only once
// Signal runs as one app. While only the chat server is up, those addresses answer 404 and the card
// says so once and stops asking.
//
// Everything here is built on chat.js's four plug-in exports and nothing else, and every piece of text
// goes in as text, never as markup. The pure helpers below are exported so a test can check the words
// without a browser.
import { appendCard, el, onEvent, plainText } from '/chat.js';

export const CONFIG = { pollMs: 3000, maxPolls: 100, lostLimit: 3, stories: 8, line: 200, title: 120 };
// A headline is written from real posts, so a post's own text can reach it. Two marks matter here: a
// right to left override reverses every character after it and can make the whole card read backwards,
// and a zero width or control character is invisible while it is still there. Both go before the text
// is measured or shown. Whitespace collapses too, so one headline is always one line.
// Written as numbers on purpose: spelled as characters, this class would be a row of invisible marks
// sitting in the source of the guard that removes them, and one tidy edit would quietly open the hole.
const HIDDEN_RANGES = [[0, 8], [11, 31], [127, 159], [0x200b, 0x200f], [0x202a, 0x202e], [0x2060, 0x2069], [0xfeff, 0xfeff]];
const HIDDEN = new RegExp(`[${HIDDEN_RANGES.map(([low, high]) => String.fromCharCode(low, 0x2d, high)).join('')}]`, 'g');

export function readable(value, limit = CONFIG.line) {
  const clean = plainText(String(value ?? '').replace(HIDDEN, '').replace(/\s+/g, ' ')).trim();
  return clean.length > limit ? `${clean.slice(0, limit - 1).trimEnd()}…` : clean;
}

export const ONE_APP = 'Briefs work once Signal runs as one app.';
export const WAITING = 'Getting your brief ready.';
export const TOO_LONG = 'This is taking longer than usual. Ask me how your brief is doing.';
export const LOST = 'We lost touch with your brief. Ask me how it is doing.';
export const GONE = 'That brief could not be found. Ask me to make a new one.';
export const NO_AUDIO = 'There is no recording for this one. Ask me to read it out instead.';

// The server's own step, in words a person reads. Its wording is a sentence about what it is doing, so
// these match on what it contains rather than on the whole line.
const STEPS = [
  [/recording|voice|eleven ?labs/i, 'Recording the voice.'],
  [/writing the script|choosing the stories/i, 'Writing the script.'],
  [/engagement|bluesky|starting|collect/i, 'Choosing the stories.'],
];
const AUDIO_FILE = /^[a-z0-9-]+\.mp3$/;

export function statusWords(brief) {
  if (!brief || typeof brief !== 'object') return WAITING;
  const status = String(brief.status || '');
  if (status === 'ready') return 'Ready.';
  if (status === 'failed') return failureWords(brief.step);
  const step = String(brief.step || '');
  for (const [pattern, words] of STEPS) if (pattern.test(step)) return words;
  return WAITING;
}

// A failure the server wrote for itself can carry a stack or a class name. One plain line either way.
export function failureWords(step) {
  const text = readable(step, 400);
  if (!text || /error|exception|traceback|http\s*\d|[{}<>_]|\bnone\b|\bnull\b/i.test(text)) {
    return 'That did not work. Ask me to make a new one.';
  }
  return text.length > 160 ? 'That did not work. Ask me to make a new one.' : text;
}

export function storyLines(brief) {
  const lines = [];
  for (const segment of (brief && Array.isArray(brief.segments) ? brief.segments : [])) {
    const stories = segment && Array.isArray(segment.stories) ? segment.stories : [];
    for (const story of stories) {
      const line = readable(story && story.title);
      if (line) lines.push(line);
    }
    if (!stories.length) {
      const line = readable(segment && segment.headline);
      if (line) lines.push(line);
    }
  }
  return lines.slice(0, CONFIG.stories);
}

export function audioSrc(brief) {
  const id = String((brief && brief.id) || '');
  const file = String((brief && brief.audio && brief.audio.full) || '');
  if (!id || !AUDIO_FILE.test(file)) return null;
  return `/api/briefs/${encodeURIComponent(id)}/audio/${file}`;
}

// What the card says next, and whether to ask again. `answer` is what one read came back with and
// `state` carries the counts, so this is the one place the polling rules live.
export function decide(answer, state = {}) {
  const tries = Number(state.tries) || 0;
  const lost = Number(state.lost) || 0;
  if (answer && answer.missing) return { stop: true, words: answer.api ? GONE : ONE_APP };
  if (answer && answer.lost) {
    return lost >= CONFIG.lostLimit ? { stop: true, words: LOST } : { stop: false, words: '' };
  }
  const brief = (answer && answer.brief) || null;
  if (!brief) return { stop: false, words: '' };
  const status = String(brief.status || '');
  if (status === 'ready' || status === 'failed') return { stop: true, words: statusWords(brief), brief };
  if (tries >= CONFIG.maxPolls) return { stop: true, words: TOO_LONG, brief };
  return { stop: false, words: statusWords(brief), brief };
}

const sleep = (ms) => new Promise((done) => setTimeout(done, Math.max(0, ms)));

async function readBrief(id) {
  // The chat server answers "No such file" for an address it does not own, the product's own server
  // answers "No such brief": the difference is how we know which of the two is missing.
  try {
    const response = await fetch(`/api/briefs/${encodeURIComponent(id)}`, { headers: { Accept: 'application/json' } });
    if (response.status === 404) {
      let said = '';
      try { said = String(((await response.json()) || {}).error || ''); } catch { said = ''; }
      return { missing: true, api: /brief/i.test(said) };
    }
    if (!response.ok) return { lost: true };
    const brief = await response.json();
    return brief && typeof brief === 'object' ? { brief } : { lost: true };
  } catch {
    return { lost: true };
  }
}

export function briefCard(card) {
  const given = card && typeof card === 'object' ? card : {};
  const title = el('h3', { class: 'card-title brief-title', text: readable(given.title, CONFIG.title) || 'Your audio brief' });
  const status = el('p', { class: 'brief-status', text: WAITING });
  const player = el('div', { class: 'brief-player' });
  const stories = el('ul', { class: 'brief-stories' });
  const node = el('section', { class: 'card brief-card' }, title, status, player, stories);
  const view = {
    node,
    id: String(given.brief_id || given.id || '').trim(),
    tries: 0,
    lost: 0,
    stopped: false,
    words: WAITING,
    played: false,
    say(words) {
      const line = readable(words);
      if (!line) return;
      view.words = line;
      status.textContent = line;
    },
    name(text) {
      const line = readable(text, CONFIG.title);
      if (line) title.textContent = line;
    },
    update(brief) {
      view.name(brief && brief.title);
      const lines = storyLines(brief);
      if (lines.length) stories.replaceChildren(...lines.map((line) => el('li', { text: line })));
      if (String((brief || {}).status || '') !== 'ready' || view.played) return;
      const source = audioSrc(brief);
      view.played = true;
      if (!source) {
        player.replaceChildren(el('p', { class: 'brief-note', text: NO_AUDIO }));
        return;
      }
      player.replaceChildren(el('audio', { class: 'brief-audio', controls: true, preload: 'none', src: source }),
                             el('p', { class: 'brief-note', text: 'Press play when you are ready.' }));
      ready(view.id, source);
    },
    stop() { view.stopped = true; },
  };
  return view;
}

// The voice half of the product may want to read a brief out the moment it lands, and only it knows
// whether the person spoke to us. Saying it happened is this file's whole part in that.
function ready(id, source) {
  if (typeof document === 'undefined' || typeof CustomEvent !== 'function') return;
  document.dispatchEvent(new CustomEvent('signal-brief-ready', { detail: { briefId: id, url: source } }));
}

export async function follow(view) {
  while (!view.stopped) {
    view.tries += 1;
    const answer = await readBrief(view.id);
    view.lost = answer && answer.lost ? view.lost + 1 : 0;
    const next = decide(answer, view);
    // One unreadable answer must not end the following: the next read is usually fine, and a card
    // frozen on "Getting your brief ready" is worse than a card that missed one of its stories.
    if (answer && answer.brief) { try { view.update(answer.brief); } catch { /* keep asking */ } }
    view.say(next.words);
    if (next.stop) {
      view.stopped = true;
      break;
    }
    await sleep(CONFIG.pollMs);
  }
  return view.words;
}

const shown = new Map();  // brief id to its card, so the same brief seen twice updates one card

export function show(card) {
  const id = String((card && (card.brief_id || card.id)) || '').trim();
  if (!id) return null;
  const already = shown.get(id);
  if (already) {
    already.name(card.title);
    return already;
  }
  const view = briefCard(card);
  shown.set(id, view);
  appendCard(view.node);
  follow(view).catch(() => { view.stop(); });  // nothing here may reach the page as a broken promise
  return view;
}

// Named brief-card.css, not brief.css: the Morning Brief page already serves its own /brief.css, and
// on the one combined server the two would be the same address.
if (typeof document !== 'undefined' && !document.querySelector('link[href="/brief-card.css"]')) {
  document.head.append(el('link', { rel: 'stylesheet', href: '/brief-card.css' }));
}

onEvent((event) => {
  if (!event || event.type !== 'card') return;
  const card = event.card;
  if (card && typeof card === 'object' && card.kind === 'brief') show(card);
});
