const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

function dashboard() {
  const nodes = new Map();
  let document;
  function domNode(selector) {
    return {
      selector, textContent: "", title: "", hidden: false, handlers: {}, dataset: {}, children: [], style: {},
      addEventListener(type, callback) { this.handlers[type] = callback; },
      querySelector(child) { return this.querySelectorAll(child)[0] || null; },
      querySelectorAll(child) {
        const matches = (node) => child === "a" ? node.selector === "a" :
          child === "[data-output]" ? Boolean(node.dataset.output) :
          child === "[data-stitch-session]" ? Boolean(node.dataset.stitchSession) : false;
        const descendants = this.children.flatMap((node) => [node, ...node.descendants()]);
        return descendants.filter(matches);
      },
      descendants() { return this.children.flatMap((node) => [node, ...node.descendants()]); },
      closest(child) {
        if (child === "[data-session]" && this.dataset.session) return this;
        if (child === "[data-output]" && this.dataset.output) return this;
        return this.parent?.closest(child) || null;
      },
      append(...children) { children.forEach((node) => { node.parent = this; this.children.push(node); }); },
      replaceChildren() { this.children = []; },
      focus() { document.activeElement = this; },
      select() { this.selected = true; },
      setSelectionRange(start, end) { this.selection = [start, end]; },
      remove() { if (this.parent) this.parent.children = this.parent.children.filter((node) => node !== this); },
      setAttribute(name, value) { this[name] = value; },
      removeAttribute(name) { delete this[name]; },
      classList: {toggle() {}},
    };
  }
  function element(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {...domNode(selector),
      querySelector(child) { return element(`${this.selector} ${child}`); },
    });
    return nodes.get(selector);
  }
  document = {querySelector: element, querySelectorAll: (selector) =>
    [...nodes.values()].flatMap((node) => node.querySelectorAll(selector)), addEventListener() {},
    createElement: domNode, createTextNode: (text) => ({...domNode("#text"), textContent: text}),
    createElementNS: (_, name) => domNode(name)};
  document.body = domNode("body");
  const context = vm.createContext({
    document,
    window: {addEventListener() {}}, location: {hash: "#live"}, navigator: {}, TextEncoder,
    setInterval() {}, setTimeout() {}, clearTimeout() {}, console, Date, Headers,
  });
  const source = fs.readFileSync(path.join(__dirname, "../reco_field/static/app.js"), "utf8")
    .replace(/\nboot\(\);\s*$/, "\n");
  vm.runInContext(source, context);
  return {context, element, run: (script) => vm.runInContext(script, context)};
}

test("missing and null readings never become fabricated zero measurements", () => {
  const ui = dashboard();
  ui.run(`state.connected = true; state.status = {record:{state:"idle"}}; renderDeviceMetrics();`);
  for (const id of ["fps", "temperature", "storage", "memory"]) {
    assert.equal(ui.element(`#metric-${id}`).textContent, "—");
  }
  ui.run(`state.status.device = {sampled_at:new Date().toISOString(),cpu_temperature_c:null,
    memory_used_bytes:null,storage:{state:"ready",free_bytes:null}}; renderDeviceMetrics();`);
  assert.equal(ui.element("#metric-temperature").textContent, "—");
  assert.equal(ui.element("#metric-memory").textContent, "—");
});

test("only fresh measured capture rate is displayed during a session", () => {
  const ui = dashboard();
  ui.run(`state.connected = true; state.status = {
    record:{state:"running",recording:{size:1048576}},
    capture:{metrics:{sampled_at:new Date().toISOString(),fps:29.36}},
    device:{sampled_at:new Date().toISOString(),cpu_temperature_c:62,memory_used_bytes:2147483648,
      storage:{state:"ready",free_bytes:1073741824}}}; renderDeviceMetrics();`);
  assert.equal(ui.element("#metric-fps").textContent, "29.4 fps");
  assert.equal(ui.element("#metric-temperature").textContent, "62 °C");
  assert.equal(ui.element("#metric-storage").textContent, "1.0 GiB");
  assert.equal(ui.element("#metric-record-size").textContent, "1.0 MiB");
  ui.run(`state.status.capture.metrics.sampled_at = new Date(Date.now()-30000).toISOString(); renderDeviceMetrics();`);
  assert.equal(ui.element("#metric-fps").textContent, "—");
  ui.run(`state.connected=false; renderDeviceMetrics();`);
  assert.equal(ui.element("#metric-temperature").textContent, "—");
  assert.match(ui.element("#metrics-note").textContent, /Disconnected/);
});

