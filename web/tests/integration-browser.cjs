const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const ts=require('typescript');
const vm=require('node:vm');
const exportsObject={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync('web/src/voice/speech.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:exportsObject});
const long='A sentence about AI. '.repeat(140);
const chunks=exportsObject.speechChunks(long);
assert(chunks.length>1 && chunks.every(c=>c.length<=900));
assert.equal(chunks.join(' '),long.trim());
assert(!exportsObject.speechChunks('Read [this](https://example.com) and https://example.com/a')[0].includes('https:'));
// A short generated PCM fixture: no paid speech calls in this browser test.
const samples=80000, audio=Buffer.alloc(44+samples*2);
audio.write('RIFF');audio.writeUInt32LE(audio.length-8,4);audio.write('WAVEfmt ',8);
audio.writeUInt32LE(16,16);audio.writeUInt16LE(1,20);audio.writeUInt16LE(1,22);
audio.writeUInt32LE(8000,24);audio.writeUInt32LE(16000,28);audio.writeUInt16LE(2,32);audio.writeUInt16LE(16,34);
audio.write('data',36);audio.writeUInt32LE(samples*2,40);
for(let i=0;i<samples;i++)audio.writeInt16LE(Math.round(1000*Math.sin(i*.2)),44+i*2);
const id='abcdefgh12345678';
const sse=events=>events.map(e=>`data: ${JSON.stringify(e)}\n\n`).join('');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.UI_BROWSER_PATH || undefined,args:['--autoplay-policy=no-user-gesture-required','--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream']});
 try{
 const page=await browser.newPage({viewport:{width:1440,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>{localStorage.setItem('signal.voice.aloud','yes');window.playedAudio=[];const Original=window.Audio;window.Audio=function(...args){const a=new Original(...args);window.playedAudio.push(a);return a};window.Audio.prototype=Original.prototype;const get=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);navigator.mediaDevices.getUserMedia=async(...args)=>{const stream=await get(...args);window.testMic=stream;return stream}});
 await page.route('**/api/voice/status',r=>r.fulfill({json:{available:true}}));
 await page.route('**/api/live/status',r=>r.fulfill({json:{available:true}}));
 await page.route('**/api/ui/scan',r=>r.fulfill({json:{series:[],now:null,streaming:false,read:0,kept:0,note:'Archive test fixture'}}));
 await page.route('**/api/sessions',r=>r.fulfill({json:{session_id:'test-session'}}));
 await page.route('**/api/briefs/20260919-230000',r=>r.fulfill({json:{id:'20260919-230000',status:'ready',title:'Audio briefing',audio:{full:'mix.mp3'},segments:[{headline:'Saved analysis',stories:[{title:'A source story'}]}],notes:[]}}));
 await page.route('**/api/briefs/20260919-230000/audio/mix.mp3',r=>r.fulfill({contentType:'audio/wav',body:audio}));
 let telegramSends=0;
 await page.route('**/api/briefs/20260919-230000/telegram',r=>{telegramSends++;return r.fulfill({json:{sent:true,message_id:42}})});
 let speechRequests=0;
 await page.route('**/api/voice/speak',r=>{speechRequests++;assert(r.request().postDataJSON().text.length<=900);return r.fulfill({contentType:'audio/wav',body:audio})});
 let transcriptionRequests=0;
 await page.route('**/api/voice/transcribe',r=>{transcriptionRequests++;assert(r.request().postDataBuffer().length>=256);return r.fulfill({json:{text:'My spoken question'}})});
 let confirmationBody=null;
 await page.route('**/api/sessions/test-session/confirm',r=>{confirmationBody=r.request().postDataJSON();return r.fulfill({contentType:'text/event-stream',body:sse([{type:'delta',text:'Confirmed.'},{type:'done'}])})});
 await page.route('**/api/sessions/test-session/messages',r=>r.fulfill({contentType:'text/event-stream',body:sse([
 {type:'step',phase:'start',id:'step1',title:'Reading saved posts',why:'Checking the archive for your topic.',detail:{tool:'preview_keywords',input:{reason:'Check saved posts',keywords:['AI']}}},
 {type:'step',phase:'end',id:'step1',ok:true,outcome:'Found twelve posts.',ms:500},
 {type:'preview',title:'Saved Twitter preview',total:12,per_day:[{day:'2026-09-09',count:12}],examples:[{id:'123',body:'A source post',full_text:'A source post with the complete text.',url:'https://x.com/i/status/123',like_count:5}]},
 {type:'card',card:{kind:'chart',title:'Sentiment analysis',bars:[{label:'Positive',share:.75,posts:9}],caption:'Based on saved posts.'}},
 {type:'card',card:{kind:'brief',brief_id:'20260919-230000',title:'Audio briefing'}},
 {type:'confirm_request',confirmation_id:id,summary:'Save this Twitter project?',expires_ms:Date.now()+300000},
 {type:'delta',text:'Here is the saved analysis.'},{type:'done'}])}));
 await page.goto('http://127.0.0.1:5196');await page.getByRole('textbox',{name:'Topic'}).fill('AI');await page.getByRole('button',{name:'Start',exact:true}).click();await page.getByRole('button',{name:'Start',exact:true}).click();
 assert.equal(await page.getByRole('button',{name:/Auto-play replies:/}).getAttribute('aria-pressed'),'false');
 await page.getByPlaceholder('Ask',{exact:true}).fill('Show the saved analysis');await page.getByRole('button',{name:'Send',exact:true}).click();
 await page.locator('.tool-step summary').first().click();await page.getByText('Found twelve posts.',{exact:true}).waitFor();await page.getByRole('heading',{name:'Saved Twitter preview'}).waitFor();await page.getByRole('heading',{name:'Sentiment analysis'}).waitFor();
 await page.getByText('Show full post',{exact:true}).click();await page.getByText('A source post with the complete text.',{exact:true}).first().waitFor();
 assert.equal(await page.getByRole('link',{name:'Open on X'}).getAttribute('href'),'https://x.com/i/status/123');
 assert.equal(speechRequests,0,'An old saved preference must not speak a reply');
 await page.getByRole('button',{name:/Auto-play replies:/}).click();
 assert.equal(confirmationBody,null);await page.getByRole('button',{name:'Confirm',exact:true}).click();await page.getByText('Confirmed.',{exact:true}).last().waitFor();assert.deepEqual(confirmationBody,{confirmation_id:id,approved:true});
 await page.waitForFunction(()=>window.playedAudio.some(a=>a.currentTime>0));assert(speechRequests>0);
 await page.getByRole('button',{name:'Stop audio',exact:true}).click();
 await page.getByRole('button',{name:/Auto-play replies:/}).click();
 await page.getByLabel('Play audio brief').waitFor();await page.getByLabel('Play audio brief').evaluate(a=>a.play());await page.waitForFunction(()=>document.querySelector('audio')?.currentTime>0);await page.getByLabel('Play audio brief').evaluate(a=>a.pause());
 await page.getByRole('button',{name:'Send to Telegram',exact:true}).click();await page.getByRole('button',{name:'Sent to Telegram',exact:true}).waitFor();assert.equal(telegramSends,1);
 await page.getByRole('button',{name:'Dictate a message'}).click();await page.getByRole('button',{name:'Stop dictation',exact:true}).waitFor();await page.waitForTimeout(1200);await page.getByRole('button',{name:'Stop dictation',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('textarea[placeholder="Ask"]')?.value==='My spoken question');assert.equal(transcriptionRequests,1);assert(await page.evaluate(()=>window.testMic.getTracks().every(t=>t.readyState==='ended')));
 await page.getByRole('button',{name:'Dictate a message'}).click();await page.getByRole('button',{name:'Stop dictation',exact:true}).waitFor();await page.getByRole('button',{name:'Cancel recording',exact:true}).click();assert.equal(transcriptionRequests,1);
 await page.evaluate(()=>{navigator.mediaDevices.getUserMedia=async()=>{throw new DOMException('Denied','NotAllowedError')}});await page.getByRole('button',{name:'Dictate a message'}).click();await page.getByText(/Microphone access was denied/).waitFor();
 await page.screenshot({path:'harness/state/cards-and-voice-controls.png'});
 assert.deepEqual(errors,[]);console.log('PASS: speech chunks, tool calls, details, previews, charts, audio brief playback, explicit confirmation, auto reading, stop, microphone transcription, cancellation, permission denial.');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
