const STORAGE_KEY = 'signal.jetstream.session.v1';
const MAX_POSTS = 200;
const ENDPOINT = 'wss://jetstream.us-west.bsky.network/xrpc/network.bsky.jetstream.subscribeEvents';
const $ = (id) => document.getElementById(id);
const filters = new Map();
let posts = new Map();
let received = 0;
let cursor;
let socket;
let reconnectTimer;
let retryDelay = 1000;
let stopped = false;
let paused = false;
let dirty = false;
let storageWarning = '';
let meter = { costUsd: 0, inputTokens: 0, chargedRequests: 0, scored: 0, failed: 0, unknownCosts: 0 };
const jobs = new Map();
const inFlight = new Set();
const MAX_CONCURRENT = 6;

function showNotice(message) {
  $('notice').textContent = message;
  $('notice').hidden = !message;
}

function matchingFilter(text) {
  const normalized = text.toLowerCase();
  for (const [needle, label] of filters) {
    if (normalized.includes(needle)) return label;
  }
  return null;
}

function renderFilters() {
  const fragment = document.createDocumentFragment();
  for (const [needle, label] of filters) {
    const item = document.createElement('li');
    const text = document.createElement('span');
    text.textContent = label;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.dataset.filter = needle;
    remove.setAttribute('aria-label', `Remove filter “${label}”`);
    remove.textContent = '×';
    item.append(text, remove);
    fragment.append(item);
  }
  $('filters').replaceChildren(fragment);
  $('filter-count').textContent = `${filters.size.toLocaleString()} active ${filters.size === 1 ? 'filter' : 'filters'} · Match ANY`;
}

try {
  const saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || 'null');
  if (saved && Array.isArray(saved.posts)) {
    paused = saved.paused === true;
    // Migrate the previous single-filter session once; future writes store only filters.
    const savedFilters = Array.isArray(saved.filters) ? saved.filters : [saved.query];
    for (const value of savedFilters) {
      if (typeof value === 'string' && value.trim()) {
        const label = value.trim();
        filters.set(label.toLowerCase(), label);
      }
    }
    for (const post of saved.posts.slice(0, MAX_POSTS)) {
      if (typeof post.text === 'string' && typeof post.did === 'string' &&
          typeof post.rkey === 'string' && typeof post.receivedAt === 'number') {
        if (!post.matchedFilter) post.matchedFilter = matchingFilter(post.text);
        posts.set(`at://${post.did}/app.bsky.feed.post/${post.rkey}`, post);
      }
    }
    if (saved.meter && Object.keys(meter).every(key => Number.isFinite(saved.meter[key]) && saved.meter[key] >= 0)) {
      meter = saved.meter;
    }
    for (const job of saved.jobs || []) {
      if (typeof job?.requestId === 'string' && typeof job.key === 'string' && typeof job.text === 'string') {
        jobs.set(job.requestId, job);
      }
    }
  }
} catch {
  storageWarning = 'Session storage could not be read. New matches will still appear here.';
}

function persist() {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ paused, filters: [...filters.values()], posts: [...posts.values()], jobs: [...jobs.values()], meter }));
    storageWarning = '';
    $('storage-state').textContent = 'Session storage';
  } catch {
    storageWarning = 'Session storage is unavailable or full. Matches are shown in memory but may not survive a reload.';
    $('storage-state').textContent = 'Memory only';
  }
  showNotice(storageWarning);
}

function enqueue(key, post) {
  if (post.analysis?.status === 'done' || post.analysis?.status === 'error') return;
  const requestId = post.analysis?.requestId || crypto.randomUUID();
  post.analysis = { status: 'pending', requestId };
  jobs.set(requestId, { requestId, key, text: post.text });
  dirty = true;
}

async function analyze(job) {
  let result;
  let succeeded = false;
  try {
    const response = await fetch('/api/sentiment', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requestId: job.requestId, text: job.text }),
      signal: AbortSignal.timeout(45000),
    });
    result = await response.json();
    succeeded = response.ok && Number.isFinite(result.sentiment?.score) &&
      Number.isFinite(result.cost?.usd) && Number.isSafeInteger(result.usage?.inputTokens);
    if (!succeeded && !result.error) throw new Error('Incomplete sentiment response');
  } catch {
    result = { error: 'The sentiment request could not be completed. Its provider cost is unknown.', costUnknown: true };
  }
  if (Number.isFinite(result.cost?.usd) && Number.isSafeInteger(result.usage?.inputTokens)) {
    meter.costUsd += result.cost.usd;
    meter.inputTokens += result.usage.inputTokens;
    meter.chargedRequests++;
  }
  if (result.costUnknown) meter.unknownCosts++;
  if (succeeded) meter.scored++;
  else meter.failed++;
  const post = posts.get(job.key);
  if (post?.analysis?.requestId === job.requestId && post.text === job.text) {
    post.analysis = { ...result, requestId: job.requestId, status: succeeded ? 'done' : 'error' };
  }
  jobs.delete(job.requestId);
  inFlight.delete(job.requestId);
  // Checkpoint receipt removal and usage together; reloads never re-meter a completed job.
  persist();
  dirty = true;
  pump();
}

