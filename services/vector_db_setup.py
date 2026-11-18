"""
Vector store setup and retrieval for the Aeterna scoring pipeline.

Uses a persistent Chroma collection ("loc_formats") seeded from Library
of Congress (LoC) format-sustainability documents, embedded with a
local SentenceTransformer model.

The exact-match PostgreSQL lookup (services.build_lookup_db) answers
"what is this file's format?" from PRONOM, and it only carries LoC
sustainability data when PRONOM and LoC are cross-referenced, which
many formats are not. This module adds the context that lookup cannot
give:

  - same_format_family: LoC records describing the file's own format
    (or a close variant of it), matched on PUID / MIME type / extension
    and ranked by semantic similarity. These fill gaps when the lookup
    row has no linked LoC sustainability data.
  - related_formats: explicit LoC relationships (later/earlier
    versions, parent formats, equivalents) of those records.
  - comparable_formats: semantically similar formats in the same
    content category (e.g. other still-image formats for a .png), to
    position the file's format among its alternatives.

services.data_pipeline.get_system_data calls build_query_profile and
then get_vector_search once per request.
"""

import json
from functools import lru_cache

import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions

# Path to the source JSON of LoC format documents used to seed the
# vector store. Each record is expected to have "id", "metadata", and
# "text" keys (see seed_vector_db).
LOC_VECTOR_JSON = "data/loc_vector_docs.json"

# Structured LoC records (same IDs as LOC_VECTOR_JSON). Used to return
# clean, per-field sustainability data for each retrieved document
# instead of the full free-text blob.
LOC_FORMATS_JSON = "data/loc_formats.json"

# On-disk location of the persistent Chroma database.
VECTOR_DB_PATH = "./db/aeterna_vector_db"

# Name of the Chroma collection holding the LoC format documents.
COLLECTION_NAME = "loc_formats"

# SentenceTransformer model used to embed both the seeded documents
# and incoming queries. Must stay consistent across seeding and
# querying, since embeddings from different models aren't comparable.
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# Retrieval limits. The corpus is small (~600 docs), so every document
# is ranked per query and filtering happens in Python.
MAX_SAME_FORMAT = 3
MAX_RELATED = 6
MAX_COMPARABLE = 5

# Comparable formats below this cosine similarity are dropped as noise.
MIN_COMPARABLE_SIMILARITY = 0.35

# Same-format candidates matched only weakly (MIME type / extension,
# not linked by the lookup or by PUID) must also be semantically close,
# since extensions and MIME types are shared by unrelated formats.
MIN_WEAK_MATCH_SIMILARITY = 0.5

# Per-field character cap for text returned to the prompt.
FIELD_CHAR_LIMIT = 400

# Fallback mapping from a MIME top-level type to an LoC content
# category, used when no LoC record matches the file directly.
MIME_TOP_LEVEL_TO_CATEGORY = {
    "image": "still-image",
    "audio": "sound",
    "video": "moving-image",
    "text": "text",
    "model": "model",
}

# LoC relationship types worth surfacing, in priority order. Types
# such as "May contain" / "Used by" are too loose to be useful here.
RELATIONSHIP_PRIORITY = [
    "Has later version",
    "Has earlier version",
    "Equivalent to",
    "Subtype of",
    "Has modified version",
    "Modification of",
    "Extension of",
    "Affinity to",
]

# Match-strength ranks for same_format_family ordering.
_MATCH_STRENGTH = {
    "linked_by_lookup": 4,
    "pronom_puid": 3,
    "mime_type": 2,
    "extension": 1,
}


@lru_cache(maxsize=1)
def _get_collection():
    """Shared helper: connects to the persistent Chroma client and
    returns the loc_formats collection (created if it doesn't exist yet).

    Cached so the embedding model is loaded once per process instead of
    on every request."""
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


@lru_cache(maxsize=1)
def _load_loc_records():
    """Return {loc_id: structured LoC record}, loaded once per process."""
    with open(LOC_FORMATS_JSON, encoding="utf-8") as file:
        return {record["id"]: record for record in json.load(file)}


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

    with open(LOC_VECTOR_JSON, encoding="utf-8") as file:
        loc_vector = json.load(file)

    collection.add(
        ids=[record["id"] for record in loc_vector],
        metadatas=[_clean_metadata(record["metadata"]) for record in loc_vector],
        documents=[record["text"] for record in loc_vector]
    )

    print(f"Seeded '{COLLECTION_NAME}' with {len(loc_vector)} documents.")
    return collection


# --------------------------------------------------------------------------
# Query construction
# --------------------------------------------------------------------------

def _split_mime(value):
    if not value:
        return []
    return [m.strip().lower() for m in str(value).split(",") if m.strip()]


