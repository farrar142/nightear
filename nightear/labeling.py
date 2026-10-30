"""로컬 AI 라벨링.

소리 분류기는 포트(`SoundClassifier`)로 주입한다: `classify(pcm, sample_rate) -> [(label, score), ...]`
(점수 내림차순). 기본 구현 `YamnetClassifier` 는 YAMNet(AudioSet 521 클래스)을 onnxruntime으로 PC 안에서만
실행하므로 오디오가 밖으로 나가지 않는다. 모델은 `scripts/fetch_models.py` 로 내려받는다(README 참고).

LabelWorker는 클립이 저장되면(ClipRecorder.on_clip_saved) 이벤트를 pending 으로 표시하고 큐에 넣어 비동기로
분류한다. 클립 전체가 아니라 이벤트 구간(앞뒤 조금 여유)만 분류기에 넘긴다: 클립 앞뒤의 pre/post roll 은 조용해서,
창별 최댓값을 쓰는 분류기에서는 거의 모든 클립의 1순위가 Silence 가 되기 때문이다. 같은 이유로 소리가 아닌
라벨(Silence)은 순위에서 뺀다. 성공하면 상위 label_top_k 개를 event_labels 에 저장하고 done, 실패하면 failed (클립은 그대로 남는다).
상태가 바뀔 때마다 실시간 채널로 {"type":"label","event":{...}} 를 방송한다.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import threading
import wave
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, Request

log = logging.getLogger(__name__)

MODEL_FILE = "yamnet.onnx"
CLASS_MAP_FILE = "yamnet_class_map.csv"
RESULT_SIZE = 10  # 분류기가 돌려주는 후보 수 상한(저장은 label_top_k 만큼)
SEGMENT_MARGIN_S = 0.5  # 이벤트 구간 앞뒤로 더 넣어 분류하는 여유
MIN_SEGMENT_S = 1.0  # 이보다 짧은 구간은 앞뒤로 넓힌다(YAMNet 한 창 0.975초)
NON_SOUND_LABELS = frozenset({"Silence"})  # 이벤트는 기준선보다 큰 소리라 이 라벨은 의미가 없다


class SoundClassifier(Protocol):
    def classify(self, pcm: bytes, sample_rate: int) -> list[tuple[str, float]]:
        """16-bit 모노 PCM -> (라벨, 확신도 0~1) 목록(확신도 내림차순)."""
        ...


class ModelUnavailable(RuntimeError):
    pass


def default_models_dir() -> Path:
    return Path(os.environ.get("NIGHTEAR_MODELS", "models"))


class YamnetClassifier:
    """YAMNet ONNX 로컬 실행. onnxruntime/numpy 와 모델 파일은 처음 분류할 때 불러온다(선택 의존성 `.[ml]`)."""

    def __init__(self, models_dir: str | Path | None = None) -> None:
        self.models_dir = Path(models_dir) if models_dir else default_models_dir()
        self._session = None
        self._names: list[str] = []
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return (self.models_dir / MODEL_FILE).is_file() and (self.models_dir / CLASS_MAP_FILE).is_file()

    def _load(self) -> None:
        with self._lock:
            if self._session is not None:
                return
            if not self.available:
                raise ModelUnavailable(f"YAMNet 모델이 없다: {self.models_dir} (scripts/fetch_models.py 로 내려받는다)")
            try:
                import onnxruntime as ort
            except ImportError as e:
                raise ModelUnavailable("onnxruntime 이 설치되지 않았다: pip install '.[ml]'") from e
            with open(self.models_dir / CLASS_MAP_FILE, encoding="utf-8", newline="") as f:
                self._names = [row["display_name"] for row in csv.DictReader(f)]
            self._session = ort.InferenceSession(str(self.models_dir / MODEL_FILE),
                                                 providers=["CPUExecutionProvider"])

    def classify(self, pcm: bytes, sample_rate: int) -> list[tuple[str, float]]:
        if sample_rate != 16000:
            raise ValueError(f"YAMNet 은 16kHz 입력이 필요하다: {sample_rate}")
        self._load()
        import numpy as np
        wave_f = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        if wave_f.size < 15600:  # YAMNet 한 창(0.975초) 미만이면 0으로 채운다
            wave_f = np.pad(wave_f, (0, 15600 - wave_f.size))
        scores = self._session.run(None, {"waveform": wave_f})[0]  # (창 수, 521)
        best = scores.max(axis=0)  # 클립 안 어느 창에서든 가장 강하게 나온 점수
        order = np.argsort(-best)[:RESULT_SIZE]
        return [(self._names[i], float(best[i])) for i in order]


def read_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate()


def event_segment(pcm: bytes, sample_rate: int, event: dict[str, Any]) -> bytes:
    """클립 PCM 에서 이벤트 구간(앞뒤 SEGMENT_MARGIN_S, 최소 MIN_SEGMENT_S)만 잘라낸다."""
    clip_start = event.get("clip_start_ts")
    if clip_start is None:
        return pcm
    total = len(pcm) // 2 / sample_rate
    start = event["start_ts"] - clip_start - SEGMENT_MARGIN_S
    end = event["end_ts"] - clip_start + SEGMENT_MARGIN_S
    short = MIN_SEGMENT_S - (end - start)
    if short > 0:
        start, end = start - short / 2, end + short / 2
    if start < 0:  # 클립 밖으로 나간 만큼 반대쪽으로 민다
        start, end = 0.0, end - start
    if end > total:
        start, end = max(0.0, start - (end - total)), total
    return pcm[int(start * sample_rate) * 2:int(end * sample_rate) * 2]


class LabelWorker:
    def __init__(self, settings, store, hub, classifier: SoundClassifier, clips_dir: Path) -> None:
        self._settings = settings
        self._store = store
        self._hub = hub
        self._classifier = classifier
        self._clips_dir = Path(clips_dir)
        self._queue: asyncio.Queue[int] | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._queue = asyncio.Queue()
        # 재시작 전에 끝내지 못한 라벨링을 이어서 한다.
        for event_id in self._store.pending_label_ids():
            self._queue.put_nowait(event_id)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def relabel(self, *, since: float | None = None, until: float | None = None) -> int:
        """클립이 남아 있는 이벤트를 다시 라벨링 대기열에 넣는다. 넣은 수를 돌려준다."""
        targets = [e for e in self._store.list(order="asc", since=since, until=until) if e["has_clip"]]
        for event in targets:
            await self.enqueue(event)
        return len(targets)

    async def enqueue(self, event: dict[str, Any]) -> None:
        """클립이 저장된 이벤트를 라벨링 대기열에 넣는다."""
        updated = self._store.set_label_status(event["id"], "pending")
        await self._hub.broadcast({"type": "label", "event": updated})
        if self._queue is not None:
            self._queue.put_nowait(event["id"])

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            event_id = await self._queue.get()
            try:
                await self._label(event_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("라벨링 처리 오류(이벤트 %s)", event_id)

    async def _label(self, event_id: int) -> None:
        path = self._store.clip_file(event_id, self._clips_dir)
        if path is None:  # 그 사이 클립이 지워졌다
            return
        try:
            pcm, rate = await asyncio.to_thread(read_wav, path)
            pcm = event_segment(pcm, rate, self._store.get(event_id))
            result = await asyncio.to_thread(self._classifier.classify, pcm, rate)
            top_k = int(self._settings.get("label_top_k"))
            labels = [(str(name), float(score)) for name, score in result
                      if name not in NON_SOUND_LABELS][:top_k]
            event = self._store.set_labels(event_id, labels)
        except Exception as e:  # 실패해도 이벤트·클립은 남기고 상태만 failed 로 둔다
            log.warning("라벨링 실패(이벤트 %s): %s", event_id, e)
            event = self._store.set_label_status(event_id, "failed")
        await self._hub.broadcast({"type": "label", "event": event})


router = APIRouter(prefix="/api/labels", tags=["labels"])


@router.get("")
def list_labels(request: Request) -> list[dict[str, Any]]:
    """붙은 라벨 목록과 이벤트 수(라벨 필터 선택지)."""
    return request.app.state.events.label_counts()


@router.post("/relabel")
async def relabel(request: Request, since: float | None = None, until: float | None = None) -> dict[str, int]:
    """클립이 남아 있는 이벤트(start_ts 가 [since, until) 안)를 다시 라벨링한다. 결과는 실시간 채널로 온다."""
    return {"queued": await request.app.state.label_worker.relabel(since=since, until=until)}
