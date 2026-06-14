FROM python:3.11-slim

WORKDIR /app

# 시스템 최소 의존성 — psycopg binary 가 함께 들어오므로 libpq 별도 설치 불필요
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app
COPY tests /app/tests

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8100/health || exit 1

ENTRYPOINT ["python", "-m", "app.main"]
