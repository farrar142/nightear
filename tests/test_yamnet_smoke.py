"""실제 YAMNet 스모크 테스트. 모델 파일(scripts/fetch_models.py)이나 onnxruntime 이 없으면 건너뛴다."""
import pytest
from fastapi.testclient import TestClient

from nightear.labeling import YamnetClassifier, default_models_dir

from .support import audio
from .support.clients import ingest, realtime

pytestmark = pytest.mark.ml

classifier = YamnetClassifier()
if not classifier.available:
    pytest.skip(f"YAMNet 모델이 없다({default_models_dir()}): py scripts/fetch_models.py", allow_module_level=True)
pytest.importorskip("onnxruntime")


def test_실제_YAMNet이_합성_소리에_라벨을_돌려준다():
    pcm = audio.noise(-20.0, 3.0, seed=1)  # 백색 잡음
    result = classifier.classify(pcm, 16000)
    assert 1 <= len(result) <= 10
    assert all(0.0 <= s <= 1.0 for _, s in result)
    assert [s for _, s in result] == sorted((s for _, s in result), reverse=True)
    assert any("noise" in name.lower() for name, _ in result[:3])  # White noise / Noise


def test_수집부터_라벨까지_실제_모델로_이어진다(make_app):
    pcm = audio.noise(-50.0, 15.0, seed=2) + audio.noise(-20.0, 2.0, seed=3) + audio.noise(-50.0, 20.0, seed=4)
    with TestClient(make_app(classifier=YamnetClassifier())) as client, realtime(client) as rt, ingest(client) as src:
        src.send_pcm(pcm)
        done = rt.receive_until(
            lambda m: m["type"] == "label" and m["event"]["label_status"] in ("done", "failed"), limit=1000)
    assert done["event"]["label_status"] == "done", done
    assert done["event"]["labels"]
