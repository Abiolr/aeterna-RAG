"""
Aggregates all the structured/semantic data that the LLM scoring
prompt (services.system_prompt.system_prompt) needs for a single file:
    - raw file metadata (ExifTool)
    - PRONOM/LoC format lookup rows for the file's extension
    - semantically similar formats from the vector store

This is the single "gather everything" step called by
services.llm_inference.run_inference before building the prompt.
"""

from services.file_extraction import get_file_metadata, get_file_extension
from services.build_lookup_db import lookup_by_extension
from services.vector_db_setup import get_vector_search


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
            "vector_results": Chroma query result dict of formats
                semantically similar to a generated "is this a
                sustainable format?" query for the file's extension
                (see vector_db_setup.get_vector_search).

    Raises:
        Propagates any exception raised by the underlying calls
        (ExifTool errors, DB errors, vector store errors). Callers
        are expected to handle/log this.
    """
    query_text = f"is {get_file_extension(file_path)} a strong, sustainable file format?"

    system_data = {}

    system_data.update({
        "file_metadata": get_file_metadata(file_path),
        "file_format_details": lookup_by_extension(get_file_extension(file_path)),
        "vector_results": get_vector_search(query_text)
    })

    return system_data