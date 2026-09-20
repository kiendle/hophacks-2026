// Run with LIVE_VOICE_CHECK=1 to spend one short realtime session using an offline speech fixture.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const path = require('node:path');

(async () => {
  if (process.env.LIVE_VOICE_CHECK !== '1') throw new Error('Set LIVE_VOICE_CHECK=1 to explicitly run the real provider check.');
  const workspace = process.env.VOICE_WORKSPACE_CHECK === '1';
  const browser = await chromium.launch({ headless: true, args: [
    '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
    `--use-file-for-fake-audio-capture=${path.resolve(process.env.VOICE_TEST_CLIP || 'harness/state/realtime-voice-test.wav')}%noloop`,
    '--enable-unsafe-swiftshader',
  ] });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
    const oldRequests = [], errors = [];
    page.on('request', request => { if (/\/api\/live\/(listen|say)$/.test(request.url())) oldRequests.push(request.url()); });
    page.on('pageerror', error => errors.push(error.name));
    await page.addInitScript(() => {
      window.voiceMedia = []; window.voiceTracks = [];
      const play = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = function () { window.voiceMedia.push(this); return play.call(this); };
      const get = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia = async constraints => { const stream = await get(constraints); window.voiceTracks.push(...stream.getTracks()); return stream; };
    });
    await page.route('**/api/ui/scan', route => route.fulfill({ json: { series: [], now: null, streaming: false, read: 0, kept: 0, note: 'Voice interface check' } }));
    let workspaceCalls = 0;
    if (workspace) {
      await page.route('**/api/sessions', route => route.fulfill({ json: { session_id: 'voice-workspace-test' } }));
      await page.route('**/api/sessions/voice-workspace-test/messages', route => {
        workspaceCalls++;
        return route.fulfill({ contentType: 'text/event-stream', body: [
          { type: 'delta', text: 'There are twelve posts in your chart.' }, { type: 'done' },
        ].map(event => `data: ${JSON.stringify(event)}\n\n`).join('') });
      });
    }
    await page.goto(process.env.VOICE_TEST_URL || 'http://127.0.0.1:5217');
    await page.getByRole('textbox', { name: 'Topic' }).fill('AI');
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.getByRole('button', { name: 'Talk live', exact: true }).click();
    await page.getByRole('dialog').waitFor();
    await page.waitForTimeout(1800);
    await page.screenshot({ path: 'harness/state/realtime-cloud-desktop.png' });
    await page.getByRole('button', { name: 'Show transcript', exact: true }).click();
    await page.locator('.live-transcript p[data-role="assistant"]').filter({ hasText: workspace ? /twelve|12/i : /hello|hi\b/i }).first().waitFor({ timeout: 35000 });
    await page.waitForFunction(() => document.querySelector('.live-room-state')?.textContent === 'Speaking' && Number(document.querySelector('.cloud-orb')?.style.transform.match(/[\d.]+/)?.[0]) > 1.005, { timeout: 10000 });
    await page.waitForFunction(() => window.voiceMedia.some(audio => audio.currentTime > 0 && !audio.muted && audio.volume > 0), { timeout: 10000 });
    assert(await page.locator('.live-transcript p[data-role="user"]').count());
    assert.deepEqual(oldRequests, [], 'Realtime voice must not use the old separate STT/TTS routes');
    if (workspace) assert.equal(workspaceCalls, 1, 'One voice question must make exactly one workspace request');
    await page.getByRole('button', { name: 'Mute microphone', exact: true }).click();
    await page.getByRole('button', { name: 'Unmute microphone', exact: true }).waitFor();
    await page.screenshot({ path: 'harness/state/realtime-cloud-transcript.png' });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: 'Hide transcript', exact: true }).click();
    await page.screenshot({ path: 'harness/state/realtime-cloud-mobile.png' });
    await page.getByRole('button', { name: 'End live voice', exact: true }).click();
    await page.waitForFunction(() => window.voiceTracks.every(track => track.readyState === 'ended'));
    assert.deepEqual(errors, []);
    console.log('PASS: real agent transcript and audible output, cloud dialog, mobile layout, mute and released microphone. No legacy STT/TTS requests.');
  } catch (error) {
    const page = browser.contexts()[0]?.pages()[0];
    if (page) {
      console.log('Voice UI:', await page.locator('.live-room-state, .live-room-error').allTextContents());
      await page.screenshot({ path: 'harness/state/realtime-cloud-debug.png' });
    }
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exit(1); });
