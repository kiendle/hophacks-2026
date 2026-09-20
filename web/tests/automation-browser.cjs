// Browser integration with deterministic chat responses; no model or inference calls.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const config = JSON.parse(fs.readFileSync('harness/automation_config/automation-config.public-policy.json', 'utf8'));
const sse = events => events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('');
const card = (revision, status) => ({kind:'automation_proposal', id:`proposal-${revision}`, title:'Policy sentiment', revision, status,
  configuration:config, target_labels:config.targets.map(t=>t.label), rules:config.categorization.rules,
  cutoff:config.categorization.accept_probability, open_questions:[]});
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.UI_BROWSER_PATH || undefined});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:960}});
    const errors=[]; page.on('pageerror', error=>errors.push(error.message));
    let turns=0;
    await page.route('**/api/voice/status', route=>route.fulfill({json:{available:false,reason:'Voice disabled in this test.'}}));
    await page.route('**/api/live/agent/status', route=>route.fulfill({json:{available:false}}));
    await page.route('**/api/sessions', route=>route.fulfill({json:{session_id:'proposal-browser'}}));
    await page.route('**/api/sessions/proposal-browser/messages', async route=>{
      const text=route.request().postDataJSON().text;
      assert.match(text,/Automation proposal conversation/);
      assert.doesNotMatch(text,/Chart context/);
      turns++;
      const events=turns===1
        ? [{type:'message',text:'Should we track congestion pricing, transit funding, or both?'}]
        : [{type:'card',card:card(turns-1,'ready')}, {type:'confirm_request',kind:'automation_proposal',confirmation_id:'abcdefgh12345678',expires_ms:Date.now()+60000,summary:'Approve this configuration. No collection will start.'}];
      await route.fulfill({contentType:'text/event-stream',body:sse([...events,{type:'done'}])});
    });
    await page.route('**/api/sessions/proposal-browser/confirm', route=>{
      assert.equal(route.request().postDataJSON().approved,true);
      return route.fulfill({contentType:'text/event-stream',body:sse([{type:'card',card:card(1,'final')},{type:'message',text:'Your final configuration is ready.'},{type:'done'}])});
    });
    await page.goto(process.env.AUTOMATION_UI_URL || 'http://127.0.0.1:5197');
    await page.getByRole('button',{name:'Build automation',exact:true}).click();
    await page.getByRole('textbox',{name:'Automation goal'}).fill('Analyze sentiment about public transport policy');
    await page.getByRole('button',{name:'Start',exact:true}).click();
    await page.getByText('Should we track congestion pricing, transit funding, or both?').waitFor();
    assert.equal(turns,1,'Initial goal must be sent exactly once');
    const input=page.locator('.input-row textarea');
    await input.fill('Both. Include implementation costs and use the proposed defaults.');
    await page.getByRole('button',{name:'Send',exact:true}).click();
    await page.getByRole('heading',{name:'Confirm automation proposal'}).waitFor();
    await page.getByRole('button',{name:'Confirm configuration',exact:true}).click();
    await page.getByRole('button',{name:'Download configuration',exact:true}).waitFor();
    const downloaded=page.waitForEvent('download');
    await page.getByRole('button',{name:'Download configuration',exact:true}).click();
    const download=await downloaded;
    assert.deepEqual(JSON.parse(fs.readFileSync(await download.path(),'utf8')),config);
    await input.fill('Revise the relevance rule to include local funding debates.');
    await page.getByRole('button',{name:'Send',exact:true}).click();
    await page.getByText('Ready for review · Revision 2').waitFor();
    assert.equal(await page.getByRole('button',{name:'Download configuration',exact:true}).count(),0,'A newer draft must supersede the previously final card');
    await page.getByRole('button',{name:'Download draft',exact:true}).waitFor();
    const screenshot=path.join(os.tmpdir(),'automation-proposal-browser.png');
    await page.screenshot({path:screenshot});
    assert.deepEqual(errors,[]);
    console.log('PASS: goal, clarification, validated proposal, confirmation, exact JSON download, revision supersession.');
    console.log(`Screenshot: ${screenshot}`);
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1});
