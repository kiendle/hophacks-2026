const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/Kaan Eroltu/AppData/Local/npm-cache/_npx/420ff84f11983ee5/node_modules/playwright');

const baseUrl = process.env.UI_BASE_URL || 'http://127.0.0.1:5196';
const start = Date.UTC(2026, 7, 17);
const end = start + 24 * 60 * 60 * 1000;
const speed = 14400;
const companies = [{ id: 'openai', name: 'OpenAI' }];
const grade = {
  company: 'openai', choice: 'positive', score: 8, confidence: 0.9,
  probabilities: { positive: 0.7, negative: 0.1, neutral: 0.1, mixed: 0.1, insufficient_evidence: 0 },
};

function post(id, t) {
  return { id: `post:${id}`, postId: id, kind: 'post', t, postTime: t,
    text: 'Saved OpenAI fixture post', authorId: 'fixture-author', contentVersion: '1',
    observedAt: t, grades: [grade] };
}

function initialize(connection, run, selected = companies) {
  connection.ws.send(JSON.stringify({ type: 'init', run, start, end, companies: selected, speed }));
}

function batch(connection, run, sequence, now, events = [], status = 'playing') {
  connection.ws.send(JSON.stringify({ type: 'batch', run, sequence, now, events, status, speed }));
}

async function placeholder(page, heading, role = 'status') {
  const shell = page.locator('.chart-loading');
  await shell.waitFor({ state: 'visible' });
  await shell.getByText(heading, { exact: true }).waitFor({ state: 'visible' });
  assert.equal(await shell.getAttribute('role'), role);
  const bounds = await shell.boundingBox();
  assert.ok(bounds && bounds.width > 200 && bounds.height > 200, 'A visible chart shell occupies the chart area');
  assert.equal(await page.locator('.chart:not(.chart-loading) .overlay').count(), 0,
    'No real chart is rendered before any data arrives');
}

async function counts(page, posts, likes) {
  const expected = `${posts} posts · ${likes} updates`;
  await page.waitForFunction(text => document.querySelector('.events')?.textContent?.trim() === text, expected);
  assert.equal((await page.locator('.events').innerText()).trim(), expected);
}

async function scenario(browser, name, verify) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(10000);
  const errors = [];
  const forbiddenScans = [];
  const connections = [];
  const waiters = new Set();
  page.on('pageerror', error => errors.push(error.message));
  // A missing stream route must never fall back to reading the real export.
  await page.route('**/api/ui/scan', route => {
    forbiddenScans.push(route.request().url());
    return route.abort();
  });
  await page.routeWebSocket(/\/api\/replay(?:\?|$)/, ws => {
    // Deliberately do not connectToServer(): no real replay dataset is loaded.
    const connection = { ws, commands: [], closed: false };
    connections.push(connection);
    ws.onMessage(message => connection.commands.push(JSON.parse(String(message))));
    ws.onClose(() => { connection.closed = true; });
    for (const notify of [...waiters]) notify();
  });
  const nextConnection = (after = 0) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      waiters.delete(check);
      reject(new Error(`${name}: replay connection ${after + 1} did not open`));
    }, 10000);
    const check = () => {
      const connection = connections.slice(after).find(item => !item.closed);
      if (!connection) return;
      clearTimeout(timer);
      waiters.delete(check);
      resolve(connection);
    };
    waiters.add(check);
    check();
  });
  try {
    await page.goto(baseUrl);
    await page.getByRole('textbox', { name: 'Keywords', exact: true }).fill('AI');
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    const connection = await nextConnection();
    await verify({ page, connection, connections, nextConnection });
    assert.deepEqual(forbiddenScans, [], 'Startup uses the intercepted stream only');
    assert.deepEqual(errors, [], 'The browser has no uncaught errors');
    return { scenario: name, connections: connections.length, errors };
  } finally {
    await page.close();
  }
}

