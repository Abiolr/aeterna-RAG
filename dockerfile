# Aeterna RAG API — production image
#
# NOTE ON PLATFORM: no --platform pin here. If you were building only
# for your own Apple Silicon Mac you could pin linux/arm64, but any
# real deploy host is very likely amd64 — leave this unpinned so Docker
# picks the correct arch for wherever the build actually runs.
FROM python:3.13.7

# exiftool is a system binary required by services/file_extraction.py
# (pyexiftool just shells out to it). libimage-exiftool-perl provides it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (includes services/, data/ source JSON, etc.)
COPY . .

# Build the format-lookup SQLite DB and seed the Chroma vector store
# AT BUILD TIME, so both ship baked into the image. This needs network
# access (downloads the sentence-transformers embedding model) and
# needs data/*.json to already be present via the COPY above.
RUN mkdir -p /app/db \
    && python services/build_lookup_db.py \
    && python services/vector_db_setup.py

# Non-root user — chown AFTER the DB build so the baked-in db/ files
# are owned correctly for the runtime user.
RUN useradd --create-home --shell /bin/bash aeterna \
    && mkdir -p /app/temp_uploads \
    && chown -R aeterna:aeterna /app
USER aeterna

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3000/health')" || exit 1

# app.run(debug=False, port=3000) is dev-mode; gunicorn serves it in prod
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--workers", "2", "--timeout", "120", "app:app"]
