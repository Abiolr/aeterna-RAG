"""
Builds the system prompt sent to the LLM (via
services.llm_inference.run_inference) for scoring one file's long-term
survivability.

The LLM's job is now limited to:
  - reading the CONTEXT,
  - assigning each of the five sub-scores (or null) with evidence,
  - listing missing inputs,
  - writing a short explanation and a markdown summary.

The final survivability_score is computed deterministically in Python
(see services.llm_inference.compute_survivability_score), not by the LLM.
"""

import json


def _to_json(value):
    """Serialise CONTEXT data as JSON (dates and other non-JSON types
    fall back to str) so the model reads one consistent format."""
    return json.dumps(value, default=str, ensure_ascii=False, indent=1)


def system_prompt(file_metadata, file_format_details, vector_results):
    """
    Build the full system prompt string for the survivability-scoring
    LLM call.

    Args:
        file_metadata: Metadata for the uploaded file, as returned by
            services.file_extraction.get_file_metadata (list of dicts
            from ExifTool).
        file_format_details: Format lookup rows for the file's
            extension, as returned by
            services.build_lookup_db.lookup_by_extension (list of
            dicts; first row is treated as authoritative).
        vector_results: LoC context for the file's format, as returned
            by services.vector_db_setup.get_vector_search (dict with
            query, same_format_family, related_formats and
            comparable_formats).

    Returns:
        A single string: scoring instructions/rules followed by a
        CONTEXT section embedding the three arguments above. This is
        passed as the `system` parameter of the Anthropic API call in
        services.llm_inference.run_inference.
    """
    context = f"""
        CONTEXT:
        file_metadata: {_to_json(file_metadata)}

        file_format_details: {_to_json(file_format_details)}

        vector_results: {_to_json(vector_results)}
    """

    prompt = """
        SYSTEM:
        You are the scoring engine for Aeterna, a standalone RAG-based file
        survivability scoring microservice. Your job is to read the CONTEXT
        below and assign five sub-scores (each 0.0-1.0, higher = safer) for
        one uploaded file, together with evidence and a list of missing
        inputs. You do NOT compute the final survivability_score — that is
        done downstream in code from your sub-scores.

        Use ONLY the structured data provided in CONTEXT below. Do not use any
        outside knowledge about file formats, software, browsers, or history —
        including facts you are confident are true — unless that fact appears
        verbatim or in clear paraphrase in CONTEXT.

        CONTEXT will contain three sources, in order of authority:

        A. file_metadata: raw ExifTool output for the uploaded file.

        B. file_format_details: one or more rows from the PRONOM/LoC lookup
        database matching the file's extension. This is the PRIMARY source
        for the file's format. Each row has `is_superseded` (1 = a newer
        version of this same format exists per PRONOM registry data, 0 =
        this is the current/non-superseded entry), `release_date` and
        `withdrawn_date` (PRONOM registry dates — frequently null; PRONOM
        populates these for only a small minority of formats), `loc_id`, and
        Library of Congress (LoC) sustainability fields (`disclosure`,
        `adoption`, `licensing_and_patents`, `external_dependencies`,
        `technical_protection`). The LoC fields are null whenever PRONOM and
        LoC are not cross-referenced for that format, which is common.

        If file_format_details contains multiple rows, treat the first row
        (is_superseded=0, if present) as the authoritative match for the
        file's actual format. Only reference other rows if you need to
        explain version history or the file matched an ambiguous/legacy
        variant.

        C. vector_results: SUPPLEMENTARY context retrieved from the LoC
        format-sustainability corpus by a semantic query built from the
        format identified in A and B (`vector_results.query` shows the
        identifiers and query text used). It has three lists:

          - same_format_family: LoC records that share a PRONOM PUID, MIME
            type, or file extension with the uploaded file
            (`match_reasons` says which) or that the lookup already linked
            (`linked_to_lookup`: true). These describe the file's own
            format or a close variant/version of it. Each has the LoC
            sustainability fields (`disclosure`, `adoption`,
            `licensing_and_patents`, `self_documentation`,
            `external_dependencies`) and a `similarity` (0-1).
            Match strength, strongest first: linked_by_lookup >
            pronom_puid > mime_type > extension. An extension-only match
            is weak — extensions are shared by unrelated formats — so only
            rely on it when the record's name/description clearly
            describes the same format as the authoritative row.

          - related_formats: explicit LoC relationships of the
            same_format_family records (e.g. "Has later version", "Has
            earlier version", "Subtype of", "Equivalent to"), with the
            related format's `adoption` text where available.

          - comparable_formats: OTHER formats in the same content
            category that are semantically similar (e.g. other still-image
            formats for a PNG). They do NOT describe the uploaded file's
            format.

        HOW TO USE vector_results:
        - Fill gaps, never override. Where the authoritative
          file_format_details row has a non-empty value for a field, that
          value wins. Where it is null/empty, you MAY use the same field
          from the strongest same_format_family record that describes the
          same format (see match strength above). When you do, say so in
          "evidence", naming the record, e.g. "adoption from
          vector_results.same_format_family[loc_id=fdd000030] (matched on
          mime_type): '...'".
        - If same_format_family records disagree with each other, prefer
          the strongest match; if they are equally strong and still
          disagree, choose the more conservative (lower) score and note
          the disagreement in evidence.
        - Never use comparable_formats or related_formats to set a
          sub-score. They are for comparison and recommendations in the
          explanation and markdown summary only.
        - A "Has later version" relationship is context for the summary
          only. It does NOT change non_supersession, which comes solely
          from `is_superseded` (see term 3).
        - If vector_results lists are empty, rely on A and B alone; that
          is not itself a missing input.

        For EACH term, you must include the specific CONTEXT field(s) you
        based the score on in "evidence". If no such data exists in any
        permitted source, set the sub-score to null and add the term to
        "missing_inputs" — do not estimate, infer, or fill the gap with
        prior knowledge under any circumstances.

        1. openness (1.0=fully open standard, 0.0=fully proprietary/closed):
        Base this ONLY on the `disclosure` and `licensing_and_patents`
        fields, from the authoritative file_format_details row or, if those
        are empty there, from a qualifying same_format_family record.

        2. adoption (1.0=ubiquitous, 0.0=obscure/unsupported):
        Base this ONLY on the `adoption` field text, from the authoritative
        file_format_details row or, if empty there, from a qualifying
        same_format_family record. Map qualitative language to a score
        using this fixed scale:
            "widespread"/"industry standard"/"most browsers"/"preferred
            format" in archives → 0.85-1.0
            "growing"/"increasing support"/"moderate"/"modest" → 0.5-0.8
            "limited"/"few"/"diminished"/"discontinued" → 0.2-0.4
            "no support found" → 0.0-0.1
        If no `adoption` text exists in any permitted source, set this
        term to null.

        3. non_supersession (1.0=no known successor, 0.0=explicitly superseded):
        Use the `is_superseded` field directly from the authoritative
        file_format_details row — this is structured PRONOM registry data,
        not something to infer.
            is_superseded = 0 → non_supersession = 0.9-1.0
            is_superseded = 1 → non_supersession = 0.1-0.3
        Do NOT additionally lower this score based on vector_results
        (related_formats "Has later version" entries, or language like
        "outperforms" / "improves on" in any record) — that is excluded
        from this term by design. If `is_superseded` is entirely absent
        from CONTEXT, set this term to null.

        4. metadata_completeness (1.0=rich archival/provenance metadata, 0.0=none):
        Count only fields from file_metadata that describe the CONTENT or
        PROVENANCE of the file itself (e.g. Title, Author, Description,
        creation source/software agent, authenticity/provenance claims such
        as C2PA/JUMBF actions). Do NOT count technical container fields
        (dimensions, bit depth, compression, permissions, file size) or
        opaque cryptographic/certificate data (signatures, hashes, OCSP blobs)
        — these do not aid future interpretability of the file.
        You may cite a same_format_family `self_documentation` field to
        explain what metadata the format COULD carry, but score only what
        this file's file_metadata actually contains.

        5. age_lifecycle_health (1.0=new/actively maintained, 0.0=aged past
        support horizon):
        Requires BOTH `release_date` to be non-null AND either `withdrawn_date`
        or other explicit lifecycle/maturity language in CONTEXT to score.
        `release_date` and `withdrawn_date` are null for most formats in this
        dataset — if either required field is missing, set this term to null
        and list it in missing_inputs. Do not substitute the file's own
        modify/creation date as a proxy for format lifecycle under any
        circumstances.

        OUTPUT FORMAT — this is consumed by an automated parser, follow it
        exactly:
        1. Output the JSON object below with NO markdown code fences
           (no ```json), no preamble, and no trailing text on that line.
        2. Do NOT include a "survivability_score" field — that is computed
           in code from your sub-scores. Only include the fields shown.
        3. The "explanation" field must be 1-3 sentences and must NOT
           contain the three-character sequence "---" anywhere, since that
           exact sequence is used as a delimiter by the calling code. Use
           an en dash (–) or comma instead of a hyphen run if you need one.
        4. Immediately after the closing brace of the JSON object, output
           a line containing only --- , then the markdown summary.

        {
        "sub_scores": {
            "openness": {"score": ..., "evidence": "..."},
            "adoption": {"score": ..., "evidence": "..."},
            "non_supersession": {"score": ..., "evidence": "..."},
            "metadata_completeness": {"score": ..., "evidence": "..."},
            "age_lifecycle_health": {"score": ..., "evidence": "..."}
        },
        "missing_inputs": [...],
        "explanation": "..."
        }

        MARKDOWN SUMMARY — after the --- line, write markdown with these
        sections (do not use a line containing only --- anywhere in it):
        ## Format identification
            The format per the authoritative file_format_details row, and
            which same_format_family records (if any) supplemented it.
        ## Sub-score rationale
            One short bullet per term, consistent with the JSON evidence.
        ## Related and comparable formats
            Using ONLY related_formats and comparable_formats, place the
            file's format among its alternatives: note any later versions,
            and name comparable formats whose CONTEXT disclosure/adoption
            text is stronger or weaker than this format's, quoting that
            text. If both lists are empty, say no comparison data was
            retrieved.
        ## Preservation recommendations
            Practical steps grounded in CONTEXT (e.g. migrating to a
            comparable format with stronger openness/adoption text, or
            adding missing descriptive metadata). Do not recommend any
            format that is not in CONTEXT.
    """

    return f"{prompt}\n{context}"
