// 실시간 그래프(레벨·기준선·트리거선·이벤트 마커)와 이벤트 목록.
// app.js 가 실시간 메시지를 handleMessage()로 넘기면 여기서 그린다.

const WINDOW_S = 300; // 그래프에 보이는 최근 구간(5분)
const DB_MIN = -90;
const DB_MAX = 0;

import { showClip } from "./clip-player.js";

let labelFilter = ""; // 선택한 라벨(빈 문자열이면 전체)
let selectedId = null; // 클립 패널에 열려 있는 이벤트
const levels = []; // {ts, db}
const lines = []; // {ts, baseline, trigger}
const events = new Map(); // id -> event
let floorDb = null;
let sortMode = "time";
let dayRange = null; // {since, until} 선택한 날짜, null 이면 최근
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
    if (ev.labels && ev.labels.length) { // 라벨이 붙은 마커에는 1순위 라벨을 쓴다
      ctx.fillStyle = "#e6e9ef";
      ctx.fillText(ev.labels[0].label, x1 + 8, 10);
    }
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

// 라벨 후보 문자열: "Door 82%, Footsteps 64%" / 진행 중·실패 표시
function labelText(ev) {
  if (ev.label_status === "pending") return "  라벨링 중...";
  if (ev.label_status === "failed") return "  라벨 실패";
  if (!ev.labels || !ev.labels.length) return "";
  return "  [" + ev.labels.map((l) => `${l.label} ${Math.round(l.score * 100)}%`).join(", ") + "]";
}

function matchesFilter(ev) {
  return !labelFilter || (ev.labels || []).some((l) => l.label === labelFilter);
}

async function loadLabelOptions() {
  const sel = $("label-filter");
  if (!sel) return;
  try {
    const list = await (await fetch("/api/labels")).json();
    sel.innerHTML = '<option value="">전체 라벨</option>';
    for (const l of list) {
      const opt = document.createElement("option");
      opt.value = l.label;
      opt.textContent = `${l.label} (${l.count})`;
      sel.appendChild(opt);
    }
    sel.value = labelFilter;
  } catch (_) {}
}

function sortedEvents() {
  const list = [...events.values()].filter(matchesFilter).filter(inDay);
  if (sortMode === "size") list.sort((a, b) => b.peak_db - a.peak_db || b.id - a.id);
  else list.sort((a, b) => b.start_ts - a.start_ts || b.id - a.id);
  return list;
}

// 날짜를 고르면 그날(로컬 자정~자정) 이벤트만 본다. 비어 있으면 최근 이벤트.
function inDay(ev) {
  return !dayRange || (ev.start_ts >= dayRange.since && ev.start_ts < dayRange.until);
}

function dayRangeOf(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  if (!m) return null;
  const y = +m[1], mo = +m[2] - 1, d = +m[3];
  return { since: new Date(y, mo, d).getTime() / 1000, until: new Date(y, mo, d + 1).getTime() / 1000 };
}

// 시간순 목록은 시간대(1시간)별로 접어 둔다. 기본은 가장 최근 시간대만 펼친다.
const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];
const openedGroups = new Set();
const closedGroups = new Set();

function groupKey(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}-${d.getHours()}`;
}

function groupTitle(ts) {
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}/${d.getDate()}(${WEEKDAYS[d.getDay()]}) ${String(d.getHours()).padStart(2, "0")}시`;
}

function groupSummary(list) {
  const peak = Math.max(...list.map((e) => e.peak_db));
  const clips = list.filter((e) => e.has_clip).length;
  const stars = list.filter((e) => e.starred).length;
  return `${list.length}건 · 최대 ${peak.toFixed(1)} dB` + (clips ? ` · 클립 ${clips}` : "") + (stars ? ` · ★${stars}` : "");
}