function pump() {
  if (stopped || paused) return;
  const next = [];
  for (const job of jobs.values()) {
    if (inFlight.size >= MAX_CONCURRENT) break;
    if (inFlight.has(job.requestId)) continue;
    inFlight.add(job.requestId);
    const post = posts.get(job.key);
    if (post?.analysis?.requestId === job.requestId) post.analysis.status = 'running';
    next.push(job);
  }
  if (!next.length) return;
  persist();
  dirty = true;
  for (const job of next) void analyze(job);
}

function sentimentCard(post) {
  const analysis = post.analysis;
  const section = document.createElement('section');
  section.className = 'sentiment';
  section.setAttribute('aria-label', 'Jev sentiment and cost');
  const heading = document.createElement('div');
  heading.className = 'sentiment-heading';
  const label = document.createElement('strong');
  const value = document.createElement('span');
  heading.append(label, value);
  section.append(heading);
  if (analysis?.status === 'done') {
    const score = analysis.sentiment.score;
    label.textContent = score < -0.25 ? 'Negative' : score > 0.25 ? 'Positive' : 'Neutral / mixed';
    value.textContent = `${score > 0 ? '+' : ''}${score.toFixed(2)}`;
    section.dataset.tone = score < -0.25 ? 'negative' : score > 0.25 ? 'positive' : 'neutral';
    const scale = document.createElement('meter');
    scale.min = -1;
    scale.max = 1;
    scale.low = -0.25;
    scale.high = 0.25;
    scale.optimum = 1;
    scale.value = score;
    scale.setAttribute('aria-label', `Sentiment ${score.toFixed(2)}, from negative minus one to positive plus one`);
    const legend = document.createElement('div');
    legend.className = 'sentiment-legend';
    for (const text of ['−1 Negative', '0 Neutral', '+1 Positive']) {
      const span = document.createElement('span');
      span.textContent = text;
      legend.append(span);
    }
    const confidence = document.createElement('p');
    confidence.className = 'sentiment-detail';
    confidence.textContent = `${analysis.sentiment.model} · ${(analysis.sentiment.confidence * 100).toFixed(0)}% model confidence · Not a factual judgment`;
    section.append(scale, legend, confidence);
  } else {
    label.textContent = analysis?.status === 'error' ? 'Sentiment unavailable' : analysis?.status === 'running' ? 'Analyzing with Jev…' : 'Queued for Jev';
    if (analysis?.error) {
      const error = document.createElement('p');
      error.className = 'sentiment-detail';
      error.textContent = analysis.error;
      section.append(error);
    }
  }
  const cost = document.createElement('p');
  cost.className = 'post-cost';
  if (analysis?.cost && analysis?.usage) {
    cost.textContent = `Est. $${analysis.cost.usd.toFixed(8)} · ${analysis.usage.inputTokens.toLocaleString()} input tokens · ${analysis.usage.outputTokens.toLocaleString()} output tokens (free)`;
  } else {
    cost.textContent = analysis?.costUnknown ? 'Cost unknown — excluded from estimated total' : analysis?.status === 'error' ? 'No provider usage reported' : 'Per-post cost appears when Jev returns token usage';
  }
  section.append(cost);
  return section;
}

function chartNode(tag, attributes, text) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
  if (text !== undefined) element.textContent = text;
  return element;
}

