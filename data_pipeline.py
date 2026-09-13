from file_extraction import get_file_metadata, get_file_extension
from build_lookup_db import lookup_by_extension
from vector_db_setup import get_vector_search

FILE_PATH = "test_files/linkedin-pfp.png"
QUERY_TEXT = f"is {get_file_extension(FILE_PATH)} a strong, sustainable file format?"

system_data = {}

system_data.update({
    "file_metadata": get_file_metadata(FILE_PATH),
    "file_format_details": lookup_by_extension(get_file_extension(FILE_PATH)),
    "vector_results": get_vector_search(QUERY_TEXT)
})

print(system_data)