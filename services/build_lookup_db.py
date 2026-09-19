"""
Build the Aeterna file-format lookup database in PostgreSQL.

Inputs:
  - data/pronom_formats.json
  - data/pronom_lifecycle.json
  - data/loc_formats.json

Usage:
  python3 services/build_lookup_db.py

Requires:
  POSTGRES_URL in .env or the environment.
"""

import json
import os

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row


load_dotenv()

POSTGRES_URL = os.getenv("POSTGRES_URL")

if not POSTGRES_URL:
    raise RuntimeError(
        "POSTGRES_URL is not set. Add it to your .env file or environment."
    )


PRONOM_JSON = "data/pronom_formats.json"
PRONOM_LIFECYCLE_JSON = "data/pronom_lifecycle.json"
LOC_JSON = "data/loc_formats.json"


def get_connection():
    print("Connecting to PostgreSQL...", flush=True)

    conn = psycopg.connect(POSTGRES_URL)

    print(
        f"Connected: host={conn.info.host} "
        f"port={conn.info.port} "
        f"db={conn.info.dbname} "
        f"user={conn.info.user}",
        flush=True,
    )

    return conn


def create_schema(conn):
    print("Creating PostgreSQL schema...", flush=True)

    with conn.cursor() as cursor:
        cursor.execute(
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

            CREATE INDEX idx_extensions_ext
                ON extensions(extension);

            CREATE INDEX idx_formats_loc_id
                ON formats(loc_id);

            CREATE INDEX idx_relationships_puid
                ON format_relationships(puid);
            """
        )

    # Commit the schema separately so the tables become visible immediately
    # even while the large data import is still running.
    conn.commit()

    print("Schema created.", flush=True)


def build():
    conn = get_connection()

    try:
        create_schema(conn)

        print("Loading JSON files...", flush=True)

        with open(PRONOM_LIFECYCLE_JSON, encoding="utf-8") as file:
            lifecycle_records = json.load(file)

        with open(PRONOM_JSON, encoding="utf-8") as file:
            pronom_records = json.load(file)

        with open(LOC_JSON, encoding="utf-8") as file:
            loc_records = json.load(file)

        print(
            f"Loaded {len(pronom_records):,} PRONOM records, "
            f"{len(loc_records):,} LoC records.",
            flush=True,
        )

        # ------------------------------------------------------------------
        # Seed PRONOM tables
        # ------------------------------------------------------------------
        print("Importing PRONOM records...", flush=True)

        with conn.cursor() as cursor:
            for i, record in enumerate(pronom_records, start=1):
                puid = record["puid"]
                name = record["name"]
                version = record["version"]
                mime_type = record["mime_type"]
                has_binary_signature = int(record["has_binary_signature"])

                lifecycle = lifecycle_records.get(puid, {})

                release_date = lifecycle.get("release_date")
                withdrawn_date = lifecycle.get("withdrawn_date")
                last_updated_date = lifecycle.get("last_updated_date")
                is_superseded = int(
                    bool(lifecycle.get("is_superseded", False))
                )

                cursor.execute(
                    """
                    INSERT INTO formats
                        (
                            puid,
                            name,
                            version,
                            mime_type,
                            has_binary_signature,
                            loc_id,
                            release_date,
                            withdrawn_date,
                            last_updated_date,
                            is_superseded
                        )
                    VALUES (
                        %s, %s, %s, %s, %s, NULL,
                        %s, %s, %s, %s
                    )
                    ON CONFLICT (puid) DO UPDATE SET
                        name = EXCLUDED.name,
                        version = EXCLUDED.version,
                        mime_type = EXCLUDED.mime_type,
                        has_binary_signature =
                            EXCLUDED.has_binary_signature,
                        release_date = EXCLUDED.release_date,
                        withdrawn_date = EXCLUDED.withdrawn_date,
                        last_updated_date = EXCLUDED.last_updated_date,
                        is_superseded = EXCLUDED.is_superseded
                    """,
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

                for extension in record.get("extensions", []):
                    cursor.execute(
                        """
                        INSERT INTO extensions (extension, puid)
                        VALUES (%s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            extension.lower(),
                            puid,
                        ),
                    )

                for rel in lifecycle.get("related_formats", []):
                    cursor.execute(
                        """
                        INSERT INTO format_relationships
                            (
                                puid,
                                relationship_type,
                                related_puid,
                                related_format_name,
                                related_format_version
                            )
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            puid,
                            rel.get("relationship_type"),
                            rel.get("related_format_id"),
                            rel.get("related_format_name"),
                            rel.get("related_format_version"),
                        ),
                    )

                if i % 1000 == 0 or i == len(pronom_records):
                    print(
                        f"  PRONOM: {i:,}/{len(pronom_records):,}",
                        flush=True,
                    )

        conn.commit()
        print("PRONOM import committed.", flush=True)

        # ------------------------------------------------------------------
        # Seed LoC table and cross-reference PRONOM
        # ------------------------------------------------------------------
        print("Importing Library of Congress records...", flush=True)

        with conn.cursor() as cursor:
            for i, records in enumerate(loc_records, start=1):
                sustainability = records["sustainability"]

                loc_id = records["id"]
                short_name = records["short_name"]
                full_name = records["full_name"]
                draft_status = records["draft_status"]

                disclosure = sustainability.get("disclosure", "")
                adoption = sustainability.get("adoption", "")
                licensing_and_patents = sustainability.get(
                    "licensingAndPatents", ""
                )
                self_documentation = sustainability.get(
                    "selfDocumentation", ""
                )
                external_dependencies = sustainability.get(
                    "externalDependencies", ""
                )
                technical_protection = sustainability.get(
                    "technicalProtectionConsiderations", ""
                )

                cursor.execute(
                    """
                    INSERT INTO loc_formats
                        (
                            id,
                            short_name,
                            full_name,
                            draft_status,
                            disclosure,
                            adoption,
                            licensing_and_patents,
                            self_documentation,
                            external_dependencies,
                            technical_protection
                        )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        short_name = EXCLUDED.short_name,
                        full_name = EXCLUDED.full_name,
                        draft_status = EXCLUDED.draft_status,
                        disclosure = EXCLUDED.disclosure,
                        adoption = EXCLUDED.adoption,
                        licensing_and_patents =
                            EXCLUDED.licensing_and_patents,
                        self_documentation =
                            EXCLUDED.self_documentation,
                        external_dependencies =
                            EXCLUDED.external_dependencies,
                        technical_protection =
                            EXCLUDED.technical_protection
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
                        technical_protection,
                    ),
                )

                # Cross-reference PRONOM
                for xref in records.get("pronom_cross_ref", []):
                    cursor.execute(
                        """
                        UPDATE formats
                        SET loc_id = %s
                        WHERE puid = %s
                        """,
                        (
                            loc_id,
                            xref["puid"],
                        ),
                    )

                # LoC-only format: create a synthetic PUID.
                if not records.get("pronom_puids"):
                    synthetic_puid = f"loc:{loc_id}"

                    cursor.execute(
                        """
                        INSERT INTO formats
                            (
                                puid,
                                name,
                                version,
                                mime_type,
                                has_binary_signature,
                                loc_id
                            )
                        VALUES (%s, %s, %s, %s, 0, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            synthetic_puid,
                            full_name,
                            "",
                            ", ".join(records.get("mime_types", [])),
                            loc_id,
                        ),
                    )

                    for ext in records.get("extensions", []):
                        cursor.execute(
                            """
                            INSERT INTO extensions (extension, puid)
                            VALUES (%s, %s)
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                ext.lower(),
                                synthetic_puid,
                            ),
                        )

                if i % 1000 == 0 or i == len(loc_records):
                    print(
                        f"  LoC: {i:,}/{len(loc_records):,}",
                        flush=True,
                    )

        conn.commit()
        print("LoC import committed.", flush=True)

        # ------------------------------------------------------------------
        # Sanity checks
        # ------------------------------------------------------------------
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM formats")
            formats_count = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM extensions")
            extensions_count = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM formats WHERE loc_id IS NOT NULL"
            )
            loc_cross_ref_count = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM formats "
                "WHERE release_date IS NOT NULL"
            )
            release_date_count = cursor.fetchone()[0]

            cursor.execute(
                "SELECT COUNT(*) FROM formats "
                "WHERE is_superseded = 1"
            )
            superseded_count = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM format_relationships")
            relationships_count = cursor.fetchone()[0]

        print("\nBuild complete.", flush=True)
        print(f"formats table: {formats_count:,} rows", flush=True)
        print(f"extensions table: {extensions_count:,} rows", flush=True)
        print(
            f"formats with LoC cross-reference: "
            f"{loc_cross_ref_count:,}",
            flush=True,
        )
        print(
            f"formats with a release_date: "
            f"{release_date_count:,}",
            flush=True,
        )
        print(
            f"formats flagged is_superseded: "
            f"{superseded_count:,}",
            flush=True,
        )
        print(
            f"format_relationships table: "
            f"{relationships_count:,} rows",
            flush=True,
        )

    except Exception:
        conn.rollback()
        print(
            "\nBuild failed. Transaction rolled back.",
            flush=True,
        )
        raise

    finally:
        conn.close()
        print("PostgreSQL connection closed.", flush=True)


