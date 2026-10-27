"""공용 픽스처. 앱 팩토리에 가짜 포트를 주입해 TestClient를 만든다."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nightear.app import create_app

from .support.classifier import FakeClassifier
from .support.clock import FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "nightear.db"


@pytest.fixture
def make_app(clock, db_path):
    """같은 DB 파일을 재사용하며 앱(=백엔드 재시작)을 새로 만드는 팩토리."""

    def _make(**overrides):
        # 기본 분류기는 가짜: 실제 YAMNet 은 스모크 테스트에서만 쓴다.
        kwargs = {"clock": clock, "db_path": db_path, "classifier": FakeClassifier()}
        kwargs.update(overrides)
        return create_app(**kwargs)

    return _make


@pytest.fixture
def client(make_app):
    # with 블록으로 단일 이벤트 루프를 공유해야 여러 WebSocket이 서로 메시지를 주고받는다.
    with TestClient(make_app()) as c:
        yield c
