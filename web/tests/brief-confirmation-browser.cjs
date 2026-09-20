// All APIs are fixtures. This regression never sends to Telegram or calls a voice provider.
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/Kaan Eroltu/AppData/Local/npm-cache/_npx/420ff84f11983ee5/node_modules/playwright');
const sse = events => events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('');
const confirmationId = 'abcdefgh12345678';
const briefId = '20260920-120000';
const script = 'This is the approved draft. Every sentence stays in the recording.';

(async () => {
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.UI_BROWSER_PATH || 'C:/Users/Kaan Eroltu/AppData/Local/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(10000);
    const errors = [], decisions = [], sockets = [];
    let directSends = 0, spoken = 0;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', route => route.fulfill({ json: { available: false } }));
    await page.route('**/api/automations/historical', route => route.fulfill({ json: { taxonomy: { categories: [{ id: 'openai', label: 'OpenAI' }] } } }));
    await page.routeWebSocket(/\/api\/replay(?:\?|$)/, socket => sockets.push(socket));
    await page.route('**/api/sessions', route => route.fulfill({ json: { session_id: 'brief-test' } }));
    await page.route(`**/api/briefs/${briefId}`, route => route.fulfill({ json: {
      id: briefId, status: 'ready', title: 'Revised brief', source: 'custom', audio: { full: 'brief.mp3' },
      segments: [{ headline: 'Revised draft', stories: [], script }], notes: [],
    } }));
    await page.route(`**/api/briefs/${briefId}/audio/brief.mp3`, route => route.fulfill({ contentType: 'audio/mpeg', body: '' }));
    await page.route('**/api/briefs/*/telegram', route => { directSends++; return route.fulfill({ json: { sent: true } }); });
    await page.route('**/api/voice/speak', route => { spoken++; return route.abort(); });
    await page.route('**/api/sessions/brief-test/messages', route => route.fulfill({ contentType: 'text/event-stream', body: sse([
      { type: 'card', card: { kind: 'brief', brief_id: briefId } },
      { type: 'confirm_request', kind: 'brief_telegram', confirmation_id: confirmationId,
        expires_ms: Date.now() + 60000, summary: 'Send this revised recording to your Telegram chat?' },
      { type: 'message', text: 'The revised recording is ready for your review.' }, { type: 'done' },
    ]) }));
    await page.route('**/api/sessions/brief-test/confirm', route => {
      decisions.push(route.request().postDataJSON());
      return route.fulfill({ contentType: 'text/event-stream', body: sse([
        { type: 'message', text: 'Your brief was sent to Telegram.' }, { type: 'done' },
      ]) });
    });
    await page.goto(process.env.UI_BASE_URL || 'http://127.0.0.1:5196');
    await page.getByRole('textbox', { name: 'Keyword', exact: true }).fill('AI');
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.waitForTimeout(100);
    const start = Date.now() - 86400000, end = Date.now();
    const ws = sockets.at(-1);
    assert.ok(ws, 'A fixture replay session opened');
    ws.send(JSON.stringify({ type: 'init', run: 'brief-fixture', start, end, companies: [{ id: 'openai', name: 'OpenAI' }], speed: 14400 }));
    ws.send(JSON.stringify({ type: 'batch', run: 'brief-fixture', sequence: 0, now: end, events: [], status: 'complete', speed: 14400 }));
    await page.getByPlaceholder('Ask', { exact: true }).fill('Record my revised draft and wait for confirmation before sending.');
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    const card = page.locator('.tool-card').filter({ has: page.getByRole('heading', { name: 'Confirm Telegram delivery', exact: true }) });
    await card.waitFor();
    await page.getByLabel('Play audio brief').waitFor();
    assert.equal(await page.getByLabel('Play audio brief').evaluate(audio => audio.paused), true);
    assert.deepEqual(decisions, []);
    assert.equal(directSends, 0);
    assert.equal(spoken, 0);
    assert.equal(await page.getByText(script, { exact: true }).isVisible(), false);
    await page.getByText('Read the script', { exact: true }).click();
    await page.getByText(script, { exact: true }).waitFor();
    assert.deepEqual(decisions, [], 'Reading a draft never authorizes delivery');
    await card.getByRole('button', { name: 'Send to Telegram', exact: true }).click();
    await page.getByText('Your brief was sent to Telegram.', { exact: true }).waitFor();
    assert.deepEqual(decisions, [{ confirmation_id: confirmationId, approved: true }]);
    assert.equal(directSends, 0, 'The confirmation sends through its bound decision endpoint');
    assert.equal(spoken, 0);
    assert.deepEqual(errors, []);
    console.log('PASS: exact draft review, no autoplay or automatic Telegram delivery, correct confirmation label, explicit bound send.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