function renderChart() {
  const points = [...posts.values()]
    .filter(post => post.analysis?.status === 'done' && Number.isFinite(post.analysis.sentiment?.score))
    .sort((a, b) => a.receivedAt - b.receivedAt);
  let sum = 0;
  let positive = 0;
  let negative = 0;
  for (const post of points) {
    const score = post.analysis.sentiment.score;
    sum += score;
    if (score > 0.25) positive++;
    else if (score < -0.25) negative++;
  }
  const neutral = points.length - positive - negative;
  const latest = points.at(-1)?.analysis.sentiment.score;
  $('chart-latest').textContent = latest === undefined ? '—' : `${latest > 0 ? '+' : ''}${latest.toFixed(2)}`;
  $('chart-mean').textContent = points.length ? `${sum > 0 ? '+' : ''}${(sum / points.length).toFixed(2)}` : '—';
  $('chart-positive').textContent = positive.toLocaleString();
  $('chart-neutral').textContent = neutral.toLocaleString();
  $('chart-negative').textContent = negative.toLocaleString();
  $('chart-status').textContent = points.length
    ? `${points.length.toLocaleString()} scored ${points.length === 1 ? 'post' : 'posts'} · Updates as Jev results arrive`
    : 'Waiting for scored posts';
  $('chart-empty').hidden = points.length > 0;
  $('chart-summary').textContent = points.length
    ? `${points.length} scored posts. Mean sentiment ${(sum / points.length).toFixed(2)}. Latest score ${latest.toFixed(2)}. ${positive} positive, ${neutral} neutral or mixed, ${negative} negative. Scale minus one to plus one; neutral or mixed is minus 0.25 through plus 0.25.`
    : 'No completed sentiment scores yet. Pending and failed analyses are not plotted.';

  const svg = $('sentiment-chart');
  const width = Math.max(280, $('chart-plot').clientWidth);
  const height = 240;
  const left = 42;
  const right = width - 14;
  const top = 16;
  const bottom = height - 35;
  const y = score => top + (1 - score) / 2 * (bottom - top);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  const fragment = document.createDocumentFragment();
  fragment.append(chartNode('rect', { x: left, y: y(0.25), width: right - left, height: y(-0.25) - y(0.25), class: 'chart-neutral-band' }));
  for (const value of [1, 0.5, 0, -0.5, -1]) {
    fragment.append(chartNode('line', { x1: left, x2: right, y1: y(value), y2: y(value), class: value === 0 ? 'chart-zero' : 'chart-grid' }));
    fragment.append(chartNode('text', { x: left - 10, y: y(value) + 4, 'text-anchor': 'end', class: 'chart-axis' }, value > 0 ? `+${value}` : String(value)));
  }
  if (points.length) {
    const firstTime = points[0].receivedAt;
    const lastTime = points.at(-1).receivedAt;
    const padding = Math.max(1000, (lastTime - firstTime) * 0.05);
    const start = firstTime - padding;
    const end = lastTime + padding;
    const x = time => left + (time - start) / (end - start) * (right - left);
    const clock = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    for (const fraction of [0, 0.5, 1]) {
      const time = start + (end - start) * fraction;
      fragment.append(chartNode('text', {
        x: x(time), y: height - 9, class: 'chart-axis',
        'text-anchor': fraction === 0 ? 'start' : fraction === 1 ? 'end' : 'middle',
      }, clock.format(time)));
    }
    let rollingSum = 0;
    const averagePoints = [];
    for (let index = 0; index < points.length; index++) {
      rollingSum += points[index].analysis.sentiment.score;
      if (index >= 10) rollingSum -= points[index - 10].analysis.sentiment.score;
      averagePoints.push(`${x(points[index].receivedAt)},${y(rollingSum / Math.min(index + 1, 10))}`);
    }
    if (points.length > 1) {
      fragment.append(chartNode('polyline', { points: averagePoints.join(' '), class: 'chart-average', 'vector-effect': 'non-scaling-stroke' }));
    }
    for (const post of points) {
      const score = post.analysis.sentiment.score;
      const dot = chartNode('circle', {
        cx: x(post.receivedAt), cy: y(score), r: 3.5,
        class: `chart-dot ${score > 0.25 ? 'positive' : score < -0.25 ? 'negative' : 'neutral'}`,
        'data-request-id': post.analysis.requestId,
      });
      dot.append(chartNode('title', {}, `${clock.format(post.receivedAt)} · Sentiment ${score.toFixed(2)} · Filter: ${post.matchedFilter || 'earlier filter'}\n${post.text}`));
      fragment.append(dot);
    }
  }
  svg.replaceChildren(fragment);
}

