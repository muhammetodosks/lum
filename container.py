import struct
import zlib
from .constants import *
from .security import validate_dimensions, validate_chunk_size, validate_file_size, SecurityError, CorruptedFileError

class PyraContainer:
    @staticmethod
    def pack_file(width: int, height: int, bit_depth: int, color_space: int, pred_type: int, method: int, tile_size: int, compressed_tiles: list[bytes]) -> bytes:
        validate_dimensions(width, height)
        if method not in (METHOD_RAW, METHOD_RLE, METHOD_LZ):
            raise ValueError(f"invalid compression method: {method}")

        header = struct.pack(
            ">8sIIIBBBBII",
            MAGIC,
            VERSION,
            width,
            height,
            bit_depth,
            color_space,
            pred_type,
            method,
            tile_size,
            len(compressed_tiles)
        )
        
        # Build tile index: list of (offset, length)
        # Header size + tile index size + tiles data + 4 bytes CRC
        index_size = len(compressed_tiles) * 8
        current_offset = len(header) + 4 + index_size # 4 bytes for index_size itself
        
        index_data = bytearray()
        tiles_data = bytearray()
        
        for tile_bytes in compressed_tiles:
            validate_chunk_size(len(tile_bytes))
            index_data.extend(struct.pack(">II", current_offset, len(tile_bytes)))
            current_offset += len(tile_bytes)
            tiles_data.extend(tile_bytes)
            
        index_size_bytes = struct.pack(">I", len(index_data))
        
        body = header + index_size_bytes + index_data + bytes(tiles_data)
        checksum = zlib.crc32(body) & 0xFFFFFFFF
        
        return body + struct.pack(">I", checksum)

    @staticmethod
    def unpack_file(data: bytes) -> tuple[dict, list[bytes]]:
        validate_file_size(len(data), 36)
        
        if not data.startswith(MAGIC):
            raise CorruptedFileError("Invalid magic number: not a PYRA file")
            
        # Verify trailing CRC32
        body = data[:-4]
        stored_crc = struct.unpack(">I", data[-4:])[0]
        calculated_crc = zlib.crc32(body) & 0xFFFFFFFF
        if stored_crc != calculated_crc:
            raise CorruptedFileError(f"CRC32 mismatch: stored 0x{stored_crc:08X}, calculated 0x{calculated_crc:08X}")
            
        header_fmt = ">8sIIIBBBBII"
        header_size = struct.calcsize(header_fmt)

        magic, version, width, height, bit_depth, color_space, pred_type, method, tile_size, num_tiles = struct.unpack(header_fmt, data[:header_size])

        validate_dimensions(width, height)
        if method not in (METHOD_RAW, METHOD_RLE, METHOD_LZ):
            raise CorruptedFileError(f"invalid compression method in header: {method}")
        
        offset = header_size
        index_size_bytes = data[offset:offset+4]
        offset += 4
        
        index_data_len = struct.unpack(">I", index_size_bytes)[0]
        index_data = data[offset:offset+index_data_len]
        offset += index_data_len
        
        tiles = []
        for i in range(num_tiles):
            if offset + 8 > len(data):
                raise CorruptedFileError("Truncated file while reading tile index")
            tile_offset, tile_len = struct.unpack(">II", index_data[i*8:(i+1)*8])
            
            if tile_offset + tile_len > len(data) - 4:
                raise CorruptedFileError(f"Invalid tile offset/length: offset {tile_offset}, len {tile_len}")
                
            tile_bytes = data[tile_offset:tile_offset+tile_len]
            tiles.append(tile_bytes)
            
        meta = {
            "version": version,
            "width": width,
            "height": height,
            "bit_depth": bit_depth,
            "color_space": color_space,
            "pred_type": pred_type,
            "method": method,
            "tile_size": tile_size,
            "num_tiles": num_tiles
        }
        
        return meta, tiles
