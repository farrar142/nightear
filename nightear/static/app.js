// 브라우저 수집 소스 + 실시간 레벨 미터.
// 마이크(원음) -> AudioWorklet(16kHz mono 16-bit PCM, 100ms) -> /ws/ingest, 레벨은 /ws/realtime 로 받는다.

import { handleMessage as handleGraphMessage, initGraph } from "./graph.js";

const DEVICE_KEY = "nightear.deviceId";
const IDLE_THRESHOLD_MS = 60_000; // IdleDetector가 허용하는 최소 임계값
const ACTIVITY_REPORT_MS = 10_000; // 상태가 안 바뀌어도 주기적으로 보낸다

const $ = (id) => document.getElementById(id);
const wsUrl = (path) => `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;

function setBadge(el, text, kind) {
  el.textContent = text;
  el.className = "badge" + (kind ? " " + kind : "");
}

function sourceId() {
  let id = null;
  try { id = localStorage.getItem("nightear.sourceId"); } catch (_) {}
  if (!id) {
    id = "browser-" + Math.random().toString(36).slice(2, 10);
    try { localStorage.setItem("nightear.sourceId", id); } catch (_) {}
  }
  return id;
}

// ---- 입력 장치 선택 (선택은 localStorage에 기억) ----
function savedDeviceId() {
  try { return localStorage.getItem(DEVICE_KEY) || ""; } catch (_) { return ""; }
}

async function refreshDevices() {
  const select = $("device");
  const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
  const saved = savedDeviceId();
  select.innerHTML = "";
  for (const d of devices) {
    const opt = document.createElement("option");
    opt.value = d.deviceId;
    opt.textContent = d.label || `입력 장치 ${select.length + 1}`;
    select.appendChild(opt);
  }
  if (saved && devices.some((d) => d.deviceId === saved)) select.value = saved;
}

// ---- 유휴 감지 (IdleDetector, Chromium 전용·권한 필요) ----
// 권한 요청은 사용자 제스처(수집 시작 클릭) 안에서만 가능하다. 미지원·거부면 granted=false로 hello에 알린다.
const idle = {
  granted: false,
  detector: null,
  state: "active",
  since: Date.now(), // 현재 상태가 시작된 시각(ms)
  timer: null,
};

function idleSeconds() {
  // active면 0, idle/locked면 마지막 입력 이후 경과(상태 전환 시점 - 임계값부터)
  if (idle.state === "active") return 0;
  return Math.max(0, (Date.now() - idle.since) / 1000 + IDLE_THRESHOLD_MS / 1000);
}

function sendActivity() {
  if (!idle.granted || !ingestWs || ingestWs.readyState !== WebSocket.OPEN) return;
  ingestWs.send(JSON.stringify({ type: "activity", state: idle.state, idle_seconds: idleSeconds() }));
}

async function startIdleDetector(requestPermission) {
  if (idle.detector) return; // 이미 동작 중
  if (!("IdleDetector" in window)) { idle.granted = false; return; }
  try {
    if (requestPermission) {
      idle.granted = (await IdleDetector.requestPermission()) === "granted";
    } else if (navigator.permissions) {
      const p = await navigator.permissions.query({ name: "idle-detection" });
      idle.granted = p.state === "granted";
    }
    if (!idle.granted) return;
    const detector = new IdleDetector();
    detector.addEventListener("change", () => {
      const next = detector.screenState === "locked" ? "locked" : detector.userState === "idle" ? "idle" : "active";
      if (next !== idle.state) { idle.state = next; idle.since = Date.now(); }
      sendActivity();
    });
    await detector.start({ threshold: IDLE_THRESHOLD_MS });
    idle.detector = detector;
    idle.timer = setInterval(sendActivity, ACTIVITY_REPORT_MS);
  } catch (_) {
    idle.granted = false; // 권한 거부·시작 실패: 밤 시간대만으로 판단하도록 미지원으로 알린다
  }
}

// ---- 캡처 ----
let stream = null;
let audioCtx = null;
let ingestWs = null;
let ingestWanted = false; // 사용자가 수집을 켜 둔 상태인가(끊기면 자동 재연결)
let ingestTimer = null;

// 지수 백오프 재연결: 1s,2s,4s... 상한 30s, 연결 성공(hello_ack) 시 초기화.
const BACKOFF_MIN_MS = 1000;
const BACKOFF_MAX_MS = 30000;
const nextBackoff = (ms) => Math.min(ms * 2, BACKOFF_MAX_MS);

function showRole(role) {
  const el = $("role-status");
  if (role === "active") setBadge(el, "활성 소스", "ok");
  else if (role === "secondary") setBadge(el, "보조(표시 전용)", "");
  else setBadge(el, "-", "");
}

function connectIngest(delay = BACKOFF_MIN_MS) {
  ingestTimer = null;
  if (!ingestWanted) return;
  const ws = new WebSocket(wsUrl("/ws/ingest"));
  ingestWs = ws;
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    ws.send(JSON.stringify({
      type: "hello",
      source_id: sourceId(),
      sample_rate: 16000,
      channels: 1,
      idle_detection_supported: idle.granted,
    }));
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "hello_ack") { delay = BACKOFF_MIN_MS; setBadge($("ingest-status"), "연결됨", "ok"); sendActivity(); }
    if (msg.type === "role") showRole(msg.role);
    if (msg.type === "error") setBadge($("ingest-status"), "거부됨: " + msg.message, "bad");
  };
  ws.onclose = () => {
    if (ingestWs === ws) ingestWs = null;
    showRole(null);
    if (!ingestWanted) return;
    setBadge($("ingest-status"), "재연결 중...", "bad");
    if (!ingestTimer) ingestTimer = setTimeout(() => connectIngest(nextBackoff(delay)), delay);
  };
  ws.onerror = () => {}; // onclose가 뒤따라 재연결을 처리한다
}

function disconnectIngest() {
  ingestWanted = false;
  if (ingestTimer) { clearTimeout(ingestTimer); ingestTimer = null; }
  if (ingestWs) { ingestWs.onclose = null; ingestWs.close(); ingestWs = null; }
  showRole(null);
}

async function stopCapture() {
  disconnectIngest();
  if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  if (audioCtx) { await audioCtx.close(); audioCtx = null; }
}

async function startCapture(fromGesture = false) {
  await stopCapture();
  await startIdleDetector(fromGesture);
  const deviceId = $("device").value || savedDeviceId();
  const constraints = (id) => ({
    audio: {
      deviceId: id ? { exact: id } : undefined,
      // 브라우저 후처리를 모두 꺼서 원음을 받는다 (소음이 지워지지 않도록)
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
      channelCount: 1,
    },
  });
  try {
    try {
      stream = await navigator.mediaDevices.getUserMedia(constraints(deviceId));
    } catch (err) {
      // 저장해 둔 장치가 사라졌으면 기본 장치로 한 번만 재시도한다.
      if (deviceId && err && (err.name === "OverconstrainedError" || err.name === "NotFoundError")) {
        try { localStorage.removeItem(DEVICE_KEY); } catch (_) {}
        stream = await navigator.mediaDevices.getUserMedia(constraints(""));
      } else {
        throw err;
      }
    }
  } catch (err) {
    if (err && (err.name === "NotAllowedError" || err.name === "SecurityError")) {
      setBadge($("ingest-status"), "마이크 권한 없음", "bad");
    } else {
      setBadge($("ingest-status"), "마이크 오류: " + (err && err.name), "bad");
    }
    return;
  }
  // 장치가 빠지거나 권한이 회수되어 트랙이 끝나면 끊김으로 표시한다.
  stream.getAudioTracks().forEach((t) => t.addEventListener("ended", () => setBadge($("ingest-status"), "끊김", "bad")));

  // 권한을 받은 뒤에야 장치 라벨이 보이므로 목록을 다시 채운다.
  const activeId = stream.getAudioTracks()[0].getSettings().deviceId;
  await refreshDevices();
  if (activeId) $("device").value = activeId;

  audioCtx = new AudioContext();
  await audioCtx.audioWorklet.addModule("capture-worklet.js");
  const node = new AudioWorkletNode(audioCtx, "capture-processor", { channelCount: 1, channelCountMode: "explicit" });
  audioCtx.createMediaStreamSource(stream).connect(node);

  ingestWanted = true;
  connectIngest();
  node.port.onmessage = (ev) => {
    if (ingestWs && ingestWs.readyState === WebSocket.OPEN) ingestWs.send(ev.data);
  };
}

// ---- 실시간 채널: 레벨 미터 ----
function showLevel(db) {
  const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
  $("meter-bar").style.width = pct + "%";
  $("meter-db").textContent = db.toFixed(1) + " dBFS";
}

// ---- 수면 모드 표시 ----
function fmtDuration(sec) {
  const m = Math.floor(sec / 60);
  return m >= 60 ? `${Math.floor(m / 60)}시간 ${m % 60}분` : `${m}분`;
}

function showSleep(s) {
  const r = s.reasons;
  setBadge($("sleep-status"), s.sleeping ? "수면 모드" : "깨어 있음", s.sleeping ? "ok" : "");
  const night = `밤 시간대(${r.night_start_hour}시~${r.night_end_hour}시 ${r.timezone}): ${r.in_night_window ? "안" : "밖"}`;
  let idleText;
  if (r.idle_detection_supported === null) idleText = "소스 없음";
  else if (!r.idle_detection_supported) idleText = "유휴 감지 미지원 - 밤 시간대만으로 판단";
  else if (r.idle_seconds === null || r.idle_state === "active") idleText = "사용 중(active)";
  else idleText = `${r.idle_state} ${fmtDuration(r.idle_seconds)} / 기준 ${fmtDuration(r.idle_threshold_seconds)}`;
  $("sleep-reasons").textContent = `${night} · ${idleText}`;
}

async function loadSleep() {
  try { showSleep(await (await fetch("/api/sleep")).json()); } catch (_) {}
}

let realtimeDelay = BACKOFF_MIN_MS;
function connectRealtime() {
  const ws = new WebSocket(wsUrl("/ws/realtime"));
  ws.onopen = () => { realtimeDelay = BACKOFF_MIN_MS; setBadge($("realtime-status"), "연결됨", "ok"); loadSleep(); };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "level") showLevel(msg.db);
    if (msg.type === "state") {
      const el = $("active-source");
      if (el) el.textContent = msg.active_source_id || "없음";
    }
    if (msg.type === "sleep") showSleep(msg);
    handleGraphMessage(msg);
  };
  ws.onclose = () => {
    setBadge($("realtime-status"), "끊김", "bad");
    setTimeout(connectRealtime, realtimeDelay); // 수집 채널과 같은 지수 백오프
    realtimeDelay = nextBackoff(realtimeDelay);
  };
}

$("device").addEventListener("change", () => {
  try { localStorage.setItem(DEVICE_KEY, $("device").value); } catch (_) {}
  if (stream) startCapture(false);
});
$("start").addEventListener("click", () => startCapture(true));

initGraph();
connectRealtime();
startIdleDetector(false); // 이미 권한이 허용돼 있으면 사용자 제스처 없이도 켠다
refreshDevices().catch(() => {});

// 밤 타임라인 화면(별도 모듈)
import { initTimeline } from "./timeline.js";
initTimeline();
