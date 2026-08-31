FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CRAWLER_DB_PATH=/data/crawler.db \
    CRAWLER_EVIDENCE_DIR=/data/evidence \
    CRAWLER_RESULT_JSONL_DIR=/data/result-sinks/jsonl

WORKDIR /app

COPY requirements.lock pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir -r requirements.lock \
    && python -m pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 crawler \
    && mkdir -p /data \
    && chown -R crawler:crawler /data

USER crawler
VOLUME ["/data"]
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/api/v1/health', timeout=3).read()"

CMD ["amazon-crawler", "serve", "--host", "0.0.0.0", "--port", "3000"]
