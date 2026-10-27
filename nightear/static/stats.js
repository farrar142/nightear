// 패턴 통계 화면: 요일×시간대 히트맵 + 라벨별 건수. /api/stats 를 기간별로 불러온다.

const DAYS = ["월", "화", "수", "목", "금", "토", "일"];
const $ = (id) => document.getElementById(id);

function heat(canvas, grid) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const padL = 24, padT = 14;
  const cw = (w - padL) / 24, ch = (h - padT) / 7;
  const max = Math.max(1, ...grid.flat());
  ctx.font = "10px system-ui, sans-serif";
  ctx.fillStyle = "#7f8aa3";
  for (let hr = 0; hr < 24; hr += 3) ctx.fillText(String(hr), padL + hr * cw + 2, 10);
  DAYS.forEach((d, i) => ctx.fillText(d, 6, padT + i * ch + ch / 2 + 3));
  grid.forEach((row, d) => row.forEach((n, hr) => {
    ctx.fillStyle = n ? `rgba(224, 169, 59, ${0.2 + 0.8 * (n / max)})` : "#262c3d";
    ctx.fillRect(padL + hr * cw + 1, padT + d * ch + 1, cw - 2, ch - 2);
    if (n) { ctx.fillStyle = "#12151c"; ctx.fillText(String(n), padL + hr * cw + cw / 2 - 3, padT + d * ch + ch / 2 + 3); }
  }));
}

function renderLabels(list, unlabeled) {
  const el = $("stats-labels");
  el.innerHTML = "";
  const rows = unlabeled ? [...list, { label: "(라벨 없음)", count: unlabeled }] : list;
  if (!rows.length) { el.innerHTML = '<li class="empty">데이터 없음</li>'; return; }
  const max = Math.max(...rows.map((r) => r.count));
  for (const r of rows) {
    const li = document.createElement("li");
    li.innerHTML = '<span class="stat-name"></span><span class="stat-bar"><i></i></span><span class="stat-n"></span>';
    li.querySelector(".stat-name").textContent = r.label;
    li.querySelector("i").style.width = (r.count / max) * 100 + "%";
    li.querySelector(".stat-n").textContent = r.count;
    el.appendChild(li);
  }
}

export async function loadStats() {
  const days = Number($("stats-period").value);
  const params = days ? `?since=${Date.now() / 1000 - days * 86400}` : "";
  try {
    const s = await (await fetch("/api/stats" + params)).json();
    $("stats-total").textContent = `이벤트 ${s.total}건 (${s.timezone})`;
    heat($("stats-heat"), s.heatmap);
    renderLabels(s.by_label, s.unlabeled);
  } catch (_) { $("stats-total").textContent = "통계를 불러오지 못했습니다."; }
}

$("stats-period").addEventListener("change", loadStats);
$("stats-refresh").addEventListener("click", loadStats);
window.addEventListener("resize", loadStats);
loadStats();