function render() {
  $('toggle-stream').textContent = paused ? 'Resume' : 'Stop';
  $('toggle-stream').setAttribute('aria-label', paused ? 'Resume live stream and sentiment analysis' : 'Stop live stream and pause queued sentiment analysis');
  $('stream-feedback').hidden = !paused;
  renderChart();
  $('retained').textContent = posts.size.toLocaleString();
  $('analysis-count').textContent = meter.scored.toLocaleString();
  $('analysis-queue').textContent = `${jobs.size.toLocaleString()} ${paused ? 'outstanding (queue paused)' : 'pending'} · ${meter.failed.toLocaleString()} failed`;
  $('session-cost').textContent = `$${meter.costUsd.toFixed(6)}`;
  $('average-cost').textContent = meter.chargedRequests ? `$${(meter.costUsd / meter.chargedRequests).toFixed(8)} / metered post` : 'Waiting for token usage';
  $('cost-warning').hidden = meter.unknownCosts === 0;
  $('cost-warning').textContent = `${meter.unknownCosts} request(s) have unknown cost, excluded from this estimate.`;
  $('clear').disabled = posts.size === 0 && jobs.size === inFlight.size;
  $('active-filter').textContent = filters.size ? `Matching any of ${filters.size.toLocaleString()} ${filters.size === 1 ? 'filter' : 'filters'} · Newest first` : 'No active filters · Collection paused';
  $('empty').hidden = posts.size > 0;
  $('empty-title').textContent = paused ? 'The stream is stopped.' : filters.size ? 'Listening for your next match.' : 'Find your corner of the conversation.';
  $('empty-description').textContent = paused
    ? 'Resume to receive new live posts and continue queued sentiment analysis.'
    : filters.size
      ? 'All live posts are being checked. A single matching filter is enough to collect a post.'
      : 'Add a word or phrase above. Add as many filters as you need; a match on any one collects the post.';
  const fragment = document.createDocumentFragment();
  for (const post of posts.values()) {
    const item = document.createElement('li');
    item.className = 'post';
    item.dataset.uri = `at://${post.did}/app.bsky.feed.post/${post.rkey}`;
    const meta = document.createElement('div');
    meta.className = 'post-meta';
    const author = document.createElement('span');
    author.className = 'post-author';
    author.textContent = post.did;
    author.title = post.did;
    const time = document.createElement('time');
    time.dateTime = new Date(post.receivedAt).toISOString();
    time.textContent = new Date(post.receivedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    time.title = `Received ${new Date(post.receivedAt).toLocaleString()}${post.createdAt ? ` · Post timestamp: ${post.createdAt}` : ''}`;
    meta.append(author, time);
    const text = document.createElement('p');
    text.className = 'post-text';
    text.textContent = post.text;
    const match = document.createElement('p');
    match.className = 'post-match';
    match.textContent = post.matchedFilter ? `Collected by “${post.matchedFilter}”` : 'Collected by an earlier filter';
    const bottom = document.createElement('div');
    bottom.className = 'post-bottom';
    const detail = document.createElement('span');
    detail.textContent = `${post.isReply ? 'Reply' : 'Post'} · ${(post.langs || []).join(', ') || 'Language unspecified'}`;
    const link = document.createElement('a');
    link.href = `https://bsky.app/profile/${encodeURIComponent(post.did)}/post/${encodeURIComponent(post.rkey)}`;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = 'View on Bluesky ↗';
    bottom.append(detail, link);
    item.append(meta, text, match, sentimentCard(post), bottom);
    fragment.append(item);
  }
  $('posts').replaceChildren(fragment);
}

function handleEvent(event) {
  if (typeof event.seq === 'number') {
    if (cursor !== undefined && event.seq <= cursor) return;
    cursor = event.seq;
  }
  const kind = event.$type?.split('#')[1];
  if (kind === 'account' || kind === 'sync') {
    if (kind === 'sync' || (event.account?.active === false && event.account?.status === 'deleted')) {
      for (const [id, job] of jobs) {
        if (job.key.startsWith(`at://${event.did}/`) && !inFlight.has(id)) { jobs.delete(id); dirty = true; }
      }
      for (const [key, post] of posts) {
        if (post.did === event.did) { posts.delete(key); dirty = true; }
      }
    }
    return;
  }
  if (kind !== 'commit' || event.collection !== 'app.bsky.feed.post') return;
  // Cancel work not yet sent when a record is removed or superseded.
  const key = `at://${event.did}/${event.collection}/${event.rkey}`;
  if (event.operation === 'delete') {
    for (const [id, job] of jobs) {
      if (job.key === key && !inFlight.has(id)) { jobs.delete(id); dirty = true; }
    }
    if (posts.delete(key)) dirty = true;
    return;
  }
  if (!['create', 'update'].includes(event.operation) || typeof event.record?.text !== 'string') return;
  received++;
  const record = event.record;
  const previous = posts.get(key);
  if (previous?.text === record.text) return;
  for (const [id, job] of jobs) {
    if (job.key === key && !inFlight.has(id)) jobs.delete(id);
  }
  const hit = matchingFilter(record.text);
  if (hit === null) {
    if (posts.delete(key)) dirty = true;
    return;
  }
  posts.delete(key);
  posts = new Map([[key, {
    did: event.did, rkey: event.rkey, text: record.text,
    createdAt: record.createdAt, receivedAt: Date.now(),
    langs: Array.isArray(record.langs) ? record.langs.filter(lang => typeof lang === 'string') : [],
    isReply: Boolean(record.reply),
    matchedFilter: hit,
  }], ...posts]);
  enqueue(key, posts.get(key));
  if (posts.size > MAX_POSTS) posts.delete([...posts.keys()].at(-1));
  dirty = true;
}

function setConnection(state, text) {
  $('connection').dataset.state = state;
  $('connection-text').textContent = text;
}

function connect() {
  if (stopped) return;
  if (paused) { setConnection('stopped', 'Stopped'); return; }
  setConnection('connecting', 'Connecting');
  const url = new URL(ENDPOINT);
  url.searchParams.set('collections', 'app.bsky.feed.post');
  if (cursor !== undefined) url.searchParams.set('cursor', String(cursor));
  let opened = false;
  const active = new WebSocket(url);
  socket = active;
  active.onopen = () => {
    if (paused || stopped || socket !== active) return;
    opened = true;
    retryDelay = 1000;
    setConnection('live', 'Live stream');
  };
  active.onmessage = ({ data }) => {
    if (paused || stopped || socket !== active) return;
    try {
      const message = JSON.parse(data);
      if (message.$type === 'message' && message.payload) handleEvent(message.payload);
      else if (message.$type === 'error') {
        showNotice(`Stream interrupted: ${message.error || 'unknown error'}. Reconnecting.`);
        active.close();
      }
    } catch {
      showNotice('An unreadable stream message was skipped.');
    }
  };
  active.onerror = () => {
    if (!paused && !stopped && socket === active) setConnection('offline', 'Connection interrupted');
  };
  active.onclose = () => {
    if (paused || stopped || socket !== active) return;
    if (!opened && cursor !== undefined) {
      cursor = undefined;
      showNotice('Could not resume the stream. Reconnecting live; posts during the gap may be missing.');
    }
    setConnection('offline', `Reconnecting in ${retryDelay / 1000}s`);
    reconnectTimer = setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 30000);
  };
}

