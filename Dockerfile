FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY nightear ./nightear
RUN pip install --no-cache-dir ".[ml]"

# YAMNet(ONNX)을 이미지에 내려받아 둔다: 라벨링은 컨테이너 안에서 로컬로만 돈다.
COPY scripts ./scripts
ENV NIGHTEAR_MODELS=/app/models
RUN python scripts/fetch_models.py

ENV NIGHTEAR_DB=/data/nightear.db
ENV NIGHTEAR_CLIPS=/data/clips
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "nightear.main:app", "--host", "0.0.0.0", "--port", "8000"]
