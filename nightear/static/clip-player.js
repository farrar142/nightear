// 이벤트 클립 패널: 파형 표시 + 재생 + 다운로드. graph.js 의 이벤트 목록에서 showClip(ev)로 연다.

const $ = (id) => document.getElementById(id);
let current = null; // 현재 열린 이벤트 id
let peaks = null;
let audioEl = null;

// 16-bit PCM WAV에서 막대 개수만큼의 최대 진폭을 뽑는다.
function wavPeaks(buf, bars) {
  const view = new DataView(buf);
  let off = 12;
  while (off + 8 <= view.byteLength) {
    const id = String.fromCharCode(view.getUint8(off), view.getUint8(off + 1), view.getUint8(off + 2), view.getUint8(off + 3));
    const size = view.getUint32(off + 4, true);
    if (id === "data") { off += 8; break; }
    off += 8 + size;
  }
  const n = Math.floor((view.byteLength - off) / 2);
  const out = new Array(bars).fill(0);
  for (let i = 0; i < n; i++) {
    const b = Math.min(bars - 1, Math.floor((i / n) * bars));
    const v = Math.abs(view.getInt16(off + i * 2, true)) / 32768;
    if (v > out[b]) out[b] = v;
  }
  return out;
}

function drawWave(ev) {
  const canvas = $("clip-wave");
  if (!canvas || !peaks) return;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const dur = ev.clip_duration_s || 1;
  // 이벤트 구간(앞 여유 이후) 배경 띠
  const x0 = ((ev.start_ts - ev.clip_start_ts) / dur) * w;
  const x1 = ((ev.end_ts - ev.clip_start_ts) / dur) * w;
  ctx.fillStyle = "rgba(210, 74, 74, 0.18)";
  ctx.fillRect(Math.max(0, x0), 0, Math.max(2, x1 - x0), h);
  ctx.fillStyle = "#4cc38a";
  const bw = w / peaks.length;
  peaks.forEach((p, i) => {
    const ph = Math.max(1, p * h);
    ctx.fillRect(i * bw, (h - ph) / 2, Math.max(1, bw - 0.5), ph);
  });
  if (audioEl) { // 재생 위치
    ctx.fillStyle = "#e6e9ef";
    ctx.fillRect((audioEl.currentTime / dur) * w, 0, 1.5, h);
  }
}

export async function showClip(ev) {
  const panel = $("clip-panel");
  if (!panel) return;
  current = ev.id;
  peaks = null;
  const info = $("clip-info");
  audioEl = $("clip-audio");
  audioEl.pause();
  panel.hidden = false;
  if (!ev.has_clip) {
    $("clip-body").hidden = true;
    info.textContent = ev.audio_deleted ? "보관 기간이 지나 오디오가 삭제됐습니다."
      : ev.sleep_mode ? "클립을 만드는 중이거나 저장하지 못했습니다." : "수면 모드가 아니었으므로 오디오가 저장되지 않았습니다.";
    return;
  }
  $("clip-body").hidden = false;
  info.textContent = `클립 ${ev.clip_duration_s.toFixed(1)}초`;
  const url = `/api/events/${ev.id}/clip`;
  audioEl.src = url;
  $("clip-download").href = `${url}?download=true`;
  audioEl.ontimeupdate = () => { if (current === ev.id) drawWave(ev); };
  $("clip-wave").onclick = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    audioEl.currentTime = ((e.clientX - r.left) / r.width) * ev.clip_duration_s;
    audioEl.play().catch(() => {});
  };
  try {
    const buf = await (await fetch(url)).arrayBuffer();
    if (current !== ev.id) return;
    peaks = wavPeaks(buf, 300);
    drawWave(ev);
  } catch (_) {
    info.textContent += " (파형을 불러오지 못했습니다)";
  }
}