function renderList() {
  if (!listEl) return;
  const scroller = listEl.parentElement;
  const scrollTop = scroller ? scroller.scrollTop : 0;
  listEl.innerHTML = "";
  const list = sortedEvents();
  if (!list.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "이벤트 없음";
    listEl.appendChild(li);
    return;
  }
  if (sortMode === "size") {
    for (const ev of list) listEl.appendChild(eventItem(ev));
  } else {
    const groups = new Map(); // 최신 시간대부터(list 가 이미 최신순)
    for (const ev of list) {
      const k = groupKey(ev.start_ts);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(ev);
    }
    let first = true;
    for (const [k, evs] of groups) {
      const holdsSelected = evs.some((e) => e.id === selectedId);
      const open = openedGroups.has(k) || holdsSelected || (first && !closedGroups.has(k));
      first = false;
      const li = document.createElement("li");
      li.className = "event-group";
      const det = document.createElement("details");
      det.open = open;
      const sum = document.createElement("summary");
      sum.innerHTML = `<b></b> <small></small>`;
      sum.querySelector("b").textContent = groupTitle(evs[0].start_ts);
      sum.querySelector("small").textContent = groupSummary(evs);
      det.appendChild(sum);
      const ul = document.createElement("ul");
      ul.className = "events";
      for (const ev of evs) ul.appendChild(eventItem(ev));
      det.appendChild(ul);
      det.addEventListener("toggle", () => {
        if (det.open) { openedGroups.add(k); closedGroups.delete(k); }
        else { closedGroups.add(k); openedGroups.delete(k); }
      });
      li.appendChild(det);
      listEl.appendChild(li);
    }
  }
  if (scroller) scroller.scrollTop = scrollTop;
}

function eventItem(ev) {
  const li = document.createElement("li");
  const dur = ev.duration_s.toFixed(1);
  li.textContent = `${fmtTime(ev.start_ts)}  ${dur}초  최대 ${ev.peak_db.toFixed(1)} dB  (기준선 ${ev.baseline_db.toFixed(1)})`
    + (ev.ongoing ? "  진행 중" : "") + (ev.truncated ? "  잘림" : "")
    + (ev.has_clip ? "  ▶ 클립" : ev.audio_deleted ? "  오디오 삭제됨" : "")
    + labelText(ev);
  li.className = "clickable" + (ev.id === selectedId ? " selected" : "");
  li.addEventListener("click", () => { selectedId = ev.id; showClip(ev); renderList(); });
  if (ev.has_clip || ev.starred) { // 별표: 켜 두면 보관 기간이 지나도 클립이 남는다
    const star = document.createElement("button");
    star.type = "button";
    star.className = "star-btn";
    star.textContent = ev.starred ? "★" : "☆";
    star.title = ev.starred ? "별표 끄기 (보관 기간 후 삭제됨)" : "별표 켜기 (오래 보관)";
    star.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        const r = await fetch(`/api/events/${ev.id}/star`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ starred: !ev.starred }),
        });
        if (r.ok) upsertEvent(await r.json());
      } catch (_) { /* 네트워크 오류면 상태를 바꾸지 않는다 */ }
    });
    li.prepend(star);
  }
  return li;
}

function upsertEvent(ev) {
  const prev = events.get(ev.id);
  events.set(ev.id, ev);
  // 열어 둔 이벤트의 클립이 방금 저장됐으면 패널을 갱신한다(라벨 갱신 등으로 재생이 끊기지 않게 처음 한 번만)
  if (ev.id === selectedId && ev.has_clip && !(prev && prev.has_clip)) showClip(ev);
  if (ev.label_status === "done") loadLabelOptions();
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
  } else if (msg.type === "event" || msg.type === "clip" || msg.type === "label") {
    upsertEvent(msg.event);
  }
}

export async function loadEvents() {
  try {
    let q = labelFilter ? "&label=" + encodeURIComponent(labelFilter) : "";
    // 날짜를 고르면 그날 이벤트를 전부, 아니면 최근 200건
    q += dayRange ? `&limit=10000&since=${dayRange.since}&until=${dayRange.until}` : "&limit=200";
    const r = await fetch("/api/events?" + q.slice(1));
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
  const lf = $("label-filter");
  if (lf) lf.addEventListener("change", () => { labelFilter = lf.value; renderList(); loadEvents(); });
  const day = $("event-day");
  if (day) day.addEventListener("change", () => {
    dayRange = dayRangeOf(day.value);
    openedGroups.clear(); closedGroups.clear();
    if (listEl.parentElement) listEl.parentElement.scrollTop = 0;
    renderList(); loadEvents();
  });
  const recent = $("event-recent");
  if (recent && day) recent.addEventListener("click", () => { day.value = ""; day.dispatchEvent(new Event("change")); });
  loadLabelOptions();
  window.addEventListener("resize", queueDraw);
  renderList();
  draw();
  loadEvents();
}
