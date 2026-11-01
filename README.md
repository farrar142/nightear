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
클립(WAV)은 `NIGHTEAR_CLIPS`(기본: DB 옆 `clips/`, 컨테이너에서는 `/data/clips`)에 `event-<id>.wav` 로 저장된다.

## 수면 모드와 「자는 동안」

- **수면 모드**: 밤 시간대(기본 22시~12시)에 PC 를 `idle_threshold_minutes`(기본 20분) 안 쓰면 켜지고,
  PC 를 다시 쓰면 바로 풀린다. 수면 모드 중의 이벤트만 클립(WAV)을 남기고 라벨링한다.
- **자는 동안(추정)**: 수면 모드는 「PC 를 안 쓴다」일 뿐이라 휴대폰·영상을 보며 깨어 있는 시간도 들어간다.
  그래서 수면 모드 구간 안에서 라벨 흐름으로 실제로 잠든 구간을 추정한다(`nightear/asleep.py`).
  숨소리(Snoring·Breathing·Snort) 1순위 클립이 20분 안에 5개 이상 이어지고 말소리·키보드보다 많아지면
  잠든 것으로, 반대로 말소리·키보드가 그렇게 이어지면 깬 것으로 본다. 숨소리가 거의 없어 추정이 안 되는 밤은
  「추정 불가」로 표시하고 수면 모드 전체를 자는 동안으로 본다.
- 아침 요약, 패턴 통계(「자는 동안만」, `GET /api/stats?asleep=true`), 밤 상세(`asleep`)는 자는 동안 기준이다.
  클립 저장·보관은 수면 모드 기준 그대로이고, 깨어 있던 이벤트는 대시보드에서 흐리게 보인다.

## 개발 / 테스트

```
py -m pip install -e ".[test]"   # 또는: pip install -e ".[test]"
py -m pytest                     # 전체 테스트
py -m pytest tests/test_settings.py   # 한 파일만
```

로컬 실행: `py -m uvicorn nightear.main:app --reload`

### 로컬 AI 라벨링 (YAMNet)

클립 라벨링은 YAMNet(AudioSet 521 클래스)을 onnxruntime으로 PC 안에서만 돌린다. Docker 이미지는 빌드 때
모델을 내려받아 둔다. 로컬 개발에서는:

```
py -m pip install -e ".[ml]"
py scripts/fetch_models.py      # ./models (NIGHTEAR_MODELS) 에 yamnet.onnx, yamnet_class_map.csv
```

분류기에는 클립 전체가 아니라 이벤트 구간(앞뒤 0.5초, 최소 1초)만 넘기고, `Silence` 는 순위에서 뺀다.
라벨 방식을 바꾼 뒤 이미 붙은 라벨을 다시 매기려면 `POST /api/labels/relabel?since=<ts>&until=<ts>`
(생략하면 클립이 남은 이벤트 전체)를 부른다.

모델이 없으면 라벨 상태가 failed 가 될 뿐 클립은 그대로 남는다. 실제 모델 스모크 테스트
(`tests/test_yamnet_smoke.py`)는 모델이 없으면 건너뛴다.

### 테스트 구조

테스트의 seam은 백엔드 입구(수집 WebSocket `/ws/ingest`)다. hello + 합성 PCM + 활동 메시지를 흘려보내고,
결과는 HTTP API와 실시간 채널(`/ws/realtime`) 메시지로만 검증한다.

- `nightear/app.py` `create_app(clock=, db_path=, ...)` : 포트(시계 등)를 주입받는 앱 팩토리
- `tests/support/clock.py` `FakeClock` : `advance()`로 시간을 진행
- `tests/support/audio.py` : 합성 PCM(`tone`, `noise`, `noise_with_bursts`, `silence`, `chunks`)
- `tests/support/clients.py` : `ingest(client)`, `realtime(client)` WebSocket 헬퍼
- `tests/conftest.py` : `clock`, `db_path`, `make_app`(재시작 흉내), `client` 픽스처

브라우저 소스(getUserMedia, 다운샘플링)와 대시보드 UI는 수동으로 확인한다.
