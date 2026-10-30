"""합성 오디오 헬퍼. 모든 출력은 16kHz 모노 16-bit 리틀엔디언 PCM(bytes)."""
from __future__ import annotations

import math
import random
import struct

SAMPLE_RATE = 16000


def _to_pcm(samples: list[float]) -> bytes:
    clipped = [max(-32768, min(32767, int(round(s)))) for s in samples]
    return struct.pack(f"<{len(clipped)}h", *clipped)


def _amp_for_rms(dbfs: float) -> float:
    return 32768.0 * (10.0 ** (dbfs / 20.0))


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * seconds)


def tone(dbfs: float, seconds: float, freq: float = 440.0) -> bytes:
    """RMS가 정확히 dbfs인 사인파."""
    amp = _amp_for_rms(dbfs) * math.sqrt(2.0)
    n = int(SAMPLE_RATE * seconds)
    return _to_pcm([amp * math.sin(2 * math.pi * freq * i / SAMPLE_RATE) for i in range(n)])


def noise(dbfs: float, seconds: float, seed: int = 0) -> bytes:
    """RMS를 dbfs에 정확히 맞춘 가우시안 잡음(재현 가능)."""
    rng = random.Random(seed)
    n = int(SAMPLE_RATE * seconds)
    raw = [rng.gauss(0.0, 1.0) for _ in range(n)]
    cur = math.sqrt(sum(x * x for x in raw) / n) if n else 1.0
    scale = _amp_for_rms(dbfs) / cur
    return _to_pcm([x * scale for x in raw])


def noise_with_bursts(
    floor_dbfs: float,
    total_seconds: float,
    bursts: list[tuple[float, float, float]],
    seed: int = 0,
) -> bytes:
    """잡음 바닥 위에 (시작초, 길이초, dBFS) 버스트(사인파)를 얹은 오디오."""
    rng = random.Random(seed)
    n = int(SAMPLE_RATE * total_seconds)
    floor_amp = _amp_for_rms(floor_dbfs)
    samples = [rng.gauss(0.0, 1.0) * floor_amp for _ in range(n)]
    for start, length, dbfs in bursts:
        amp = _amp_for_rms(dbfs) * math.sqrt(2.0)
        i0 = int(SAMPLE_RATE * start)
        i1 = min(n, i0 + int(SAMPLE_RATE * length))
        for i in range(i0, i1):
            samples[i] = amp * math.sin(2 * math.pi * 440.0 * i / SAMPLE_RATE)
    return _to_pcm(samples)


def rms_dbfs(pcm: bytes) -> float:
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    rms = math.sqrt(sum(x * x for x in samples) / n) if n else 0.0
    return 20.0 * math.log10(rms / 32768.0) if rms > 0 else -120.0


def chunks(pcm: bytes, chunk_ms: int = 100) -> list[bytes]:
    """PCM을 chunk_ms 단위 바이너리 프레임으로 나눈다."""
    size = SAMPLE_RATE * chunk_ms // 1000 * 2
    return [pcm[i : i + size] for i in range(0, len(pcm), size)]
