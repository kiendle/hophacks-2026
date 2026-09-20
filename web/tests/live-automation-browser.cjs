// Browser workflow with deterministic network boundaries; no paid calls.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const config = JSON.parse(fs.readFileSync('harness/automation_config/automation-config.public-policy.json', 'utf8'));
const sse = events => events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('');
const card = status => ({ kind:'automation_proposal', id:'hash', session_id:'session', title:'Policy sentiment', revision:1, status,
  configuration:config, target_labels:config.targets.map(t=>t.label), rules:config.categorization.rules,
  cutoff:config.categorization.accept_probability, open_questions:[] });
const originalPost = 'ChatGPTと他サービスの連携のメリットが分かってきて触っていて楽しい これが仕事に繋がればもっといいのに';
const englishPost = "I'm starting to see the benefits of connecting ChatGPT with other services, and it's fun to experiment. It would be even better if this led to work.";
(async () => {
  const browser = await chromium.launch({ headless:true, executablePath:process.env.UI_BROWSER_PATH });
  try {
    const page = await browser.newPage({ viewport:{width:1440,height:960} });
    const errors=[]; page.on('pageerror', e=>errors.push(e.message));
    let paused=0, released=0, turns=0, polls=0, deliverMatches=false;
    let translations=0, finishTranslation;
    await page.route('**/api/posts/translate', async r=>{
      translations++;
      assert.equal(r.request().postDataJSON().text, originalPost);
      await new Promise(resolve=>finishTranslation=resolve);
      return r.fulfill({json:{translated_text:englishPost,source_language:'Japanese',is_english:false}});
    });
    await page.route('**/api/voice/status', r=>r.fulfill({json:{available:false}}));
    await page.route('**/api/live/agent/status', r=>r.fulfill({json:{available:false}}));
    await page.route('**/api/sessions', r=>r.fulfill({json:{session_id:'session'}}));
    await page.route('**/api/sessions/session/messages', r=>{
      const text=r.request().postDataJSON().text;
      turns++;
      if(turns===1) assert.match(text,/Automation proposal conversation/);
      else { assert.match(text,/get_live_tracking_data/); assert.match(text,/tracker-test/); }
      return r.fulfill({contentType:'text/event-stream',body:sse(turns===1
        ? [{type:'card',card:card('ready')},{type:'confirm_request',kind:'automation_proposal',confirmation_id:'abcdefgh12345678',expires_ms:Date.now()+60000,summary:'Review your transit schema.'},{type:'done'}]
        : [{type:'message',text:'These observations come from your transit tracker.'},{type:'done'}])});
    });
    await page.route('**/api/sessions/session/confirm', r=>r.fulfill({contentType:'text/event-stream',body:sse([{type:'card',card:card('final')},{type:'done'}])}));
    await page.route('**/api/automations/start', r=>{
      assert.deepEqual(r.request().postDataJSON(),{session_id:'session',proposal_hash:'hash',max_usd:0.1});
      return r.fulfill({json:{id:'tracker-test',title:'Policy sentiment',config,max_usd:.1}});
    });
    await page.route('**/api/automations/tracker-test/events?*', r=>{
      polls++;
      const now=Date.now(), start=now-180000;
      const events=deliverMatches && new URL(r.request().url()).searchParams.get('after')==='0'
        ? [0,1,2].map(i=>({id:'post:'+i,kind:'post',t:start+i*60000,postId:'at://did:plc:test/app.bsky.feed.post/'+i,
          postTime:start+i*60000,text:originalPost,authorId:'did:plc:test',contentVersion:''+i,observedAt:start+i*60000,
          publication:true,grades:[{company:config.targets[0].id,choice:'positive',confidence:.9,score:8,
            probabilities:{positive:.7,negative:.1,neutral:.1,mixed:.05,insufficient_evidence:.05}}]})) : [];
      return r.fulfill({json:{automation:{enabled:paused===0,worker_state:'idle',max_usd:.1,config,created_at:new Date(start).toISOString()},
        counts:deliverMatches ? {ready:3,pending:2} : {},source:{status:'connected',ingestion:{window_seconds:300,
          posts_last_5m:469,events_last_5m:3241,last_post_at:new Date(now-1000).toISOString(),
          last_event_at:new Date(now-500).toISOString(),as_of:new Date(now).toISOString()}},
        events,cursor:deliverMatches ? 3 : 0,more:false}});
    });
    await page.route('**/api/automations/tracker-test/pause', r=>{paused++;return r.fulfill({json:{enabled:false}})});
    await page.route('**/api/automations/tracker-test/release', r=>{released++;return r.fulfill({json:{released:true}})});
    await page.goto(process.env.AUTOMATION_UI_URL || 'http://127.0.0.1:5196');
    assert.equal(await page.getByRole('button',{name:'Historical data',exact:true}).getAttribute('aria-pressed'),'true');
    await page.getByRole('button',{name:'Live data',exact:true}).click();
    await page.getByRole('textbox',{name:'Automation goal'}).fill('Track sentiment about transit policy');
    await page.getByRole('button',{name:'Start',exact:true}).click();
    await page.getByRole('heading',{name:'Confirm automation proposal'}).waitFor();
    const dimensions=await page.evaluate(()=>({chat:document.querySelector('.chat').getBoundingClientRect().width,
      main:document.querySelector('.automation-setup').getBoundingClientRect().width,
      overflow:document.documentElement.scrollWidth>innerWidth}));
    assert.ok(dimensions.chat>=dimensions.main*.95,JSON.stringify(dimensions));
    assert.equal(dimensions.overflow,false);
    await page.screenshot({path:'harness/state/live-schema-chat.png'});
    await page.getByRole('button',{name:'Confirm configuration',exact:true}).click();
    await page.getByRole('button',{name:'Start live tracking',exact:true}).click();
    const ingestion = page.getByRole('status', {name:'Live ingestion activity'});
    await ingestion.getByText('Receiving posts', {exact:true}).waitFor();
    assert.match(await ingestion.innerText(), /469 posts ingested in the last 5 minutes/);
    assert.match(await ingestion.innerText(), /0 matching texts/);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:'harness/state/live-ingestion-no-matches.png'});
    deliverMatches=true;
    await page.getByText('3 classified texts · 2 awaiting Jev').waitFor();
    assert.equal(await page.getByRole('combobox',{name:'Playback speed'}).count(),0);
    assert.equal(await page.getByRole('combobox',{name:'Point interval'}).inputValue(),'60000');
    assert.equal(translations,0,'Hovering must not trigger a paid translation');
    const plot=await page.locator('.chart .overlay').boundingBox();
    await page.mouse.move(plot.x+plot.width/2,plot.y+plot.height/3);
    const preview=page.getByRole('region',{name:'Post preview'});
    await preview.waitFor();
    await preview.getByRole('button',{name:'Translate to English',exact:true}).click();
    await preview.getByRole('button',{name:'Translating…',exact:true}).waitFor();
    await page.mouse.move(10,10);
    await page.waitForTimeout(400);
    assert.equal(await preview.count(),1,'Translation keeps the selected post open');
    finishTranslation();
    await preview.getByText(englishPost,{exact:true}).waitFor();
    assert.equal(await preview.locator('.gauge-value').innerText(),'8.0');
    await page.screenshot({path:'harness/state/post-translation.png'});
    await preview.getByRole('button',{name:'Show original',exact:true}).click();
    await preview.getByText(originalPost,{exact:true}).waitFor();
    await preview.getByRole('button',{name:'Translate to English',exact:true}).click();
    await preview.getByText(englishPost,{exact:true}).waitFor();
    assert.equal(translations,1,'Toggling reuses the exact-text cached translation');
    await preview.getByRole('button',{name:'Close post preview'}).click();
    await page.getByRole('textbox',{name:'Message'}).fill('Why is transit sentiment positive?');
    await page.getByRole('button',{name:'Send',exact:true}).click();
    await page.getByText('These observations come from your transit tracker.').waitFor();
    await page.screenshot({path:'harness/state/live-tracking-dashboard.png'});
    await page.getByRole('button',{name:'Bubble view'}).click();
    await page.screenshot({path:'harness/state/live-tracking-bubbles.png'});
    await page.getByRole('button',{name:'Stop tracking',exact:true}).click();
    await ingestion.getByText('Tracking stopped', {exact:true}).waitFor();
    assert.equal(await ingestion.getByText('Receiving posts', {exact:true}).count(),0);
    await page.getByRole('button',{name:'Close tracker',exact:true}).click();
    await page.getByRole('textbox',{name:'Keyword', exact:true}).waitFor();
    await page.waitForTimeout(200);
    assert.ok(paused>=1); assert.ok(released>=1); assert.ok(polls>=1);
    assert.deepEqual(errors,[]);
    console.log('PASS: historical default, expanded schema chat, reviewed launch, minute chart, generic targets, live chat context, close/pause/release.');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1});
