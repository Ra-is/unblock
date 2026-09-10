/* Real browser recording with a clearly labeled controlled-mailbox test harness.
 * Requires Playwright (Chrome channel), ffmpeg, ffprobe and macOS `say` for narration.
 * No application responses or case state are mocked. Credentials are never rendered.
 */
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const http = require('node:http');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const exec = promisify(execFile);
const root = path.resolve(__dirname, '..');
const output = path.join(root, '.local/recording');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const helper = async (...args) => (await exec('uv', ['run','python','scripts/record_helpers.py',...args],{cwd:root,maxBuffer:2e6})).stdout;
const esc = value => String(value || '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function main(){
  await fs.mkdir(output,{recursive:true});
  const meta = JSON.parse(await helper('setup'));
  const privateSession = JSON.parse(await fs.readFile(path.join(output,'session.json'),'utf8'));
  const browser = await chromium.launch({headless:true,channel:'chrome'});
  const context = await browser.newContext({viewport:{width:1440,height:900},recordVideo:{dir:output,size:{width:1440,height:900}},deviceScaleFactor:1});
  await context.addInitScript(({origin,token})=>{if(location.origin===origin)sessionStorage.setItem('unblock_token',token);},{origin:meta.url,token:privateSession.token});
  const page = await context.newPage();
  const began = Date.now();
  const clips = [];
  const errors = [];
  page.on('pageerror',error=>errors.push(error.message));
  let mailbox, replied = false;
  const server = http.createServer(async(req,res)=>{
    try{
      if(req.method==='POST' && req.url==='/reply'){
        if(!replied){replied=true;await helper('reply','correct');}
        res.writeHead(200,{'Content-Type':'application/json'});res.end('{"sent":true}');return;
      }
      if(req.method!=='GET' || req.url!=='/mail'){res.writeHead(404);res.end();return;}
      const html=`<!doctype html><html><head><title>Controlled supplier inbox — recording harness</title><style>body{font:18px system-ui;background:#f0f5f7;color:#163743;margin:0;padding:65px 140px}small{color:#607a85}article{background:white;border:1px solid #d4e0e6;border-radius:12px;padding:36px;margin-top:22px}h1{font-size:30px}pre{font:18px/1.7 system-ui;white-space:pre-wrap}button{background:#086c65;color:white;border:0;border-radius:6px;padding:16px 22px;font:17px system-ui;cursor:pointer}.label{background:#dcece9;padding:12px 18px;border-radius:6px;font-size:15px}#sent{color:#096c56}</style></head><body><div class="label">Controlled supplier inbox · Recording/test harness · Real SES delivery</div><h1>${esc(mailbox.subject)}</h1><small>Received ${esc(mailbox.received_at)}<br>From ${esc(mailbox.from)}<br>To ${esc(mailbox.to)}</small><article><pre>${esc(mailbox.body)}</pre><hr><p>Attachment for reply: <strong>correct-receipt.txt</strong> · synthetic delivery receipt for PO-1042</p><button id="send">Reply with corrected receipt</button><p id="sent"></p></article><script>document.getElementById('send').onclick=async()=>{document.getElementById('send').disabled=true;const r=await fetch('/reply',{method:'POST'});document.getElementById('sent').textContent=r.ok?'Reply sent through Amazon SES.':'Delivery failed.';};</script></body></html>`;
      res.writeHead(200,{'Content-Type':'text/html'});res.end(html);
    }catch(error){res.writeHead(500);res.end('Recording harness error');errors.push(error.message);}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  async function narrate(text){
    console.log(text);
    const audio = path.join(output,`narration-${clips.length}.aiff`);
    await exec('say',['-v','Samantha','-r','172','-o',audio,text]);
    const duration=Number((await exec('ffprobe',['-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',audio])).stdout);
    clips.push({start:(Date.now()-began)/1000,duration,text,audio});
    await sleep((duration+1)*1000);
  }
  try{
    await page.goto(meta.url);
    await page.locator('#workspace').waitFor({state:'visible'});
    await page.locator('#new-case').click();
    for(const [name,value] of Object.entries({supplier:'Ama Catering Ltd',invoice_ref:'INV-2041',order_ref:'PO-1042',amount:'4800.00',contact_name:'Synthetic supplier contact',contact_email:meta.contact}))await page.locator(`[name="${name}"]`).fill(value);
    await narrate('A small supplier has delivered the work, but an invoice is waiting on the right paperwork. Unblock helps the operations team resolve that missing evidence. This recording uses the real AWS deployment, with synthetic documents and controlled mailboxes.');
    const created=page.waitForResponse(r=>r.url().endsWith('/api/cases')&&r.request().method()==='POST');
    await page.locator('#create-form button[type="submit"]').click();
    const caseId=(await (await created).json()).id;
    await helper('bind',caseId);
    const selectCase=async()=>{await page.locator(`[data-case="${caseId}"]`).click();};
    await page.locator('#create-dialog').waitFor({state:'hidden'});
    await selectCase();
    for(const name of ['invoice.txt','purchase-order.txt','wrong-receipt.txt']){
      const uploaded=page.waitForResponse(r=>r.url().endsWith(`/cases/${caseId}/documents`)&&r.request().method()==='POST');
      await page.locator('#upload-file').setInputFiles(path.join(root,'examples',name));
      await uploaded;
      await page.waitForFunction(name=>[...document.querySelectorAll('[data-download]')].some(e=>e.textContent.includes(name)),name);
    }
    await narrate('The case expects an invoice, a purchase order, and a signed delivery receipt for the same order. These files contain a deliberate mistake. We ask the Strands agent to investigate.');
    await page.locator('#run-agent').click();
    await page.getByText('Waiting for supplier reply',{exact:true}).waitFor({timeout:180000});
    await page.locator('[data-tab="requests"]').click();
    await narrate('The first receipt belongs to the wrong order. Unblock leaves the case blocked, sends a correction request through Amazon SES, and stops. It does not pretend that a sent request means the evidence is complete.');
    mailbox=JSON.parse(await helper('mail'));
    await helper('reply','wrong');
    await page.locator('[data-tab="held"]').click();
    await page.getByText('Sender is not the case contact',{exact:true}).waitFor({timeout:180000});
    await narrate('An authenticated message from a different address is held for review. Its attachment does not become evidence. The receiving code checks email authentication and the contact assigned to this case.');
    await page.screenshot({path:path.join(output,'held.png')});
    await page.goto(`http://127.0.0.1:${server.address().port}/mail`);
    await narrate('This is the request received in our controlled supplier inbox. The viewer is a test harness, not another product feature. The message arrived through real email transport. Now the actual case contact replies with the corrected receipt.');
    await page.locator('#send').click();
    await page.getByText('Reply sent through Amazon SES.',{exact:true}).waitFor({timeout:60000});
    await sleep(2000);
    await page.goto(meta.url);
    await page.locator('#workspace').waitFor({state:'visible'});
    await selectCase();
    await page.waitForFunction(()=>document.querySelectorAll('.requirement .check.ok').length===3,{},{timeout:180000});
    await narrate('The email wakes the background worker. Strands examines the replacement, and application rules confirm that it matches the order. All three evidence requirements are now satisfied. Nobody clicked Review evidence again.');
    await page.locator('[data-tab="activity"]').click();
    await narrate('The activity trail shows the supplier reply and resumed review. The model can interpret evidence and request corrections, but it cannot authorize payment. A reviewer makes the final packet decision.');
    await page.locator('#review').click();
    await page.locator('#review-note').fill('Live demonstration: reviewed the matching invoice, purchase order, and corrected delivery receipt. Synthetic documents; no payment authorized.');
    await page.locator('#review-form button[type="submit"]').click();
    await page.locator('#review-dialog').waitFor({state:'hidden'});
    await page.getByText('Accepted by reviewer',{exact:true}).waitFor({timeout:30000});
    await helper('proof');
    await page.screenshot({path:path.join(output,'complete.png')});
    await narrate('Unblock combines Strands and Bedrock with private S3 storage, Cognito, DynamoDB, SQS, Lambda and SES. This is a working pilot: real follow-up, a real reply, and a reviewable outcome. The public sample is separately labeled as simulated.');
    if(errors.length)throw new Error('Browser errors: '+errors.join('; '));
    await context.close();
    const raw=await page.video().path();
    await fs.writeFile(path.join(output,'timeline.json'),JSON.stringify(clips,null,2));
    const args=['-y','-i',raw];
    for(const clip of clips)args.push('-i',clip.audio);
    const delayed=clips.map((c,i)=>`[${i+1}:a]adelay=${Math.round(c.start*1000)}:all=1[a${i}]`);
    const mix=clips.map((_,i)=>`[a${i}]`).join('')+`amix=inputs=${clips.length}:normalize=0[audio]`;
    args.push('-filter_complex',[...delayed,mix].join(';'),'-map','0:v','-map','[audio]','-c:v','libx264','-preset','fast','-crf','21','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-movflags','+faststart',path.join(output,'unblock-demo.mp4'));
    await exec('ffmpeg',args,{maxBuffer:3e6});
    const clock=seconds=>{const ms=Math.round(seconds*1000);return `${String(Math.floor(ms/3600000)).padStart(2,'0')}:${String(Math.floor(ms/60000)%60).padStart(2,'0')}:${String(Math.floor(ms/1000)%60).padStart(2,'0')}.${String(ms%1000).padStart(3,'0')}`;};
    await fs.writeFile(path.join(output,'unblock-demo.vtt'),'WEBVTT\n\n'+clips.map(c=>`${clock(c.start)} --> ${clock(c.start+c.duration)}\n${c.text}\n`).join('\n'));
    console.log('Recorded and encoded real live demo.');
  }finally{await browser.close();server.close();}
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
