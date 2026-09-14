"""
Builds the Aeterna file-format lookup database (SQLite) from the
PRONOM + Library of Congress exports, plus PRONOM lifecycle/supersession
data scraped separately (see pronom_lifecycle_scraper.py).

Expected input files, all in the same directory as this script or
update the paths below:
  - pronom_formats.json     core PRONOM identity data (puid, name, mime, ext)
  - pronom_lifecycle.json   release/withdrawn dates + supersession relationships
  - loc_formats.json        Library of Congress sustainability prose

Usage:
  python3 build_lookup_db.py
Produces:
  format_lookup.db
"""

import json
import sqlite3

# Path to the raw PRONOM format-identity export (puid, name, mime, ext).
PRONOM_JSON = "data/pronom_formats.json"

# Path to the PRONOM lifecycle export (release/withdrawn dates,
# is_superseded flag, and related-format relationships), keyed by puid.
PRONOM_LIFECYCLE_JSON = "data/pronom_lifecycle.json"

# Path to the Library of Congress format-sustainability export.
LOC_JSON = "data/loc_formats.json"

# Output SQLite database path. build() drops and recreates all tables
# here on every run, so this is fully regenerated, not incrementally
# updated.
DB_PATH = "db/format_lookup.sqlite3"


def build():
    """
    Rebuild the Aeterna file-format lookup database from scratch.

    Drops and recreates all four tables (formats, extensions,
    format_relationships, loc_formats), then:
      1. Seeds `formats`, `extensions`, and `format_relationships` from
         PRONOM_JSON, joined against PRONOM_LIFECYCLE_JSON for
         release/withdrawn dates and supersession info.
      2. Seeds `loc_formats` from LOC_JSON, and cross-references it
         into `formats.loc_id` (or, for LoC entries with no PRONOM
         match, inserts a synthetic `loc:<id>` row into `formats` so
         the format is still lookupable by extension).
      3. Prints row counts as a basic build sanity check.

    Side effects:
        Overwrites the database file at DB_PATH.

    Raises:
        Propagates any exception from reading the input JSON files
        (e.g. FileNotFoundError if they haven't been generated/placed
        yet) or from the SQLite operations.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Database Schema
    cursor.executescript(
        """
        DROP TABLE IF EXISTS extensions;
        DROP TABLE IF EXISTS format_relationships;
        DROP TABLE IF EXISTS formats;
        DROP TABLE IF EXISTS loc_formats;

        CREATE TABLE formats (
            puid TEXT PRIMARY KEY,
            name TEXT,
            version TEXT,
            mime_type TEXT,
            has_binary_signature INTEGER,
            loc_id TEXT,
            release_date TEXT,
            withdrawn_date TEXT,
            last_updated_date TEXT,
            is_superseded INTEGER DEFAULT 0
        );

        CREATE TABLE extensions (
            extension TEXT NOT NULL,
            puid TEXT NOT NULL,
            PRIMARY KEY (extension, puid)
        );

        CREATE TABLE format_relationships (
            puid TEXT NOT NULL,
            relationship_type TEXT,
            related_puid TEXT,
            related_format_name TEXT,
            related_format_version TEXT
        );

        CREATE TABLE loc_formats (
            id TEXT PRIMARY KEY,
            short_name TEXT,
            full_name TEXT,
            draft_status TEXT,
            disclosure TEXT,
            adoption TEXT,
            licensing_and_patents TEXT,
            self_documentation TEXT,
            external_dependencies TEXT,
            technical_protection TEXT
        );

        CREATE INDEX idx_extensions_ext ON extensions(extension);
        CREATE INDEX idx_formats_loc_id ON formats(loc_id);
        CREATE INDEX idx_relationships_puid ON format_relationships(puid);
        """
    )

    # Load lifecycle data up front so it can be joined in during the
    # initial PRONOM insert rather than as a separate UPDATE pass.
    lifecycle_records = json.load(open(PRONOM_LIFECYCLE_JSON))

    # Seed PRONOM Tables
    pronom_records = json.load(open(PRONOM_JSON))
    for record in pronom_records:
        puid = record["puid"]
        name = record["name"]
        version = record["version"]
        mime_type = record["mime_type"]
        has_binary_signature = int(record["has_binary_signature"])

        lifecycle = lifecycle_records.get(puid, {})
        release_date = lifecycle.get("release_date")
        withdrawn_date = lifecycle.get("withdrawn_date")
        last_updated_date = lifecycle.get("last_updated_date")
        is_superseded = int(bool(lifecycle.get("is_superseded", False)))

        cursor.execute(
            "INSERT OR REPLACE INTO formats "
            "(puid, name, version, mime_type, has_binary_signature, loc_id, "
            " release_date, withdrawn_date, last_updated_date, is_superseded) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (
                puid,
                name,
                version,
                mime_type,
                has_binary_signature,
                release_date,
                withdrawn_date,
                last_updated_date,
                is_superseded,
            ),
        )

        for extension in record["extensions"]:
            cursor.execute(
                "INSERT OR IGNORE INTO extensions (extension, puid) VALUES (?, ?)",
                (
                    extension.lower(),
                    puid
                ),
            )

        for rel in lifecycle.get("related_formats", []):
            cursor.execute(
                "INSERT INTO format_relationships "
                "(puid, relationship_type, related_puid, related_format_name, "
                " related_format_version) VALUES (?, ?, ?, ?, ?)",
                (
                    puid,
                    rel.get("relationship_type"),
                    rel.get("related_format_id"),
                    rel.get("related_format_name"),
                    rel.get("related_format_version"),
                ),
            )

    # Seed LoC Table and Cross-reference PRONOM
    loc_records = json.load(open(LOC_JSON))
    for records in loc_records:
        sustainability = records["sustainability"]
        loc_id = records["id"]
        short_name = records["short_name"]
        full_name = records["full_name"]
        draft_status = records["draft_status"]
        disclosure = sustainability.get("disclosure", "")
        adoption = sustainability.get("adoption", "")
        licensing_and_patents = sustainability.get("licensingAndPatents", "")
        self_documentation = sustainability.get("selfDocumentation", "")
        external_dependencies = sustainability.get("externalDependencies", "")
        technical_protection = sustainability.get("technicalProtectionConsiderations", "")

        cursor.execute(
            """
            INSERT OR REPLACE INTO loc_formats
               (id, short_name, full_name, draft_status, disclosure, adoption,
                licensing_and_patents, self_documentation, external_dependencies,
                technical_protection)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                loc_id,
                short_name,
                full_name,
                draft_status,
                disclosure,
                adoption,
                licensing_and_patents,
                self_documentation,
                external_dependencies,
                technical_protection
            ),
        )

        # Cross-ref PRONOM
        for xref in records.get("pronom_cross_ref", []):
            puid = xref["puid"]
            cursor.execute(
                "UPDATE formats SET loc_id = ? WHERE puid = ?",
                (
                    loc_id,
                    puid
                ),
            )

        if not records.get("pronom_puids"):
            # This LoC format has no matching PRONOM entry at all, so
            # there's no puid to cross-reference into `formats`. Give
            # it a synthetic puid ("loc:<id>") and insert a minimal
            # `formats` row + its extensions directly, so the format
            # is still reachable via lookup_by_extension() even though
            # it only exists in the LoC dataset.
            synthetic_puid = f"loc:{loc_id}"
            cursor.execute(
                "INSERT OR IGNORE INTO formats "
                "(puid, name, version, mime_type, has_binary_signature, loc_id) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (
                    synthetic_puid,
                    full_name,
                    "",
                    ", ".join(records.get("mime_types", [])),
                    loc_id
                ),
            )
            for ext in records.get("extensions", []):
                cursor.execute(
                    "INSERT OR IGNORE INTO extensions (extension, puid) VALUES (?, ?)",
                    (
                        ext.lower(),
                        synthetic_puid
                    ),
                )

    conn.commit()

    cursor.execute("SELECT COUNT(*) FROM formats")
    print(f"formats table: {cursor.fetchone()[0]} rows")
    cursor.execute("SELECT COUNT(*) FROM extensions")
    print(f"extensions table: {cursor.fetchone()[0]} rows")
    cursor.execute("SELECT COUNT(*) FROM formats WHERE loc_id IS NOT NULL")
    print(f"formats with LoC cross-reference: {cursor.fetchone()[0]}")
    cursor.execute("SELECT COUNT(*) FROM formats WHERE release_date IS NOT NULL")
    print(f"formats with a release_date: {cursor.fetchone()[0]}")
    cursor.execute("SELECT COUNT(*) FROM formats WHERE is_superseded = 1")
    print(f"formats flagged is_superseded: {cursor.fetchone()[0]}")
    cursor.execute("SELECT COUNT(*) FROM format_relationships")
    print(f"format_relationships table: {cursor.fetchone()[0]} rows")

    conn.close()
    print(f"\nBuilt {DB_PATH}")


