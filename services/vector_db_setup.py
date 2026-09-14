"""
Vector store setup and query helpers for the Aeterna scoring pipeline.

Uses a persistent Chroma collection ("loc_formats") seeded from Library
of Congress format-sustainability documents, embedded with a local
SentenceTransformer model. services.data_pipeline.get_system_data
queries this collection per-request to surface formats semantically
similar to the uploaded file's format, for comparative context in the
LLM scoring prompt.
"""

import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions
import json

# Path to the source JSON of LoC format documents used to seed the
# vector store. Each record is expected to have "id", "metadata", and
# "text" keys (see seed_vector_db).
LOC_VECTOR_JSON = "data/loc_vector_docs.json"

# On-disk location of the persistent Chroma database.
VECTOR_DB_PATH = "./db/aeterna_vector_db"

# Name of the Chroma collection holding the LoC format documents.
COLLECTION_NAME = "loc_formats"

# SentenceTransformer model used to embed both the seeded documents
# and incoming queries. Must stay consistent across seeding and
# querying, since embeddings from different models aren't comparable.
EMBEDDING_MODEL = "all-MiniLM-L6-v2"


def _get_collection():
    """Shared helper: connects to the persistent Chroma client and
    returns the loc_formats collection (created if it doesn't exist yet)."""
    client = chromadb.PersistentClient(
        path=VECTOR_DB_PATH,
        settings=Settings(anonymized_telemetry=False) # Disable telemetry here
    )

    sentence_transformer_ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL,
    )

    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=sentence_transformer_ef
    )


def _clean_metadata(metadata):
    """
    Clean metadata to ensure all values are scalar.
    Converts lists into comma-separated strings and drops empty lists.
    """
    cleaned = {}
    for key, value in metadata.items():
        if value == []:
            continue
        
        if isinstance(value, list):
            cleaned[key] = ", ".join(str(v) for v in value)
        else:
            cleaned[key] = value
            
    return cleaned

def seed_vector_db(force: bool = False):
    """
    One-time setup step: loads LOC format docs from LOC_VECTOR_JSON and
    adds them to the collection. Safe to call multiple times — skips
    re-seeding if the collection already has data, unless force=True.

    Run this once during setup (e.g. from a setup script), not on every
    pipeline request.

    Args:
        force: If True, re-seed even if the collection already has
            documents (existing documents are not explicitly cleared
            first — duplicate IDs are simply re-added/overwritten by
            Chroma's `add`).

    Returns:
        The Chroma collection object (either the pre-existing one, if
        skipped, or the freshly seeded one).

    Raises:
        Propagates any exception from reading LOC_VECTOR_JSON (e.g.
        FileNotFoundError) or from the Chroma `add` call.
    """
    collection = _get_collection()

    if not force and collection.count() > 0:
        print(f"Collection '{COLLECTION_NAME}' already has {collection.count()} "
              f"documents — skipping seed (pass force=True to reseed).")
        return collection

    loc_vector = json.load(open(LOC_VECTOR_JSON))

    collection.add(
        ids=[record["id"] for record in loc_vector],
        metadatas=[_clean_metadata(record["metadata"]) for record in loc_vector],
        documents=[record["text"] for record in loc_vector]
    )

    print(f"Seeded '{COLLECTION_NAME}' with {len(loc_vector)} documents.")
    return collection


def get_vector_search(query_text: str, n_results: int = 5):
    """
    Per-file/per-request query against the already-seeded collection.
    Does NOT reload or re-add documents — call seed_vector_db() once
    beforehand (e.g. during setup) to populate the collection.

    Args:
        query_text: Natural-language query to embed and search with
            (e.g. "is .png a strong, sustainable file format?").
        n_results: Maximum number of semantically similar documents
            to return.

    Returns:
        The raw Chroma query result dict (ids, documents, metadatas,
        distances, etc. — see chromadb's Collection.query docs).

    Raises:
        Propagates any exception from the underlying Chroma query
        (e.g. if the collection doesn't exist / DB path is unreadable).
    """
    collection = _get_collection()

    return collection.query(
        query_texts=[query_text],
        n_results=n_results,
    )


if __name__ == "__main__":
    seed_vector_db()