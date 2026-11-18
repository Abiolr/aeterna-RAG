"""
Aggregates all the structured/semantic data that the LLM scoring
prompt (services.system_prompt.system_prompt) needs for a single file:
    - raw file metadata (ExifTool)
    - PRONOM/LoC format lookup rows for the file's extension
    - LoC context from the vector store: the file's own format family,
      its explicit relationships, and comparable formats

This is the single "gather everything" step called by
services.llm_inference.run_inference before building the prompt.
"""

from services.file_extraction import get_file_metadata, get_file_extension
from services.build_lookup_db import lookup_by_extension
from services.vector_db_setup import build_query_profile, get_vector_search


def get_system_data(file_path):
    """
    Gather all CONTEXT data for one uploaded file, ready to be handed
    to services.system_prompt.system_prompt.

    Args:
        file_path: Path to the uploaded file on disk.

    Returns:
        A dict with three keys:
            "file_metadata": list of dicts from ExifTool
                (see file_extraction.get_file_metadata).
            "file_format_details": list of dict rows from the
                PRONOM/LoC lookup DB matching the file's extension
                (see build_lookup_db.lookup_by_extension).
            "vector_results": LoC context for the file's format, built
                from a query describing the format as identified by the
                lookup + ExifTool (see vector_db_setup.get_vector_search).

    Raises:
        Propagates any exception raised by the underlying calls
        (ExifTool errors, DB errors, vector store errors). Callers
        are expected to handle/log this.
    """
    extension = get_file_extension(file_path)

    file_metadata = get_file_metadata(file_path)
    file_format_details = lookup_by_extension(extension)

    profile = build_query_profile(extension, file_format_details, file_metadata)

    return {
        "file_metadata": file_metadata,
        "file_format_details": file_format_details,
        "vector_results": get_vector_search(profile),
    }