test("metadata duration is rounded for presentation and unsafe numeric values are unavailable", () => {
  const ui = dashboard();
  assert.equal(ui.run("formatDuration(91.166)"), "01:31");
  assert.equal(ui.run("formatDuration(7201)"), "2:00:01");
  assert.equal(ui.run("formatBytes(null)"), "—");
  assert.equal(ui.run("formatBytes(-1)"), "—");
  assert.equal(ui.run("formatDate('not-a-date')"), "Date unavailable");
});

test("preview shows the viewfinder without a recording timer or recording indicator", () => {
  const ui = dashboard();
  ui.run(`renderSnapshots=()=>{};renderCalibration=()=>{};renderLensCalibration=()=>{};
    renderDestinations=()=>{};renderStreamPublisher=()=>{};renderReadinessSummary=()=>{};
    updateActiveRecording=()=>{};renderDeviceMetrics=()=>{};ensurePreview=()=>{};
    state.settings={recording_mode:'panorama'};state.status={
      record:{state:'idle',elapsed_seconds:0,error:''},
      capture:{kind:'preview',state:'running',elapsed_seconds:42,error:'',mode:null},
      capabilities:{preview:true,recording_modes:['panorama','sources']},destinations:[]};
    renderStatus();`);
  assert.equal(ui.element("#record-chip").hidden, true);
  assert.equal(ui.element("#preview-chip").hidden, false);
  assert.equal(ui.element("#record-time").hidden, true);
  assert.equal(ui.element("#preview-button").textContent, "Stop preview");
  assert.equal(ui.element("#record-button span:last-child").textContent, "Record");
  assert.match(ui.element("#capture-state").textContent, /Preview · 00:42/);
  ui.run(`state.status.capabilities={preview:false,recording_modes:['panorama']};renderStatus();`);
  assert.equal(ui.element("#preview-button").hidden, true);
  assert.equal(ui.element("#record-mode-control").hidden, true);
});

test("HLS mounts follow capture identity even when polling misses the idle transition", () => {
  const ui = dashboard();
  ui.run(`globalThis.players=[];
    window.Hls=class {
      static isSupported(){return true;}
      static Events={MANIFEST_PARSED:'manifest',ERROR:'error'};
      static ErrorTypes={MEDIA_ERROR:'media'};
      constructor(){this.handlers={};players.push(this);}
      loadSource(){} attachMedia(){} on(event,callback){this.handlers[event]=callback;}
      destroy(){this.destroyed=true;} startLoad(){this.reconnected=true;}
      recoverMediaError(){this.recovered=true;}
    };
    globalThis.video=$('#preview-video');video.pause=()=>{};video.load=()=>{};
    video.play=()=>Promise.resolve();
    state.status={record:{state:'idle'},capture:{kind:'preview',state:'running',mode:'panorama',
      session_id:null,metrics:{session_id:'preview-1'}}};
    ensurePreview();ensurePreview();`);
  assert.equal(ui.run("players.length"), 1);
  ui.run(`state.status.capture={kind:'recording',state:'starting',mode:'sources',session_id:'sources-1',metrics:null};
    state.status.record.state='starting';ensurePreview();`);
  assert.equal(ui.run("players.length"), 2);
  assert.equal(ui.run("players[0].destroyed"), true);
  assert.equal(ui.element("#preview-video").controls, false);
  ui.run(`state.status.capture.state='running';state.status.capture.metrics={session_id:'source-process-1'};
    ensurePreview();ensurePreview();`);
  assert.equal(ui.run("players.length"), 2, "first metrics sample must not remount the new capture");
  ui.run(`players[0].handlers.error(null,{fatal:true,type:'network'});`);
  assert.equal(ui.run("players[0].reconnected"), undefined, "destroyed player cannot reconnect");
  assert.equal(ui.element("#preview-error").textContent, "");
  ui.run(`players[1].handlers.error(null,{fatal:true,type:'network'});`);
  assert.equal(ui.run("players[1].reconnected"), true);
  assert.equal(ui.element("#preview-error").textContent, "Live preview is reconnecting.");
  ui.run(`state.status.capture.session_id='sources-2';state.status.capture.metrics=null;ensurePreview();
    state.status.capture.metrics={session_id:'source-process-2'};ensurePreview();`);
  assert.equal(ui.run("players.length"), 3, "same-kind saved sessions must remount exactly once");
  ui.run(`state.status.capture={kind:'preview',state:'running',mode:'panorama',metrics:{session_id:'preview-2'}};
    state.status.record={state:'idle'};ensurePreview();
    state.status.capture.metrics={session_id:'preview-3'};ensurePreview();ensurePreview();`);
  assert.equal(ui.run("players.length"), 5, "same-kind previews are distinguished by native process ID");
});

