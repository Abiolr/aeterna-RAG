import chromadb
from chromadb.utils import embedding_functions
import json

LOC_VECTOR_JSON = "data/loc_vector_docs.json"
VECTOR_DB_PATH = "./aeterna_vector_db"
COLLECTION_NAME = "loc_formats"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"


def _get_collection():
    """Shared helper: connects to the persistent Chroma client and
    returns the loc_formats collection (created if it doesn't exist yet)."""
    client = chromadb.PersistentClient(path=VECTOR_DB_PATH)

    sentence_transformer_ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL
    )

    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=sentence_transformer_ef
    )


def _clean_metadata(metadata):
    return {
        key: value
        for key, value in metadata.items()
        if value != []
    }


def seed_vector_db(force: bool = False):
    """
    One-time setup step: loads LOC format docs from LOC_VECTOR_JSON and
    adds them to the collection. Safe to call multiple times — skips
    re-seeding if the collection already has data, unless force=True.

    Run this once during setup (e.g. from a setup script), not on every
    pipeline request.
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
    """
    collection = _get_collection()

    return collection.query(
        query_texts=[query_text],
        n_results=n_results,
    )


if __name__ == "__main__":
    seed_vector_db()