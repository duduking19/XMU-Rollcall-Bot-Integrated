/* Run against scripts/scanner_test_server.py. Uses synthetic camera tracks and QR images. */
const {chromium} = require('playwright');
const QR = require('qrcode');
const {PNG} = require('pngjs');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname,'..');
const base = process.env.SCANNER_TEST_URL || 'http://127.0.0.1:8765';
const payload = JSON.stringify({rollcallId:'123',data:'synthetic-test-only'});
const output = path.join(root,'artifacts');
fs.mkdirSync(output,{recursive:true});

async function fakeCamera(page, options={}) {
  await page.addInitScript(options => {
    window.cameraCalls=[];window.cameraStops=0;window.constraintCalls=[];
    Object.defineProperty(navigator.mediaDevices,'getUserMedia',{value:async constraints => {
      window.cameraCalls.push(constraints);
      if(options.denied)throw new DOMException('Permission denied','NotAllowedError');
      const canvas=document.createElement('canvas');canvas.width=1280;canvas.height=1920;
      const c=canvas.getContext('2d');c.fillStyle='#455c63';c.fillRect(0,0,1280,1920);
      const stream=canvas.captureStream(15), track=stream.getVideoTracks()[0];
      const originalStop=track.stop.bind(track);track.stop=()=>{window.cameraStops++;originalStop();};
      const deviceId=constraints.video.deviceId?.exact || 'wide';let zoom=1;
      track.getCapabilities=()=>options.nozoom ? {} : {zoom:{min:1,max:5,step:.1},focusMode:['continuous']};
      track.getSettings=()=>({deviceId,width:1280,height:1920,focusMode:'continuous',...(options.nozoom?{}:{zoom})});
      track.applyConstraints=async value=>{window.constraintCalls.push(value);if(!options.ignoreZoom)zoom=value.zoom??value.advanced?.[0]?.zoom??zoom;};
      return stream;
    }});
    Object.defineProperty(navigator.mediaDevices,'enumerateDevices',{value:async()=>[
      {kind:'videoinput',deviceId:'wide',label:'后置主摄'},
      {kind:'videoinput',deviceId:'tele',label:'后置长焦'}]});
  },options);
}
async function waitText(page,id,text) {
  await page.waitForFunction(({id,text})=>document.getElementById(id)?.textContent.includes(text),{id,text},{timeout:25000});
}
async function realPage(browser, options={}) {
  const page=await browser.newPage({viewport:{width:390,height:844},deviceScaleFactor:2});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await fakeCamera(page,options);
  const session=await (await page.request.get(base+'/test-session')).json();
  await page.goto(base+'/scan/'+session.sid);
  return {page,session,errors};
}
(async()=>{
  const qr=await QR.toBuffer(payload,{width:360,margin:4});
  fs.writeFileSync(path.join(output,'synthetic-qr.png'),qr);
  const wrong=await QR.toBuffer(JSON.stringify({rollcallId:'999',data:'wrong-class'}),{width:360});
  const large=new PNG({width:1800,height:3000});large.data.fill(255);
  PNG.bitblt(PNG.sync.read(qr),large,0,0,360,360,110,2400);
  const portrait=PNG.sync.write(large);
  const browser=await chromium.launch({headless:true});let tests=0;
  try {
    {
      const page=await browser.newPage({viewport:{width:390,height:844},deviceScaleFactor:2});
      const errors=[];page.on('pageerror',e=>errors.push(e.message));
      await page.goto(base);await waitText(page,'cameraLabel','后置主摄');
      await page.screenshot({path:path.join(output,'scanner-mobile.png'),fullPage:true});
      await page.locator('#mockPanel summary').click();
      await page.locator('#zoom').fill('2.5');await page.locator('#zoom').dispatchEvent('input');
      await waitText(page,'zoomValue','2.5');
      await page.selectOption('#lens','tele');await waitText(page,'cameraLabel','长焦');
      await page.click('#mockScan');await waitText(page,'statusTitle','签到成功');
      await page.screenshot({path:path.join(output,'scanner-success.png'),fullPage:true});
      await page.click('#mockReset');await page.selectOption('#mockScenario','offline');
      await page.click('#mockScan');await waitText(page,'statusTitle','网络中断');
      await page.click('#retry');await waitText(page,'statusTitle','签到成功');
      await page.click('#mockReset');await page.selectOption('#mockScenario','uncertain');
      await page.click('#mockScan');await waitText(page,'statusTitle','待确认');
      await page.click('#retry');await waitText(page,'statusTitle','签到成功');
      assert.deepEqual(errors,[]);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.close();console.log('PASS mock: zoom, lens, success, offline retry, uncertain verify, mobile layout');tests++;
    }
    {
      const {page,errors}=await realPage(browser);await waitText(page,'cameraLabel','');
      await page.waitForFunction(()=>!document.getElementById('zoom').disabled);
      await page.locator('#zoom').fill('3');await page.locator('#zoom').dispatchEvent('input');await waitText(page,'zoomValue','3.0');
      assert(await page.evaluate(()=>window.constraintCalls.some(c=>c.zoom===3)));
      assert.equal(await page.locator('#video').evaluate(v=>getComputedStyle(v).transform),'none');
      await page.selectOption('#lens','tele');await page.waitForFunction(()=>window.cameraCalls.some(c=>c.video.deviceId?.exact==='tele'));
      await page.waitForFunction(()=>!document.getElementById('lens').disabled);
      assert((await page.evaluate(()=>window.cameraStops))>=1);
      await page.setInputFiles('#photoInput',{name:'wrong.png',mimeType:'image/png',buffer:wrong});
      await waitText(page,'statusTitle','请重新扫描');
      await page.setInputFiles('#photoInput',{name:'portrait.png',mimeType:'image/png',buffer:portrait});
      await waitText(page,'statusTitle','签到成功');assert.deepEqual(errors,[]);
      await page.close();console.log('PASS real page: actual track zoom constraints, lens switch, wrong QR retry, off-center portrait decode');tests++;
    }
    {
      const {page,errors}=await realPage(browser,{denied:true});
      await waitText(page,'statusTitle','相机未开启');
      assert.equal(await page.locator('#photoButton').isDisabled(),false);
      await page.setInputFiles('#photoInput',{name:'qr.png',mimeType:'image/png',buffer:qr});
      await waitText(page,'statusTitle','签到成功');assert.deepEqual(errors,[]);await page.close();
      console.log('PASS denied camera still permits photo decoding');tests++;
    }
    {
      const {page,errors}=await realPage(browser,{ignoreZoom:true});
      await page.waitForFunction(()=>!document.getElementById('zoom').disabled);
      await page.locator('#zoom').fill('3');await page.locator('#zoom').dispatchEvent('input');
      await waitText(page,'zoomHint','变焦不可用');assert.equal(await page.locator('#zoomValue').textContent(),'1.0×');
      assert.deepEqual(errors,[]);await page.close();console.log('PASS ignored zoom never claims fake camera magnification');tests++;
    }
    {
      const {page,errors}=await realPage(browser,{nozoom:true});await waitText(page,'zoomValue','不可用');
      assert(await page.locator('#zoom').isDisabled());assert.equal(await page.locator('#photoButton').isDisabled(),false);
      assert.deepEqual(errors,[]);await page.close();console.log('PASS unavailable hardware zoom clearly reported');tests++;
    }
    {
      const {page,errors}=await realPage(browser,{denied:true});let submits=0;
      await page.route('**/submit/*',async route=>{submits++;await route.fetch();await route.abort();});
      await page.setInputFiles('#photoInput',{name:'qr.png',mimeType:'image/png',buffer:qr});
      await waitText(page,'statusTitle','签到成功');assert.equal(submits,1);assert.deepEqual(errors,[]);
      await page.close();console.log('PASS lost submit response resolves via status without duplicate PUT');tests++;
    }
    {
      const page=await browser.newPage();await fakeCamera(page);
      const session=await (await page.request.get(base+'/test-session')).json();
      await page.route('**/scan/*',async route=>{
        const response=await route.fetch();const html=await response.text();
        await route.fulfill({response,body:html.replace(/(<script id="decoder"[^>]*>)[\s\S]*?<\/script>/,'$1</script>')});
      });
      await page.goto(base+'/scan/'+session.sid);await waitText(page,'statusTitle','扫码暂不可用');
      assert(await page.locator('#photoButton').isDisabled());assert(await page.locator('#captureButton').isDisabled());
      assert.equal(await page.evaluate(()=>window.cameraCalls.length),0);
      await page.close();console.log('PASS missing decoder blocks both paths with truthful recovery message');tests++;
    }
    {
      const {page,errors}=await realPage(browser,{denied:true});let submits=0;const ids=[];
      await page.route('**/submit/*',async route=>{submits++;ids.push(route.request().postDataJSON().request_id);if(submits===1)await route.abort();else await route.continue();});
      await page.setInputFiles('#photoInput',{name:'qr.png',mimeType:'image/png',buffer:qr});
      await waitText(page,'statusTitle','网络中断');await page.click('#retry');await waitText(page,'statusTitle','签到成功');
      assert.equal(submits,2);assert.equal(ids[0],ids[1]);assert.deepEqual(errors,[]);await page.close();
      console.log('PASS failure before receipt retries the same request safely');tests++;
    }
    {
      const {page,errors}=await realPage(browser,{denied:true});
      await page.setInputFiles('#photoInput',{name:'broken.png',mimeType:'image/png',buffer:Buffer.from('not-an-image')});
      await waitText(page,'statusTitle','照片识别未完成');
      assert.equal(await page.locator('#photoButton').isDisabled(),false);
      await page.setInputFiles('#photoInput',{name:'qr.png',mimeType:'image/png',buffer:qr});
      await waitText(page,'statusTitle','签到成功');assert.deepEqual(errors,[]);await page.close();
      console.log('PASS corrupt photo recovers and accepts the next photo');tests++;
    }
    {
      const {page,errors}=await realPage(browser);await page.waitForFunction(()=>!document.getElementById('zoom').disabled);
      const rejected=await QR.toBuffer(JSON.stringify({rollcallId:'123',data:'synthetic-rejected'}),{width:360});
      await page.setInputFiles('#photoInput',{name:'rejected.png',mimeType:'image/png',buffer:rejected});
      await waitText(page,'statusTitle','签到未完成');await page.click('#retry');
      await page.waitForTimeout(11000);
      assert.equal(await page.locator('#statusTitle').textContent(),'等待识别二维码');
      await page.setInputFiles('#photoInput',{name:'qr.png',mimeType:'image/png',buffer:qr});
      await waitText(page,'statusTitle','签到成功');
      await page.reload();await waitText(page,'statusTitle','签到成功');assert.deepEqual(errors,[]);await page.close();
      console.log('PASS rejected request can rescan past a status poll; confirmed result survives reload');tests++;
    }
    {
      const page=await browser.newPage({viewport:{width:320,height:720}});
      await page.goto(base);await page.locator('#mockPanel summary').click();await page.selectOption('#mockScenario','expired');await page.click('#mockScan');
      await waitText(page,'statusTitle','已过期');assert(await page.locator('#photoButton').isDisabled());
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.close();console.log('PASS expired session disables capture; narrow screen has no horizontal overflow');tests++;
    }
    console.log(`${tests} browser scenarios passed`);
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