(async () => {
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.UI_BROWSER_PATH || 'C:/Users/Kaan Eroltu/AppData/Local/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe' });
  try {
    const screenshots = path.resolve(__dirname, '../../harness/data/replay-performance');
    fs.mkdirSync(screenshots, { recursive: true });
    const results = [];
    results.push(await scenario(browser, 'delayed init and gradual real data', async ({ page, connection }) => {
      await placeholder(page, 'Loading your chart');
      assert.match(await page.locator('.events').innerText(), /Loading posts/);
      // Give the shell two paint opportunities while metadata is still held.
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await placeholder(page, 'Loading your chart');
      await page.screenshot({ path: path.join(screenshots, 'startup-loading.png') });

      const run = 'startup-fixture';
      initialize(connection, run);
      await placeholder(page, 'Waiting for the first posts');
      await counts(page, 0, 0);
      batch(connection, run, 0, start + 1000);
      await placeholder(page, 'Waiting for the first posts');
      batch(connection, run, 1, start + 1000, [], 'paused');
      await placeholder(page, 'Replay paused');
      batch(connection, run, 2, start + 1000);
      await placeholder(page, 'Waiting for the first posts');

      const first = post('one', start + 60 * 60 * 1000);
      batch(connection, run, 3, first.t + 1000, [first]);
      await page.locator('.chart-loading').waitFor({ state: 'detached' });
      await page.locator('.chart:not(.chart-loading) .series-dot').first().waitFor({ state: 'visible' });
      await counts(page, 1, 0);

      const second = post('two', first.t + 60 * 1000);
      const like = { ...first, id: 'like:one', kind: 'like', t: second.t + 1000,
        observedAt: second.t + 1000, opening: true, delta: 9 };
      batch(connection, run, 4, second.t, [second]);
      batch(connection, run, 5, like.t, [like]);
      await counts(page, 2, 1);
      assert.equal(await page.locator('.chart-loading').count(), 0);
      assert.equal(await page.getByRole('alert').count(), 0);
      await page.screenshot({ path: path.join(screenshots, 'startup-populated.png') });
    }));

    results.push(await scenario(browser, 'completed replay with no matching posts', async ({ page, connection }) => {
      await placeholder(page, 'Loading your chart');
      const run = 'empty-fixture';
      initialize(connection, run, []);
      await placeholder(page, 'Waiting for the first posts');
      batch(connection, run, 0, end, [], 'complete');
      await placeholder(page, 'No posts to show');
      await counts(page, 0, 0);
      assert.equal(await page.getByRole('alert').count(), 0);
    }));

    results.push(await scenario(browser, 'failed startup retries on a fresh connection', async ({ page, connection, connections, nextConnection }) => {
      await placeholder(page, 'Loading your chart');
      connection.ws.send(JSON.stringify({ type: 'error', message: 'Fixture export could not be loaded.' }));
      await placeholder(page, 'Could not load your chart', 'alert');
      await page.getByRole('alert').filter({ hasText: 'Fixture export could not be loaded.' }).waitFor();
      const previousConnections = connections.length;
      await page.getByRole('button', { name: 'Try again', exact: true }).click();
      const retried = await nextConnection(previousConnections);
      assert.notEqual(retried, connection);
      await placeholder(page, 'Loading your chart');
      assert.match(await page.locator('.events').innerText(), /Loading posts/);
      assert.equal(await page.getByRole('alert').count(), 0, 'Retry clears the failed attempt');

      const run = 'retry-fixture';
      initialize(retried, run);
      await placeholder(page, 'Waiting for the first posts');
      const first = post('retry-one', start + 60 * 60 * 1000);
      batch(retried, run, 0, first.t + 1000, [first]);
      await page.locator('.chart-loading').waitFor({ state: 'detached' });
      await page.locator('.chart:not(.chart-loading) .series-dot').first().waitFor({ state: 'visible' });
      await counts(page, 1, 0);
    }));
    console.log(JSON.stringify({ baseUrl, realDatasetLoaded: false, results }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
