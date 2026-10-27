"""아침 Discord 요약(#10).

- 알림기 포트: `Notifier.send(text)` (동기/비동기 모두 허용). 기본은 Discord 웹훅, 테스트는 가짜를 주입한다.
- 발송 조건(밤마다 한 번): 밤 창이 끝난 뒤, 또는 마지막 수면 모드 해제 후 유예 시간(summary_grace_minutes)
  동안 재진입이 없을 때 — 먼저 오는 쪽. 한 밤에 수면 on/off 가 여러 번이어도 요약은 한 통이다.
- 보낸 밤은 DB(summarized_nights)에 남겨 재시작해도 다시 보내지 않는다.
- 본문은 텍스트뿐이다(오디오 첨부 없음).
- 판단 로직은 `await app.state.summary.check()` 로 직접 호출할 수 있다(시계는 주입된 Clock).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .nights import night_window

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 60
LOOKBACK_DAYS = 2  # 서버가 꺼져 있던 오래된 밤까지 한꺼번에 보내지 않도록 최근 밤만 본다


class Notifier(Protocol):
    def send(self, text: str) -> Any:
        """요약 텍스트를 보낸다. 실패하면 예외를 던진다(다음 점검에서 재시도)."""
        ...


class DiscordWebhookNotifier:
    """설정의 discord_webhook_url 로 {"content": ...} 를 POST 한다."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    async def send(self, text: str) -> None:
        url = self._settings.get("discord_webhook_url")
        if not url:
            return
        await asyncio.to_thread(self._post, url, text)

    @staticmethod
    def _post(url: str, text: str) -> None:
        body = json.dumps({"content": text[:2000]}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json", "User-Agent": "nightear"})
        with urllib.request.urlopen(req, timeout=10):
            pass


def _fmt_duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes}분"
    h, m = divmod(minutes, 60)
    return f"{h}시간 {m}분" if m else f"{h}시간"


def build_message(night: dict[str, Any], cfg: dict[str, Any]) -> str:
    """밤 상세(NightService.detail) -> 요약 텍스트."""
    tz = ZoneInfo(cfg["timezone"])
    events = night["events"]
    summary = night["summary"]
    lines = [f"[nightear] {night['id']} 밤 요약"]
    if not events:
        lines.append("조용한 밤이었습니다. 감지된 소리 이벤트가 없습니다.")
    else:
        lines.append(f"소리 이벤트 {len(events)}건")
        counts: dict[str, int] = {}
        unlabeled = 0
        for e in events:
            if e.get("labels"):
                top = e["labels"][0]["label"]
                counts[top] = counts.get(top, 0) + 1
            else:
                unlabeled += 1
        parts = [f"{k} {v}건" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        if unlabeled:
            parts.append(f"분류 안 됨 {unlabeled}건")
        lines.append("종류별: " + ", ".join(parts))
        loudest = max(events, key=lambda e: (e["peak_db"], -e["start_ts"]))
        when = datetime.fromtimestamp(loudest["start_ts"], tz).strftime("%H:%M")
        lines.append(f"가장 큰 소리: {when} ({loudest['peak_db']:.1f} dB)")
    if summary["gap_count"]:
        lines.append(f"{_fmt_duration(summary['gap_seconds'])}간 기록 없음 ({summary['gap_count']}회)")
    base = (cfg.get("dashboard_base_url") or "").rstrip("/")
    if base:
        lines.append(f"대시보드: {base}/#night={night['id']}")
    return "\n".join(lines)


class SummaryService:
    def __init__(self, app_state: Any, notifier: Notifier) -> None:
        self._s = app_state
        self.notifier = notifier
        self._task: asyncio.Task | None = None

    # ---- 발송 이력 ----
    def already_sent(self, night_id: str) -> bool:
        return bool(self._s.db.execute("SELECT 1 FROM summarized_nights WHERE night_id = ?", (night_id,)))

    def _mark_sent(self, night_id: str) -> None:
        self._s.db.execute(
            "INSERT OR IGNORE INTO summarized_nights(night_id, sent_ts) VALUES(?, ?)",
            (night_id, self._s.clock.now().timestamp()))

    # ---- 판단 ----
    def _candidate_ids(self, cfg: dict[str, Any], now: float) -> list[str]:
        ids = set()
        for back in range(LOOKBACK_DAYS + 1):
            local = datetime.fromtimestamp(now - back * 86400, ZoneInfo(cfg["timezone"]))
            ids.add(local.date().isoformat())
            ids.add((local.date() - timedelta(days=1)).isoformat())
        return sorted(ids)

    def _due(self, night: dict[str, Any], cfg: dict[str, Any], now: float) -> bool:
        if now >= night["end_ts"]:
            return True
        sleeps = night["sleep_intervals"]
        if not sleeps or any(s["ongoing"] for s in sleeps):
            return False
        grace = cfg["summary_grace_minutes"] * 60
        return now >= max(s["end_ts"] for s in sleeps) + grace

    @staticmethod
    def _has_data(night: dict[str, Any]) -> bool:
        return bool(night["levels"]["rows"] or night["events"] or night["sleep_intervals"] or night["gaps"])

    async def check(self) -> list[str]:
        """보낼 때가 된 밤의 요약을 보낸다. 보낸 밤 id 목록을 돌려준다."""
        cfg = self._s.settings.get_all()
        if not cfg["discord_webhook_url"]:
            return []
        now = self._s.clock.now().timestamp()
        sent: list[str] = []
        for nid in self._candidate_ids(cfg, now):
            w0, _ = night_window(nid, cfg)
            if now < w0 or self.already_sent(nid):
                continue
            night = self._s.nights.detail(nid)
            if not self._has_data(night) or not self._due(night, cfg, now):
                continue
            try:
                res = self.notifier.send(build_message(night, cfg))
                if inspect.isawaitable(res):
                    await res
            except Exception:
                log.exception("아침 요약 발송 실패(다음 점검에서 재시도): %s", nid)
                continue
            self._mark_sent(nid)
            sent.append(nid)
        return sent

    # ---- 주기 점검 ----
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(CHECK_INTERVAL_S)
            try:
                await self.check()
            except Exception:
                log.exception("아침 요약 점검 오류(무시하고 계속)")