def build_query_profile(extension, format_rows, file_metadata):
    """
    Describe the uploaded file's format using everything already known
    from the exact-match lookup and ExifTool, so the vector query is
    about the actual format rather than a bare extension.

    Args:
        extension: File extension, with or without the leading dot.
        format_rows: Rows from build_lookup_db.lookup_by_extension
            (first row is the authoritative match). May be empty.
        file_metadata: ExifTool output (list of dicts). May be empty.

    Returns:
        A dict with extension, format_name, mime_types, pronom_puids,
        loc_ids (LoC records the lookup already linked),
        content_categories and the generated query_text.
    """
    ext = (extension or "").lower().lstrip(".")
    rows = list(format_rows or [])
    meta = (file_metadata or [{}])[0] or {}
    exif_mime = (meta.get("File:MIMEType") or "").lower()

    # An extension is often shared by unrelated formats (e.g. .png rows
    # include a DAISY/XML entry). ExifTool sniffs the file's content, so
    # keep only rows whose primary MIME type agrees with it, then rows
    # that merely list it (aggregate formats list many MIME types);
    # otherwise fall back to the authoritative (first) row alone.
    primary_match = [r for r in rows if exif_mime and _split_mime(r.get("mime_type"))[:1] == [exif_mime]]
    listed_match = [r for r in rows if exif_mime and exif_mime in _split_mime(r.get("mime_type"))]
    identifying_rows = primary_match or listed_match or rows[:1]

    mime_types, puids, loc_ids = [], [], []
    for row in identifying_rows:
        for mime in _split_mime(row.get("mime_type"))[:1]:
            if mime not in mime_types:
                mime_types.append(mime)
        puid = row.get("puid") or ""
        if puid.startswith("loc:"):
            loc_ids.append(puid.removeprefix("loc:"))
        elif puid:
            puids.append(puid)
        if row.get("loc_id") and row["loc_id"] not in loc_ids:
            loc_ids.append(row["loc_id"])

    if exif_mime and exif_mime not in mime_types:
        mime_types.insert(0, exif_mime)

    authoritative = identifying_rows[0] if identifying_rows else {}
    format_name = (
        authoritative.get("name")
        or meta.get("File:FileType")
        or (ext.upper() if ext else "Unknown format")
    )

    # Content category: prefer LoC's own categorisation of records the
    # lookup linked; otherwise derive it from the MIME top-level type.
    records = _load_loc_records()
    categories = []
    for loc_id in loc_ids:
        for category in records.get(loc_id, {}).get("content_categories", []):
            if category not in categories:
                categories.append(category)
    if not categories:
        for mime in mime_types:
            category = MIME_TOP_LEVEL_TO_CATEGORY.get(mime.split("/")[0])
            if category and category not in categories:
                categories.append(category)

    # Mirror the layout of the seeded documents' opening lines (name,
    # category, extensions, MIME, PUID) so the query embeds close to
    # documents describing the same kind of format.
    parts = [f"{format_name}."]
    if categories:
        parts.append(f"Content category: {', '.join(categories)}.")
    if ext:
        parts.append(f"Common file extensions: .{ext}.")
    if mime_types:
        parts.append(f"MIME type(s): {', '.join(mime_types[:3])}.")
    if puids:
        parts.append(f"PRONOM PUID: {', '.join(puids[:3])}.")

    return {
        "extension": ext,
        "format_name": format_name,
        "mime_types": mime_types,
        "pronom_puids": puids,
        "loc_ids": loc_ids,
        "content_categories": categories,
        "query_text": " ".join(parts),
    }


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------

def _collection_space(collection):
    """Distance function the collection was built with (default l2)."""
    try:
        hnsw = (collection.configuration or {}).get("hnsw") or {}
        space = hnsw.get("space") if isinstance(hnsw, dict) else getattr(hnsw, "space", None)
        if space:
            return str(getattr(space, "value", space))
    except Exception:
        pass
    return (collection.metadata or {}).get("hnsw:space", "l2")


def _to_similarity(distance, space):
    """Convert a Chroma distance to cosine similarity. all-MiniLM-L6-v2
    produces unit-length embeddings, so squared L2 = 2 - 2*cos."""
    if space == "l2":
        return 1.0 - distance / 2.0
    return 1.0 - distance


def _truncate(text, limit=FIELD_CHAR_LIMIT):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _match_reasons(record, profile):
    reasons = []
    if record["id"] in profile["loc_ids"]:
        reasons.append("linked_by_lookup")
    if set(record.get("pronom_puids") or []) & set(profile["pronom_puids"]):
        reasons.append("pronom_puid")
    record_mimes = {m.lower() for m in record.get("mime_types") or []}
    if record_mimes & set(profile["mime_types"]):
        reasons.append("mime_type")
    record_exts = {e.lower().lstrip(".") for e in record.get("extensions") or []}
    if profile["extension"] and profile["extension"] in record_exts:
        reasons.append("extension")
    return reasons


def _family_key(record):
    """Coarse grouping so comparable_formats isn't five variants of the
    same format (e.g. TIFF_UNC, TIFF_6, TIFF_G4 -> "TIFF")."""
    return (record.get("short_name") or record["id"]).split("_")[0].upper()


