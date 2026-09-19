# Aeterna RAG API — production image

FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create a venv at a shared, non-root-restricted path so the final
# stage's non-root user can execute everything in it (installing with
# `pip --user` instead put packages under /root/.local, which the
# aeterna user can't traverse since /root is 700 — permission denied
# on gunicorn at runtime).
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY requirements.txt .

# Install the CPU-only torch wheel FIRST and explicitly. Left to its
# own resolution, pip (via sentence-transformers' torch dependency)
# will happily pull the CUDA-enabled build, which drags in ~3.3GB of
# unused nvidia-* packages — everything here runs on CPU (local
# sentence-transformers embeddings) or over the network (Anthropic
# API); nothing needs a local GPU.
RUN pip install --no-cache-dir torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir -r requirements.txt


FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY . .

RUN mkdir -p /app/db \
    && python services/vector_db_setup.py

RUN useradd --create-home --shell /bin/bash aeterna \
    && mkdir -p /app/temp_uploads \
    && chown -R aeterna:aeterna /app /opt/venv
USER aeterna

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3000/health')" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--workers", "2", "--timeout", "120", "app:app"]