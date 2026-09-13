"""
Scrapes per-format lifecycle data (ReleaseDate, WithdrawnDate,
LastUpdatedDate, and supersession relationships) from the PRONOM beta
site's per-PUID XML export: https://pronom.nationalarchives.gov.uk/{puid}.xml

This fills the age_lifecycle_health and supersession_flag gaps in
Aeterna's scoring pipeline with authoritative, structured PRONOM data,
instead of LLM-inferred estimates from prose.

Must be run somewhere with real internet access (not a sandboxed
environment) — the target domain is a government-run service, so this
script is deliberately rate-limited and polite.

Usage:
    python3 pronom_lifecycle_scraper.py

Reads:
    pronom_formats.json   (existing PUID list — only needs the 'puid' field)

Writes (incrementally, safe to interrupt/resume):
    raw_xml/{puid_safe}.xml         one raw XML file per format (cache)
    pronom_lifecycle.json           parsed output, keyed by puid
    pronom_lifecycle_failures.json  list of PUIDs that failed, for retry
"""

import json
import time
import re
import os
from pathlib import Path

import requests
from bs4 import BeautifulSoup

INPUT_JSON = "data/pronom_formats.json"
RAW_XML_DIR = Path("raw_xml")
OUTPUT_JSON = "data/pronom_lifecycle.json"
FAILURES_JSON = "data/pronom_lifecycle_failures.json"

BASE_URL = "https://pronom.nationalarchives.gov.uk/{puid}.xml"
REQUEST_DELAY_SECONDS = 1.0   # politeness delay between requests
TIMEOUT_SECONDS = 15
MAX_RETRIES = 2

HEADERS = {
    "User-Agent": "aeterna-rag-research-scraper/1.0 (educational project; "
                  "one-time polite crawl; contact: set-your-email-here)"
}


def safe_filename(puid: str) -> str:
    """PUIDs look like 'fmt/1' or 'x-fmt/263' — sanitize for use as a filename."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", puid)


def load_puid_list() -> list[str]:
    with open(INPUT_JSON) as f:
        records = json.load(f)
    return [r["puid"] for r in records]


def load_existing_output() -> dict:
    if os.path.exists(OUTPUT_JSON):
        with open(OUTPUT_JSON) as f:
            return json.load(f)
    return {}


def save_output(data: dict):
    with open(OUTPUT_JSON, "w") as f:
        json.dump(data, f, indent=2)


def save_failures(failures: list[str]):
    with open(FAILURES_JSON, "w") as f:
        json.dump(failures, f, indent=2)


def fetch_raw_xml(puid: str) -> str | None:
    """Fetch XML for a PUID, using the on-disk cache if present."""
    cache_path = RAW_XML_DIR / f"{safe_filename(puid)}.xml"

    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    url = BASE_URL.format(puid=puid)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)
            if resp.status_code == 200:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(resp.text, encoding="utf-8")
                return resp.text
            else:
                print(f"  [{puid}] HTTP {resp.status_code} (attempt {attempt})")
        except requests.RequestException as e:
            print(f"  [{puid}] request error: {e} (attempt {attempt})")

        if attempt < MAX_RETRIES:
            time.sleep(REQUEST_DELAY_SECONDS * 2)  # back off a bit before retry

    return None


def text_or_none(el) -> str | None:
    """bs4 element -> stripped text, or None if missing/blank."""
    if el is None:
        return None
    val = el.get_text(strip=True)
    return val if val else None


def parse_lifecycle(xml_text: str) -> dict:
    """Extract lifecycle + supersession fields from one format's XML."""
    soup = BeautifulSoup(xml_text, "lxml-xml")

    fmt = soup.find("FileFormat")
    if fmt is None:
        return {}

    related_formats = []
    for rel in fmt.find_all("RelatedFormat"):
        related_formats.append({
            "relationship_type": text_or_none(rel.find("RelationshipType")),
            "related_format_id": text_or_none(rel.find("RelatedFormatID")),
            "related_format_name": text_or_none(rel.find("RelatedFormatName")),
            "related_format_version": text_or_none(rel.find("RelatedFormatVersion")),
        })

    # Direct supersession signals worth flagging as a convenience boolean.
    is_superseded = any(
        r["relationship_type"] == "Is previous version of"
        for r in related_formats
    )

    return {
        "format_id": text_or_none(fmt.find("FormatID")),
        "format_name": text_or_none(fmt.find("FormatName")),
        "format_version": text_or_none(fmt.find("FormatVersion")),
        "release_date": text_or_none(fmt.find("ReleaseDate")),
        "withdrawn_date": text_or_none(fmt.find("WithdrawnDate")),
        "last_updated_date": text_or_none(fmt.find("LastUpdatedDate")),
        "is_superseded": is_superseded,
        "related_formats": related_formats,
    }


def main():
    puids = load_puid_list()
    output = load_existing_output()
    failures = []

    remaining = [p for p in puids if p not in output]
    print(f"Total PUIDs: {len(puids)} | Already cached/parsed: {len(output)} "
          f"| Remaining: {len(remaining)}")

    for i, puid in enumerate(remaining, start=1):
        print(f"[{i}/{len(remaining)}] Fetching {puid}...")

        xml_text = fetch_raw_xml(puid)
        if xml_text is None:
            failures.append(puid)
            continue

        try:
            parsed = parse_lifecycle(xml_text)
            output[puid] = parsed
        except Exception as e:
            print(f"  [{puid}] parse error: {e}")
            failures.append(puid)
            continue

        # Save incrementally every 25 records so progress isn't lost on interrupt.
        if i % 25 == 0:
            save_output(output)
            save_failures(failures)
            print(f"  ...progress saved ({len(output)} parsed so far)")

        time.sleep(REQUEST_DELAY_SECONDS)

    save_output(output)
    save_failures(failures)

    print(f"\nDone. Parsed: {len(output)} | Failed: {len(failures)}")
    if failures:
        print(f"Failed PUIDs written to {FAILURES_JSON} — re-run this script "
              f"to retry them (succeeded ones are cached and will be skipped).")


if __name__ == "__main__":
    main()