def lookup_by_extension(ext: str):
    """
    Look up all known formats matching a file extension.

    Returns:
        A list of dictionaries.
    """
    with psycopg.connect(
        POSTGRES_URL,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    f.puid,
                    f.name,
                    f.mime_type,
                    f.has_binary_signature,
                    f.release_date,
                    f.withdrawn_date,
                    f.is_superseded,
                    l.disclosure,
                    l.adoption,
                    l.licensing_and_patents,
                    l.external_dependencies,
                    l.technical_protection
                FROM extensions e
                JOIN formats f
                    ON e.puid = f.puid
                LEFT JOIN loc_formats l
                    ON f.loc_id = l.id
                WHERE e.extension = %s
                ORDER BY f.is_superseded ASC
                """,
                (ext.lower().lstrip("."),),
            )

            return cursor.fetchall()


def get_relationships(puid: str):
    """
    Fetch known relationships for a given PUID.

    Returns:
        A list of dictionaries.
    """
    with psycopg.connect(
        POSTGRES_URL,
        row_factory=dict_row,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    relationship_type,
                    related_puid,
                    related_format_name,
                    related_format_version
                FROM format_relationships
                WHERE puid = %s
                """,
                (puid,),
            )

            return cursor.fetchall()


if __name__ == "__main__":
    build()