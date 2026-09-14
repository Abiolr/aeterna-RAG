"""
Builds the system prompt sent to the LLM (via
services.llm_inference.run_inference) for scoring one file's long-term
survivability. Prompt content/scoring rules are intentionally left
unmodified here — only documentation was added.
"""


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
        vector_results: Semantically similar LoC format documents, as
            returned by services.vector_db_setup.get_vector_search
            (raw Chroma query result dict).

    Returns:
        A single string: scoring instructions/rules followed by a
        CONTEXT section embedding the three arguments above. This is
        passed as the `system` parameter of the Anthropic API call in
        services.llm_inference.run_inference.
    """
    context = f"""
        CONTEXT:
        file_metadata: {file_metadata}

        file_format_details: {file_format_details}

        vector_results: {vector_results}
    """

    prompt = """
        SYSTEM:
        You are the scoring engine for Aeterna, a standalone RAG-based file
        survivability scoring microservice. Compute a survivability_score (0-100)
        for one uploaded file — HIGHER means MORE likely to remain accessible/
        readable long-term, LOWER means MORE at risk of format obsolescence or
        data loss.

        Use ONLY the structured data provided in CONTEXT below. Do not use any 
        outside knowledge about file formats, software, browsers, or history — 
        including facts you are confident are true — unless that fact appears 
        verbatim or in clear paraphrase in CONTEXT.

        CONTEXT will contain:
        - file_metadata: raw ExifTool output for the uploaded file
        - file_format_details: one or more rows from the PRONOM/LoC lookup 
        database matching the file's extension, each with `is_superseded` 
        (1 = a newer version of this same format exists per PRONOM registry 
        data, 0 = this is the current/non-superseded entry), `release_date` 
        and `withdrawn_date` (PRONOM registry dates — frequently null; PRONOM 
        populates these for only a small minority of formats)
        - vector_results: semantically similar formats retrieved from a Library 
        of Congress sustainability corpus, for comparative context only — NOT 
        necessarily describing the uploaded file's own format

        If file_format_details contains multiple rows, treat the first row 
        (is_superseded=0, if present) as the authoritative match for the file's 
        actual format. Only reference other rows if you need to explain version 
        history or the file matched an ambiguous/legacy variant.

        Score using this formula when ALL five sub-scores are non-null:
        survivability_score = round(100 × [w1·openness + w2·adoption + w3·non_supersession 
                            + w4·metadata_completeness + w5·age_lifecycle_health])
        Weights: w1=0.25, w2=0.25, w3=0.20, w4=0.15, w5=0.15
        (each sub-score is 0.0-1.0, where 1.0 = maximally favorable/safe)
        If any sub-score is null, do NOT use this formula as-is — see
        "HOW TO HANDLE NULL TERMS" below for the required adjustment.

        For EACH term, you must include the specific CONTEXT field(s) you based 
        the score on in "evidence". If no such data exists, set the sub-score to 
        null and add the term to "missing_inputs" — do not estimate, infer, or 
        fill the gap with prior knowledge under any circumstances.

        HOW TO HANDLE NULL TERMS — follow this exact arithmetic, do not
        substitute your own renormalization method:
        1. Compute weighted_sum = the sum of (weight × score) for ONLY the
           terms that are non-null. Skip null terms entirely in this sum —
           do not include them as 0.
        2. Compute active_weight = the sum of the weights belonging ONLY to
           the non-null terms.
        3. final_fraction = weighted_sum / active_weight
        4. survivability_score = round(100 × final_fraction)
        This is the ONLY normalization step. Do not apply any additional
        multiplier, boost, or second renormalization on top of step 3 — the
        division in step 3 already accounts for the missing weight. If ALL
        five terms are null, do not compute a score: set survivability_score
        to null and explain why in "explanation".

        Worked example (openness=1.0, adoption=0.95, non_supersession=0.95,
        metadata_completeness=0.3, age_lifecycle_health=null):
          weighted_sum = 0.25(1.0) + 0.25(0.95) + 0.20(0.95) + 0.15(0.3) = 0.7225
          active_weight = 0.25 + 0.25 + 0.20 + 0.15 = 0.85
          final_fraction = 0.7225 / 0.85 = 0.85
          survivability_score = 85
        (Note: this means the score is NEVER simply the weighted_sum itself
        when a term is null — it must be divided by active_weight.)

        1. openness (1.0=fully open standard, 0.0=fully proprietary/closed):
        Base this ONLY on the `disclosure` and `licensing_and_patents` fields 
        from the authoritative file_format_details row.

        2. adoption (1.0=ubiquitous, 0.0=obscure/unsupported):
        Base this ONLY on the `adoption` field text. Map qualitative language 
        to a score using this fixed scale:
            "widespread"/"industry standard"/"most browsers" → 0.85-1.0
            "growing"/"increasing support" → 0.6-0.8
            "limited"/"few" → 0.2-0.4
            "no support found" or field absent → 0.0-0.1

        3. non_supersession (1.0=no known successor, 0.0=explicitly superseded):
        Use the `is_superseded` field directly from the authoritative 
        file_format_details row — this is structured PRONOM registry data, 
        not something to infer.
            is_superseded = 0 → non_supersession = 0.9-1.0
            is_superseded = 1 → non_supersession = 0.1-0.3
        Do NOT additionally lower this score based on vector_results language 
        like "outperforms" or "improves on" — that describes competing formats, 
        not confirmed supersession of the uploaded file's format, and is 
        already excluded from this term by design. If `is_superseded` is 
        entirely absent from CONTEXT, set this term to null.

        4. metadata_completeness (1.0=rich archival/provenance metadata, 0.0=none):
        Count only fields from file_metadata that describe the CONTENT or 
        PROVENANCE of the file itself (e.g. Title, Author, Description, 
        creation source/software agent, authenticity/provenance claims such 
        as C2PA/JUMBF actions). Do NOT count technical container fields 
        (dimensions, bit depth, compression, permissions, file size) or 
        opaque cryptographic/certificate data (signatures, hashes, OCSP blobs) 
        — these do not aid future interpretability of the file.

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
        2. The "explanation" field must be 1-3 sentences and must NOT
           contain the three-character sequence "---" anywhere, since that
           exact sequence is used as a delimiter by the calling code. Use
           an en dash (–) or comma instead of a hyphen run if you need one.
        3. Immediately after the closing brace of the JSON object, output
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
        "survivability_score": ...,
        "explanation": "..."
        }        
    """

    return f"{prompt}\n{context}"