(() => {
  'use strict';
  const boot = JSON.parse(document.getElementById('boot').textContent);
  const $ = id => document.getElementById(id);
  const video = $('video'), zoom = $('zoom'), lens = $('lens');
  let stream, cameraGeneration = 0, scanGeneration = 0, track, worker, workerUrl;
  let decodeJob, decodeId = 0, scanTimer, pollTimer, cameraStarting = false;
  let mode = 'starting', terminal = false, pending = null, retryAction = null, requestInFlight = false;
  let serverState = boot.state || 'waiting', deadline = performance.now() + boot.expires_in * 1000;
  let ignoredFailureId = null;
  let lastMiss = '', lastMissAt = 0, zoomPending = null, zoomApplying = false;
  let photoActive = false, pickerOpen = false, torchOn = false;
  let cameraWanted = true, lastDevice = '', eventCount = 0;
  const canvas = document.createElement('canvas'), ctx = canvas.getContext('2d', {willReadFrequently:true});
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const busy = () => ['submitting','verifying','uncertain'].includes(serverState) || mode === 'sending';
  function message(title, detail, state='waiting', action=null, label='重试') {
    $('statusTitle').textContent = title; $('statusDetail').textContent = detail; $('statusDetail').hidden = !detail;
    $('statusCard').dataset.state = state;
    $('statusIcon').textContent = state === 'success' ? '✓' : state === 'error' || state === 'uncertain' ? '!' : '⌁';
    retryAction = action; $('actions').hidden = !action; $('retry').textContent = label;
  }
  $('retry').onclick = () => retryAction?.();
  function emit(event, values={}) {
    if (boot.mock || eventCount++ >= 30) return;
    // Whitelisted numeric measurements only; no QR text, filenames, IDs or error messages.
    const body = {event};
    for (const key of ['width','height','elapsed_ms']) if (Number.isFinite(values[key])) body[key] = values[key];
    fetch('/events/' + boot.sid, {method:'POST', headers:{'Content-Type':'application/json','ngrok-skip-browser-warning':'1'},
      body:JSON.stringify(body), keepalive:true}).catch(() => {});
  }
  window.addEventListener('error', () => emit('runtime_error'));
  window.addEventListener('unhandledrejection', () => emit('runtime_error'));
  async function api(path, data) {
    if (boot.mock) return mockApi(path, data);
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(path + '/' + boot.sid, {signal:controller.signal, cache:'no-store',
        method:data === undefined ? 'GET' : 'POST',
        headers:{'ngrok-skip-browser-warning':'1', ...(data === undefined ? {} : {'Content-Type':'application/json'})},
        ...(data === undefined ? {} : {body:JSON.stringify(data)})});
      if (!response.headers.get('Content-Type')?.includes('application/json')) throw Error('unexpected_response');
      const body = await response.json();
      return {...body, httpStatus:response.status};
    } finally { clearTimeout(timer); }
  }
  function stopDecode() { scanGeneration++; clearTimeout(scanTimer); }
  function stopCamera() {
    cameraGeneration++; stopDecode();
    stream?.getTracks().forEach(t => t.stop()); stream = null; track = null; video.srcObject = null;
    zoomPending = null;
  }
  function lockInputs() {
    const disabled = terminal || busy() || photoActive;
    $('photoButton').disabled = $('captureButton').disabled = disabled;
    lens.disabled = disabled || cameraStarting || lens.options.length < 2;
    $('torch').disabled = disabled;
    const noZoom = !track || !Number.isFinite(Number(track.getSettings?.().zoom)) || Number(zoom.max) <= Number(zoom.min);
    zoom.disabled = $('zoomIn').disabled = $('zoomOut').disabled = disabled || cameraStarting || noZoom;
  }
  function buildWorker() {
    if (!window.Worker) return;
    workerUrl = URL.createObjectURL(new Blob([$('decoder').textContent, '\n', $('workerSource').textContent], {type:'text/javascript'}));
    worker = new Worker(workerUrl);
    worker.onmessage = ({data}) => {
      if (!decodeJob || data.id !== decodeJob.id) return;
      const job = decodeJob; decodeJob = null; clearTimeout(job.timer);
      data.error ? job.reject(Error('decode_error')) : job.resolve(data.text);
    };
    worker.onerror = () => {
      const job = decodeJob; decodeJob = null;
      if (job) { clearTimeout(job.timer); job.reject(Error('decoder_worker')); }
      worker?.terminate(); worker = null; URL.revokeObjectURL(workerUrl);
      emit('decoder_error');
    };
  }
  async function decodeRegion(source, sx, sy, sw, sh, maxSide) {
    const scale = Math.min(1, maxSide / Math.max(sw, sh));
    const width = Math.max(1, Math.round(sw * scale)), height = Math.max(1, Math.round(sh * scale));
    canvas.width = width; canvas.height = height;
    ctx.drawImage(source, sx, sy, sw, sh, 0, 0, width, height);
    const image = ctx.getImageData(0, 0, width, height);
    if (!worker) {
      await delay(0);
      return window.jsQR(image.data, width, height, {inversionAttempts:'attemptBoth'})?.data || null;
    }
    if (decodeJob) throw Error('decoder_busy');
    return new Promise((resolve, reject) => {
      const id = ++decodeId;
      const timer = setTimeout(() => {
        decodeJob = null; worker?.terminate(); worker = null; URL.revokeObjectURL(workerUrl);
        emit('decoder_error'); reject(Error('decode_timeout'));
      }, 8000);
      decodeJob = {id, resolve, reject, timer};
      worker.postMessage({id, width, height, buffer:image.data.buffer}, [image.data.buffer]);
    });
  }
  function scanLoop() {
    const generation = scanGeneration;
    const tick = async () => {
      if (generation !== scanGeneration || terminal || busy() || photoActive || pickerOpen || document.hidden || boot.mock) return;
      try {
        if (video.readyState >= 2 && video.videoWidth && !decodeJob) {
          const w = video.videoWidth, h = video.videoHeight, side = Math.min(w,h);
          // The same centered region shown in the viewfinder, with independent scales.
          let text = await decodeRegion(video, (w-side)/2, (h-side)/2, side, side, 1200);
          if (!text && generation === scanGeneration) text = await decodeRegion(video, (w-side)/2, (h-side)/2, side, side, 800);
          if (text && generation === scanGeneration && !busy() && !photoActive) {
            if (text !== lastMiss || Date.now()-lastMissAt > 5000) { await submitText(text); return; }
          }
        }
      } catch (_) { emit('decoder_error'); }
      if (generation === scanGeneration) scanTimer = setTimeout(tick, 200);
    };
    scanTimer = setTimeout(tick, 100);
  }
  function resumeScanning() {
    if (terminal || busy() || photoActive || mode === 'failed') return;
    mode = 'scanning'; stopDecode();
    if (cameraWanted && !stream && !document.hidden) startCamera(lastDevice);
    else if (stream) scanLoop();
    lockInputs();
  }
  async function startCamera(deviceId='') {
    if (terminal || busy() || photoActive || cameraStarting) return;
    cameraWanted = true; cameraStarting = true; stopCamera();
    const generation = cameraGeneration;
    $('cameraPlaceholder').hidden = false; $('cameraPrompt').textContent = '正在连接相机'; $('startCamera').hidden = true;
    lockInputs();
    try {
      if (boot.mock) { await setupMockCamera(deviceId); return; }
      if (!navigator.mediaDevices?.getUserMedia) throw Error('unavailable');
      let expired = false, permissionTimer;
      const acquisition = navigator.mediaDevices.getUserMedia({audio:false, video:{
        ...(deviceId ? {deviceId:{exact:deviceId}} : {facingMode:{ideal:'environment'}}),
        width:{ideal:3840}, height:{ideal:2160}}}).then(s => {
          if (expired || generation !== cameraGeneration || terminal || busy()) { s.getTracks().forEach(t => t.stop()); throw Error('stale'); }
          return s;
        });
      try {
        stream = await Promise.race([acquisition, new Promise((_, reject) => {
          permissionTimer = setTimeout(() => { expired = true; reject(Error('permission_timeout')); }, 20000);
        })]);
      } finally { clearTimeout(permissionTimer); }
      if (generation !== cameraGeneration) return;
      video.srcObject = stream; await video.play();
      track = stream.getVideoTracks()[0]; lastDevice = track.getSettings?.().deviceId || deviceId;
      await configureTrack(track, generation);
      let devices = [];
      try { devices = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'videoinput'); } catch (_) {}
      if (generation !== cameraGeneration) return;
      lens.replaceChildren();
      devices.forEach((device, index) => lens.add(new Option(device.label || '相机 ' + (index+1), device.deviceId)));
      if (!devices.length) lens.add(new Option('当前相机', lastDevice));
      lens.value = lastDevice;
      $('cameraLabel').textContent = track.label || '后置相机';
      $('cameraPlaceholder').hidden = true;
      emit('ready'); mode = 'scanning'; stopDecode(); scanLoop();
    } catch (_) {
      if (generation === cameraGeneration) {
        stopCamera(); cameraWanted = false;
        $('cameraPrompt').textContent = '相机未开启'; $('startCamera').hidden = false;
        $('cameraLabel').textContent = '相机未连接';
        message('相机未开启', '请允许相机权限，或选择照片。', 'error', () => startCamera(deviceId), '重试相机');
        emit('camera_error');
      }
    } finally { cameraStarting = false; lockInputs(); }
  }
  async function boundedConstraint(t, constraint) {
    let timer;
    try { return await Promise.race([t.applyConstraints(constraint), new Promise((_, reject) => {
      timer = setTimeout(() => reject(Error('constraint_timeout')), 3000);
    })]); } finally { clearTimeout(timer); }
  }
  async function configureTrack(t, generation) {
    let caps = {}; try { caps = t.getCapabilities?.() || {}; } catch (_) {}
    if (caps.focusMode?.includes('continuous')) {
      for (const constraint of [{focusMode:'continuous'},{advanced:[{focusMode:'continuous'}]}]) {
        try { await boundedConstraint(t, constraint); if (t.getSettings?.().focusMode === 'continuous') break; } catch (_) {}
      }
    }
    if (generation !== cameraGeneration) return;
    const settings = t.getSettings?.() || {};
    $('viewHint').textContent = settings.focusMode === 'manual' ? '画面模糊时可改用拍照' : '对准二维码';
    const supported = caps.zoom && caps.zoom.max > caps.zoom.min && Number.isFinite(settings.zoom);
    zoom.min = supported ? caps.zoom.min : 1; zoom.max = supported ? caps.zoom.max : 1;
    zoom.step = supported ? (caps.zoom.step || .1) : .1; zoom.value = supported ? settings.zoom : 1;
    $('zoomValue').textContent = supported ? Number(settings.zoom).toFixed(1)+'×' : '不可用';
    $('zoomHint').textContent = supported ? '' : '此镜头不支持变焦';
    $('zoomHint').hidden = supported;
    $('torch').hidden = !caps.torch; torchOn = false;
  }
  async function pumpZoom() {
    if (zoomApplying || zoomPending === null || !track) return;
    zoomApplying = true;
    const t = track, generation = cameraGeneration, value = zoomPending; zoomPending = null;
    stopDecode();
    try {
      try { await boundedConstraint(t, {zoom:value}); }
      catch (_) { await boundedConstraint(t, {advanced:[{zoom:value}]}); }
      if (generation !== cameraGeneration) return;
      const actual = t.getSettings?.().zoom;
      if (!Number.isFinite(actual) || Math.abs(actual-value) > Math.max(Number(zoom.step),.1)) throw Error('zoom_ignored');
      $('zoomValue').textContent = Number(actual).toFixed(1)+'×';
      if (zoomPending === null) zoom.value = actual;
      if (boot.mock) drawMock();
    } catch (_) {
      if (generation === cameraGeneration) {
        zoom.value = t.getSettings?.().zoom || 1;
        $('zoomValue').textContent = Number(zoom.value).toFixed(1)+'×';
        $('zoomHint').textContent = '变焦不可用，请切换镜头'; $('zoomHint').hidden=false; emit('zoom_error');
      }
    } finally {
      zoomApplying = false;
      if (zoomPending !== null) pumpZoom(); else if (generation === cameraGeneration) resumeScanning();
    }
  }
  zoom.oninput = () => { zoomPending = Number(zoom.value); pumpZoom(); };
  for (const [id, direction] of [['zoomIn',1],['zoomOut',-1]]) $(id).onclick = () => {
    zoom.value = Math.max(Number(zoom.min),Math.min(Number(zoom.max),Number(zoom.value)+direction*Math.max(.5,Number(zoom.step))));
    zoom.oninput();
  };
  lens.onchange = () => startCamera(lens.value);
  $('startCamera').onclick = () => startCamera(lastDevice);
  $('torch').onclick = async () => {
    const t = track; if (!t) return;
    try { await boundedConstraint(t, {advanced:[{torch:!torchOn}]}); torchOn = !!t.getSettings?.().torch;
      $('torch').setAttribute('aria-pressed',String(torchOn)); $('torch').setAttribute('aria-label',torchOn ? '关闭补光灯' : '打开补光灯');
    } catch (_) { emit('camera_error'); }
  };
  function newRequestId() {
    const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
    return Array.from(bytes, b => b.toString(16).padStart(2,'0')).join('');
  }
  async function submitText(text, reuse=false) {
    if (terminal || requestInFlight || (!reuse && busy())) return;
    stopDecode(); mode = 'sending';
    if (!reuse) { pending = {text, request_id:newRequestId()}; ignoredFailureId=null; }
    if (!pending) { mode = 'scanning'; resumeScanning(); return; }
    requestInFlight = true; $('retry').disabled = true;
    lockInputs(); message('正在签到…', '');
    try {
      const response = await api('/submit',pending);
      if (response.state && ['submitting','verifying','success','uncertain','expired'].includes(response.state)) {
        stopCamera(); applyStatus(response); schedulePoll(); return;
      }
      if (response.state === 'failed') { applyStatus(response); return; }
      if (!response.ok) {
        lastMiss = pending.text; lastMissAt = Date.now(); pending = null; mode = 'scanning';
        message('请重新扫描', response.message || '二维码不属于本次签到。', 'error'); resumeScanning(); return;
      }
      throw Error('unexpected_state');
    } catch (_) {
      emit('submit_error'); mode = 'sending';
      // Preserve the request ID and text. A lost response must not create a second PUT.
      message('网络中断', '签到结果尚未确认。','uncertain', () => submitText('',true), '重试提交');
      schedulePoll();
    } finally { requestInFlight=false; $('retry').disabled=false; }
  }
  function applyStatus(data) {
    if (terminal && serverState === 'success' && data.state !== 'success') return;
    if (data.state === 'failed' && data.request_id && data.request_id === ignoredFailureId && data.expires_in > 0) data = {...data,state:'waiting'};
    if (Number.isFinite(data.expires_in)) deadline = performance.now() + data.expires_in*1000;
    serverState = data.state || serverState;
    if (serverState === 'success') {
      terminal = true; mode = 'done'; pending = null; stopCamera();
      $('viewHint').textContent = '已签到'; message('签到成功', '','success');
    } else if (serverState === 'expired') {
      terminal = true; pending = null; stopCamera(); message('签到链接已过期','请打开新的签到链接。','error');
    } else if (['submitting','verifying'].includes(serverState)) {
      mode = 'sending'; stopDecode(); message(serverState === 'verifying' ? '正在核对…' : '正在签到…','');
    } else if (serverState === 'uncertain') {
      mode = 'sending'; stopCamera();
      message('结果待确认','','uncertain', verifyResult,'核对签到结果');
    } else if (serverState === 'failed') {
      mode = 'failed'; pending = null; stopCamera();
      message('签到未完成','请扫描最新二维码。','error', () => {
        ignoredFailureId=data.request_id; serverState = 'waiting'; mode = 'scanning'; message('等待识别二维码',''); resumeScanning();
      },'重新扫描');
    }
    lockInputs(); updateCountdown();
  }
  async function verifyResult() {
    try { applyStatus(await api('/verify',{})); schedulePoll(); }
    catch (_) { message('暂时无法核对','请稍后重试。','uncertain',verifyResult,'重试核对'); }
  }
  function schedulePoll() {
    clearTimeout(pollTimer); if (terminal) return;
    pollTimer = setTimeout(pollStatus, ['submitting','verifying'].includes(serverState) || pending ? 1800 : 10000);
  }
  async function pollStatus() {
    if (terminal) return;
    try { applyStatus(await api('/status')); } catch (_) { /* retain current outcome and retry */ }
    schedulePoll();
  }
  function updateCountdown() {
    const seconds = Math.max(0,Math.ceil((deadline-performance.now())/1000));
    $('countdown').textContent = terminal && serverState === 'success' ? '已确认' : `${Math.floor(seconds/60).toString().padStart(2,'0')}:${(seconds%60).toString().padStart(2,'0')}`;
    if (!seconds && !terminal && !busy()) applyStatus({state:'expired',expires_in:0});
  }
  function openPicker(id) {
    if (terminal || busy() || photoActive) return;
    pickerOpen = true; stopDecode(); $(id).value = ''; $(id).click();
  }
  $('photoButton').onclick = () => openPicker('photoInput');
  $('captureButton').onclick = () => openPicker('captureInput');
  for (const id of ['photoInput','captureInput']) {
    $(id).addEventListener('cancel', () => { pickerOpen=false; resumeScanning(); });
    $(id).onchange = async () => {
      pickerOpen = false; const file = $(id).files?.[0]; if (!file) { resumeScanning(); return; }
      await readPhoto(file); $(id).value = '';
    };
  }
  async function readPhoto(file) {
    if (terminal || busy() || photoActive) return;
    if (file.size > 15*1024*1024 || !file.type.startsWith('image/')) {
      message('照片暂时无法读取','请选择小于 15 MB 的图片。','error'); resumeScanning(); return;
    }
    photoActive = true; stopDecode(); lockInputs();
    message('正在识别照片','');
    const began = performance.now(), url = URL.createObjectURL(file);
    try {
      const img = new Image(); img.src = url;
      let imageTimer;
      try { await Promise.race([img.decode(), new Promise((_,reject) => { imageTimer=setTimeout(() => reject(Error('image_timeout')),10000); })]); }
      finally { clearTimeout(imageTimer); }
      const w = img.naturalWidth, h = img.naturalHeight;
      if (!w || !h || w*h > 40000000) throw Error('image_size');
      // Let the in-flight live frame finish before reusing the single decode worker.
      while (decodeJob) await delay(30);
      const regions = [[0,0,w,h,1600],[0,0,w,h,1000]];
      const side = Math.min(w,h);
      for (const factor of [1,.55]) {
        const size = Math.round(side*factor);
        for (const y of [0,.5,1]) for (const x of [0,.5,1]) regions.push([(w-size)*x,(h-size)*y,size,size,1200]);
      }
      let text = null;
      for (const region of regions) {
        if (terminal || busy()) break;
        text = await decodeRegion(img,...region);
        if (text) break;
        if (performance.now()-began > 18000) break;
        await delay(0);
      }
      if (terminal || busy()) return;
      emit(text ? 'photo_decoded' : 'photo_miss',{width:w,height:h,elapsed_ms:performance.now()-began});
      if (text) { photoActive=false; await submitText(text); }
      else message('未识别到二维码','请换一张清晰照片。','error');
    } catch (_) { emit('photo_error'); if (!terminal && !busy()) message('照片识别未完成','请重新选择照片。','error'); }
    finally { URL.revokeObjectURL(url); photoActive=false; lockInputs(); if (!busy() && !terminal) resumeScanning(); }
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopCamera();
    else { pollStatus(); if (!pickerOpen) resumeScanning(); }
  });
  window.addEventListener('focus', () => {
    // Older WebViews omit the file-input cancel event.
    setTimeout(() => { if (pickerOpen && !photoActive) { pickerOpen=false; resumeScanning(); } },500);
  });
  window.addEventListener('pagehide', () => { stopCamera(); worker?.terminate(); worker=null; if(decodeJob){clearTimeout(decodeJob.timer);decodeJob.reject(Error('page_hidden'));decodeJob=null;} if(workerUrl) URL.revokeObjectURL(workerUrl); clearTimeout(pollTimer); });
  window.addEventListener('pageshow', event => { if (event.persisted) { if(!worker) buildWorker(); pollStatus(); resumeScanning(); } });

  // Mock mode is set only by the separate preview generator, never by URL parameters.
  let mockState = 'waiting', mockAttempt = '', mockScenario = 'success', mockZoom = 1, mockLens = 'wide';
  let mockImage, mockFinishTimer;
  async function mockApi(path, data) {
    await delay(250);
    if (path === '/submit') {
      if (mockScenario === 'offline') { mockScenario='success'; throw Error('mock_offline'); }
      if (mockScenario === 'invalid') return {ok:false,httpStatus:422,message:'这不是本次签到二维码，请重新扫描'};
      if (mockScenario === 'expired') mockState='expired';
      else if (mockAttempt !== data.request_id) {
        mockAttempt=data.request_id; mockState='submitting';
        mockFinishTimer=setTimeout(() => {mockState=mockScenario==='uncertain' ? 'uncertain' : 'success';},1600);
      }
    }
    if (path === '/verify') mockState='success';
    return {ok:true,state:mockState,expires_in:Math.max(0,Math.ceil((deadline-performance.now())/1000))};
  }
  async function setupMockCamera(deviceId) {
    mockLens=deviceId || mockLens; mockZoom=1;
    const t = {label:mockLens==='tele' ? '后置长焦' : '后置主摄',stop(){},
      getCapabilities:() => mockScenario==='nozoom' ? {} : {zoom:{min:1,max:5,step:.1}},
      getSettings:() => ({deviceId:mockLens,...(mockScenario==='nozoom' ? {} : {zoom:mockZoom})}),
      applyConstraints:async c => {mockZoom=c.zoom ?? c.advanced?.[0]?.zoom ?? mockZoom;}};
    stream={getTracks:() => [t]}; track=t; lastDevice=mockLens;
    lens.replaceChildren(new Option('后置主摄','wide'),new Option('后置长焦','tele'));
    lens.value=mockLens; $('cameraLabel').textContent=t.label;
    $('video').hidden=true; $('mockCanvas').hidden=false; $('cameraPlaceholder').hidden=true;
    await configureTrack(t,cameraGeneration); drawMock(); mode='scanning';
  }
  function drawMock() {
    const cv=$('mockCanvas'), c=cv.getContext('2d'); cv.width=960; cv.height=828;
    const g=c.createLinearGradient(0,0,0,828);g.addColorStop(0,'#3b4b4d');g.addColorStop(.55,'#65746b');g.addColorStop(1,'#263e42');
    c.fillStyle=g;c.fillRect(0,0,960,828); c.save();
    const z=mockZoom*(mockLens==='tele'?1.7:1);c.translate(480,414);c.scale(z,z);c.translate(-480,-278);
    c.fillStyle='#829083';c.fillRect(70,75,820,460);c.fillStyle='#435c53';c.fillRect(90,105,780,360);
    c.fillStyle='#c5cabb';c.fillRect(270,130,420,290);c.fillStyle='#e2e5d9';c.fillRect(281,141,398,268);
    c.fillStyle='#758575';c.font='16px sans-serif';c.textAlign='center';c.fillText('课堂签到',480,176);
    c.fillStyle='#fff';c.fillRect(414,211,132,132);
    if(mockImage?.complete)c.drawImage(mockImage,416,213,128,128);
    c.strokeStyle='#536b5c';c.lineWidth=4;c.beginPath();c.moveTo(145,500);c.lineTo(815,500);c.stroke();
    for(let row=0;row<3;row++)for(let col=0;col<4;col++){
      const x=40+col*246-row*26,y=584+row*101;c.fillStyle=row===0?'#827d68':'#575e50';
      c.beginPath();c.moveTo(x+15,y);c.lineTo(x+168,y);c.lineTo(x+192,y+33);c.lineTo(x-5,y+33);c.closePath();c.fill();
      c.fillStyle='#243b3c';c.fillRect(x+18,y+34,5,65);c.fillRect(x+166,y+34,5,65);
    }
    c.restore();const shade=c.createRadialGradient(480,400,140,480,400,640);shade.addColorStop(0,'#10293500');shade.addColorStop(1,'#10293599');c.fillStyle=shade;c.fillRect(0,0,960,828);
  }
  if (boot.mock) {
    $('demoBadge').hidden=$('mockPanel').hidden=false;
    if(boot.mockQr){mockImage=new Image();mockImage.onload=drawMock;mockImage.src=boot.mockQr;}
    $('mockScenario').onchange=() => { mockScenario=$('mockScenario').value; if(mockScenario==='nozoom')startCamera(mockLens); };
    $('mockScan').onclick=() => submitText('{"rollcallId":"123","data":"preview-only"}');
    $('mockReset').onclick=() => {
      clearTimeout(mockFinishTimer); clearTimeout(pollTimer); terminal=false; pending=null; serverState=mockState='waiting'; mockAttempt='';
      mockScenario=$('mockScenario').value;deadline=performance.now()+300000;mode='scanning';cameraWanted=true;
      message('等待识别二维码','');startCamera(mockLens);schedulePoll();
    };
  }
  if (typeof window.jsQR !== 'function') {
    emit('decoder_error'); cameraWanted=false;
    message('扫码暂不可用','请刷新页面重试。','error',() => location.reload(),'重新加载');
    $('cameraPrompt').textContent='扫码暂不可用';
    $('photoButton').disabled=$('captureButton').disabled=true;
  } else {
    try {buildWorker();} catch (_) {worker=null;emit('decoder_error');}
    applyStatus(boot);
    if(!terminal && !busy())startCamera();
    pollStatus();
  }
  updateCountdown();setInterval(updateCountdown,1000);
})();
