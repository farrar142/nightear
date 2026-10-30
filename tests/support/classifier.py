"""테스트용 가짜 소리 분류기. 정해진 라벨을 돌려주거나 실패한다."""
from __future__ import annotations

import threading


class FakeClassifier:
    def __init__(self, results=None, fail: bool = False, gate: threading.Event | None = None) -> None:
        self.results = results if results is not None else [
            ("Door", 0.82), ("Footsteps", 0.64), ("Knock", 0.31), ("Speech", 0.1)]
        self.fail = fail
        self.gate = gate  # 설정하면 gate.set() 전까지 분류가 끝나지 않는다(pending 상태 관찰용)
        self.calls: list[tuple[int, int]] = []  # (PCM 바이트 수, 샘플레이트)
        self.pcms: list[bytes] = []  # 받은 PCM 그대로

    def classify(self, pcm: bytes, sample_rate: int):
        self.calls.append((len(pcm), sample_rate))
        self.pcms.append(pcm)
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail:
            raise RuntimeError("분류기 오류")
        return list(self.results)
