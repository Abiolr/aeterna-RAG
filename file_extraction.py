import exiftool
import os

def get_file_metadata(file_path):

    with exiftool.ExifToolHelper() as et:
        file_metadata = et.get_metadata([file_path])
        return file_metadata

def get_file_extension(file_path):

    return os.path.splitext(file_path)[1]