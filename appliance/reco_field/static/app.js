"use strict";

const state = {
  csrf: null,
  settings: null,
  status: null,
  preflight: null,
  recordings: [],
  sessions: [],
  sessionsAvailable: null,
  sessionsError: "",
  sessionsRenderKey: null,
  recordingMode: null,
  offlineOperation: null,
  offlineSessionId: null,
  offlineError: "",
  offlineLastState: "idle",
  hls: null,
  busy: false,
  editingDestination: null,
  lensError: "",
  captureError: "",
  activeView: "live",
  setupSection: "cameras",
  statusPending: false,
  recordingsPending: false,
  networkPending: false,
  network: null,
  networkSaving: false,
  networkGeneration: 0,
  wifiPasswordVisible: false,
  wifiQrRevision: null,
  connected: false,
  previewMounted: false,
  previewCaptureKey: null,
  previewProcessId: null,
  destinationRenderKey: "",
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function randomId(prefix) {
  const bytes = new Uint8Array(6);
  if (globalThis.crypto?.getRandomValues) {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  const suffix = Array.from(bytes, (value) =>
    value.toString(16).padStart(2, "0")
  ).join("");
  return `${prefix}-${suffix}`;
}

const appView = $("#app-view");
const globalNotice = $("#global-notice");
const toast = $("#toast");

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

async function api(path, options = {}, retry = true) {
  const method = options.method || "GET";
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof Blob) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!["GET", "HEAD"].includes(method) && state.csrf) {
    headers.set("X-CSRF-Token", state.csrf);
  }
  const response = await fetch(path, {
    ...options,
    method,
    headers,
    credentials: "same-origin",
  });
  let payload = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    payload = await response.json();
  } else {
    payload = await response.text();
  }
  if (response.status === 401 && retry && path !== "/api/session") {
    state.csrf = (await api("/api/session")).csrf;
    return api(path, options, false);
  }
  if (!response.ok) {
    const detail = payload && typeof payload === "object" ? payload.detail : payload;
    const error = new Error(detail || `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function setError(element, message) {
  element.textContent = message || "";
  element.hidden = !message;
}

let toastTimer = null;
function showToast(message, isError = false) {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.hidden = false;
  toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 3600);
}

async function loadAll() {
  try {
    const [settings, status, preflight, recordings] = await Promise.all([
      api("/api/settings"),
      api("/api/status"),
      api("/api/preflight"),
      api("/api/recordings"),
    ]);
    state.settings = settings;
    state.status = status;
    state.preflight = preflight;
    state.recordings = recordings;
    state.connected = true;
    renderSettings();
    renderStatus();
    renderPreflight();
    renderRecordings();
    refreshNetwork();
  } catch (error) {
    if (appView.hidden) return;
    showToast(error.message, true);
  }
}

async function refreshStatus() {
  if (appView.hidden || document.hidden || state.busy || state.statusPending) return;
  state.statusPending = true;
  try {
    state.status = await api("/api/status");
    state.connected = true;
    renderStatus();
  } catch {
    state.connected = false;
    setChip("#connection-chip", "Disconnected", "failed");
    renderDeviceMetrics();
    destroyPreview();
    $("#preview-empty-title").textContent = "Jetson disconnected";
    $("#preview-detail").textContent = "Waiting to reconnect";
  } finally {
    state.statusPending = false;
  }
}

function setChip(id, label, className = "") {
  const chip = $(id);
  chip.className = `status-chip ${className}`.trim();
  $("span:last-child", chip).textContent = label;
}

function activeState(value) {
  return ["starting", "running", "stopping"].includes(value);
}

function captureStatus(status = state.status) {
  if (typeof status?.capture?.state === "string") return status.capture;
  const record = status?.record || {state: "idle", error: "", elapsed_seconds: 0};
  return {...record, kind: activeState(record.state) ? "recording" : null, mode: "panorama", bytes: record.recording?.size};
}

function renderRecordingMode() {
  const modes = state.status?.capabilities?.recording_modes || ["panorama"];
  const sources = modes.includes("sources");
  const selected = state.recordingMode || state.settings?.recording_mode || "panorama";
  const capture = captureStatus();
  const recording = capture.kind === "recording" && activeState(capture.state);
  const displayed = recording ? capture.mode : selected;
  const mode = displayed === "sources" && sources ? "sources" : "panorama";
  if (!recording) state.recordingMode = mode;
  $("#record-mode-control").hidden = !sources;
  $("#recording-mode").value = mode;
  $("#recording-mode-help").textContent = mode === "sources"
    ? "Saves each camera separately. Stitched preview and streaming run at up to 10 fps."
    : "Saves the stitched panorama.";
  $("#recording-mode").disabled = state.busy || offlineActive() || capture.state === "stopping" ||
    recording;
}

function renderStatus() {
  const status = state.status;
  if (!status) return;
  const record = status.record;
  const capture = captureStatus(status);
  const active = activeState(capture.state);
  const recording = active && capture.kind === "recording";
  const previewing = active && capture.kind === "preview";
  const cameraBusy = offlineActive() || ["calibration", "snapshots", "lens"].some((key) => activeState(status[key]?.state));
  setChip("#connection-chip", "Connected", "ready");
  $("#record-chip").hidden = !recording;
  $("#preview-chip").hidden = !previewing;
  setChip("#record-chip", capture.state === "stopping" ? "Finishing" : "Recording", "recording");
  const live = (status.destinations || []).some((item) => item.state === "running");
  setChip("#live-chip", live ? "Sending" : recording && record.stream_enabled ? "Connecting" : "Stream off", live ? "live" : "");
  $("#live-chip").hidden = !live && !(recording && record.stream_enabled);
  $("#record-time").hidden = !recording;
  $("#record-time").textContent = formatDuration(record.elapsed_seconds || capture.elapsed_seconds || 0);
  const recordButton = $("#record-button");
  recordButton.disabled = capture.state === "stopping" || state.busy || cameraBusy || (previewing && capture.state === "starting");
  recordButton.classList.toggle("active", recording);
  $("span:last-child", recordButton).textContent = recording && capture.state === "stopping" ? "Finishing…" : recording ? "Stop recording" : "Record";
  $("use", recordButton).setAttribute("href", recording ? "#i-stop" : "#i-record");
  const previewButton = $("#preview-button");
  previewButton.hidden = !status.capabilities?.preview || recording;
  previewButton.disabled = state.busy || cameraBusy || capture.state === "stopping";
  previewButton.textContent = previewing && capture.state === "stopping" ? "Stopping…" : previewing ? "Stop preview" : "Start preview";
  $("#capture-state").hidden = !active;
  $("#capture-state").textContent = previewing ? `${capture.state === "starting" ? "Starting preview" : "Preview"} · ${formatDuration(capture.elapsed_seconds || 0)}` : capture.mode === "sources" ? "Saving originals" : "Saving panorama";
  $("#start-stream").disabled = recording || state.busy;
  if (recording) $("#start-stream").checked = Boolean(record.stream_enabled);
  $("#simulator-badge").hidden = !status.simulator;
  globalNotice.hidden = !status.simulator;
  globalNotice.classList.toggle("simulator", Boolean(status.simulator));
  if (status.simulator) globalNotice.textContent = "Simulator mode is active. Hardware capture is disabled.";
  setError($("#record-error"), state.captureError || capture.error || record.error);
  $("#preview-empty-title").textContent = capture.state === "failed" ? "Camera unavailable" : "Cameras idle";
  $("#preview-detail").textContent = capture.state === "starting" ? "Camera is warming up" : capture.state === "failed" ? "Camera process failed" : status.capabilities?.preview ? "Start preview to check the alignment" : "Record to open the stitched viewfinder";
  if (active && state.activeView === "live" && !document.hidden) ensurePreview();
  else destroyPreview();
  renderRecordingMode();
  renderDeviceMetrics();
  updateActiveRecording();
  renderReadinessSummary();
  renderOfflineJob();
  renderSnapshots(status.snapshots);
  renderCalibration(status.calibration);
  renderLensCalibration(status.lens);
  renderDestinations(status.destinations || []);
  renderStreamPublisher(status.stream_publisher);
  setSettingsLocked(state.busy || active || cameraBusy);
}

function formatDuration(totalSeconds) {
  totalSeconds = Number.isFinite(totalSeconds) ? Math.max(0, Math.floor(totalSeconds)) : 0;
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return [hours, minutes, seconds]
    .map((value, index) =>
      index ? String(value).padStart(2, "0") : String(value)
    )
    .join(":")
    .replace(/^0:/, "");
}

function titleCase(value) {
  return String(value || "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function ensurePreview() {
  const capture = captureStatus();
  const captureKey = JSON.stringify([capture.kind, capture.mode, capture.session_id || null,
    state.status?.record?.recording?.name || null]);
  const processId = capture.metrics?.session_id || null;
  if (state.previewMounted && state.previewCaptureKey === captureKey &&
      (!processId || !state.previewProcessId || processId === state.previewProcessId)) {
    if (processId) state.previewProcessId = processId;
    return;
  }
  if (state.previewMounted) destroyPreview();
  state.previewMounted = true;
  state.previewCaptureKey = captureKey;
  state.previewProcessId = processId;
  const video = $("#preview-video");
  const empty = $("#preview-empty");
  const source = "/api/preview/reco/live/index.m3u8";
  empty.hidden = true;
  setError($("#preview-error"), "");
  if (state.hls) return;
  if (window.Hls && window.Hls.isSupported()) {
    const hls = new window.Hls({
      lowLatencyMode: true,
      liveSyncDurationCount: 2,
      manifestLoadingRetryDelay: 1000,
      manifestLoadingMaxRetry: 30,
      levelLoadingRetryDelay: 1000,
      levelLoadingMaxRetry: 30,
    });
    state.hls = hls;
    hls.loadSource(source);
    hls.attachMedia(video);
    hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
      if (state.hls !== hls) return;
      video.play().catch(() => {});
    });
    hls.on(window.Hls.Events.ERROR, (_event, data) => {
      if (state.hls !== hls) return;
      if (!data.fatal) return;
      if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
        hls.recoverMediaError();
        return;
      }
      setError($("#preview-error"), "Live preview is reconnecting.");
      hls.startLoad();
    });
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = source;
    video.play().catch(() => {});
  } else {
    setError($("#preview-error"), "This browser cannot play HLS preview.");
  }
}

function destroyPreview() {
  const video = $("#preview-video");
  video.controls = false;
  state.previewCaptureKey = null;
  state.previewProcessId = null;
  if (!state.previewMounted) return;
  state.previewMounted = false;
  if (state.hls) {
    state.hls.destroy();
    state.hls = null;
  }
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("#preview-empty").hidden = false;
  setError($("#preview-error"), "");
}

async function saveRecordingMode() {
  if (!state.settings || !state.status?.capabilities?.recording_modes ||
      state.settings.recording_mode === state.recordingMode) return;
  state.settings = await api("/api/settings", {method: "PUT",
    body: JSON.stringify({...state.settings, recording_mode: state.recordingMode})});
}

$("#recording-mode").addEventListener("change", async () => {
  state.recordingMode = $("#recording-mode").value;
  state.captureError = "";
  renderRecordingMode();
  if (activeState(captureStatus().state)) return;
  state.busy = true;
  renderStatus();
  try {
    await saveRecordingMode();
  } catch (error) {
    state.recordingMode = state.settings?.recording_mode || "panorama";
    state.captureError = error.message;
  } finally {
    state.busy = false;
    renderStatus();
  }
});

$("#preview-button").addEventListener("click", async () => {
  if (state.busy || !state.status?.capabilities?.preview) return;
  const capture = captureStatus();
  const stopping = capture.kind === "preview" && activeState(capture.state);
  state.captureError = "";
  state.busy = true;
  renderStatus();
  $("#preview-button").textContent = stopping ? "Stopping…" : "Starting…";
  try {
    state.status = await api(stopping ? "/api/preview/stop" : "/api/preview/start", {method: "POST", body: "{}"});
    if (stopping) destroyPreview();
  } catch (error) {
    state.captureError = error.message;
  } finally {
    state.busy = false;
    renderStatus();
  }
});

$("#record-button").addEventListener("click", async () => {
  if (state.busy) return;
  const capture = captureStatus();
  const recording = capture.kind === "recording" && activeState(capture.state);
  state.captureError = "";
  state.busy = true;
  renderStatus();
  $("span:last-child", $("#record-button")).textContent = recording ? "Finishing…" : "Starting…";
  try {
    if (!recording && capture.kind === "preview" && activeState(capture.state)) {
      state.status = await api("/api/preview/stop", {method: "POST", body: "{}"});
      destroyPreview();
    }
    if (!recording) await saveRecordingMode();
    const payload = {stream: $("#start-stream").checked};
    if (state.status?.capabilities?.recording_modes) payload.mode = state.recordingMode || "panorama";
    state.status = await api(recording ? "/api/recordings/stop" : "/api/recordings/start", {
      method: "POST", body: recording ? undefined : JSON.stringify(payload),
    });
    renderStatus();
    await refreshRecordings();
  } catch (error) {
    state.captureError = error.message;
  } finally {
    state.busy = false;
    renderStatus();
  }
});

function renderSettings() {
  const settings = state.settings;
  if (!settings) return;
  const camera = settings.camera;
  const fields = {
    "left-device": camera.left_device,
    "right-device": camera.right_device,
    "sensor-mode": camera.sensor_mode,
    "capture-width": camera.capture_width,
    "capture-height": camera.capture_height,
    "capture-fps": camera.capture_fps,
    "output-width": camera.output_width,
    "output-height": camera.output_height,
    projection: camera.projection,
    encoder: camera.encoder,
    preset: camera.preset,
    quality: camera.quality,
    blend: camera.blend,
    "exposure-mode": camera.exposure_mode,
    "exposure-min": camera.exposure_min_ns,
    "exposure-max": camera.exposure_max_ns,
    "gain-min": camera.gain_min,
    "gain-max": camera.gain_max,
    "digital-gain-min": camera.digital_gain_min,
    "digital-gain-max": camera.digital_gain_max,
    "awb-mode": camera.awb_mode,
    "calibration-frames": settings.calibration_frames,
    "min-free-gb": settings.min_free_gb,
    "stream-profile": settings.stream_profile,
  };
  for (const [id, value] of Object.entries(fields)) {
    const element = $(`#${id}`);
    if (element) element.value = value;
  }
  $("#awb-lock").checked = camera.awb_lock;
  $("#preview-mode").textContent = camera.projection === "panorama" ? "Panorama" : "Camera crop";
  $("#preview-frame").style.aspectRatio = `${camera.output_width} / ${camera.output_height}`;
  for (const frame of document.querySelectorAll(".snapshot-frame")) {
    frame.style.aspectRatio = `${camera.capture_width} / ${camera.capture_height}`;
  }
  $("#auto-stream").checked = settings.auto_stream;
  $("#auto-record").checked = settings.auto_record;
  $("#start-stream").checked = settings.auto_stream;
  $("#capture-summary").textContent = `2 cameras · ${camera.capture_width} × ${camera.capture_height} · ${camera.capture_fps} fps configured`;
  state.recordingMode = settings.recording_mode || "panorama";
  renderRecordingMode();
  renderDestinations(state.status?.destinations || []);
  renderStreamPublisher(state.status?.stream_publisher);
}