def lookup_by_extension(ext: str):
    """
    Look up all known formats matching a file extension.

    This is the query used by the live scoring pipeline (called from
    services.data_pipeline.get_system_data) — the DB itself must
    already exist (built ahead of time via build()).

    Args:
        ext: File extension to look up. Leading dot and case are
            normalized automatically (".PNG", "png", and ".png" are
            all equivalent).

    Returns:
        A list of dict rows (one per matching format), each with:
            puid, name, mime_type, has_binary_signature,
            release_date, withdrawn_date, is_superseded,
            disclosure, adoption, licensing_and_patents,
            external_dependencies, technical_protection
        Ordered so that non-superseded formats (is_superseded=0) come
        first — callers should treat the first row as the
        authoritative match when multiple rows are returned. Returns
        an empty list if no format matches the extension.

    Raises:
        Propagates any sqlite3 error (e.g. if DB_PATH doesn't exist
        because build() hasn't been run yet).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT f.puid, f.name, f.mime_type, f.has_binary_signature,
               f.release_date, f.withdrawn_date, f.is_superseded,
               l.disclosure, l.adoption, l.licensing_and_patents,
               l.external_dependencies, l.technical_protection
        FROM extensions e
        JOIN formats f ON e.puid = f.puid
        LEFT JOIN loc_formats l ON f.loc_id = l.id
        WHERE e.extension = ?
        ORDER BY f.is_superseded ASC
        """,
        (ext.lower().lstrip(".") ,),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_relationships(puid: str):
    """
    Fetch known relationships (e.g. supersession, migration paths) for
    a given PUID.

    Not currently called by the live scoring pipeline (see
    services.data_pipeline.get_system_data) — available for future
    use or manual inspection of the `format_relationships` table
    populated by build().

    Args:
        puid: The PRONOM format identifier to look up relationships
            for (e.g. "fmt/13").

    Returns:
        A list of dict rows, each with:
            relationship_type, related_puid, related_format_name,
            related_format_version
        Returns an empty list if the PUID has no recorded
        relationships.

    Raises:
        Propagates any sqlite3 error (e.g. if DB_PATH doesn't exist
        because build() hasn't been run yet).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT relationship_type, related_puid, related_format_name, "
        "related_format_version FROM format_relationships WHERE puid = ?",
        (puid,),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


if __name__ == "__main__":
    build()