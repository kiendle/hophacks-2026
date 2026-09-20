// Real chat UI and SSE parser, with deterministic streams and no model calls.
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/Kaan Eroltu/AppData/Local/npm-cache/_npx/420ff84f11983ee5/node_modules/playwright');

(async () => {
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.UI_BROWSER_PATH || 'C:/Users/Kaan Eroltu/AppData/Local/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    page.setDefaultTimeout(10_000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/sessions') return route.fulfill({ json: { session_id: 'scroll-fixture' } });
      if (url.pathname.endsWith('/status')) return route.fulfill({ json: { available: false } });
      return route.fulfill({ status: 404, json: { error: 'Unmocked test endpoint' } });
    });
    await page.addInitScript(() => {
      const fetchOriginal = window.fetch.bind(window);
      window.testTurns = [];
      window.fetch = async (url, options) => {
        if (String(url).endsWith('/sessions/scroll-fixture/messages')) {
          window.testTurns.push(JSON.parse(options.body));
          const body = new ReadableStream({
            start(controller) {
              window.chatEvent = event => controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`));
              window.chatDone = () => { window.chatEvent({ type: 'done' }); controller.close(); };
              options.signal.addEventListener('abort', () => controller.error(new DOMException('Stopped', 'AbortError')));
            },
          });
          return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } });
        }
        return fetchOriginal(url, options);
      };
    });
    await page.goto(process.env.UI_BASE_URL || 'http://127.0.0.1:5198');
    await page.getByRole('button', { name: 'Live data', exact: true }).click();
    await page.getByRole('textbox', { name: 'Automation goal' }).fill('Explain this activity');
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.waitForFunction(() => window.testTurns.length === 1);
    const scroller = page.getByRole('region', { name: 'Chat messages' });
    const input = page.getByRole('textbox', { name: 'Message', exact: true });
    const latest = page.getByRole('button', { name: 'Latest message', exact: true });
    const activity = page.locator('.messages .tool-activity').last();
    const atBottom = () => page.waitForFunction(() => {
      const node = document.querySelector('.messages');
      return Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop) <= 2;
    });
    const emit = async event => {
      await page.evaluate(event => window.chatEvent(event), event);
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    };
    await emit({ type: 'step', id: 'read', phase: 'start', title: 'Reading saved sentiment', detail: { why: 'Look through the saved chart.' } });
    await page.waitForFunction(() => document.querySelector('.tool-activity')?.open);
    await activity.locator('.tool-step summary').click();
    await emit({ type: 'delta', text: 'A long response paragraph about the chart.\n\n'.repeat(40) });
    await atBottom();
    assert.ok(await scroller.evaluate(node => node.scrollHeight > node.clientHeight + 300));

    // Wheel upward while streaming must pause following, without fighting the reader.
    await scroller.hover();
    await page.mouse.wheel(0, -650);
    await latest.waitFor();
    await page.waitForTimeout(200);
    const readingTop = await scroller.evaluate(node => node.scrollTop);
    await emit({ type: 'delta', text: '\n\nMore observations. '.repeat(120) });
    await page.waitForTimeout(100);
    assert.ok(Math.abs(await scroller.evaluate(node => node.scrollTop) - readingTop) < 3, 'Streaming preserves the reading position');

    // Natural wheel scrolling back to the end resumes following.
    await page.mouse.wheel(0, 100000);
    await atBottom();
    await latest.waitFor({ state: 'hidden' });
    await emit({ type: 'delta', text: '\n\nA later streamed paragraph. '.repeat(80) });
    await atBottom();
    await scroller.evaluate(node => { node.scrollTop = 0; });
    await latest.waitFor();
    await latest.click();
    await atBottom();

    // Completing a reply closes the whole report and any open nested details.
    await emit({ type: 'step', id: 'read', phase: 'end', ok: true, outcome: 'Found saved posts.', ms: 1200 });
    await page.evaluate(() => window.chatDone());
    await page.getByRole('button', { name: 'Send', exact: true }).waitFor();
    assert.equal(await activity.evaluate(node => node.open), false);
    assert.equal(await activity.locator('.tool-step details').evaluate(node => node.open), false);
    await atBottom();
    await activity.locator(':scope > summary').click();
    assert.equal(await activity.evaluate(node => node.open), true, 'Completed reports can be reopened');
    await activity.locator('.tool-step summary').click();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await scroller.evaluate(node => { node.scrollTop = 0; });
    await latest.waitFor();
    await latest.click();
    await atBottom();

    // Composer growth, viewport changes, and wrapping keep the final message reachable.
    await input.fill('A multiline question\n'.repeat(20));
    await atBottom();
    await page.setViewportSize({ width: 1000, height: 650 });
    await atBottom();
    await input.fill('');
    await atBottom();
    const tail = await page.locator('.messages .msg-assistant').last().boundingBox();
    const composer = await page.locator('.input-row').boundingBox();
    assert.ok(tail.y + tail.height <= composer.y, 'Last reply is above the composer');
    assert.equal(await scroller.evaluate(node => getComputedStyle(node).scrollbarWidth), 'thin');
    assert.equal(await page.evaluate(() => document.documentElement.scrollHeight > innerHeight), false, 'Chat stays within the viewport');

    // Sending while reading history returns to the new turn; keyboard navigation also works.
    await scroller.evaluate(node => { node.scrollTop = 0; });
    await latest.waitFor();
    await input.fill('Follow up');
    await input.press('Shift+Enter');
    assert.equal(await page.evaluate(() => window.testTurns.length), 1);
    await input.press('Enter');
    await page.waitForFunction(() => window.testTurns.length === 2);
    await atBottom();
    await emit({ type: 'delta', text: 'Final reply. '.repeat(130) });
    await page.evaluate(() => window.chatDone());
    await page.getByRole('button', { name: 'Send', exact: true }).waitFor();
    await atBottom();
    await scroller.focus();
    await scroller.press('Control+Home');
    await latest.waitFor();
    await scroller.press('Control+End');
    await atBottom();
    assert.equal(await activity.evaluate(node => node.open), true, 'Reopened activity stays open across later turns');
    await page.screenshot({ path: path.join(os.tmpdir(), 'chat-scroll-browser.png') });
    assert.deepEqual(errors, []);
    console.log('PASS: streaming, wheel and keyboard scrolling, latest button, activity collapse/reopen, composer growth, viewport resizing, follow-up send.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