function renderStreamPublisher(publisher) {
  const profile = state.settings?.stream_profile || "mobile";
  const mbps = (publisher?.video_bitrate || 6000000) / 1000000;
  $("#stream-profile option[value=mobile]").textContent = `Mobile · ${mbps} Mbps`;
  $("#stream-profile-help").textContent = profile === "mobile"
    ? `${mbps} Mbps per destination, plus audio and network overhead.`
    : "Each destination uses the recording bitrate.";
  $("#stream-publisher-status").textContent = profile === "original"
    ? "Original quality · stream copy"
    : `Mobile stream · ${publisher?.state || "idle"}${publisher?.error ? ` · ${publisher.error}` : ""}`;
}

$("#stream-profile").addEventListener("change", async () => {
  if (!state.settings || state.busy) return;
  state.busy = true;
  setSettingsLocked(true);
  try {
    state.settings = await api("/api/settings", {method: "PUT",
      body: JSON.stringify({...state.settings, stream_profile: $("#stream-profile").value})});
    showToast("Stream profile saved.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.busy = false;
    renderSettings();
    renderStatus();
  }
});

function cameraFromForm() {
  const camera = {
    ...state.settings.camera,
    left_device: $("#left-device").value.trim(),
    right_device: $("#right-device").value.trim(),
    sensor_mode: Number($("#sensor-mode").value),
    capture_width: Number($("#capture-width").value),
    capture_height: Number($("#capture-height").value),
    capture_fps: Number($("#capture-fps").value),
    output_width: Number($("#output-width").value),
    output_height: Number($("#output-height").value),
    projection: $("#projection").value,
    encoder: $("#encoder").value,
    codec: "h264",
    preset: $("#preset").value,
    quality: $("#quality").value,
    blend: Number($("#blend").value),
    exposure_mode: $("#exposure-mode").value,
    exposure_min_ns: Number($("#exposure-min").value),
    exposure_max_ns: Number($("#exposure-max").value),
    gain_min: Number($("#gain-min").value),
    gain_max: Number($("#gain-max").value),
    digital_gain_min: Number($("#digital-gain-min").value),
    digital_gain_max: Number($("#digital-gain-max").value),
    awb_mode: $("#awb-mode").value,
    awb_lock: $("#awb-lock").checked,
  };
  if (camera.exposure_mode === "manual") {
    camera.exposure_max_ns = camera.exposure_min_ns;
  }
  return camera;
}

$("#camera-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setError($("#camera-form-error"), "");
  try {
    const payload = {
      ...state.settings,
      camera: cameraFromForm(),
      auto_stream: $("#auto-stream").checked,
      auto_record: $("#auto-record").checked,
      calibration_frames: Number($("#calibration-frames").value),
    };
    state.settings = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    renderSettings();
    showToast("Camera settings saved.");
  } catch (error) {
    setError($("#camera-form-error"), error.message);
  }
});

