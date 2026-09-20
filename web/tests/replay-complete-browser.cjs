const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/Kaan Eroltu/AppData/Local/npm-cache/_npx/420ff84f11983ee5/node_modules/playwright');

(async () => {
  const manifest = JSON.parse(fs.readFileSync('harness/data/classified/processed-streams-20260920T063350Z/manifest.json', 'utf8'));
  const browser = await chromium.launch({ headless: true, executablePath: process.env.UI_BROWSER_PATH || 'C:/Users/Kaan Eroltu/AppData/Local/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.__replayCheck = { posts: 0, likes: 0, sequence: -1, complete: false, error: null };
      window.WebSocket = new Proxy(window.WebSocket, {
        construct(Target, args) {
          const socket = Reflect.construct(Target, args);
          if (String(args[0]).includes('/api/replay')) {
            window.__replaySocket = socket;
            socket.addEventListener('message', ({ data }) => {
              const message = JSON.parse(data), result = window.__replayCheck;
              if (message.type === 'init') { result.sequence = -1; return; }
              if (message.type === 'error') { result.error = message.message; return; }
              if (message.sequence !== ++result.sequence) result.error = 'Sequence gap';
              for (const event of message.events) result[event.kind === 'post' ? 'posts' : 'likes']++;
              if (message.status === 'complete') result.complete = true;
            });
          }
          return socket;
        },
      });
    });
    await page.goto('http://127.0.0.1:5196/');
    await page.getByRole('textbox', { name: 'Keywords', exact: true }).fill('AI');
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.getByRole('button', { name: 'Pause', exact: true }).waitFor({ timeout: 90000 });
    await page.evaluate(() => window.__replaySocket.send(JSON.stringify({ type: 'speed', speed: 1_000_000 })));
    await page.waitForFunction(() => window.__replayCheck.complete || window.__replayCheck.error, null, { timeout: 120000 });
    await page.getByRole('button', { name: 'Play', exact: true }).waitFor();
    const result = await page.evaluate(() => window.__replayCheck);
    assert.equal(result.error, null);
    assert.equal(result.posts, manifest.counts.exported_post_events);
    assert.equal(result.likes, manifest.counts.exported_like_events);
    assert.equal(await page.getByRole('alert').count(), 0);
    const counts = await page.locator('.events').innerText();
    assert.match(counts, /767k posts/);
    assert.match(counts, /274k updates/);
    const overlay = page.locator('.chart .overlay');
    const bounds = await overlay.boundingBox();
    await page.mouse.move(bounds.x + bounds.width * .3, bounds.y + bounds.height * .4);
    await page.mouse.down();
    await page.mouse.move(bounds.x + bounds.width * .7, bounds.y + bounds.height * .4, { steps: 8 });
    await page.mouse.up();
    assert.ok(await page.locator('.chart .selection').count(), 'Historical selection remains available');
    await page.getByRole('button', { name: 'Bubble view', exact: true }).click();
    await page.locator('.bubble-chart .bubble').first().waitFor();
    await page.getByRole('button', { name: 'Line view', exact: true }).click();
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ...result, counts, historicalSelection: true, bothViews: true, errors }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
