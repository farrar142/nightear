# nightear

수면 중 소음 기록기. 방 안 PC의 마이크를 하루 종일 듣고, 자는 동안 튀는 소리만 기록한다.
브라우저 대시보드 탭이 마이크를 잡아 백엔드(FastAPI + SQLite)로 오디오를 흘려보낸다.

## 실행

```
docker compose up --build
```

같은 PC에서 http://localhost:8000 을 열고 입력 장치를 고른 뒤 "수집 시작"을 누른다.
(`getUserMedia`는 보안 컨텍스트가 필요해 `localhost`로 접속해야 한다. 기본 바인딩은 `127.0.0.1`이며,
같은 와이파이의 다른 기기에서 결과만 보려면 `NIGHTEAR_BIND=<PC의 LAN IP>`로 지정한다.)

데이터(SQLite)는 `nightear-data` 볼륨의 `/data`에 저장된다. 로컬 개발 시에는 `NIGHTEAR_DB`(기본 `data/nightear.db`).

## 개발 / 테스트

```
py -m pip install -e ".[test]"   # 또는: pip install -e ".[test]"
py -m pytest                     # 전체 테스트
py -m pytest tests/test_settings.py   # 한 파일만
```

로컬 실행: `py -m uvicorn nightear.main:app --reload`

### 테스트 구조

테스트의 seam은 백엔드 입구(수집 WebSocket `/ws/ingest`)다. hello + 합성 PCM + 활동 메시지를 흘려보내고,
결과는 HTTP API와 실시간 채널(`/ws/realtime`) 메시지로만 검증한다.

- `nightear/app.py` `create_app(clock=, db_path=, ...)` : 포트(시계 등)를 주입받는 앱 팩토리
- `tests/support/clock.py` `FakeClock` : `advance()`로 시간을 진행
- `tests/support/audio.py` : 합성 PCM(`tone`, `noise`, `noise_with_bursts`, `silence`, `chunks`)
- `tests/support/clients.py` : `ingest(client)`, `realtime(client)` WebSocket 헬퍼
- `tests/conftest.py` : `clock`, `db_path`, `make_app`(재시작 흉내), `client` 픽스처

브라우저 소스(getUserMedia, 다운샘플링)와 대시보드 UI는 수동으로 확인한다.