test("native HLS fallback resets the media element for a new capture generation", () => {
  const ui = dashboard();
  ui.run(`globalThis.loads=0;globalThis.plays=0;globalThis.video=$('#preview-video');
    video.canPlayType=()=>true;video.pause=()=>{};video.load=()=>{loads++;};
    video.play=()=>{plays++;return Promise.resolve();};
    state.status={record:{state:'idle'},capture:{kind:'preview',state:'running',mode:'panorama',
      metrics:{session_id:'preview-1'}}};ensurePreview();ensurePreview();
    state.status.capture.metrics={session_id:'preview-2'};ensurePreview();ensurePreview();`);
  assert.equal(ui.run("loads"), 1);
  assert.equal(ui.run("plays"), 2);
  assert.equal(ui.element("#preview-video").src, "/api/preview/reco/live/index.m3u8");
  ui.element("#preview-video").handlers.loadedmetadata();
  assert.equal(ui.element("#preview-video").controls, true);
});

test("recording originals stops preview first, persists the choice, then starts source capture", async () => {
  const ui = dashboard();
  ui.run(`renderStatus=()=>{};refreshRecordings=async()=>{};destroyPreview=()=>{};
    state.recordingMode='sources';state.settings={recording_mode:'panorama'};
    state.status={record:{state:'idle'},capture:{kind:'preview',state:'running'},
      capabilities:{preview:true,recording_modes:['panorama','sources']}};
    globalThis.requests=[];api=async(path,options)=>{
      requests.push({path,body:options.body?JSON.parse(options.body):null});
      if(path==='/api/settings') return {recording_mode:'sources'};
      return {record:{state:path.endsWith('/start')?'starting':'idle'},
        capture:{kind:null,state:'idle'},capabilities:{preview:true,recording_modes:['panorama','sources']}};
    };`);
  ui.element("#start-stream").checked = false;
  await ui.element("#record-button").handlers.click();
  const requests = JSON.parse(ui.run("JSON.stringify(requests)"));
  assert.deepEqual(requests.map((item) => item.path), ["/api/preview/stop", "/api/settings", "/api/recordings/start"]);
  assert.deepEqual(requests[2].body, {stream: false, mode: "sources"});
});

test("source recording reports its bytes while labeling renderer rate as preview fps", () => {
  const ui = dashboard();
  ui.run(`state.connected=true;state.status={record:{state:'running',recording:null},
    capture:{kind:'recording',state:'running',mode:'sources',bytes:2147483648,
      metrics:{sampled_at:new Date().toISOString(),fps:9.8}}};renderDeviceMetrics();`);
  assert.equal(ui.element("#metric-fps-label").textContent, "Preview fps");
  assert.equal(ui.element("#metric-fps").textContent, "9.8 fps");
  assert.equal(ui.element("#metric-record-size").textContent, "2.0 GiB");
});

test("active source capture overrides the displayed mode without changing the idle preference", () => {
  const ui = dashboard();
  ui.run(`state.recordingMode='panorama';state.settings={recording_mode:'panorama'};
    state.status={capabilities:{recording_modes:['panorama','sources']},
      capture:{kind:'recording',state:'running',mode:'sources'}};renderRecordingMode();`);
  assert.equal(ui.element("#recording-mode").value, "sources");
  assert.equal(ui.element("#recording-mode").disabled, true);
  assert.match(ui.element("#recording-mode-help").textContent, /each camera separately/);
  assert.equal(ui.run("state.recordingMode"), "panorama");
  assert.equal(ui.run("state.settings.recording_mode"), "panorama");
  ui.run(`state.status.capture={kind:null,state:'idle'};renderRecordingMode();`);
  assert.equal(ui.element("#recording-mode").value, "panorama");
  assert.equal(ui.element("#recording-mode").disabled, false);
  assert.match(ui.element("#recording-mode-help").textContent, /stitched panorama/);
});

