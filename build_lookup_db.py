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

PRONOM_JSON = "data/pronom_formats.json"
PRONOM_LIFECYCLE_JSON = "data/pronom_lifecycle.json"
LOC_JSON = "data/loc_formats.json"
DB_PATH = "format_lookup.sqlite3"

def build():
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
    """Example query function: what your upload handler would call."""
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
        """,
        (ext.lower().lstrip(".") ,),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_relationships(puid: str):
    """Fetch known relationships (e.g. supersession) for a given PUID."""
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