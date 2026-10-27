// 브라우저 수집 소스 + 실시간 레벨 미터.
// 마이크(원음) -> AudioWorklet(16kHz mono 16-bit PCM, 100ms) -> /ws/ingest, 레벨은 /ws/realtime 로 받는다.

import { handleMessage as handleGraphMessage, initGraph } from "./graph.js";

const DEVICE_KEY ="nightear.deviceId";
// IdleDetector 연동은 수면 모드 티켓에서 붙인다. 그 전까지는 미지원으로 알린다.
const IDLE_DETECTION_SUPPORTED = false;

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

// ---- 캡처 ----
let stream = null;
let audioCtx = null;
let ingestWs = null;

async function stopCapture() {
  if (ingestWs) { ingestWs.onclose = null; ingestWs.close(); ingestWs = null; }
  if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  if (audioCtx) { await audioCtx.close(); audioCtx = null; }
}

async function startCapture() {
  await stopCapture();
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

  ingestWs = new WebSocket(wsUrl("/ws/ingest"));
  ingestWs.binaryType = "arraybuffer";
  ingestWs.onopen = () => {
    ingestWs.send(JSON.stringify({
      type: "hello",
      source_id: sourceId(),
      sample_rate: 16000,
      channels: 1,
      idle_detection_supported: IDLE_DETECTION_SUPPORTED,
    }));
  };
  ingestWs.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "hello_ack") setBadge($("ingest-status"), "연결됨", "ok");
    if (msg.type === "error") setBadge($("ingest-status"), "거부됨: " + msg.message, "bad");
  };
  ingestWs.onclose = () => setBadge($("ingest-status"), "끊김", "bad");
  ingestWs.onerror = () => setBadge($("ingest-status"), "끊김", "bad");
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

function connectRealtime() {
  const ws = new WebSocket(wsUrl("/ws/realtime"));
  ws.onopen = () => setBadge($("realtime-status"), "연결됨", "ok");
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "level") showLevel(msg.db);
    handleGraphMessage(msg);
  };
  ws.onclose = () => {
    setBadge($("realtime-status"), "끊김", "bad");
    setTimeout(connectRealtime, 2000); // 화면용 채널이므로 단순 재시도
  };
}

$("device").addEventListener("change", () => {
  try { localStorage.setItem(DEVICE_KEY, $("device").value); } catch (_) {}
  if (stream) startCapture();
});
$("start").addEventListener("click", startCapture);

initGraph();
connectRealtime();
refreshDevices().catch(() => {});
