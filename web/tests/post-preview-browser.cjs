// Exercise the real preview with local fixtures; never contact Twitter or a model.
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/Kaan Eroltu/AppData/Local/npm-cache/_npx/420ff84f11983ee5/node_modules/playwright');

(async () => {
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.UI_BROWSER_PATH || 'C:/Users/Kaan Eroltu/AppData/Local/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe' });
  try {
    const page = await browser.newPage({ viewport: { width: 1000, height: 750 } });
    page.setDefaultTimeout(10_000);
    const errors = [], translations = [];
    page.on('pageerror', error => errors.push(error.message));
    const original = 'Ein langer Beitrag über künstliche Intelligenz. '.repeat(30) + 'ORIGINAL TAIL';
    const translated = 'A translated post about artificial intelligence. '.repeat(30) + 'TRANSLATED TAIL';
    await page.route('**/api/**', route => {
      if (route.request().url().endsWith('/posts/translate')) {
        translations.push(route.request().postDataJSON().text);
        return route.fulfill({ json: { translated_text: translated, source_language: 'German', is_english: false } });
      }
      return route.fulfill({ json: { available: false } });
    });
    await page.goto(process.env.UI_BASE_URL || 'http://127.0.0.1:5198');
    await page.evaluate(async original => {
      const { default: React } = await import('/node_modules/.vite/deps/react.js');
      const { default: ReactDOM } = await import('/node_modules/.vite/deps/react-dom_client.js');
      const { HoverCard } = await import('/src/components/HoverCard.tsx');
      const fixture = document.createElement('div');
      fixture.style.cssText = 'position:fixed;inset:0;background:#f6f6f4;z-index:10000';
      document.body.append(fixture);
      const root = ReactDOM.createRoot(fixture);
      window.renderPreview = (text, id = 'twitter:1234567890123456789', bounds = { width: 800, height: 500 }) => root.render(React.createElement(React.StrictMode, null, React.createElement(HoverCard, {
        post: { id, text, handle: '', time: 0, sentiment: 7, likes: 100, replies: 0, retweets: 0, quotes: 0 },
        anchor: { x: 300, y: 250 }, bounds,
        onInteract: () => { window.previewPinned = true; },
        onClose: () => root.render(null),
      })));
      window.renderPreview(original);
    }, original);
    const card = page.getByRole('region', { name: 'Post preview' });
    const text = card.locator('.card-text');
    const source = card.getByRole('link', { name: 'See on Twitter' });
    await card.waitFor();
    assert.doesNotMatch(await text.innerText(), /ORIGINAL TAIL/);
    assert.equal(await source.getAttribute('href'), 'https://x.com/i/status/1234567890123456789');
    assert.equal(await source.getAttribute('target'), '_blank');
    await card.getByRole('button', { name: 'Show full text', exact: true }).click();
    const reader = page.getByRole('dialog', { name: 'Full post', exact: true });
    const body = reader.getByRole('region', { name: 'Full post text' });
    const fullText = body.locator('p');
    await reader.waitFor();
    assert.equal(await fullText.textContent(), original);
    assert.equal(await page.evaluate(() => window.previewPinned), true, 'Interacting pins the chart preview');
    await body.evaluate(node => { node.scrollTop = node.scrollHeight; });
    assert.ok(await body.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop < 2), 'The actual end of the tweet is reachable');
    await reader.getByRole('button', { name: 'Translate to English', exact: true }).click();
    await reader.getByRole('button', { name: 'Show original', exact: true }).waitFor();
    assert.equal(await fullText.textContent(), translated, 'Translation is complete inside the reader');
    assert.deepEqual(translations, [original]);
    await page.keyboard.press('Escape');
    await reader.waitFor({ state: 'detached' });
    assert.equal(await card.getByRole('button', { name: 'Show full text', exact: true }).evaluate(node => node === document.activeElement), true, 'Closing restores focus to the opener');
    assert.doesNotMatch(await text.innerText(), /TRANSLATED TAIL/);
    await card.getByRole('button', { name: 'Show full text', exact: true }).click();
    assert.equal(await fullText.textContent(), translated);
    assert.equal(await reader.getByRole('link', { name: 'See on Twitter' }).getAttribute('href'), 'https://x.com/i/status/1234567890123456789');
    const box = await reader.boundingBox();
    const footer = await reader.locator('.post-reader-footer').boundingBox();
    assert.ok(box.width >= 600 && box.y >= 0 && box.y + box.height <= 750, 'Reader uses a comfortable width inside the viewport');
    assert.ok(footer.y + footer.height <= box.y + box.height, 'Reader footer remains visible');
    await page.screenshot({ path: path.join(os.tmpdir(), 'post-reader-desktop.png') });
    await page.setViewportSize({ width: 390, height: 700 });
    const mobile = await reader.boundingBox();
    assert.ok(mobile.x >= 0 && mobile.x + mobile.width <= 390 && mobile.y + mobile.height <= 700, 'Reader fits mobile viewport');
    await body.focus();
    for (let i = 0; i < 8; i++) {
      await page.keyboard.press('Tab');
      assert.equal(await reader.evaluate(node => node.contains(document.activeElement)), true, 'Focus stays inside the dialog');
    }
    await page.screenshot({ path: path.join(os.tmpdir(), 'post-reader-mobile.png') });
    await reader.getByRole('button', { name: 'Close full post', exact: true }).click();
    await reader.waitFor({ state: 'detached' });
    await page.setViewportSize({ width: 1000, height: 750 });
    await card.getByRole('button', { name: 'Show full text', exact: true }).click();
    await page.mouse.click(5, 5);
    await reader.waitFor({ state: 'detached' });

    await page.evaluate(() => window.renderPreview('Short post', 'not-a-twitter-id'));
    await page.waitForFunction(() => document.querySelector('.card-text')?.textContent === 'Short post');
    assert.equal(await source.count(), 0, 'Non-Twitter IDs do not generate broken Twitter links');
    assert.equal(await card.getByRole('button', { name: 'Show full text', exact: true }).count(), 0);
    // Even a post below the character limit can be clipped by a small viewport.
    await page.evaluate(() => window.renderPreview('Many words in a narrow preview. '.repeat(10), '123', { width: 180, height: 250 }));
    await card.getByRole('button', { name: 'Show full text', exact: true }).click();
    assert.equal(await fullText.textContent(), 'Many words in a narrow preview. '.repeat(10));
    await reader.getByRole('button', { name: 'Close full post', exact: true }).click();
    await reader.waitFor({ state: 'detached' });
    assert.equal(await source.getAttribute('href'), 'https://x.com/i/status/123', 'Plain numeric IDs still work');
    await card.getByRole('button', { name: 'Close post preview' }).click();
    await card.waitFor({ state: 'detached' });
    assert.deepEqual(errors, []);
    console.log('PASS: Twitter links, full-text dialog, translation persistence, scrolling, desktop/mobile layout, focus trapping/restoration, Escape/backdrop/close, short/clipped posts.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