$("#sensor-mode").addEventListener("change", () => {
  const modes = {
    0: [4032, 3040, 30],
    1: [3840, 2160, 30],
    2: [1920, 1080, 60],
  };
  const values = modes[$("#sensor-mode").value];
  if (!values) return;
  $("#capture-width").value = values[0];
  $("#capture-height").value = values[1];
  $("#capture-fps").value = values[2];
});

function setSettingsLocked(locked) {
  $("#settings-lock").hidden = !locked;
  for (const element of $$(
    "#camera-form input, #camera-form select, #camera-form button, " +
      "#service-form input, #service-form button, #add-destination, " +
      "#destination-list button, #snapshot-button, #calibrate-button, #stream-profile, " +
      "#calibration-file, #lens-file, #right-lens-file"
  )) {
    element.disabled = locked;
  }
  renderNetworkControls();
}

function renderPreflight() {
  const container = $("#preflight-list");
  container.replaceChildren();
  const preflight = state.preflight;
  if (!preflight) return;
  for (const [name, check] of Object.entries(preflight.checks || {})) {
    if (name === "tracking" && check.state === "unavailable") continue;
    const row = document.createElement("div");
    row.className = `check-row ${check.state || "unknown"}`;
    row.append(
      icon(
        check.state === "ready" || check.state === "simulated"
          ? "check"
          : "alert"
      )
    );
    const label = document.createElement("span");
    label.textContent = titleCase(name);
    const detail = document.createElement("span");
    detail.className = "check-detail";
    detail.textContent = check.detail || check.state || "";
    row.append(label, detail);
    container.append(row);
  }
  renderReadinessSummary();
  renderDeviceMetrics();
}

