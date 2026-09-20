// The chart cards of the analysis stream.
//
// chat.js imports this file after start-up and hands it four things: onEvent, appendCard, el and
// plainText. Everything here is built from a {type: "card", card: {kind: "chart", ...}} event, whose
// title, caption and lines were written in plain words on the server (harness/analysis_tools.py), so
// this file only places them. Text always goes in as text, never as markup, and the picture's
// address has to be one of our own chart addresses before it is put on an image.
import { onEvent, appendCard, el, plainText } from './chat.js';

const STYLESHEET = '/analysis.css';
const PNG = /^\/api\/projects\/[a-z0-9_-]{1,60}\/charts\/[a-z0-9_-]{1,60}\.png$/;
const LINES = 3;

// The stylesheet lives in its own file because the page forbids inline styles.
function addStylesheet() {
  try {
    if (document.querySelector(`link[href="${STYLESHEET}"]`)) return;
    document.head.append(el('link', { rel: 'stylesheet', href: STYLESHEET }));
  } catch { /* a page without a head is not our problem */ }
}

// A project's name and its group names were typed by a person, so a card line can carry a right to
// left override that turns the rest of the line backwards. They come out here before anything is drawn.
const UNSEEN = new RegExp(`[${[[0x00, 0x08], [0x0b, 0x1f], [0x7f, 0x9f], [0xad, 0xad], [0x200b, 0x200f],
  [0x202a, 0x202e], [0x2060, 0x2064], [0x2066, 0x2069], [0xfeff, 0xfeff]]
  .map(([low, high]) => String.fromCharCode(low) + '-' + String.fromCharCode(high)).join('')}]`, 'g');
const words = (value, limit = 200) => (typeof value === 'string'
  ? plainText(value.replace(UNSEEN, '').replace(/\s+/g, ' ').trim()).slice(0, limit) : '');

// "Sep 9: minus 0.31, 412 posts", the three numbers on the card, each already written for a reader.
function pointLine(point) {
  const label = words(point && point.label, 60);
  const value = words(point && point.value, 60);
  const note = words(point && point.note, 60);
  if (!label && !value) return null;
  const tail = [value, note].filter(Boolean).join(', ');
  return el('p', { class: 'chart-point' },
    el('span', { class: 'chart-point-label', text: label ? `${label}: ` : '' }),
    document.createTextNode(tail));
}

function chartCard(card) {
  const title = words(card.title, 120) || 'A chart';
  const caption = words(card.caption, 240);
  const source = typeof card.png_url === 'string' && PNG.test(card.png_url) ? card.png_url : '';
  const points = (Array.isArray(card.points) ? card.points : []).slice(0, LINES).map(pointLine).filter(Boolean);
  const picture = source
    ? el('img', {
      class: 'chart-picture', src: source, alt: caption || title, loading: 'lazy', decoding: 'async',
      // A picture that cannot be drawn leaves the sentence and the numbers standing on their own.
      onerror: (event) => { event.target.hidden = true; },
    })
    : null;
  return el('div', { class: 'card chart-card' },
    el('p', { class: 'card-title', text: title }),
    picture,
    caption ? el('p', { class: 'chart-caption', text: caption }) : null,
    points.length ? el('div', { class: 'chart-points' }, ...points) : null);
}

addStylesheet();

onEvent((event) => {
  if (!event || event.type !== 'card') return;
  const card = event.card;
  if (!card || typeof card !== 'object' || card.kind !== 'chart') return;  // another plug-in's card
  appendCard(chartCard(card));
});
