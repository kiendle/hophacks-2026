const $ = (id) => document.getElementById(id);
const HOT = new Set(['alarmed', 'angry']);
const WARM = new Set(['excited', 'celebratory', 'amused']);
let status;
let shown;        // the brief on screen
let watching;     // id of a brief still being made
let speaking = false;
let toggling = false;  // a /api/collector request is in flight; poll() must not re-enable the button under it

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? '' : value);
  }
  node.append(...children.filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, options.body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, ...options, body: JSON.stringify(options.body) } : options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (HTTP ${response.status}).`);
  return data;
}

const number = (value) => Number(value || 0).toLocaleString();
const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const stamp = (ms) => new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const day = (ms) => new Date(ms).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
const mmss = (seconds) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;

function renderSource(live, paused) {
  $('source-bar').hidden = !status.source;  // a server started before this feature reports no source
  if (!status.source) return;
  $('source-network').textContent = status.source.network;
  $('source-stream').textContent = status.source.stream;
  $('source-host').textContent = status.source.host;
  $('source-appview').textContent = status.engagement_host;
  $('source-live').textContent = paused
    ? 'Stopped — nothing is being collected. Posts already kept stay available for a brief.'
    : live.state !== 'live' || !live.event_ms ? (live.state === 'connecting' ? 'Connecting…' : 'Reconnecting…')
      : `Receiving posts · last one at ${stamp(live.event_ms)} · ${Math.round(live.lag_s)} s behind live`;
  $('pipeline').classList.toggle('paused', paused);
  $('collect-toggle').textContent = paused ? 'Resume collecting' : 'Stop collecting';
  $('collect-toggle').className = paused ? 'primary' : 'secondary';
  $('collect-toggle').disabled = toggling;
}

function renderStatus() {
  const { live, backfill, queued, interests, paused } = status;
  const catchingUp = Boolean(backfill) || queued > 0;
  $('connection').dataset.state = paused ? 'stopped' : live.state !== 'live' ? 'offline' : catchingUp ? 'catching-up' : 'live';
  $('connection-text').textContent = paused ? 'Stopped' : live.state !== 'live' ? 'Reconnecting' : catchingUp ? `Live · replaying the past ${Math.round((backfill?.fraction || 0) * 100)}%` : 'Live · collecting';
  renderSource(live, paused);

  $('interests').replaceChildren(...interests.map((interest) => el('li', {},
    el('strong', { text: interest.name }),
    el('span', { class: 'count', text: `${number(interest.posts)} posts${interest.covered_from ? ` since ${clock(interest.covered_from)}` : ' · replaying'}` }),
    el('button', { type: 'button', 'aria-label': `Stop following ${interest.name}`, text: '×', onclick: () => unfollow(interest) }),
    el('span', { class: 'terms', text: interest.terms.join(' · ') }),
  )));

  $('backfill').hidden = !backfill;
  if (backfill) {
    $('backfill-fill').style.width = `${Math.max(2, backfill.fraction * 100)}%`;
    const remaining = backfill.fraction > 0.05 ? ` · about ${Math.max(1, Math.round(backfill.elapsed_s * (1 - backfill.fraction) / backfill.fraction / 60))} min left` : '';
    $('backfill-text').textContent = `Replaying the last ${status.backfill_hours} hours for ${backfill.interests.join(', ')}: ${number(backfill.scanned)} posts scanned, ${number(backfill.matched)} kept${remaining}. You can make a brief now; it will cover what has arrived.`;
  }

  $('scanned').textContent = number(live.scanned + live.replayed);
  $('kept').textContent = number(status.posts);
  const floor = interests.length && interests.every((interest) => interest.covered_from) ? Math.max(...interests.map((interest) => interest.covered_from)) : null;
  $('coverage').textContent = !interests.length ? 'Nothing followed' : floor ? `${((Date.now() - floor) / 3600000).toFixed(1)} h · since ${clock(floor)}` : 'Catching up';

  const busy = Boolean(status.working) && watching === status.working;
  $('make').disabled = !interests.length || busy;
  $('brief-hint').textContent = status.brief_at
    ? `One is made automatically every day at ${status.brief_at}. Or make one now.`
    : 'Pick how far back to look and how long to listen. Set BRIEF_AT=07:30 in .env to have one waiting every morning.';
}

async function poll() {
  try {
    status = await api('/api/status');
    renderStatus();
    if (status.working && status.working !== watching) watch(status.working);  // e.g. the scheduled morning brief
  } catch {
    $('connection').dataset.state = 'offline';
    $('connection-text').textContent = 'Server unreachable';
  }
}

async function unfollow(interest) {
  await api(`/api/interests/${encodeURIComponent(interest.id)}`, { method: 'DELETE' });
  $('interest-feedback').textContent = `Stopped following ${interest.name}; its posts were dropped.`;
  poll();
}

$('interest-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const query = $('query').value.trim();
  if (!query) return;
  $('follow').disabled = true;
  $('interest-feedback').textContent = 'Choosing search terms…';
  try {
    const { interest, note } = await api('/api/interests', { body: { query } });
    $('query').value = '';
    $('interest-feedback').textContent = note || `Following ${interest.name}. New posts match from now on, and the last ${status.backfill_hours} hours are being replayed.`;
  } catch (error) {
    $('interest-feedback').textContent = error.message;
  }
  $('follow').disabled = false;
  poll();
});

$('collect-toggle').addEventListener('click', async () => {
  const stopping = !status.paused;
  toggling = true;
  $('collect-toggle').disabled = true;
  try {
    status = await api('/api/collector', { body: { action: stopping ? 'stop' : 'start' } });
    if (!stopping) $('interest-feedback').textContent = 'Collecting again — the time you were stopped is being replayed.';
  } catch (error) {
    $('interest-feedback').textContent = error.message;
  }
  toggling = false;
  renderStatus();
});

$('make').addEventListener('click', async () => {
  stopSpeaking();
  try {
    const { id } = await api('/api/briefs', { body: { hours: Number($('hours').value), seconds: Number($('seconds').value), voice: $('voice').value || undefined } });
    watch(id);
  } catch (error) {
    progress(error.message, 'failed');
  }
});

function progress(text, state = 'working') {
  $('brief-progress').hidden = !text;
  $('brief-progress').textContent = text;
  $('brief-progress').dataset.state = state;
}

async function watch(id) {
  watching = id;
  $('make').disabled = true;
  while (watching === id) {
    let brief;
    try { brief = await api(`/api/briefs/${id}`); } catch (error) { progress(error.message, 'failed'); break; }
    if (brief.status === 'working') progress(`${brief.step}…`);
    else if (brief.status === 'failed') { progress(brief.step, 'failed'); break; }
    else { progress(''); show(brief); loadEarlier(); break; }
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
  watching = undefined;
  poll();
}

function sourcePost(post) {
  return el('li', {},
    el('div', { class: 'who' }, el('b', { text: post.author || post.handle }), el('span', { text: `@${post.handle}` }), el('span', { text: `${day(post.t)} ${clock(post.t)}` }),
      el('a', { href: post.url, target: '_blank', rel: 'noopener noreferrer', text: 'Open on Bluesky ↗' })),
    el('p', { class: 'said', text: post.text }),
    post.link?.title && el('p', { class: 'counts', text: `↗ ${post.link.title}` }),
    el('p', { class: 'counts', text: `${number(post.likes)} likes · ${number(post.reposts)} reposts · ${number(post.quotes)} quotes · ${number(post.replies)} replies` }),
  );
}

function show(brief) {
  shown = brief;
  const usage = brief.usage || {};
  const runtime = brief.estimated_seconds && `about ${mmss(brief.estimated_seconds)} spoken`;
  const cost = [runtime, brief.made_in_s && `made in ${Math.round(brief.made_in_s)} s`, usage.model && `written by ${usage.model}`,
    usage.input_tokens && `${number(usage.input_tokens)} in / ${number(usage.output_tokens)} out tokens`,
    usage.usd && `≈ $${usage.usd.toFixed(3)}`, usage.tts_characters && `${number(usage.tts_characters)} characters voiced${brief.audio?.voice ? ` by ${brief.audio.voice}` : ''}`].filter(Boolean).join(' · ');
  const spoken = brief.segments.map((segment) => segment.script).join('\n\n');

  const player = el('div', { class: 'player' });
  if (brief.audio) {
    player.append(el('audio', { id: 'audio', src: `/api/briefs/${brief.id}/audio/${brief.audio.full}`, controls: true, preload: 'metadata' }),
      el('p', { class: 'helper' }, el('a', { href: `/api/briefs/${brief.id}/audio/${brief.audio.full}`, download: `morning-brief-${brief.id}.mp3`, text: 'Download the episode (MP3)' })));
  } else {
    player.append(el('div', { class: 'brief-actions' }, el('button', { class: 'primary', id: 'read-aloud', type: 'button', text: 'Read it aloud', onclick: () => (speaking ? stopSpeaking() : readAloud()) })),
      el('p', { class: 'helper', text: 'No recorded audio for this brief, so your browser\'s built-in voice reads the script.' }));
  }

  $('brief').replaceChildren(
    el('div', { class: 'brief-head' },
      el('p', { class: 'eyebrow', text: `${day(brief.created).toUpperCase()} · ${clock(brief.created)} · LAST ${brief.hours} HOUR${brief.hours === 1 ? '' : 'S'}` }),
      el('h2', { text: brief.title }),
      el('p', { class: 'meta', text: [`${brief.segments.length} topic${brief.segments.length === 1 ? '' : 's'}`, cost].filter(Boolean).join(' · ') })),
    player,
    ...brief.notes.map((note) => el('p', { class: 'notice', text: note })),
    ...brief.segments.map((segment) => el('article', { class: 'segment' },
      el('p', { class: 'eyebrow', text: segment.topic.toUpperCase() }),
      el('h3', { text: segment.headline }),
      el('p', { class: 'meta', text: `${number(segment.posts_collected)} posts from ${number(segment.distinct_authors)} people in the window` }),
      ...segment.stories.map((story) => el('section', { class: 'story' },
        el('div', { class: 'story-top' }, el('h4', { text: story.title }), el('span', { class: 'mood', dataset: { tone: HOT.has(story.mood) ? 'hot' : WARM.has(story.mood) ? 'warm' : 'plain' }, text: story.mood })),
        el('p', { text: story.summary }),
        el('p', { class: 'why', text: story.why_it_matters }),
        el('ul', { class: 'sources' }, ...story.posts.map(sourcePost)))),
      el('details', { class: 'script' }, el('summary', { text: 'What the host says' }), el('p', { text: segment.script })))),
  );
  $('brief').hidden = false;

  function readAloud() {
    // Long utterances are cut off in Chromium, so speak one sentence at a time.
    stopSpeaking();
    speaking = true;
    $('read-aloud').textContent = 'Stop reading';
    const sentences = spoken.match(/[^.!?]+[.!?]+["')\]]*|[^.!?]+$/g) || [spoken];
    sentences.forEach((sentence, position) => {
      const utterance = new SpeechSynthesisUtterance(sentence.trim());
      if (position === sentences.length - 1) utterance.onend = stopSpeaking;
      speechSynthesis.speak(utterance);
    });
  }
}

function stopSpeaking() {
  if ('speechSynthesis' in window) speechSynthesis.cancel();
  speaking = false;
  if ($('read-aloud')) $('read-aloud').textContent = 'Read it aloud';
}

async function loadEarlier() {
  const { briefs } = await api('/api/briefs');
  const ready = briefs.filter((brief) => brief.status === 'ready');
  $('earlier').hidden = ready.length < 2 && (!ready.length || shown?.id === ready[0].id);
  $('earlier-list').replaceChildren(...ready.map((brief) => el('li', {}, el('button', { type: 'button', onclick: async () => { stopSpeaking(); show(await api(`/api/briefs/${brief.id}`)); $('brief').scrollIntoView({ behavior: 'smooth' }); } },
    el('b', { text: brief.title }), el('span', { text: `${day(brief.created)} ${clock(brief.created)} · ${brief.hours} h${brief.audio ? ' · audio' : ''}` })))));
  return ready;
}

function setupLength() {
  const input = $('seconds');
  input.min = status?.min_seconds || 45;
  input.max = status?.max_seconds || 300;
  let remembered;
  try { remembered = Number(localStorage.getItem('brief.seconds')); } catch { /* storage can be blocked; the default length still works */ }
  input.value = remembered >= Number(input.min) && remembered <= Number(input.max) ? String(remembered) : String(status?.default_seconds || 180);
  const label = () => { $('seconds-value').textContent = mmss(Number(input.value)); };
  input.addEventListener('input', label);
  input.addEventListener('change', () => { try { localStorage.setItem('brief.seconds', input.value); } catch { /* see above */ } });
  label();
}

async function loadVoices() {
  const { voices, default: fallback } = await api('/api/voices');
  if (!status?.elevenlabs || !voices.length) return;
  let remembered;
  try { remembered = localStorage.getItem('brief.voice'); } catch { /* storage can be blocked; the default voice still works */ }
  $('voice').replaceChildren(...voices.map((voice) => el('option', { value: voice.id, text: `${voice.name} · ${voice.note}` })));
  $('voice').value = voices.some((voice) => voice.id === remembered) ? remembered : voices.some((voice) => voice.id === fallback) ? fallback : voices[0].id;
  $('voice').hidden = $('hear').hidden = false;
  const sample = new Audio();
  $('voice').addEventListener('change', () => { sample.pause(); try { localStorage.setItem('brief.voice', $('voice').value); } catch { /* see above */ } });
  $('hear').addEventListener('click', () => {
    if (!sample.paused) { sample.pause(); return; }
    sample.src = `/api/voices/${$('voice').value}/preview`;
    sample.play().catch(() => {});
  });
}

window.addEventListener('beforeunload', stopSpeaking);
await poll();
$('hours').value = [6, 8, 10, 24].includes(status?.default_hours) ? String(status.default_hours) : '8';
setupLength();
loadVoices().catch(() => {});
const ready = await loadEarlier().catch(() => []);
if (status?.working) watch(status.working);
else if (ready.length) show(await api(`/api/briefs/${ready[0].id}`));
setInterval(poll, 2000);
