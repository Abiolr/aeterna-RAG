# Aeterna RAG API

A standalone RAG-based microservice that scores a file's long-term
survivability (0–100) based on file-format openness, adoption,
supersession status, metadata richness, and lifecycle health — using
PRONOM + Library of Congress format data, a vector store for
semantic context, and an LLM (via the Anthropic API) to produce the
final score and explanation.

## How it works

1. **`POST /score`** receives an uploaded file.
2. `services/file_extraction.py` pulls technical metadata via
   ExifTool and derives the file extension.
3. `services/build_lookup_db.py` looks up that extension in a SQLite
   DB built from PRONOM + Library of Congress format data.
4. `services/vector_db_setup.py` queries a persistent Chroma vector
   store (seeded from LoC sustainability docs) for semantically
   similar formats.
5. `services/data_pipeline.py` aggregates all of the above.
6. `services/system_prompt.py` builds the scoring prompt; and
   `services/llm_inference.py` sends it to the Anthropic API
   (`claude-haiku-4-5-20251001`) and returns the raw response.
7. `app.py` parses that response into JSON + a markdown summary and
   returns it to the caller.

## Prerequisites

- Python 3.11+
- [ExifTool](https://exiftool.org/) (system binary, not just the
  Python wrapper)
- An Anthropic API key
- PRONOM / Library of Congress export JSON files (see
  `services/build_lookup_db.py` and `services/vector_db_setup.py`
  for the expected file names/paths under `data/`)

### macOS

```bash
brew install exiftool
```

### Debian/Ubuntu (used in the Docker image)

```bash
apt-get install libimage-exiftool-perl
```

## Local development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=your-key-here
```

Build the format lookup DB and seed the vector store (one-time,
re-run whenever the source PRONOM/LoC JSON exports change):

```bash
python services/build_lookup_db.py
python services/vector_db_setup.py
```

Run the API:

```bash
python app.py
```

The service listens on `http://localhost:3000`.

## Endpoints

| Method | Path      | Description                                   |
|--------|-----------|------------------------------------------------|
| GET    | `/`       | Basic service identity check                   |
| GET    | `/health` | Liveness/readiness probe (env var presence, uptime) |
| POST   | `/score`  | Upload a file (`multipart/form-data`, field `file`) and get its survivability score |

Example:

```bash
curl -X POST http://localhost:3000/score \
  -F "file=@/path/to/some/file.png"
```

Response:

```json
{
  "json": {
    "sub_scores": { "...": "..." },
    "missing_inputs": [],
    "survivability_score": 85,
    "explanation": "..."
  },
  "summary": "..."
}
```

## Docker (production)

The Dockerfile installs the `exiftool` system binary (required by
`services/file_extraction.py`), installs Python dependencies, and
serves the app with `gunicorn` instead of Flask's dev server.

```bash
# Build and run
docker compose up --build -d

# Logs
docker compose logs -f

# Stop
docker compose down
```

Before running, make sure you have:

- A `.env` file in the project root with `ANTHROPIC_API_KEY` set
  (referenced by `docker-compose.yml` via `env_file`).
- A `data/` directory containing the PRONOM/LoC export JSON files
  expected by `services/build_lookup_db.py` and
  `services/vector_db_setup.py`. It's mounted read-only into the
  container.

The SQLite lookup DB and Chroma vector store live under `/app/db`
inside the container, backed by the `aeterna-db` named volume, so
they persist across restarts and redeploys. You'll still need to run
the build/seed scripts once inside the running container (or as a
one-off job) to populate them:

```bash
docker compose exec aeterna-api python services/build_lookup_db.py
docker compose exec aeterna-api python services/vector_db_setup.py
```

## Error handling

`/score` never returns internal detail (stack traces, LLM raw
output, file paths, library errors) in the HTTP response — everything
specific is logged server-side via the `aeterna` logger, and the
client only receives a generic message with an appropriate status
code (400 for bad requests, 500 for processing failures).

## Project structure

```
.
├── app.py                  # Flask entrypoint
├── services/
│   ├── file_extraction.py  # ExifTool metadata + extension helpers
│   ├── build_lookup_db.py  # PRONOM/LoC SQLite lookup DB build + query
│   ├── vector_db_setup.py  # Chroma vector store setup + query
│   ├── data_pipeline.py    # Aggregates context for one file
│   ├── system_prompt.py    # Builds the LLM scoring prompt
│   └── llm_inference.py    # Calls the Anthropic API
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── data/                   # PRONOM/LoC export JSON
```