$("#refresh-preflight").addEventListener("click", async () => {
  const button = $("#refresh-preflight");
  button.disabled = true;
  try {
    state.preflight = await api("/api/preflight");
    renderPreflight();
    showToast("Readiness refreshed.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

function renderDestinations(liveDestinations) {
  const renderKey = JSON.stringify([state.settings?.destinations, liveDestinations]);
  if (state.destinationRenderKey === renderKey) return;
  state.destinationRenderKey = renderKey;
  const container = $("#destination-list");
  container.replaceChildren();
  const destinations = state.settings?.destinations || [];
  if (!destinations.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No destinations configured";
    container.append(empty);
    return;
  }
  const liveById = new Map(liveDestinations.map((item) => [item.id, item]));
  for (const destination of destinations) {
    const live = liveById.get(destination.id);
    const row = document.createElement("div");
    row.className = "destination-row";
    const main = document.createElement("div");
    main.className = "destination-main";
    const name = document.createElement("strong");
    name.textContent = destination.name;
    const url = document.createElement("span");
    url.textContent = destination.url || "URL not set";
    main.append(name, url);
    const status = document.createElement("span");
    status.className = `destination-state ${live?.state || ""}`;
    status.textContent = destination.enabled
      ? live?.state === "running" ? "Sending" : titleCase(live?.state || "ready")
      : "Disabled";
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const edit = document.createElement("button");
    edit.className = "icon-button";
    edit.type = "button";
    edit.setAttribute("aria-label", `Edit ${destination.name}`);
    edit.title = "Edit destination";
    edit.append(icon("edit"));
    edit.addEventListener("click", () => openDestinationDialog(destination));
    const remove = document.createElement("button");
    remove.className = "icon-button";
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${destination.name}`);
    remove.title = "Remove destination";
    remove.append(icon("trash"));
    remove.addEventListener("click", () => removeDestination(destination.id));
    actions.append(edit, remove);
    row.append(main, status, actions);
    if (live?.error) {
      const error = document.createElement("p");
      error.className = "inline-error";
      error.textContent = live.error;
      row.append(error);
    }
    container.append(row);
  }
}

function openDestinationDialog(destination = null) {
  state.editingDestination = destination;
  $("#destination-dialog-title").textContent = destination
    ? "Edit destination"
    : "Add destination";
  $("#destination-id").value = destination?.id || "";
  $("#destination-name").value = destination?.name || "";
  $("#destination-provider").value = destination?.provider || "youtube";
  $("#destination-url").value = "";
  $("#destination-url").placeholder = destination
    ? "Leave blank to keep current URL"
    : "rtmp://host/app/key";
  $("#destination-url-help").textContent = destination
    ? "Leave the URL blank to preserve the current private stream key."
    : "The stream key is stored privately and never returned to the browser.";
  $("#destination-enabled").checked = destination?.enabled ?? true;
  setError($("#destination-error"), "");
  $("#destination-dialog").showModal();
  $("#destination-name").focus();
}

$("#add-destination").addEventListener("click", () =>
  openDestinationDialog()
);

for (const button of $$("[data-close-dialog]")) {
  button.addEventListener("click", () => $("#destination-dialog").close());
}

$("#destination-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setError($("#destination-error"), "");
  const editing = state.editingDestination;
  const id = editing?.id || randomId("dest");
  const url = $("#destination-url").value.trim();
  if (!editing && !url) {
    setError($("#destination-error"), "Enter an ingest URL.");
    return;
  }
  const destination = {
    id,
    name: $("#destination-name").value.trim(),
    provider: $("#destination-provider").value,
    url: url || null,
    enabled: $("#destination-enabled").checked,
  };
  try {
    const destinations = state.settings.destinations.filter(
      (item) => item.id !== id
    );
    destinations.push(destination);
    state.settings = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ ...state.settings, destinations }),
    });
    $("#destination-dialog").close();
    renderSettings();
    showToast("Destination saved.");
  } catch (error) {
    setError($("#destination-error"), error.message);
  }
});

async function removeDestination(id) {
  const destination = state.settings.destinations.find(
    (item) => item.id === id
  );
  if (!destination || !window.confirm(`Remove ${destination.name}?`)) return;
  try {
    const destinations = state.settings.destinations.filter(
      (item) => item.id !== id
    );
    state.settings = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({ ...state.settings, destinations }),
    });
    renderSettings();
    showToast("Destination removed.");
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderSnapshots(snapshot) {
  if (!snapshot) return;
  for (const side of ["left", "right"]) {
    const image = $(`#snapshot-${side}`);
    const empty = $(`#snapshot-${side}-empty`);
    const data = snapshot[side];
    if (data) {
      image.src = `${data.url}?v=${encodeURIComponent(data.modified_at)}`;
      image.hidden = false;
      empty.hidden = true;
    } else {
      image.hidden = true;
      image.removeAttribute("src");
      empty.hidden = false;
    }
  }
  const active = ["starting", "running", "stopping"].includes(snapshot.state);
  $("#snapshot-button").disabled = active || state.busy;
  $("#snapshot-cancel").hidden = !active;
  $("#snapshot-status").textContent =
    snapshot.progress ||
    (snapshot.state === "complete"
      ? "Both setup views ready"
      : "No setup views captured");
  setError($("#snapshot-error"), snapshot.error);
}

$("#snapshot-button").addEventListener("click", async () => {
  state.busy = true;
  renderStatus();
  try {
    state.status = await api("/api/snapshots", { method: "POST" });
    renderStatus();
  } catch (error) {
    setError($("#snapshot-error"), error.message);
  } finally {
    state.busy = false;
    renderStatus();
  }
});

$("#snapshot-cancel").addEventListener("click", async () => {
  try {
    state.status = await api("/api/snapshots/stop", { method: "POST" });
    renderStatus();
  } catch (error) {
    showToast(error.message, true);
  }
});

function renderLensCalibration(lens) {
  if (!lens) return;
  const side = $("#lens-side").value;
  const data = lens.lenses[side];
  const active = ["starting", "running", "stopping"].includes(lens.state);
  const cameraBusy = offlineActive() || activeState(captureStatus().state) || ["record", "calibration", "snapshots"].some((key) =>
    ["starting", "running", "stopping"].includes(state.status[key].state));
  const locked = active || cameraBusy || state.busy;
  $("#lens-side").disabled = active || state.busy;
  $("#lens-capture").disabled = locked || data.views >= lens.maximum_views;
  $("#lens-solve").disabled = locked || data.views < lens.minimum_views;
  $("#lens-apply").disabled = locked || !data.ready;
  $("#lens-reset").disabled = locked || !data.views;
  $("#lens-cancel").hidden = !active;
  $("#lens-count").textContent = `${data.views} views · ${lens.minimum_views} minimum · ${titleCase(side)} lens`;
  $("#lens-progress").textContent = (side === lens.side ? lens.progress : "") ||
    "Move the printed board between captures. Keep the rig and board still during capture.";
  const quality = data.quality;
  $("#lens-quality").hidden = !quality;
  $("#lens-quality").textContent = quality
    ? `Fit ${quality.training_rms_px.toFixed(2)} px · Held-out ${quality.holdout_rms_px.toFixed(2)} px. Applying backs up the current profile and requires geometric calibration again.` : "";
  const preview = $("#lens-preview");
  if (data.preview) {
    const url = `${data.preview}?v=${encodeURIComponent(data.preview_revision)}`;
    if (preview.dataset.source !== url) {
      preview.src = url;
      preview.dataset.source = url;
    }
  }
  preview.hidden = !data.preview;
  $("#lens-preview-empty").hidden = Boolean(data.preview);
  setError($("#lens-error"), state.lensError || (side === lens.side ? lens.error : ""));
}

$("#lens-side").addEventListener("change", () => {
  state.lensError = "";
  renderLensCalibration(state.status?.lens);
});
for (const operation of ["capture", "solve", "apply", "reset", "cancel"]) {
  $(`#lens-${operation}`).addEventListener("click", async () => {
    if (state.busy) return;
    state.lensError = "";
    state.busy = true;
    renderStatus();
    try {
      const side = $("#lens-side").value;
      state.status = await api(operation === "cancel" ? "/api/lenses/stop" : `/api/lenses/${side}/${operation}`, {method: "POST"});
    } catch (error) {
      state.lensError = error.message;
    } finally {
      state.busy = false;
      renderStatus();
      if (state.lensError) $("#lens-error").focus();
    }
  });
}

function renderCalibration(calibration) {
  if (!calibration) return;
  const badge = $("#calibration-badge");
  badge.className = "badge";
  if (calibration.valid) {
    badge.classList.add("success");
    badge.textContent = "Calibration ready";
  } else if (calibration.state === "failed") {
    badge.classList.add("warning");
    badge.textContent = "Needs attention";
  } else {
    badge.textContent = "Calibration needed";
  }
  const active = ["starting", "running", "stopping"].includes(
    calibration.state
  );
  $("#calibrate-button").disabled = active || state.busy;
  $("#calibrate-cancel").hidden = !active;
  $("#calibration-progress").textContent =
    `${
      calibration.progress ||
      (calibration.valid
        ? "Valid calibration loaded"
        : "Import a valid calibration or run stereo matching.")
    }${
      active
        ? ` (${formatDuration(calibration.elapsed_seconds || 0)})`
        : ""
    }`;
  setError($("#calibration-error"), calibration.error);
}

$("#calibrate-button").addEventListener("click", async () => {
  state.busy = true;
  renderStatus();
  try {
    state.status = await api("/api/calibrate", { method: "POST" });
    renderStatus();
  } catch (error) {
    setError($("#calibration-error"), error.message);
  } finally {
    state.busy = false;
    renderStatus();
  }
});

$("#calibrate-cancel").addEventListener("click", async () => {
  try {
    state.status = await api("/api/calibrate/stop", { method: "POST" });
    renderStatus();
  } catch (error) {
    showToast(error.message, true);
  }
});

async function importJsonFile(input, kind) {
  const file = input.files?.[0];
  if (!file) return;
  try {
    const text = await file.text();
    const result = await api(
      `/api/calibration/import?kind=${encodeURIComponent(kind)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: text,
      }
    );
    state.status.calibration = result;
    renderCalibration(result);
    showToast(
      kind === "calibration"
        ? "Calibration imported."
        : "Lens profile imported. Run geometric calibration to apply it."
    );
  } catch (error) {
    setError($("#calibration-error"), error.message);
  } finally {
    input.value = "";
  }
}

$("#calibration-file").addEventListener("change", (event) =>
  importJsonFile(event.currentTarget, "calibration")
);
$("#lens-file").addEventListener("change", (event) =>
  importJsonFile(event.currentTarget, "left-lens")
);
$("#right-lens-file").addEventListener("change", (event) =>
  importJsonFile(event.currentTarget, "right-lens")
);

async function refreshRecordings() {
  if (document.hidden || state.recordingsPending) return;
  state.recordingsPending = true;
  try {
    const requests = [api("/api/recordings")];
    const sessionsSupported = state.sessionsAvailable !== false || state.status?.capabilities?.recording_modes?.includes("sources");
    if (sessionsSupported) requests.push(api("/api/sessions"));
    const [files, sessions] = await Promise.allSettled(requests);
    if (files.status === "fulfilled") state.recordings = files.value;
    else showToast(files.reason.message, true);
    if (sessions?.status === "fulfilled") {
      state.sessions = sessions.value;
      state.sessionsAvailable = true;
      state.sessionsError = "";
    } else if (sessions?.reason?.status === 404) {
      state.sessionsAvailable = false;
      state.sessionsError = "";
    } else if (sessions?.status === "rejected") state.sessionsError = sessions.reason.message;
    renderRecordings();
  } finally {
    state.recordingsPending = false;
  }
}

function outputDetails(output) {
  const values = [formatBytes(output.size)];
  if (validNumber(output.duration_seconds)) values.push(formatDuration(output.duration_seconds));
  if (validNumber(output.width) && validNumber(output.height)) values.push(`${output.width} × ${output.height}`);
  if (output.codec) values.push(output.codec === "h264" ? "H.264" : String(output.codec).toUpperCase());
  if (validNumber(output.fps)) values.push(`${Number(output.fps.toFixed(2))} fps`);
  return values.join(" · ");
}

function renderSessions() {
  const container = $("#sessions-list");
  setError($("#sessions-error"), state.sessionsError);
  const renderKey = JSON.stringify([state.sessions, Boolean(state.status?.capabilities?.offline_stitch)]);
  if (state.sessionsRenderKey === renderKey) return;
  state.sessionsRenderKey = renderKey;
  const focusedSession = document.activeElement?.closest("[data-session]")?.dataset.session;
  const focusedOutput = document.activeElement?.closest("[data-output]")?.dataset.output;
  const focusedAction = document.activeElement?.dataset.stitchSession;
  container.replaceChildren();
  container.hidden = !state.sessions.length;
  for (const session of state.sessions) {
    const row = document.createElement("article");
    row.className = "session-row";
    row.dataset.session = session.id;
    row.tabIndex = -1;
    const heading = document.createElement("header");
    heading.className = "session-heading";
    const info = document.createElement("div");
    const title = document.createElement("h2");
    title.textContent = session.mode === "sources" ? "Original cameras" : "Camera session";
    const date = document.createElement("p");
    date.textContent = formatDate(session.started_at);
    const total = document.createElement("p");
    total.className = "session-total";
    total.textContent = `${formatBytes(session.size)}${validNumber(session.duration_seconds) ? ` · ${formatDuration(session.duration_seconds)}` : ""}`;
    info.append(title, date, total);
    const stateLabel = document.createElement("span");
    stateLabel.className = "status-line";
    stateLabel.textContent = session.state === "finishing" ? "Finishing files…" : titleCase(session.state);
    heading.append(info, stateLabel);
    row.append(heading);
    if (session.error) {
      const error = document.createElement("p");
      error.className = "inline-error";
      error.textContent = session.error;
      row.append(error);
    }
    const outputs = document.createElement("div");
    outputs.className = "session-outputs";
    const exportCount = (session.outputs || []).filter((output) => output.role === "export").length;
    let exportNumber = 0;
    for (const output of session.outputs || []) {
      const file = document.createElement("div");
      file.className = "recording-row";
      file.dataset.output = output.name;
      const details = document.createElement("div");
      details.className = "recording-info";
      const label = document.createElement("strong");
      if (output.role === "export") exportNumber += 1;
      label.textContent = output.role === "export" ? `Panorama export${exportCount > 1 ? ` ${exportNumber}` : ""}` : ({left: "Left camera", right: "Right camera", panorama: "Panorama"})[output.role] || output.name;
      const metadata = document.createElement("span");
      metadata.textContent = outputDetails(output);
      details.append(label, metadata);
      file.append(details);
      if (output.complete && output.download_url) {
        const link = document.createElement("a");
        link.href = output.download_url;
        link.setAttribute("aria-label", `Download ${label.textContent} from ${session.id}`);
        link.append(icon("download"), document.createTextNode("Download"));
        file.append(link);
      }
      outputs.append(file);
    }
    row.append(outputs);
    if (session.can_stitch && state.status?.capabilities?.offline_stitch) {
      const actions = document.createElement("div");
      actions.className = "session-actions";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button primary";
      button.dataset.stitchSession = session.id;
      button.disabled = offlineActive() || activeState(captureStatus().state);
      button.textContent = "Create panorama";
      button.addEventListener("click", () => requestOfflineJob(session.id));
      actions.append(button);
      row.append(actions);
    }
    container.append(row);
    if (session.id === focusedSession && focusedOutput) {
      let link = null;
      for (const output of $$("[data-output]", row)) {
        if (output.dataset.output === focusedOutput) link = $("a", output);
      }
      (link || row).focus({preventScroll: true});
    } else if (session.id === focusedAction) {
      const action = $("[data-stitch-session]", row);
      (action && !action.disabled ? action : row).focus({preventScroll: true});
    } else if (session.id === focusedSession) row.focus({preventScroll: true});
  }
}

function renderRecordings() {
  renderSessions();
  renderOfflineJob();
  const container = $("#recordings-list");
  const focusedRecording = document.activeElement?.closest("[data-recording]")?.dataset.recording;
  container.replaceChildren();
  const active = ["starting", "running", "stopping"].includes(state.status?.record.state)
    ? state.status.record.recording : null;
  const recordings = [...state.recordings];
  if (active && !recordings.some((item) => item.name === active.name)) recordings.unshift(active);
  const totalSize = recordings.reduce((total, item) => total + (Number.isFinite(item.size) ? item.size : 0), 0);
  const sessionsSize = state.sessions.reduce((total, session) => total + (validNumber(session.size) ? session.size : 0), 0);
  $("#recordings-summary").textContent = `${recordings.length} ${recordings.length === 1 ? "file" : "files"}${state.sessions.length ? ` · ${state.sessions.length} camera ${state.sessions.length === 1 ? "session" : "sessions"}` : ""} · ${formatBytes(totalSize + sessionsSize)} on SSD`;
  container.hidden = !recordings.length && state.sessions.length > 0;
  if (!recordings.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No recordings yet. Your finished files will appear here.";
    container.append(empty);
    return;
  }
  for (const recording of recordings) {
    const row = document.createElement("article");
    row.className = "recording-row";
    row.dataset.recording = recording.name;
    const info = document.createElement("div");
    info.className = "recording-info";
    const name = document.createElement("strong");
    name.textContent = recording.name;
    const meta = document.createElement("span");
    meta.textContent = recording.modified_at ? `Modified ${formatDate(recording.modified_at)}` : "Recording in progress";
    info.append(name, meta);
    const details = document.createElement("div");
    details.className = "recording-details";
    const size = document.createElement("span");
    size.className = "recording-size";
    size.textContent = formatBytes(recording.name === active?.name ? active.size : recording.size);
    details.append(size);
    if (Number.isFinite(recording.duration_seconds)) {
      const duration = document.createElement("span");
      duration.textContent = formatDuration(recording.duration_seconds);
      details.append(duration);
    }
    if (Number.isFinite(recording.width) && Number.isFinite(recording.height)) {
      const dimensions = document.createElement("span");
      dimensions.textContent = `${recording.width} × ${recording.height}`;
      details.append(dimensions);
    }
    if (recording.codec) {
      const codec = document.createElement("span");
      codec.textContent = recording.codec === "h264" ? "H.264" : String(recording.codec).toUpperCase();
      details.append(codec);
    }
    if (recording.metadata_state === "pending") {
      const pending = document.createElement("span");
      pending.textContent = "Reading details…";
      details.append(pending);
    }
    info.append(details);
    row.append(info);
    if (recording.name === active?.name || recording.metadata_state === "recording") {
      const status = document.createElement("span");
      status.className = "status-line";
      status.textContent = state.status?.record.state === "stopping" ? "Finishing files…" : "Recording in progress";
      row.append(status);
    } else if (recording.download_url) {
      const link = document.createElement("a");
      link.href = recording.download_url;
      link.setAttribute("aria-label", `Download ${recording.name}`);
      link.append(icon("download"), document.createTextNode("Download"));
      row.append(link);
    }
    container.append(row);
    if (recording.name === focusedRecording) $("a", row)?.focus({preventScroll: true});
  }
}

function updateActiveRecording() {
  const capture = captureStatus();
  if (capture.kind === "recording" && activeState(capture.state) && capture.session_id) {
    for (const row of $$("[data-session]")) {
      if (row.dataset.session === capture.session_id) {
        $(".session-total", row).textContent = `${formatBytes(capture.bytes)} · ${formatDuration(capture.elapsed_seconds || 0)}`;
        for (const file of $$("[data-output]", row)) {
          const output = capture.outputs?.find((item) => item.name === file.dataset.output);
          if (output) $(".recording-info > span", file).textContent = outputDetails(output);
        }
      }
    }
  }
  const active = state.status?.record?.recording;
  if (!active || !["starting", "running", "stopping"].includes(state.status.record.state)) return;
  for (const row of $$("[data-recording]")) {
    if (row.dataset.recording === active.name) $(".recording-size", row).textContent = formatBytes(active.size);
  }
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function formatDate(value) {
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(value));
  } catch {
    return "Date unavailable";
  }
}

$("#refresh-recordings").addEventListener("click", refreshRecordings);

$("#service-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    state.settings = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        ...state.settings,
        min_free_gb: Number($("#min-free-gb").value),
      }),
    });
    renderSettings();
    showToast("Service settings saved.");
  } catch (error) {
    showToast(error.message, true);
  }
});

function validNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function freshSample(timestamp) {
  const sampledAt = Date.parse(timestamp);
  return Number.isFinite(sampledAt) && Date.now() - sampledAt <= 10000 && sampledAt - Date.now() <= 2000;
}

function metric(id, text) {
  const element = $(id);
  if (element.textContent !== text) element.textContent = text;
  element.title = text === "—" ? "Measurement unavailable" : "";
}

function renderDeviceMetrics() {
  const status = state.status;
  const device = status?.device;
  const fresh = state.connected && freshSample(device?.sampled_at);
  const capture = status?.capture?.metrics;
  const captureFresh = state.connected && freshSample(capture?.sampled_at);
  const currentCapture = captureStatus(status);
  const active = activeState(currentCapture.state);
  const recording = active && currentCapture.kind === "recording";
  $("#metric-fps-label").textContent = currentCapture.mode === "sources" ? "Preview fps" : "Frame rate";
  metric("#metric-fps", captureFresh && active && validNumber(capture?.fps) ? `${capture.fps.toFixed(1)} fps` : "—");
  const recordedBytes = validNumber(currentCapture.bytes) ? currentCapture.bytes : status?.record?.recording?.size;
  metric("#metric-record-size", state.connected && recording && validNumber(recordedBytes) ? formatBytes(recordedBytes) : "—");
  metric("#metric-storage", fresh && device.storage?.state === "ready" && validNumber(device.storage.free_bytes) ? formatBytes(device.storage.free_bytes) : "—");
  metric("#metric-temperature", fresh && validNumber(device.cpu_temperature_c) ? `${device.cpu_temperature_c.toFixed(0)} °C` : "—");
  metric("#metric-memory", fresh && validNumber(device.memory_used_bytes) ? formatBytes(device.memory_used_bytes) : "—");
  $(".metrics-strip").classList.toggle("stale", !state.connected);
  $("#metrics-note").textContent = !state.connected ? "Disconnected · measurements unavailable" : fresh ? "Device measurements updated" : "Device measurements unavailable";
  $("#device-sample-state").textContent = fresh ? "Current measurements" : "Measurements unavailable";
  metric("#device-model", device?.model || "—");
  metric("#device-uptime", fresh && validNumber(device.uptime_seconds) ? formatDuration(device.uptime_seconds) : "—");
  metric("#device-cpu", fresh && validNumber(device.cpu_percent) ? `${device.cpu_percent.toFixed(0)}%${validNumber(device.cpu_cores) ? ` · ${device.cpu_cores} cores` : ""}` : "—");
  metric("#device-memory", fresh && validNumber(device.memory_used_bytes) && validNumber(device.memory_total_bytes) ? `${formatBytes(device.memory_used_bytes)} of ${formatBytes(device.memory_total_bytes)}` : "—");
  metric("#device-storage", fresh && device.storage?.state === "ready" && validNumber(device.storage.free_bytes) && validNumber(device.storage.total_bytes) ? `${formatBytes(device.storage.free_bytes)} free of ${formatBytes(device.storage.total_bytes)}` : device?.storage?.state && device.storage.state !== "ready" ? titleCase(device.storage.state) : "—");
  const platform = state.preflight?.platform;
  metric("#device-software", platform ? `Reco Field 0.1.0 · ${platform.machine || ""}${platform.release ? ` · ${platform.release}` : ""}` : "Reco Field 0.1.0");
}

function offlineActive() {
  return Boolean(state.offlineOperation) || activeState(state.status?.offline_job?.state);
}

async function requestOfflineJob(sessionId, cancel = false) {
  if (state.offlineOperation || (!cancel && (activeState(captureStatus().state) || offlineActive()))) return;
  state.offlineSessionId = sessionId;
  state.offlineError = "";
  state.offlineOperation = cancel ? "cancel" : "start";
  renderOfflineJob();
  try {
    state.status = await api(`/api/sessions/${encodeURIComponent(sessionId)}/stitch${cancel ? "/cancel" : ""}`, {method: "POST", body: "{}"});
  } catch (error) {
    if (cancel && error.status === 409) {
      try {
        const latest = await api("/api/status");
        if (latest.offline_job?.session_id === sessionId &&
            ["complete", "cancelled", "failed", "interrupted"].includes(latest.offline_job.state)) state.status = latest;
        else state.offlineError = error.message;
      } catch {
        state.offlineError = error.message;
      }
    } else state.offlineError = error.message;
  } finally {
    state.offlineOperation = null;
    renderStatus();
  }
}

function renderOfflineJob() {
  const job = state.status?.offline_job;
  const phase = state.offlineError ? "failed" : job?.state || "idle";
  const busy = offlineActive();
  $("#offline-job-panel").hidden = phase === "idle" && !state.offlineOperation && !state.offlineError;
  $("#offline-job-heading").textContent = phase === "complete" ? "Panorama ready" : "Create panorama";
  const progress = state.offlineOperation === "start" ? "Starting panorama…" : state.offlineOperation === "cancel" || phase === "stopping" ? "Cancelling panorama…" : phase === "starting" ? "Preparing saved camera views…" : phase === "running" ? "Creating panorama" : phase === "complete" ? "The original camera files are preserved." : phase === "cancelled" ? "Cancelled. The original camera files are preserved." : phase === "interrupted" ? "The panorama job was interrupted. Original camera files are preserved." : phase === "failed" ? "Panorama could not be created." : "";
  $("#offline-job-progress").textContent = phase === "running" && validNumber(job?.processed_frames) ? `${progress} · ${Math.floor(job.processed_frames).toLocaleString()} frames${validNumber(job.fps) ? ` · ${Number(job.fps.toFixed(1))} fps` : ""}` : progress;
  setError($("#offline-job-error"), state.offlineError || job?.error);
  const cancel = $("#offline-job-cancel");
  cancel.hidden = !activeState(job?.state);
  cancel.disabled = Boolean(state.offlineOperation) || job?.state === "stopping";
  cancel.textContent = job?.state === "stopping" || state.offlineOperation === "cancel" ? "Cancelling…" : "Cancel";
  const download = $("#offline-job-download");
  download.hidden = phase !== "complete" || !job?.output?.download_url;
  if (!download.hidden) download.href = job.output.download_url;
  else download.removeAttribute("href");
  for (const button of $$("[data-stitch-session]")) {
    const focused = document.activeElement === button;
    button.disabled = busy || activeState(captureStatus().state);
    if (focused && button.disabled) button.closest("[data-session]")?.focus({preventScroll: true});
  }
  if (state.offlineLastState !== (job?.state || "idle")) {
    state.offlineLastState = job?.state || "idle";
    if (["complete", "cancelled", "failed", "interrupted"].includes(job?.state)) refreshRecordings();
  }
}

$("#offline-job-cancel").addEventListener("click", () => {
  const sessionId = state.status?.offline_job?.session_id;
  if (sessionId) requestOfflineJob(sessionId, true);
});

function renderReadinessSummary() {
  const summary = $("#readiness-summary");
  const preflight = state.preflight;
  const alignment = state.status?.calibration;
  summary.classList.toggle("failed", preflight?.ready === false || alignment?.valid === false);
  summary.href = offlineActive() ? "#recordings" : "#setup/device";
  summary.textContent = offlineActive() ? "Creating panorama · View job" : state.status?.simulator ? "Simulator mode" : preflight?.ready === false ? "Device needs attention" : alignment?.valid === false ? "Camera alignment needed" : preflight?.ready ? "Cameras ready" : "Checking device readiness";
}

function navigate() {
  const [requestedView, requestedSection] = location.hash.slice(1).split("/");
  const view = ["live", "recordings", "setup"].includes(requestedView) ? requestedView : "live";
  const section = ["cameras", "alignment", "network", "device"].includes(requestedSection) ? requestedSection : "cameras";
  state.activeView = view;
  state.setupSection = section;
  for (const panel of $$(".main-view")) panel.hidden = panel.id !== `view-${view}`;
  for (const link of $$("[data-view]")) {
    if (link.dataset.view === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  for (const panel of $$(".setup-panel")) panel.hidden = panel.id !== `setup-${section}`;
  for (const link of $$("[data-setup]")) {
    if (link.dataset.setup === section) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (view === "recordings") refreshRecordings();
  if (view === "setup" && section === "network") refreshNetwork();
  if (state.status) renderStatus();
}
window.addEventListener("hashchange", navigate);
$(".skip-link").addEventListener("click", (event) => {
  event.preventDefault();
  $("#main-content").focus();
});
$("#preview-video").addEventListener("loadedmetadata", () => {
  if (state.previewMounted) $("#preview-video").controls = true;
});
$("#preview-video").addEventListener("playing", () => setError($("#preview-error"), ""));

async function refreshNetwork() {
  if (appView.hidden || document.hidden || state.networkPending) return;
  state.networkPending = true;
  const generation = state.networkGeneration;
  try {
    const network = await api("/api/network");
    if (generation !== state.networkGeneration) return;
    state.network = network;
    $("#network-state").textContent = network.available
      ? `${network.connected ? "Connected" : "Disconnected"} · ${network.connection || network.interface}${network.address ? ` · ${network.address}` : ""}`
      : network.error;
    if (network.message) $("#wifi-message").textContent = network.message;
    renderWifiSetup();
  } catch {
    if (generation !== state.networkGeneration) return;
    $("#network-state").textContent = "Connection changed. Reopen reco-jetson.local:8081 from the hotspot network.";
    if (state.network) state.network.available = false;
  } finally {
    state.networkPending = false;
    renderNetworkControls();
  }
}

function networkLocked() {
  return state.busy || state.networkSaving || !state.network?.available || state.network.busy ||
    activeState(captureStatus().state) || ["calibration", "snapshots", "lens"].some((key) => activeState(state.status?.[key]?.state));
}

function renderNetworkControls() {
  const locked = networkLocked();
  for (const id of ["wifi-connect", "wifi-scan", "wifi-setup-edit", "wifi-setup-save", "wifi-setup-name-input", "wifi-setup-password-input"]) {
    $(`#${id}`).disabled = locked;
  }
  $("#wifi-setup-cancel").disabled = state.networkSaving;
  $("#wifi-setup-save").textContent = state.networkSaving ? "Saving…" : "Save Wi-Fi";
}

function renderWifiSetup() {
  const setup = state.network?.setup;
  $("#wifi-setup-details").hidden = !setup;
  if (!setup) return;
  const revision = String(setup.revision || "");
  if (revision !== state.wifiQrRevision) {
    state.wifiQrRevision = revision;
    state.wifiPasswordVisible = false;
    const url = `/api/network/setup/qr.png${revision ? `?v=${encodeURIComponent(revision)}` : ""}`;
    $("#wifi-setup-qr").src = url;
    $("#wifi-setup-download").href = url;
    setError($("#wifi-qr-error"), "");
  }
  $("#wifi-setup-availability").textContent = setup.active ? "Ready to join nearby." : "Starts automatically when no saved network is available.";
  $("#wifi-setup-ssid").textContent = setup.ssid;
  $("#wifi-setup-password").textContent = state.wifiPasswordVisible ? setup.password : "••••••••";
  $("#wifi-show-password").textContent = state.wifiPasswordVisible ? "Hide" : "Show";
  $("#wifi-show-password").setAttribute("aria-pressed", String(state.wifiPasswordVisible));
  $("#wifi-setup-reconnect").hidden = !setup.active;
  if (state.network.state === "failed" && state.network.message) {
    $("#wifi-setup-message").textContent = state.network.message;
  }
  renderNetworkControls();
}

$("#wifi-setup-qr").addEventListener("error", () => {
  setError($("#wifi-qr-error"), "QR code unavailable. Use the network name and password below.");
});
$("#wifi-setup-qr").addEventListener("load", () => setError($("#wifi-qr-error"), ""));

$("#wifi-show-password").addEventListener("click", () => {
  state.wifiPasswordVisible = !state.wifiPasswordVisible;
  renderWifiSetup();
});

async function copyWifiValue(value, label) {
  if (!value) return;
  let copied = false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      copied = true;
    }
  } catch {}
  if (!copied) {
    const previous = document.activeElement;
    const input = document.createElement("textarea");
    input.value = value;
    input.readOnly = true;
    input.style.position = "fixed";
    input.style.left = "-9999px";
    document.body.append(input);
    try {
      input.select();
      input.setSelectionRange(0, value.length);
      copied = document.execCommand("copy");
    } catch {} finally {
      input.remove();
      previous?.focus();
    }
  }
  showToast(copied ? `${label} copied.` : label === "Password" ? "Copy unavailable. Tap Show, then select the password." : "Copy unavailable. Select the network name to copy it.", !copied);
}

$("#wifi-copy-ssid").addEventListener("click", () => copyWifiValue(state.network?.setup?.ssid, "Network name"));
$("#wifi-copy-password").addEventListener("click", () => copyWifiValue(state.network?.setup?.password, "Password"));

function closeWifiSetupEditor() {
  $("#wifi-setup-form").hidden = true;
  $("#wifi-setup-password-input").value = "";
  $("#wifi-setup-edit").setAttribute("aria-expanded", "false");
  setError($("#wifi-setup-error"), "");
  $("#wifi-setup-edit").focus();
}

$("#wifi-setup-edit").addEventListener("click", () => {
  if (networkLocked() || !state.network?.setup) return;
  if (!$("#wifi-setup-form").hidden) {
    closeWifiSetupEditor();
    return;
  }
  $("#wifi-setup-name-input").value = state.network.setup.ssid;
  $("#wifi-setup-password-input").value = "";
  setError($("#wifi-setup-error"), "");
  $("#wifi-setup-form").hidden = false;
  $("#wifi-setup-edit").setAttribute("aria-expanded", "true");
  $("#wifi-setup-name-input").focus();
});
$("#wifi-setup-cancel").addEventListener("click", closeWifiSetupEditor);

$("#wifi-setup-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (networkLocked()) return;
  setError($("#wifi-setup-error"), "");
  const ssid = $("#wifi-setup-name-input").value;
  const password = $("#wifi-setup-password-input").value;
  if (!ssid || new TextEncoder().encode(ssid).length > 32 || /[\u0000-\u001f\u007f]/.test(ssid)) {
    setError($("#wifi-setup-error"), "Choose a shorter network name without control characters (32 bytes maximum).");
    $("#wifi-setup-name-input").focus();
    return;
  }
  if (password && !/^[\x20-\x7e]{8,63}$/.test(password)) {
    setError($("#wifi-setup-error"), "Use 8–63 letters, numbers, spaces or symbols for the password.");
    $("#wifi-setup-password-input").focus();
    return;
  }
  state.networkSaving = true;
  renderNetworkControls();
  try {
    const result = await api("/api/network/setup", {method: "POST", body: JSON.stringify({ssid, password})});
    state.networkGeneration += 1;
    if (result.setup) {
      state.network = {...state.network, setup: result.setup, busy: Boolean(result.setup.active)};
      renderWifiSetup();
    }
    closeWifiSetupEditor();
    $("#wifi-setup-message").textContent = result.message || "Camera Wi-Fi updated.";
    $("#wifi-setup-message").focus();
  } catch (error) {
    setError($("#wifi-setup-error"), error.message);
  } finally {
    state.networkSaving = false;
    renderNetworkControls();
  }
});

$("#wifi-scan").addEventListener("click", async () => {
  if (networkLocked()) return;
  $("#wifi-scan").disabled = true;
  try {
    const result = await api("/api/network/scan", { method: "POST" });
    if (result.error) throw new Error(result.error);
    const list = $("#wifi-networks");
    list.replaceChildren();
    for (const network of result.networks || []) {
      const option = document.createElement("option");
      option.value = network.ssid;
      option.label = `${network.signal}% · ${network.security}`;
      list.append(option);
    }
    $("#wifi-message").textContent = "Choose a network name or enter your hotspot name manually.";
    $("#wifi-ssid").focus();
  } catch (error) { $("#wifi-message").textContent = error.message; }
  finally { renderNetworkControls(); }
});

$("#wifi-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (networkLocked()) return;
  $("#wifi-connect").disabled = true;
  try {
    const result = await api("/api/network/connect", {
      method: "POST",
      body: JSON.stringify({ ssid: $("#wifi-ssid").value, password: $("#wifi-password").value }),
    });
    $("#wifi-password").value = "";
    $("#wifi-message").textContent = result.message;
  } catch (error) {
    $("#wifi-message").textContent = error.message;
    renderNetworkControls();
  }
});

setInterval(() => {
  if (state.activeView === "setup" && state.setupSection === "network") refreshNetwork();
}, 5000);

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshStatus();
  else destroyPreview();
});

async function boot() {
  try {
    const session = await api("/api/session");
    state.csrf = session.csrf;
    await loadAll();
    navigate();
  } catch (error) {
    showToast(error.message, true);
  }
}

setInterval(() => {
  refreshStatus();
  if (state.activeView === "recordings" && (state.recordings.some((item) => item.metadata_state === "pending") || state.sessions.some((session) => ["recording", "finishing"].includes(session.state)))) refreshRecordings();
}, 2000);
boot();
