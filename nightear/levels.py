"""프레임 레벨 계산: 16-bit PCM -> dBFS."""
from __future__ import annotations

import math
import sys
from array import array

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes (16-bit)
FRAME_MS = 100  # 레벨 프레임 길이 -> 10Hz
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH
DB_FLOOR = -120.0  # 무음일 때 돌려주는 하한
_FULL_SCALE = 32768.0


def rms_dbfs(pcm: bytes) -> float:
    """리틀엔디언 16-bit 모노 PCM의 RMS를 dBFS로 계산한다. 무음이면 DB_FLOOR."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % SAMPLE_WIDTH)])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return DB_FLOOR
    mean_sq = sum(s * s for s in samples) / len(samples)
    if mean_sq <= 0:
        return DB_FLOOR
    db = 10.0 * math.log10(mean_sq / (_FULL_SCALE * _FULL_SCALE))
    return max(db, DB_FLOOR)


class FrameSplitter:
    """임의 길이로 들어오는 PCM 바이트를 고정 길이(FRAME_BYTES) 프레임으로 잘라낸다."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        frames: list[bytes] = []
        while len(self._buf) >= FRAME_BYTES:
            frames.append(bytes(self._buf[:FRAME_BYTES]))
            del self._buf[:FRAME_BYTES]
        return frames
