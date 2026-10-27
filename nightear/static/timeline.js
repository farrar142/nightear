// 밤 타임라인 화면: 밤 목록 -> 하룻밤 선택 -> 전체 dB 타임라인(수면 구간·수집 공백·이벤트 마커).
// 독립 모듈: index.html 의 #timeline 섹션만 건드린다. 주소 #night=YYYY-MM-DD 로 특정 밤을 바로 연다.

const DB_MIN = -90;
const DB_MAX = 0;

const $ = (id) => document.getElementById(id);
let night = null; // 현재 보고 있는 밤 상세
let hit = []; // 마커 히트 영역 {x, y, r, id}
let selectedId = null;

const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString("ko-KR", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
const fmtDur = (s) => {
  const m = Math.round(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)}시간 ${m % 60}분` : `${m}분`;
};

export function summaryText(n) {
  const s = n.summary;
  return `${n.id}  이벤트 ${s.event_count}건 · 수면 ${fmtDur(s.sleep_seconds)} · 공백 ${s.gap_count}회(${fmtDur(s.gap_seconds)})`;
}

async function loadNightList() {
  const ul = $("tl-night-list");
  if (!ul) return;
  try {
    const r = await fetch("/api/nights");
    if (!r.ok) return;
    const { nights } = await r.json();
    ul.innerHTML = "";
    if (!nights.length) {
      ul.innerHTML = '<li class="empty">기록된 밤 없음</li>';
      return;
    }
    for (const n of nights) {
      const li = document.createElement("li");
      li.textContent = summaryText(n);
      li.className = "clickable";
      li.dataset.night = n.id;
      li.addEventListener("click", () => openNight(n.id));
      ul.appendChild(li);
    }
  } catch (_) { /* 서버 미기동 */ }
}

export async function openNight(id) {
  const r = await fetch("/api/nights/" + encodeURIComponent(id));
  if (!r.ok) return;
  night = await r.json();
  selectedId = null;
  try { history.replaceState(null, "", "#night=" + id); } catch (_) {}
  $("tl-detail").hidden = false;
  $("tl-title").textContent = summaryText(night);
  renderEvents();
  draw();
}

function renderEvents() {
  const ul = $("tl-events");
  ul.innerHTML = "";
  if (!night.events.length) {
    ul.innerHTML = '<li class="empty">이벤트 없음</li>';
    return;
  }
  for (const ev of night.events) {
    const li = document.createElement("li");
    li.id = "tl-event-" + ev.id;
    li.className = "clickable";
    li.textContent = `${fmtTime(ev.start_ts)}  ${ev.duration_s.toFixed(1)}초  최대 ${ev.peak_db.toFixed(1)} dB`
      + (ev.sleep_mode ? "  (수면 중)" : "");
    li.addEventListener("click", () => focusEvent(ev.id));
    ul.appendChild(li);
  }
}

// 마커(또는 목록 항목)를 누르면 해당 이벤트 항목을 강조하고 스크롤한다.
export function focusEvent(id) {
  const li = $("tl-event-" + id);
  if (!li) return;
  selectedId = id;
  document.querySelectorAll("#tl-events li.selected").forEach((e) => e.classList.remove("selected"));
  li.classList.add("selected");
  li.scrollIntoView({ block: "center", behavior: "smooth" });
  draw();
}

function draw() {
  const canvas = $("tl-canvas");
  if (!canvas || !night) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const padL = 38, padB = 18, padT = 12;
  const pw = w - padL, ph = h - padB - padT;
  const t0 = night.start_ts, span = night.end_ts - night.start_ts;
  const x = (ts) => padL + ((ts - t0) / span) * pw;
  const y = (db) => padT + ph - ((Math.max(DB_MIN, Math.min(DB_MAX, db)) - DB_MIN) / (DB_MAX - DB_MIN)) * ph;

  // 수면 모드 구간(파란 띠), 수집 공백(회색 띠)
  ctx.fillStyle = "rgba(106,165,255,0.16)";
  for (const s of night.sleep_intervals) ctx.fillRect(x(s.start_ts), padT, Math.max(1, x(s.end_ts) - x(s.start_ts)), ph);
  ctx.fillStyle = "rgba(160,160,160,0.35)";
  for (const g of night.gaps) ctx.fillRect(x(g.start_ts), padT, Math.max(1, x(g.end_ts) - x(g.start_ts)), ph);

  // 눈금: dB 가로선, 2시간 간격 시각
  ctx.font = "10px system-ui, sans-serif";
  ctx.fillStyle = "#7f8aa3";
  ctx.strokeStyle = "#2a3043";
  ctx.lineWidth = 1;
  for (let db = DB_MIN; db <= DB_MAX; db += 30) {
    ctx.beginPath(); ctx.moveTo(padL, y(db)); ctx.lineTo(w, y(db)); ctx.stroke();
    ctx.fillText(db + "", 4, y(db) + 3);
  }
  for (let t = Math.ceil(t0 / 3600) * 3600; t < night.end_ts; t += 7200) {
    ctx.fillText(new Date(t * 1000).toLocaleTimeString("ko-KR", { hour12: false, hour: "2-digit", minute: "2-digit" }), x(t) - 12, h - 4);
  }

  // 레벨: 초당 max(연한 녹색)와 mean(진한 녹색). 데이터가 5초 넘게 끊기면 선을 끊는다.
  const rows = night.levels.rows;
  const line = (col, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.beginPath();
    let prev = null;
    for (const r of rows) {
      const px = x(r[0]), py = y(r[col]);
      if (prev === null || r[0] - prev > 5) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      prev = r[0];
    }
    ctx.stroke();
  };
  line(1, "#4cc38a");
  line(2, "#2a7a55");

  // 이벤트 마커
  hit = [];
  for (const ev of night.events) {
    const mx = x(ev.start_ts), my = y(ev.peak_db);
    const sel = ev.id === selectedId;
    ctx.fillStyle = sel ? "#ffd166" : "#d24a4a";
    ctx.beginPath(); ctx.arc(mx, my, sel ? 6 : 4, 0, Math.PI * 2); ctx.fill();
    hit.push({ x: mx, y: my, r: 8, id: ev.id });
  }
}

function onCanvasClick(e) {
  const rect = e.currentTarget.getBoundingClientRect();
  const px = e.clientX - rect.left, py = e.clientY - rect.top;
  let best = null, bd = Infinity;
  for (const m of hit) {
    const d = Math.hypot(m.x - px, m.y - py);
    if (d <= m.r && d < bd) { best = m; bd = d; }
  }
  if (best) focusEvent(best.id);
}

export function initTimeline() {
  const canvas = $("tl-canvas");
  if (!canvas) return;
  canvas.addEventListener("click", onCanvasClick);
  window.addEventListener("resize", draw);
  const refresh = $("tl-refresh");
  if (refresh) refresh.addEventListener("click", loadNightList);
  loadNightList();
  const m = /#night=(\d{4}-\d{2}-\d{2})/.exec(location.hash);
  if (m) openNight(m[1]);
}
