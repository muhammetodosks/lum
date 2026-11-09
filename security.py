from .constants import *

MAX_WIDTH = 65536
MAX_HEIGHT = 65536
MAX_PIXELS = 16384 * 16384 # 268M pixels limit to prevent decompression bombs
MAX_CHUNK_SIZE = 512 * 1024 * 1024 # 512 MB
MAX_RAM_USAGE = 1024 * 1024 * 1024 # 1 GB

def validate_dimensions(width: int, height: int):
    if width <= 0 or height <= 0:
        raise SecurityError(f"Invalid dimensions: {width}x{height}")
    if width > MAX_WIDTH or height > MAX_HEIGHT:
        raise SecurityError(f"Dimensions exceed maximum allowed ({MAX_WIDTH}x{MAX_HEIGHT}): {width}x{height}")
    if width * height > MAX_PIXELS:
        raise SecurityError(f"Decompression bomb risk: pixel count {width * height} exceeds limit {MAX_PIXELS}")

def validate_chunk_size(size: int):
    if size < 0 or size > MAX_CHUNK_SIZE:
        raise SecurityError(f"Chunk size {size} exceeds maximum allowed ({MAX_CHUNK_SIZE})")

def validate_file_size(file_size: int, header_size: int):
    if file_size < header_size:
        raise CorruptedFileError("Truncated file: file size smaller than header")
