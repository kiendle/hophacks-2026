const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/Kaan Eroltu/AppData/Local/npm-cache/_npx/420ff84f11983ee5/node_modules/playwright');

(async () => {
  const mode = process.argv[2] || 'after';
  const duration = Number(process.env.REPLAY_PROFILE_MS || 30000);
  const root = path.resolve(__dirname, '../../harness/data/replay-performance');
  const browser = await chromium.launch({ headless: true, executablePath: process.env.UI_BROWSER_PATH || 'C:/Users/Kaan Eroltu/AppData/Local/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    if (mode === 'before') {
      const html = fs.readFileSync(path.join(root, 'before/dist/index.html'), 'utf8');
      const script = html.match(/src="\/assets\/([^"]+\.js)"/)[1];
      const stylesheet = html.match(/href="\/assets\/([^"]+\.css)"/)[1];
      // Keep the real HTTP document response so Chromium retains its local
      // address-space classification when opening the replay WebSocket.
      await page.route('http://127.0.0.1:5196/assets/**', async route => {
        const name = new URL(route.request().url()).pathname;
        if (name.endsWith('.js') || name.endsWith('.css')) {
          const file = path.join(root, 'before/dist/assets', name.endsWith('.js') ? script : stylesheet);
          await route.fulfill({ path: file });
        } else await route.continue();
      });
    }
    await page.addInitScript(() => {
      window.__replayProfile = null;
      new PerformanceObserver(list => {
        const p = window.__replayProfile;
        if (p) for (const entry of list.getEntries()) if (entry.startTime >= p.start) p.tasks.push(entry.duration);
      }).observe({ type: 'longtask', buffered: true });
      let last = 0;
      function frame(t) {
        const p = window.__replayProfile;
        if (p && last >= p.start) p.frames.push(t - last);
        last = t;
        requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    });
    await page.goto('http://127.0.0.1:5196/');
    await page.getByRole('textbox', { name: 'Keywords', exact: true }).fill('AI');
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.getByLabel('Playback speed', { exact: true }).selectOption('43200', { timeout: 20000 }).catch(async error => {
      console.error(JSON.stringify({ errors, text: (await page.locator('body').innerText()).slice(-1800) }));
      throw error;
    });
    await page.waitForFunction(() => /[1-9]/.test(document.querySelector('.events')?.textContent || ''), null, { timeout: 90000 });
    const metrics = await page.evaluate(duration => new Promise(resolve => {
      const p = window.__replayProfile = { start: performance.now(), frames: [], tasks: [] };
      setTimeout(() => {
        const percentile = (values, q) => [...values].sort((a, b) => a - b)[Math.min(values.length - 1, Math.floor(values.length * q))] || 0;
        resolve({ elapsedMs: performance.now() - p.start,
          displayCounts: document.querySelector('.events')?.textContent, longTasks: p.tasks.length,
          longTaskMs: p.tasks.reduce((sum, n) => sum + n, 0), worstTaskMs: Math.max(0, ...p.tasks),
          frameP50Ms: percentile(p.frames, .5), frameP95Ms: percentile(p.frames, .95),
          worstFrameMs: Math.max(0, ...p.frames), frameSamples: p.frames.length });
        window.__replayProfile = null;
      }, duration);
    }), duration);
    const result = { mode, speed: 43200, durationMs: duration, ...metrics, errors };
    fs.mkdirSync(root, { recursive: true });
    fs.writeFileSync(path.join(root, `browser-${mode}.json`), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
    if (errors.length) process.exitCode = 1;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