test("session polling preserves action identity and restores focus when metadata changes", () => {
  const ui = dashboard();
  ui.run(`state.status={capabilities:{offline_stitch:true},capture:{state:'idle'},offline_job:{state:'idle'}};
    state.sessions=[{id:'match',mode:'sources',state:'complete',size:42,can_stitch:true,outputs:[]}];
    renderSessions();globalThis.action=$$('[data-stitch-session]')[0];action.focus();renderSessions();`);
  assert.equal(ui.run("$$('[data-stitch-session]')[0] === action"), true);
  assert.equal(ui.run("document.activeElement === action"), true);
  ui.run(`state.sessions[0].size=43;renderSessions();`);
  assert.equal(ui.run("$$('[data-stitch-session]')[0] === action"), false);
  assert.equal(ui.run("document.activeElement.dataset.stitchSession"), "match");
  ui.run(`state.status.offline_job={state:'running',session_id:'match'};renderOfflineJob();`);
  assert.equal(ui.run("document.activeElement.dataset.session"), "match");
  assert.equal(ui.run("$$('[data-stitch-session]')[0].disabled"), true);
  ui.run(`state.sessions[0].can_stitch=false;renderSessions();`);
  assert.equal(ui.run("$$('[data-stitch-session]').length"), 0);
  assert.equal(ui.run("document.activeElement.dataset.session"), "match");
});

test("failed preview requests retain the actionable error after controls recover", async () => {
  const ui = dashboard();
  ui.run(`renderSnapshots=()=>{};renderCalibration=()=>{};renderLensCalibration=()=>{};
    renderDestinations=()=>{};renderStreamPublisher=()=>{};renderReadinessSummary=()=>{};
    updateActiveRecording=()=>{};renderDeviceMetrics=()=>{};
    state.settings={recording_mode:'panorama'};state.status={record:{state:'idle',error:''},
      capture:{kind:null,state:'idle',error:''},capabilities:{preview:true,recording_modes:['panorama']},destinations:[]};
    api=async()=>{throw new Error('Both cameras are already in use.');};`);
  await ui.element("#preview-button").handlers.click();
  assert.equal(ui.element("#record-error").hidden, false);
  assert.equal(ui.element("#record-error").textContent, "Both cameras are already in use.");
  assert.equal(ui.element("#preview-button").disabled, false);
  assert.equal(ui.run("state.busy"), false);
});

test("offline panorama start and cancel use the saved session endpoints", async () => {
  const ui = dashboard();
  ui.run(`renderStatus=()=>{};state.status={capture:{kind:null,state:'idle'},offline_job:{state:'idle'}};
    globalThis.requests=[];api=async(path,options)=>{requests.push({path,body:JSON.parse(options.body)});
      return {capture:{kind:null,state:'idle'},offline_job:{id:'job-1',session_id:'fixture-match',
        state:path.endsWith('/cancel')?'cancelled':'running',error:'',processed_frames:12}};};`);
  await ui.run("requestOfflineJob('fixture-match')");
  await ui.run("requestOfflineJob('fixture-match',true)");
  const requests = JSON.parse(ui.run("JSON.stringify(requests)"));
  assert.deepEqual(requests, [
    {path: "/api/sessions/fixture-match/stitch", body: {}},
    {path: "/api/sessions/fixture-match/stitch/cancel", body: {}},
  ]);
});

test("offline creation is blocked during capture and incomplete exports cannot be downloaded", async () => {
  const ui = dashboard();
  ui.run(`state.status={capture:{kind:'recording',state:'running'},offline_job:{state:'idle'}};
    globalThis.requests=[];api=async(path)=>{requests.push(path);};`);
  await ui.run("requestOfflineJob('fixture-match')");
  assert.equal(ui.run("requests.length"), 0);
  ui.run(`refreshRecordings=()=>{};state.status={capture:{kind:null,state:'idle'},
    offline_job:{state:'failed',session_id:'fixture-match',error:'Source decode failed.',
      output:{name:'partial.mkv',download_url:null}}};renderOfflineJob();`);
  assert.equal(ui.element("#offline-job-download").hidden, true);
  assert.equal(ui.element("#offline-job-error").textContent, "Source decode failed.");
  ui.run(`state.status.offline_job={state:'complete',error:'',output:{download_url:'/api/sessions/fixture-match/export-test.mkv'}};renderOfflineJob();`);
  assert.equal(ui.element("#offline-job-download").hidden, false);
  assert.equal(ui.element("#offline-job-download").href, "/api/sessions/fixture-match/export-test.mkv");
});

