# syntax=docker/dockerfile:1

########################
# 1) Builder stage
########################
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VENV_PATH=/opt/venv

WORKDIR /app

# สร้าง virtualenv
RUN python -m venv ${VENV_PATH}
ENV PATH="${VENV_PATH}/bin:$PATH"

# ติดตั้ง dependencies สำหรับ build บาง package
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# copy requirements ก่อน เพื่อใช้ docker layer cache
COPY requirements.txt .

RUN pip install --upgrade pip setuptools wheel && \
    pip install -r requirements.txt

########################
# 2) Runtime stage
########################
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VENV_PATH=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8002

WORKDIR /app

# สร้าง user non-root
RUN groupadd -r appuser && useradd -r -g appuser appuser

# copy virtualenv จาก builder
COPY --from=builder /opt/venv /opt/venv

# copy app files
COPY main.py /app/main.py
COPY .env.example /app/.env.example
COPY README-fastapi-ocr.md /app/README-fastapi-ocr.md

# ถ้ามีโฟลเดอร์อื่น เช่น app/, src/ หรือ prompts/ ให้ copy เพิ่มเอง
# COPY app /app/app

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/health').read()" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]