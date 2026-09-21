# VPS 배포용. 로컬 개발에서는 쓰지 않는다 (호스트에서 직접 파이썬 실행).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
COPY db ./db
RUN pip install --no-cache-dir .

CMD ["python", "-m", "th.cli", "stream"]