test("a panorama finishing just before cancellation remains a successful export", async () => {
  const ui = dashboard();
  ui.run(`renderStatus=()=>{};state.status={capture:{state:'idle'},
    offline_job:{state:'running',session_id:'fixture-match',error:''}};
    api=async(path)=>{if(path.endsWith('/cancel')){const error=new Error('Job already finished');error.status=409;throw error;}
      return {capture:{state:'idle'},offline_job:{state:'complete',session_id:'fixture-match',error:'',
        output:{download_url:'/api/sessions/fixture-match/export-test.mkv'}}};};`);
  await ui.run("requestOfflineJob('fixture-match',true)");
  assert.equal(ui.run("state.offlineError"), "");
  assert.equal(ui.run("state.status.offline_job.state"), "complete");
});

test("camera Wi-Fi keeps credentials masked and polling preserves an edit draft", async () => {
  const ui = dashboard();
  ui.element("#wifi-setup-form").hidden = true;
  ui.run(`state.network={available:true,busy:false,setup:{ssid:'Fixture Camera',password:'fixture-pass',revision:'a',active:false}};
    renderWifiSetup();`);
  assert.equal(ui.element("#wifi-setup-password").textContent, "••••••••");
  assert.equal(ui.element("#wifi-setup-qr").src, "/api/network/setup/qr.png?v=a");
  assert.match(ui.element("#wifi-setup-availability").textContent, /automatically/);
  ui.element("#wifi-show-password").handlers.click();
  assert.equal(ui.element("#wifi-setup-password").textContent, "fixture-pass");
  ui.element("#wifi-setup-edit").handlers.click();
  ui.element("#wifi-setup-name-input").value = "My camera draft";
  ui.element("#wifi-setup-password-input").value = "draft-password";
  ui.run(`api=async()=>({...state.network,setup:{ssid:'Updated elsewhere',password:'new-password',revision:'b',active:true}});`);
  await ui.run("refreshNetwork()");
  assert.equal(ui.element("#wifi-setup-name-input").value, "My camera draft");
  assert.equal(ui.element("#wifi-setup-password-input").value, "draft-password");
  assert.equal(ui.element("#wifi-setup-password").textContent, "••••••••");
  assert.equal(ui.element("#wifi-setup-qr").src, "/api/network/setup/qr.png?v=b");
  assert.equal(ui.element("#wifi-setup-reconnect").hidden, false);
  ui.element("#wifi-setup-cancel").handlers.click();
  assert.equal(ui.element("#wifi-setup-form").hidden, true);
  assert.equal(ui.element("#wifi-setup-password-input").value, "");
  assert.equal(ui.run("document.activeElement.selector"), "#wifi-setup-edit");
});

test("camera Wi-Fi validates UTF-8 names and password lengths before submitting", async () => {
  const ui = dashboard();
  ui.run(`state.network={available:true,busy:false,setup:{ssid:'Old',password:'old-password',revision:'a',active:false}};
    globalThis.requests=[];api=async(path,options)=>{requests.push({path,body:JSON.parse(options.body)});
      return {accepted:true,message:'Camera Wi-Fi saved.',setup:{ssid:'New camera',password:'old-password',revision:'b',active:false}};};`);
  ui.element("#wifi-setup-name-input").value = "界".repeat(11);
  ui.element("#wifi-setup-password-input").value = "";
  const submit = () => ui.element("#wifi-setup-form").handlers.submit({preventDefault() {}});
  await submit();
  assert.equal(ui.run("requests.length"), 0);
  assert.match(ui.element("#wifi-setup-error").textContent, /32 bytes/);
  ui.element("#wifi-setup-name-input").value = "New camera";
  ui.element("#wifi-setup-password-input").value = "short";
  await submit();
  assert.equal(ui.run("requests.length"), 0);
  assert.match(ui.element("#wifi-setup-error").textContent, /8–63/);
  ui.element("#wifi-setup-password-input").value = "";
  await submit();
  assert.deepEqual(JSON.parse(ui.run("JSON.stringify(requests)")), [{path: "/api/network/setup", body: {ssid: "New camera", password: ""}}]);
  assert.equal(ui.element("#wifi-setup-form").hidden, true);
  assert.equal(ui.element("#wifi-setup-password-input").value, "");
  assert.equal(ui.element("#wifi-setup-ssid").textContent, "New camera");
  assert.equal(ui.element("#wifi-setup-download").href, "/api/network/setup/qr.png?v=b");
  assert.equal(ui.element("#wifi-setup-message").textContent, "Camera Wi-Fi saved.");
});