$('toggle-stream').addEventListener('click', () => {
  paused = !paused;
  if (paused) {
    clearTimeout(reconnectTimer);
    cursor = undefined;
    socket?.close();
    socket = undefined;
    setConnection('stopped', 'Stopped');
  } else {
    connect();
    pump();
  }
  persist();
  render();
});

$('filter-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const next = $('query').value.trim();
  if (!next) {
    $('filter-feedback').textContent = 'Enter a word or phrase to add a filter.';
    return;
  }
  const needle = next.toLowerCase();
  if (filters.has(needle)) {
    $('filter-feedback').textContent = `Already listening for “${filters.get(needle)}”.`;
    return;
  }
  filters.set(needle, next);
  $('query').value = '';
  $('query').focus();
  $('filter-feedback').textContent = `Added “${next}”. A match on any filter collects a post.`;
  renderFilters();
  dirty = false;
  persist();
  render();
});
$('filters').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-filter]');
  if (!button) return;
  const label = filters.get(button.dataset.filter);
  filters.delete(button.dataset.filter);
  $('filter-feedback').textContent = `Removed “${label}”. Previously collected posts stay saved.`;
  renderFilters();
  persist();
  render();
  $('query').focus();
});
$('clear').addEventListener('click', () => {
  const cleared = posts.size;
  let cancelled = 0;
  for (const id of jobs.keys()) {
    if (!inFlight.has(id)) { jobs.delete(id); cancelled++; }
  }
  posts.clear();
  dirty = false;
  persist();
  render();
  $('collection-feedback').textContent = `Cleared ${cleared.toLocaleString()} posts and cancelled ${cancelled.toLocaleString()} queued analyses. Filters and recorded costs kept. ${paused ? 'The stream remains stopped.' : filters.size ? 'New matches will continue arriving.' : 'Add a filter to collect new posts.'}${inFlight.size ? ` ${inFlight.size} running analyses will still be metered, but will not restore cleared posts.` : ''}`;
});

// Batch synchronous storage writes and DOM work rather than doing either per event.
setInterval(() => {
  $('received').textContent = received.toLocaleString();
  if (dirty) { dirty = false; persist(); render(); }
  pump();
}, 500);
window.addEventListener('pagehide', () => {
  stopped = true;
  clearTimeout(reconnectTimer);
  socket?.close();
  persist();
});
window.addEventListener('pageshow', (event) => {
  if (event.persisted) { stopped = false; connect(); pump(); }
});
for (const [key, post] of posts) enqueue(key, post);
renderFilters();
persist();
render();
new ResizeObserver(renderChart).observe($('chart-plot'));
connect();
pump();