def _sustainability_fields(record, limit=FIELD_CHAR_LIMIT):
    sustainability = record.get("sustainability") or {}
    return {
        "disclosure": _truncate(sustainability.get("disclosure"), limit),
        "adoption": _truncate(sustainability.get("adoption"), limit),
        "licensing_and_patents": _truncate(sustainability.get("licensingAndPatents"), limit),
        "self_documentation": _truncate(sustainability.get("selfDocumentation"), limit),
        "external_dependencies": _truncate(sustainability.get("externalDependencies"), limit),
    }


def _summarize_record(record, similarity, **extra):
    return {
        "loc_id": record["id"],
        "short_name": record.get("short_name"),
        "full_name": record.get("full_name"),
        "content_categories": record.get("content_categories") or [],
        "extensions": record.get("extensions") or [],
        "draft_status": record.get("draft_status"),
        "similarity": round(similarity, 3),
        **extra,
        "description": _truncate(record.get("short_description") or record.get("description")),
        **_sustainability_fields(record),
    }


def _rank_all(query_text):
    """Return [(loc_id, cosine_similarity)] for every document, best first."""
    collection = _get_collection()
    total = collection.count()
    if total == 0:
        return []
    result = collection.query(
        query_texts=[query_text],
        n_results=total,
        include=["distances"],
    )
    space = _collection_space(collection)
    return [
        (doc_id, _to_similarity(distance, space))
        for doc_id, distance in zip(result["ids"][0], result["distances"][0])
    ]


def get_vector_search(profile):
    """
    Retrieve LoC context for one file's format.

    Args:
        profile: Output of build_query_profile.

    Returns:
        A dict with:
            query: the profile used (extension, name, MIME, PUIDs,
                content_categories, query_text).
            same_format_family: up to MAX_SAME_FORMAT LoC records that
                share a PUID / MIME type / extension with the file (or
                that the lookup linked), strongest match first, each
                with `match_reasons` and `linked_to_lookup`.
            related_formats: up to MAX_RELATED explicit LoC
                relationships of those records (e.g. "Has later
                version"), with the related format's adoption text.
            comparable_formats: up to MAX_COMPARABLE semantically
                similar formats in the same content category, one per
                format family, excluding same_format_family.

    Raises:
        Propagates any exception from the underlying Chroma query.
    """
    records = _load_loc_records()
    ranked = [(doc_id, sim) for doc_id, sim in _rank_all(profile["query_text"]) if doc_id in records]
    similarity = dict(ranked)

    # ---- same format family: identifier match, ranked semantically ----
    same_candidates = []
    for doc_id, sim in ranked:
        reasons = _match_reasons(records[doc_id], profile)
        if reasons:
            strength = max(_MATCH_STRENGTH[r] for r in reasons)
            if strength < _MATCH_STRENGTH["pronom_puid"] and sim < MIN_WEAK_MATCH_SIMILARITY:
                continue
            same_candidates.append((strength, sim, doc_id, reasons))
    same_candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)

    same_format_family = [
        _summarize_record(
            records[doc_id],
            sim,
            match_reasons=reasons,
            linked_to_lookup="linked_by_lookup" in reasons,
        )
        for _, sim, doc_id, reasons in same_candidates[:MAX_SAME_FORMAT]
    ]
    same_ids = {c[2] for c in same_candidates}

    # Content category: from matched records if the profile had none.
    categories = set(profile["content_categories"])
    if not categories:
        for entry in same_format_family:
            categories.update(entry["content_categories"])

    # ---- related formats: explicit LoC relationships ----
    related, seen_related = [], set()
    for entry in same_format_family:
        for rel in records[entry["loc_id"]].get("relationships") or []:
            rel_type, rel_id = rel.get("type"), rel.get("related_id")
            if rel_type not in RELATIONSHIP_PRIORITY or rel_id in same_ids or rel_id in seen_related:
                continue
            target = records.get(rel_id, {})
            short_name = rel.get("related_short_name") or target.get("short_name")
            title = (rel.get("related_title") or target.get("full_name") or "").strip()
            if not (short_name or title):
                continue
            seen_related.add(rel_id)
            related.append({
                "relationship": rel_type,
                "of": entry["short_name"],
                "loc_id": rel_id,
                "short_name": short_name,
                "title": title,
                "adoption": _truncate((target.get("sustainability") or {}).get("adoption"), 250),
            })
    related.sort(key=lambda r: RELATIONSHIP_PRIORITY.index(r["relationship"]))
    related = related[:MAX_RELATED]

    # ---- comparable formats: same category, semantically close ----
    comparable, seen_families = [], {_family_key(records[i]) for i in same_ids}
    for doc_id, sim in ranked:
        if len(comparable) >= MAX_COMPARABLE or sim < MIN_COMPARABLE_SIMILARITY:
            break
        record = records[doc_id]
        if doc_id in same_ids:
            continue
        # No known category means nothing to compare against: skip.
        if not categories & set(record.get("content_categories") or []):
            continue
        family = _family_key(record)
        if family in seen_families:
            continue
        seen_families.add(family)
        comparable.append(_summarize_record(record, sim))

    return {
        "query": {**profile, "content_categories": sorted(categories)},
        "same_format_family": same_format_family,
        "related_formats": related,
        "comparable_formats": comparable,
    }


if __name__ == "__main__":
    seed_vector_db()