test("old network polls cannot replace newly committed camera Wi-Fi details", async () => {
  const ui = dashboard();
  ui.run(`state.network={available:true,busy:false,setup:{ssid:'Old',password:'old-password',revision:'a',active:false}};
    globalThis.resolveOld=null;api=(path)=>path==='/api/network'?new Promise(resolve=>{resolveOld=resolve;}):
      Promise.resolve({accepted:true,setup:{ssid:'New',password:'new-password',revision:'b',active:true}});
    globalThis.oldPoll=refreshNetwork();`);
  ui.element("#wifi-setup-name-input").value = "New";
  ui.element("#wifi-setup-password-input").value = "new-password";
  await ui.element("#wifi-setup-form").handlers.submit({preventDefault() {}});
  ui.run(`resolveOld({available:true,busy:false,setup:{ssid:'Old',password:'old-password',revision:'a',active:false}});`);
  await ui.run("oldPoll");
  assert.equal(ui.element("#wifi-setup-ssid").textContent, "New");
  assert.equal(ui.element("#wifi-setup-password-input").value, "");
  assert.equal(ui.element("#wifi-setup-edit").disabled, true);
  assert.equal(ui.element("#wifi-setup-reconnect").hidden, false);
});

test("HTTP clipboard fallback removes temporary credentials and restores focus", async () => {
  const ui = dashboard();
  ui.element("#wifi-copy-password").focus();
  ui.run(`globalThis.copied='';navigator.clipboard={writeText:async()=>{throw new Error('HTTP clipboard unavailable');}};
    document.execCommand=(command)=>{copied=document.body.children[0].value;return command==='copy';};`);
  await ui.run("copyWifiValue('test-password','Password')");
  assert.equal(ui.run("copied"), "test-password");
  assert.equal(ui.run("document.body.children.length"), 0);
  assert.equal(ui.run("document.activeElement.selector"), "#wifi-copy-password");
  assert.equal(ui.element("#toast").textContent, "Password copied.");
  ui.run("document.execCommand=()=>false;");
  await ui.run("copyWifiValue('test-password','Password')");
  assert.match(ui.element("#toast").textContent, /Tap Show/);
  assert.equal(ui.run("document.body.children.length"), 0);
});

test("camera work and network changes disable Wi-Fi edits without blocking QR sharing", async () => {
  const ui = dashboard();
  ui.run(`state.network={available:true,busy:false,setup:{ssid:'Camera',password:'test-password',revision:'a',active:false}};
    state.status={capture:{state:'running'}};renderWifiSetup();globalThis.requests=[];api=async(path)=>{requests.push(path);};`);
  assert.equal(ui.element("#wifi-setup-edit").disabled, true);
  assert.equal(ui.element("#wifi-connect").disabled, true);
  assert.equal(ui.element("#wifi-setup-save").disabled, true);
  await ui.element("#wifi-setup-form").handlers.submit({preventDefault() {}});
  assert.equal(ui.run("requests.length"), 0);
  assert.equal(ui.element("#wifi-setup-qr").src, "/api/network/setup/qr.png?v=a");
  ui.run(`state.status.capture.state='idle';state.network.busy=true;renderNetworkControls();`);
  assert.equal(ui.element("#wifi-setup-edit").disabled, true);
  ui.run(`state.network.busy=false;renderNetworkControls();`);
  assert.equal(ui.element("#wifi-setup-edit").disabled, false);
});
