# Aeterna RAG API

*Aeterna* — Latin for **"eternal"** — is a standalone RAG-based microservice
that scores a file's long-term survivability (0–100): how likely it is to
remain readable and accessible decades from now, based on file-format
openness, adoption, supersession status, metadata richness, and lifecycle
health. It uses PRONOM + Library of Congress format registry data, a vector
store for semantic context, and an LLM (via the Anthropic API) to produce a
grounded, citation-backed score and explanation.

## Origin

Aeterna is a reimplementation of the scoring engine from **[Domus
Memoriae](https://github.com/Abiolr/domus-memoriae)**, a family digital-archive
platform built for CalgaryHacks 2026. Domus Memoriae's original "Archive
Engine" scored file risk using a Random Forest classifier trained on
synthetic data. Aeterna rebuilds that idea from scratch as a RAG system:
instead of a black-box model producing a number, it retrieves real
preservation literature (PRONOM registry data, Library of Congress
sustainability documentation) and has an LLM reason over that evidence to
produce an explainable, evidence-cited score.

This project exists as a personal learning vehicle — a deliberate way to go
deeper on Python, LLMs, RAG architecture, SQLite, ChromaDB, and vector
databases — rather than as a production system or a direct integration with
the original hackathon codebase.

## The scoring framework

The survivability score is a weighted combination of five sub-scores, each
in the range 0.0–1.0 (1.0 = maximally favorable/safe):

| Term | Weight | What it measures |
|------|--------|-------------------|
| **Openness** | 0.25 | Is the format an open, disclosed standard, or closed/proprietary? Based on the `disclosure` and `licensing_and_patents` fields from the Library of Congress sustainability data — i.e. whether the format's specification is publicly documented, and whether it's encumbered by patents or restrictive licensing. |
| **Adoption** | 0.25 | How widely is the format actually used and supported? Based on the LoC `adoption` field, mapped from qualitative language (e.g. "industry standard" → high, "limited support" → low). |
| **Non-supersession** | 0.20 | Has this format been replaced by a newer version? Based directly on the `is_superseded` flag from PRONOM registry data — structured fact, not LLM inference. |
| **Metadata completeness** | 0.15 | How much provenance/content metadata (title, author, description, authenticity/C2PA claims) does the *uploaded file itself* carry, versus just opaque technical container fields? |
| **Age / lifecycle health** | 0.15 | Is the format actively maintained, or aged past its support horizon? Requires both a `release_date` and either a `withdrawn_date` or explicit lifecycle language — PRONOM populates these for only a small minority of formats, so this term is frequently `null`. |

If any sub-score can't be determined from the retrieved data, it's marked
`null` and excluded from `missing_inputs` — the LLM is explicitly instructed
never to guess or fall back on outside knowledge, only to redistribute the
remaining weights across the terms it *does* have evidence for (see
`services/system_prompt.py` for the exact renormalization formula).

## How it works (pipeline walkthrough)

Here's what happens, step by step, from the moment a file is uploaded to
the moment a score comes back:

1. **Upload** — `POST /score` receives a file over `multipart/form-data`
   and saves it to a temp folder.
2. **Extract metadata** — `services/file_extraction.py` runs the file
   through ExifTool to pull out whatever embedded metadata exists (title,
   author, timestamps, technical details), and figures out the file's
   extension.
3. **Look up the format** — `services/build_lookup_db.py` takes that
   extension and searches a local SQLite database, pre-built from PRONOM
   (the UK National Archives' format registry) and Library of Congress
   format data. This returns hard facts about the format: whether it's
   open or proprietary, whether it's been superseded, and any known
   release/withdrawal dates.
4. **Find similar formats** — `services/vector_db_setup.py` takes a short
   natural-language question ("is .png a strong, sustainable file
   format?") and searches a vector database (ChromaDB) of Library of
   Congress sustainability write-ups, pulling back documents about
   comparable formats. This gives the LLM extra context to reason with —
   it's not used to identify the file itself, just to add comparative
   grounding.
5. **Assemble the context** — `services/data_pipeline.py` bundles the
   ExifTool metadata, the SQLite lookup results, and the vector search
   results into one package.
6. **Build the prompt** — `services/system_prompt.py` takes that package
   and turns it into a detailed instruction set for the LLM: the scoring
   formula, the five sub-scores and how to compute each one, and strict
   rules against inventing information that isn't in the retrieved data.
7. **Score it** — `services/llm_inference.py` sends that prompt to the
   Anthropic API (`claude-haiku-4-5-20251001`), which returns a JSON
   object with the five sub-scores (each backed by cited evidence), the
   final survivability score, and a plain-language explanation.
8. **Return the result** — `app.py` parses that response into JSON plus a
   markdown summary and sends it back to the caller. If anything fails
   along the way, the client gets a generic error message — the real
   error (stack trace, raw LLM output, etc.) is only ever logged
   server-side, never exposed over the API.

## Tech stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Language | Python 3.11+ | |
| Web framework | Flask | Dev server locally; `gunicorn` in Docker (see Dockerfile) |
| LLM | Anthropic API (`claude-haiku-4-5-20251001`) | Called via the official `anthropic` Python SDK in `services/llm_inference.py` |
| Vector store | ChromaDB (persistent client) | Seeded with Library of Congress format-sustainability documents |
| Embeddings | `all-MiniLM-L6-v2` (sentence-transformers) | Runs locally — embeds both the seeded LoC documents and incoming queries |
| Structured lookup DB | SQLite (raw SQL, no ORM) | Built once from PRONOM + LoC exports; dataset is static/write-once, so no ORM/migrations layer is used |
| File metadata extraction | ExifTool, via `pyexiftool` | Requires the ExifTool system binary, not just the Python wrapper |
| Config | `python-dotenv` | Loads `ANTHROPIC_API_KEY` from `.env` |
| Containerization | Docker + Docker Compose | `dockerfile` builds the image; `docker-compose.yml` wires up env vars, a read-only `data/` mount, and a named volume for `db/` |
| Data sources | PRONOM registry, Library of Congress Sustainability of Digital Formats | See `data/` and `raw_xml/` |

**Deliberately not used:** LangChain — the RAG pipeline (retrieval, prompt
assembly, LLM call) is hand-built on top of the ChromaDB and Anthropic SDKs
directly, since abstracting away those mechanics would defeat the point of
using this project to learn RAG from primitives. PostgreSQL + pgvector was
also considered and dropped in favor of SQLite + ChromaDB, since the format
dataset is static and doesn't need a full relational server.

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
    "sub_scores": {
      "openness": { "score": 0.9, "evidence": "..." },
      "adoption": { "score": 0.95, "evidence": "..." },
      "non_supersession": { "score": 0.95, "evidence": "..." },
      "metadata_completeness": { "score": 0.3, "evidence": "..." },
      "age_lifecycle_health": { "score": null, "evidence": "..." }
    },
    "missing_inputs": ["age_lifecycle_health"],
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

## Known limitations

- **`age_lifecycle_health` is frequently null.** PRONOM populates
  `release_date` for only a small fraction of formats and
  `withdrawn_date` for almost none, so this term is often excluded
  from the score (see the weight-redistribution logic in
  `services/system_prompt.py`).
- **`related_puid` in `format_relationships` is not yet joinable.**
  It currently stores PRONOM's internal numeric `FormatID` rather
  than a `fmt/___`-style PUID, so it can't be joined back against
  `formats.puid` yet.
- **No fallback yet for formats absent from the LoC vector store.**
  If a PRONOM format has no corresponding Library of Congress entry,
  the vector search step currently just returns whatever it finds as
  the nearest (possibly irrelevant) semantic neighbors, rather than
  a distance-threshold check or a PRONOM-only rule-based score.

## Project structure

```
.
├── app.py                    # Flask entrypoint
├── services/
│   ├── __init__.py
│   ├── file_extraction.py    # ExifTool metadata + extension helpers
│   ├── build_lookup_db.py    # PRONOM/LoC SQLite lookup DB build + query
│   ├── vector_db_setup.py    # Chroma vector store setup + query
│   ├── data_pipeline.py      # Aggregates context for one file
│   ├── system_prompt.py      # Builds the LLM scoring prompt
│   └── llm_inference.py      # Calls the Anthropic API
├── data/                     # PRONOM/LoC export JSON (build_lookup_db.py, vector_db_setup.py inputs)
├── db/                       # Generated on build — not committed
│   ├── format_lookup.sqlite3 # SQLite lookup DB (build_lookup_db.py output)
│   └── aeterna_vector_db/    # Persistent Chroma vector store (vector_db_setup.py output)
├── raw_xml/                  # Raw PRONOM/LoC source XML, pre-JSON-conversion
├── temp_uploads/             # Scratch space for in-flight /score uploads, cleaned up per-request
├── test_files/                # Sample files for manually exercising /score
├── dockerfile
├── docker-compose.yml
├── .dockerignore
├── .gitignore
├── requirements.txt
└── README.md
```