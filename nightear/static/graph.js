// 실시간 그래프(레벨·기준선·트리거선·이벤트 마커)와 이벤트 목록.
// app.js 가 실시간 메시지를 handleMessage()로 넘기면 여기서 그린다.

const WINDOW_S = 300; // 그래프에 보이는 최근 구간(5분)
const DB_MIN = -90;
const DB_MAX = 0;

const levels = []; // {ts, db}
const lines = []; // {ts, baseline, trigger}
const events = new Map(); // id -> event
let floorDb = null;
let sortMode = "time";
let canvas = null;
let listEl = null;
let drawQueued = false;

const $ = (id) => document.getElementById(id);

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("ko-KR", { hour12: false });
}

function trimOld(arr, now) {
  const cutoff = now - WINDOW_S;
  let n = 0;
  while (n < arr.length && arr[n].ts < cutoff) n++;
  if (n > 0) arr.splice(0, n);
}

function queueDraw() {
  if (drawQueued) return;
  drawQueued = true;
  requestAnimationFrame(() => { drawQueued = false; draw(); });
}

function draw() {
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const padL = 38, padB = 16;
  const pw = w - padL, ph = h - padB;
  const now = levels.length ? levels[levels.length - 1].ts : Date.now() / 1000;
  const t0 = now - WINDOW_S;
  const x = (ts) => padL + ((ts - t0) / WINDOW_S) * pw;
  const y = (db) => ph - ((Math.max(DB_MIN, Math.min(DB_MAX, db)) - DB_MIN) / (DB_MAX - DB_MIN)) * ph;

  // 눈금
  ctx.font = "10px system-ui, sans-serif";
  ctx.fillStyle = "#7f8aa3";
  ctx.strokeStyle = "#2a3043";
  ctx.lineWidth = 1;
  for (let db = DB_MIN; db <= DB_MAX; db += 30) {
    ctx.beginPath(); ctx.moveTo(padL, y(db)); ctx.lineTo(w, y(db)); ctx.stroke();
    ctx.fillText(db + "", 4, y(db) + 3);
  }

  // 이벤트 구간(배경 띠)과 마커
  for (const ev of events.values()) {
    const end = ev.ongoing ? now : ev.end_ts;
    if (end < t0) continue;
    const x1 = Math.max(padL, x(ev.start_ts)), x2 = Math.max(x1 + 2, x(end));
    ctx.fillStyle = "rgba(210, 74, 74, 0.18)";
    ctx.fillRect(x1, 0, x2 - x1, ph);
    ctx.fillStyle = "#d24a4a";
    ctx.beginPath();
    ctx.moveTo(x1, 0); ctx.lineTo(x1 + 6, 0); ctx.lineTo(x1, 8);
    ctx.fill();
  }

  const series = (arr, key, color, dash) => {
    if (!arr.length) return;
    ctx.strokeStyle = color;
    ctx.setLineDash(dash || []);
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    arr.forEach((p, i) => (i ? ctx.lineTo(x(p.ts), y(p[key])) : ctx.moveTo(x(p.ts), y(p[key]))));
    ctx.stroke();
    ctx.setLineDash([]);
  };
  series(levels, "db", "#4cc38a");
  series(lines, "baseline", "#6aa5ff");
  series(lines, "trigger", "#e0a93b", [5, 4]);
  if (floorDb !== null) {
    ctx.strokeStyle = "#5a6178";
    ctx.setLineDash([2, 4]);
    ctx.beginPath(); ctx.moveTo(padL, y(floorDb)); ctx.lineTo(w, y(floorDb)); ctx.stroke();
    ctx.setLineDash([]);
  }
}

function sortedEvents() {
  const list = [...events.values()];
  if (sortMode === "size") list.sort((a, b) => b.peak_db - a.peak_db || b.id - a.id);
  else list.sort((a, b) => b.start_ts - a.start_ts || b.id - a.id);
  return list;
}

function renderList() {
  if (!listEl) return;
  listEl.innerHTML = "";
  const list = sortedEvents();
  if (!list.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "이벤트 없음";
    listEl.appendChild(li);
    return;
  }
  for (const ev of list) {
    const li = document.createElement("li");
    const dur = ev.duration_s.toFixed(1);
    li.textContent = `${fmtTime(ev.start_ts)}  ${dur}초  최대 ${ev.peak_db.toFixed(1)} dB  (기준선 ${ev.baseline_db.toFixed(1)})`
      + (ev.ongoing ? "  진행 중" : "") + (ev.truncated ? "  잘림" : "");
    listEl.appendChild(li);
  }
}

function upsertEvent(ev) {
  events.set(ev.id, ev);
  renderList();
  queueDraw();
}

export function handleMessage(msg) {
  if (msg.type === "level") {
    levels.push({ ts: msg.ts, db: msg.db });
    trimOld(levels, msg.ts);
    queueDraw();
  } else if (msg.type === "baseline") {
    lines.push({ ts: msg.ts, baseline: msg.baseline_db, trigger: msg.trigger_db });
    floorDb = msg.floor_db;
    trimOld(lines, msg.ts);
    const el = $("baseline-text");
    if (el) el.textContent = `기준선 ${msg.baseline_db.toFixed(1)} dB / 트리거 ${msg.trigger_db.toFixed(1)} dB / 하한 ${msg.floor_db.toFixed(1)} dB`;
  } else if (msg.type === "event") {
    upsertEvent(msg.event);
  }
}

export async function loadEvents() {
  try {
    const r = await fetch("/api/events?limit=200");
    if (!r.ok) return;
    for (const ev of await r.json()) events.set(ev.id, ev);
    renderList();
    queueDraw();
  } catch (_) { /* 서버가 아직 안 떴으면 실시간 메시지로 채워진다 */ }
}

export function initGraph() {
  canvas = $("graph");
  listEl = $("event-list");
  const sel = $("event-sort");
  if (sel) sel.addEventListener("change", () => { sortMode = sel.value; renderList(); });
  window.addEventListener("resize", queueDraw);
  renderList();
  draw();
  loadEvents();
}
