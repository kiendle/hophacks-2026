// Navigation regression: all data/analysis endpoints are fixtures, with no paid calls.
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/Kaan Eroltu/AppData/Local/npm-cache/_npx/420ff84f11983ee5/node_modules/playwright');
const baseUrl = process.env.UI_BASE_URL || 'http://127.0.0.1:5198';
const start = Date.now() - 180_000;
const companies = [{ id: 'openai', name: 'OpenAI' }];
const originalPost = 'Ein langer Beitrag über künstliche Intelligenz. '.repeat(20) + 'ORIGINAL TAIL';
const config = { targets: [{ id: 'openai', label: 'OpenAI' }], keyword_filter: { groups: [{ direct: ['AI'] }] } };
const post = (id, time) => ({ id, postId: id, kind: 'post', t: time, postTime: time,
  text: originalPost, authorId: 'fixture', contentVersion: '1', observedAt: time,
  grades: [{ company: 'openai', choice: 'positive', score: 8, confidence: .9,
    probabilities: { positive: .8, negative: .05, neutral: .05, mixed: .05, insufficient_evidence: .05 } }] });
const sse = events => events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('');

(async () => {
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.UI_BROWSER_PATH || 'C:/Users/Kaan Eroltu/AppData/Local/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
    const errors = [], sockets = [], cursors = [], questions = [], translations = [];
    let analysisCalls = 0, sessionCalls = 0, releaseCalls = 0, pauseCalls = 0;
    let finishAnalysis, finishPoll, deferPoll = false, deliverNew = false;
    page.on('pageerror', error => errors.push(error.message));
    page.setDefaultTimeout(10_000);
    await page.route('**/api/voice/status', route => route.fulfill({ json: { available: false } }));
    await page.route('**/api/live/agent/status', route => route.fulfill({ json: { available: false } }));
    await page.route('**/api/sessions', route => route.fulfill({ json: { session_id: `chat-${++sessionCalls}` } }));
    await page.route('**/api/sessions/*/messages', async route => {
      questions.push(JSON.parse(route.request().postDataJSON().text.split('\n').at(-1)));
      analysisCalls++;
      if (analysisCalls === 1) await new Promise(resolve => { finishAnalysis = resolve; });
      return route.fulfill({ contentType: 'text/event-stream', body: sse([
        { type: 'message', text: analysisCalls === 1 ? 'Historical analysis retained.' : 'Live analysis retained.' },
        { type: 'done' },
      ]) });
    });
    await page.route('**/api/posts/translate', route => {
      translations.push(route.request().postDataJSON().text);
      return route.fulfill({ json: { translated_text: 'A translated post. '.repeat(40) + 'TRANSLATED TAIL',
        source_language: 'German', is_english: false } });
    });
    await page.routeWebSocket(/\/api\/replay(?:\?|$)/, ws => {
      const connection = { ws, closed: false };
      sockets.push(connection);
      ws.onClose(() => { connection.closed = true; });
    });
    await page.route('**/api/automations/tracker/events?*', async route => {
      const after = Number(new URL(route.request().url()).searchParams.get('after'));
      cursors.push(after);
      if (deferPoll) await new Promise(resolve => { finishPoll = resolve; });
      const events = after === 0 ? [post('live-1', start + 60_000)]
        : deliverNew && after === 1 ? [post('live-2', start + 120_000)] : [];
      return route.fulfill({ json: {
        automation: { enabled: pauseCalls === 0, worker_state: 'idle', max_usd: .1, config, created_at: new Date(start).toISOString() },
        counts: { ready: deliverNew ? 2 : 1 }, source: { status: 'connected' }, events,
        cursor: deliverNew ? 2 : 1, more: false,
      } });
    });
    await page.route('**/api/automations/tracker/pause', route => {
      pauseCalls++; return route.fulfill({ json: { enabled: false } });
    });
    await page.route('**/api/automations/tracker/release', route => {
      releaseCalls++; return route.fulfill({ json: { released: true } });
    });
    await page.goto(baseUrl);
    await page.getByRole('textbox', { name: 'Keyword', exact: true }).fill('AI');
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.waitForTimeout(100);
    const historical = sockets.find(socket => !socket.closed);
    assert.ok(historical);
    const frame = (sequence, count, status = 'playing') => historical.ws.send(JSON.stringify({
      type: 'batch', run: 'saved-run', sequence, now: start + (count + 1) * 30_000,
      events: [post(`historical-${count}`, start + count * 30_000)], status, speed: 14400,
    }));
    historical.ws.send(JSON.stringify({ type: 'init', run: 'saved-run', start, end: start + 180_000, companies, speed: 14400 }));
    frame(0, 1);
    const active = page.locator('.retained-workspace:not([hidden])');
    await active.locator('.series-dot').first().waitFor({ state: 'visible' });
    const point = await active.locator('.series-dot').first().boundingBox();
    await page.mouse.move(point.x + point.width / 2, point.y + point.height / 2);
    const preview = active.getByRole('region', { name: 'Post preview' });
    await preview.waitFor({ state: 'visible' });
    assert.ok((await preview.locator('.card-text').innerText()).endsWith('…'));
    assert.doesNotMatch(await preview.innerText(), /ORIGINAL TAIL/);
    await preview.getByRole('button', { name: 'Translate to English', exact: true }).click();
    await preview.getByRole('button', { name: 'Show original', exact: true }).waitFor();
    assert.deepEqual(translations, [originalPost], 'Translation receives the full post, not its excerpt');
    assert.doesNotMatch(await preview.innerText(), /TRANSLATED TAIL/);
    const chartBounds = await active.locator('.chart').boundingBox();
    const cardBounds = await preview.boundingBox();
    const footerBounds = await preview.locator('.card-foot').boundingBox();
    assert.ok(cardBounds.y >= chartBounds.y && cardBounds.y + cardBounds.height <= chartBounds.y + chartBounds.height);
    assert.ok(footerBounds.y + footerBounds.height <= cardBounds.y + cardBounds.height,
      'Bounded preview retains its footer alongside translation controls');
    await preview.getByRole('button', { name: 'Close post preview', exact: true }).click();
    await preview.waitFor({ state: 'detached' });
    await active.getByRole('combobox', { name: 'Point interval' }).selectOption('86400000');
    const overlay = await active.locator('.chart .overlay').boundingBox();
    await page.mouse.move(overlay.x + overlay.width * .2, overlay.y + overlay.height * .3);
    await page.mouse.down();
    await page.mouse.move(overlay.x + overlay.width * .6, overlay.y + overlay.height * .3, { steps: 8 });
    await page.mouse.up();
    await active.getByRole('button', { name: 'Clear selection', exact: true }).waitFor();
    await active.getByRole('textbox', { name: 'Message' }).fill('Explain this chart');
    await active.getByRole('button', { name: 'Send', exact: true }).click();
    await active.getByRole('button', { name: 'Clear selection', exact: true }).waitFor({ state: 'detached' });
    await page.waitForTimeout(100);
    assert.equal(questions[0].scope.rangeSource, 'selection', 'Clearing the composer preserves the submitted question scope');
    assert.equal(typeof finishAnalysis, 'function');
    await page.evaluate(() => {
      window.savedChart = document.querySelector('.retained-workspace:not([hidden]) .chart');
      window.hiddenMutations = 0;
      window.chartObserver = new MutationObserver(records => { window.hiddenMutations += records.length; });
    });
    const connections = sockets.length;
    await page.getByRole('button', { name: 'New', exact: true }).click();
    await page.getByRole('textbox', { name: 'Keyword', exact: true }).waitFor();
    await page.waitForTimeout(500); // Let axis easing settle before measuring background redraws.
    await page.evaluate(() => window.chartObserver.observe(window.savedChart, { subtree: true, attributes: true, childList: true }));
    frame(1, 2, 'complete');
    finishAnalysis();
    await page.waitForTimeout(300);
    assert.equal(historical.closed, false, 'Navigation preserves the replay connection');
    assert.equal(await page.evaluate(() => window.hiddenMutations), 0, 'Background replay does not redraw hidden charts');
    await page.locator('.recent').filter({ hasText: /^AI$/ }).click();
    await active.getByText('Historical analysis retained.', { exact: true }).waitFor();
    assert.equal(await page.locator('.setup-overlay').count(), 0, 'Reopening skips setup');
    assert.equal(await active.getByRole('combobox', { name: 'Point interval' }).inputValue(), '86400000');
    assert.equal(await page.evaluate(() => window.savedChart === document.querySelector('.retained-workspace:not([hidden]) .chart')), true);
    assert.match(await active.locator('.events').innerText(), /2 posts/);
    assert.equal(sockets.length, connections, 'Completed replay is reused');
    assert.equal(analysisCalls, 1, 'The in-flight answer finishes without being regenerated');
    assert.equal(await active.getByRole('button', { name: 'Stop', exact: true }).count(), 0);
    await active.getByRole('button', { name: 'Bubble view' }).click();
    await active.locator('.bubble-chart svg').waitFor();
    await page.getByRole('button', { name: 'New', exact: true }).click();
    await page.locator('.recent').filter({ hasText: /^AI$/ }).click();
    await active.locator('.bubble-chart svg').waitFor();
    assert.equal(sockets.length, connections);

    // A newly created live tracker shares the same retention path as historical runs.
    await page.evaluate(config => window.dispatchEvent(new CustomEvent('sentimeter:tracker-created', {
      detail: { id: 'tracker', title: 'Live fixture', config, max_usd: .1 },
    })), config);
    await active.locator('.series-dot').first().waitFor({ state: 'visible' });
    assert.equal(await active.getByText('Historical analysis retained.', { exact: true }).count(), 0, 'Sessions do not share analysis');
    await active.getByRole('textbox', { name: 'Message' }).fill('Explain live observations');
    await active.getByRole('button', { name: 'Send', exact: true }).click();
    await active.getByText('Live analysis retained.', { exact: true }).waitFor();
    await page.evaluate(() => { window.savedLiveChart = document.querySelector('.retained-workspace:not([hidden]) .chart'); });
    await page.getByRole('button', { name: 'New', exact: true }).click();
    await page.getByRole('textbox', { name: 'Keyword', exact: true }).waitFor();
    await page.waitForTimeout(100);
    assert.equal(pauseCalls, 0, 'Leaving live must not stop background tracking');
    assert.ok(releaseCalls > 0, 'Leaving live releases only the viewer');
    const pollsBefore = cursors.length;
    await page.waitForTimeout(2100);
    assert.equal(cursors.length, pollsBefore, 'Hidden live workspaces do not poll');
    deferPoll = true;
    deliverNew = true;
    await page.locator('.recent').filter({ hasText: /^Live fixture$/ }).click();
    await active.locator('.series-dot').first().waitFor({ state: 'visible' });
    await active.getByText('Live analysis retained.', { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => window.savedLiveChart === document.querySelector('.retained-workspace:not([hidden]) .chart')), true);
    assert.equal(await active.locator('.chart-loading').count(), 0, 'Saved live chart is visible while refresh is pending');
    await page.waitForTimeout(100);
    assert.equal(cursors.at(-1), 1, 'Live delivery resumes from the saved completion cursor');
    deferPoll = false;
    finishPoll();
    await page.waitForFunction(() => document.querySelector('.retained-workspace:not([hidden]) .events')?.textContent.includes('2 matching texts'));
    assert.equal(analysisCalls, 2);
    assert.equal(sessionCalls, 2, 'Each workspace retains its own assistant conversation');
    await active.getByRole('button', { name: 'Stop tracking', exact: true }).waitFor();
    await page.locator('.recent').filter({ hasText: /^AI$/ }).click();
    await active.locator('.bubble-chart svg').waitFor();
    assert.equal(pauseCalls, 0, 'Switching workspaces must not stop tracking');
    await page.locator('.recent').filter({ hasText: /^Live fixture$/ }).click();
    await active.getByRole('button', { name: 'Close tracker', exact: true }).click();
    await page.getByRole('textbox', { name: 'Keyword', exact: true }).waitFor();
    assert.equal(pauseCalls, 0, 'Close tracker only closes the view');
    await page.locator('.recent').filter({ hasText: /^Live fixture$/ }).click();
    await active.getByRole('button', { name: 'Stop tracking', exact: true }).click();
    await active.getByRole('button', { name: 'Resume tracking', exact: true }).waitFor();
    assert.equal(pauseCalls, 1, 'Only an explicit Stop tracking pauses the tracker');
    await page.getByRole('button', { name: 'New', exact: true }).click();
    await page.locator('.recent').filter({ hasText: /^Live fixture$/ }).click();
    await active.getByRole('button', { name: 'Resume tracking', exact: true }).waitFor();
    await page.getByRole('button', { name: 'More actions for Live fixture', exact: true }).click();
    await page.getByRole('menuitem', { name: 'Delete', exact: true }).click();
    await page.getByRole('textbox', { name: 'Keyword', exact: true }).waitFor();
    assert.equal(await page.locator('.retained-workspace').count(), 1, 'Deleting a session removes its retained workspace');
    assert.deepEqual(errors, []);
    console.log('PASS: chart DOM, controls, background replay, in-flight analysis, session isolation, live cursor refresh, background tracking navigation, explicit stop, and deletion.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
