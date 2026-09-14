"""
File-level metadata and extension helpers used by the scoring pipeline
(see services.data_pipeline.get_system_data).
"""

import exiftool
import os


def get_file_metadata(file_path):
    """
    Extract technical/embedded metadata from a file on disk using
    ExifTool.

    Args:
        file_path: Path to the file to inspect.

    Returns:
        A list of dicts as returned by pyexiftool's
        ExifToolHelper.get_metadata — one dict per file passed in
        (here, always a single-element list since one path is given).
        Each dict maps ExifTool tag names (e.g. "File:FileSize",
        "XMP:Title") to their values.

    Raises:
        Propagates any exception raised by exiftool (e.g. if the
        `exiftool` binary is not installed/on PATH, or the file is
        unreadable/corrupt). Callers are expected to handle/log this.
    """
    with exiftool.ExifToolHelper() as et:
        file_metadata = et.get_metadata([file_path])
        return file_metadata


def get_file_extension(file_path):
    """
    Return a file's extension, including the leading dot (e.g. ".png").

    Args:
        file_path: Path (or bare filename) to inspect.

    Returns:
        The extension as a string, e.g. ".png". Returns "" if the
        path has no extension.
    """
    return os.path.splitext(file_path)[1]