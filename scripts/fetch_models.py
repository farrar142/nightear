"""YAMNet ONNX 모델과 클래스 맵을 models 디렉터리(NIGHTEAR_MODELS, 기본 ./models)로 내려받는다.

  py scripts/fetch_models.py

- 모델: Hugging Face `zeropointnine/yamnet-onnx` (Google YAMNet 의 ONNX 변환, Apache-2.0).
  입력은 16kHz float 파형, 출력은 (창 수, 521) AudioSet 점수.
- 클래스 맵: TensorFlow models 저장소의 공식 yamnet_class_map.csv.
내려받은 파일은 SHA-256을 확인한다. 이후 라벨링은 전부 로컬에서 돌며 오디오는 밖으로 나가지 않는다.
"""
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

FILES = {
    "yamnet.onnx": (
        "https://huggingface.co/zeropointnine/yamnet-onnx/resolve/main/yamnet.onnx",
        "1510041dce24a2e9e84ec546807ac408ae496da6d1ed41bc3ccba649623f8e19",
    ),
    "yamnet_class_map.csv": (
        "https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv",
        "cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2",
    ),
}


def main() -> int:
    dest = Path(os.environ.get("NIGHTEAR_MODELS", "models"))
    dest.mkdir(parents=True, exist_ok=True)
    for name, (url, sha) in FILES.items():
        target = dest / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == sha:
            print(f"이미 있음: {target}")
            continue
        print(f"내려받는 중: {url}")
        data = urllib.request.urlopen(url, timeout=120).read()
        if hashlib.sha256(data).hexdigest() != sha:
            print(f"SHA-256 불일치: {name}", file=sys.stderr)
            return 1
        target.write_bytes(data)
        print(f"저장: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
