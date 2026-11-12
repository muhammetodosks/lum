import struct
from .constants import *
from .codec import PyraCodec
from .container import PyraContainer
from .tile import split_into_tiles, Tile

def encode(pixels: list[int], width: int, height: int, bit_depth: int = 8, color_space: int = CS_RGB, pred_type: int = PRED_PAETH, tile_size: int = 256, method: int = -1, transform: int = -1, filter_mode: int = -1) -> bytes:
    """Encode raw pixels into PYRA byte format. method=-1 auto-selects the
    compression method; otherwise forced to METHOD_*/-constants.
    transform=-1 and filter_mode=-1 auto-select the colour transform and the
    row filter mode (pred_type=-1 auto-selects the global predictor)."""
    return PyraCodec.encode(pixels, width, height, bit_depth, color_space, pred_type, tile_size, method, transform, filter_mode)

def decode(file_bytes: bytes) -> tuple[dict, list[int]]:
    """Decode PYRA byte format into metadata and raw pixels."""
    return PyraCodec.decode(file_bytes)

def _parse_pnm_raw(input_path: str) -> tuple[bytes, int, int, bytes, int, int]:
    """Parse a PNM file into raw sample bytes.

    Returns (magic, width, height, raw_samples, bit_depth, max_val) where
    raw_samples holds one byte per sample for 8-bit images and big-endian
    u16 samples for 16-bit images (the PNM order)."""
    with open(input_path, "rb") as f:
        magic = f.readline().strip()
        if magic not in (b"P5", b"P6"):
            raise ValueError(f"Unsupported input format: {magic!r} (only P5/PGM and P6/PPM)")
        # header tokens: skip comment lines, collect width height maxval
        tokens: list[bytes] = []
        while len(tokens) < 3:
            line = f.readline()
            if not line:
                raise ValueError("Truncated PNM header")
            line = line.split(b"#")[0]  # strip trailing comments
            # tokens may be split across whitespace including newlines
            tokens.extend([t for t in line.split() if t])
        width, height, max_val = int(tokens[0]), int(tokens[1]), int(tokens[2])
        bit_depth = 8 if max_val <= 255 else 16
        sample_bytes = 2 if bit_depth == 16 else 1
        channels = 1 if magic == b"P5" else 3
        count = width * height * channels * sample_bytes
        pixel_data = f.read(count)
        if len(pixel_data) < count:
            raise ValueError("Truncated PNM pixel data")
    return magic, width, height, pixel_data, bit_depth, max_val

def _parse_pnm(input_path: str) -> tuple[int, int, int, list[int], int, int]:
    """Parse PNM (P5/PGM gray, P6/PPM RGB). Returns (magic, width, height, pixels, bit_depth, max_val).
    Pixels are ints in the sample domain; 16-bit samples are unpacked per PNM spec (big-endian)."""
    magic, width, height, raw, bit_depth, max_val = _parse_pnm_raw(input_path)
    if bit_depth == 8:
        pixels = list(raw)
    else:
        import struct
        pixels = list(struct.unpack(f">{len(raw)//2}H", raw))
    return magic, width, height, pixels, bit_depth, max_val

def encode_file(input_path: str, output_path: str, color_space: int = -1, pred_type: int = PRED_PAETH, tile_size: int = 256, method: int = -1, transform: int = -1, filter_mode: int = -1) -> dict:
    """Encode an image file (PGM P5 / PPM P6, 8- or 16-bit) into a .pyra file.
    color_space=-1 auto-detects from the PNM magic; method=-1 picks the
    method automatically. transform=-1 / filter_mode=-1 pick the colour
    transform and the row filter mode automatically. Returns encode metadata."""
    magic, width, height, raw, bit_depth, extent_max = _parse_pnm_raw(input_path)
    if color_space == -1:
        color_space = CS_GRAY if magic == b"P5" else CS_RGB
    channels = 1 if color_space == CS_GRAY else 3
    # The native core widens the raw samples itself; the pure-Python path
    # converts them here. Both produce identical container bytes.
    encoded = PyraCodec.encode_samples(raw, width, height, bit_depth,
                                        color_space, pred_type, tile_size,
                                        method, transform, filter_mode)
    with open(output_path, "wb") as f:
        f.write(encoded)
    return {"width": width, "height": height, "bit_depth": bit_depth,
            "color_space": color_space, "pred_type": pred_type, "tile_size": tile_size}

def decode_file(input_path: str, output_path: str) -> dict:
    """Decode a .pyra file into a PNM image file (P5 for gray, P6 for RGB).
    16-bit images are written with 65535 maxval (big-endian samples)."""
    import struct
    with open(input_path, "rb") as f:
        file_bytes = f.read()

    meta, pixels = decode(file_bytes)
    width = meta["width"]
    height = meta["height"]
    color_space = meta["color_space"]
    bit_depth = meta["bit_depth"]

    max_val = (1 << bit_depth) - 1
    magic = b"P5" if color_space == CS_GRAY else b"P6"

    with open(output_path, "wb") as f:
        f.write(f"{magic.decode()}\n{width} {height}\n{max_val}\n".encode())
        if bit_depth == 8:
            f.write(bytes(pixels))
        else:
            f.write(struct.pack(f">{len(pixels)}H", *pixels))
    return meta

def decode_region(file_bytes: bytes, rx: int, ry: int, rw: int, rh: int) -> tuple[dict, list[int]]:
    """Region-of-interest decoding: decode only tiles intersecting the region (rx, ry, rw, rh)."""
    meta, compressed_tiles = PyraContainer.unpack_file(file_bytes)
    width = meta["width"]
    height = meta["height"]
    tile_size = meta["tile_size"]
    color_space = meta["color_space"]
    bit_depth = meta["bit_depth"]
    pred_type = meta["pred_type"]
    method = meta["method"]
    channels = PyraCodec._channels_for_colorspace(color_space)
    legacy = meta.get("version", VERSION) < VERSION

    # Identify tiles intersecting region
    region_pixels = [0] * (rw * rh * channels)

    tile_idx = 0
    for ty in range(0, height, tile_size):
        th = min(tile_size, height - ty)
        for tx in range(0, width, tile_size):
            tw = min(tile_size, width - tx)

            # Check intersection
            if not (tx + tw <= rx or tx >= rx + rw or ty + th <= ry or ty >= ry + rh):
                # Decode tile
                tile_method, tile_bytes, filter_bits = PyraCodec._tile_body(
                    meta, compressed_tiles[tile_idx], th)
                tile_pixels = PyraCodec.decode_tile(
                    tile_bytes, tw, th, color_space, pred_type, bit_depth,
                    tile_method, meta.get("transform", TRANSFORM_YCOCG),
                    meta.get("filter_mode", FILTER_GLOBAL), filter_bits,
                    legacy=legacy)
                
                # Copy intersecting pixels into region buffer
                for y in range(th):
                    img_y = ty + y
                    if ry <= img_y < ry + rh:
                        for x in range(tw):
                            img_x = tx + x
                            if rx <= img_x < rx + rw:
                                src_idx = (y * tw + x) * channels
                                dst_idx = ((img_y - ry) * rw + (img_x - rx)) * channels
                                region_pixels[dst_idx:dst_idx+channels] = tile_pixels[src_idx:src_idx+channels]
                                
            tile_idx += 1
            
    region_meta = meta.copy()
    region_meta["width"] = rw
    region_meta["height"] = rh
    return region_meta, region_pixels

def iter_tiles(file_bytes: bytes) -> Generator[tuple[dict, list[int]], None, None]:
    """Iterate over all decoded tiles in the PYRA file."""
    meta, compressed_tiles = PyraContainer.unpack_file(file_bytes)
    width = meta["width"]
    height = meta["height"]
    tile_size = meta["tile_size"]
    color_space = meta["color_space"]
    bit_depth = meta["bit_depth"]
    pred_type = meta["pred_type"]
    method = meta["method"]
    legacy = meta.get("version", VERSION) < VERSION
    
    tile_idx = 0
    for ty in range(0, height, tile_size):
        th = min(tile_size, height - ty)
        for tx in range(0, width, tile_size):
            tw = min(tile_size, width - tx)
            tile_method, tile_bytes, filter_bits = PyraCodec._tile_body(
                meta, compressed_tiles[tile_idx], th)
            tile_pixels = PyraCodec.decode_tile(
                tile_bytes, tw, th, color_space, pred_type, bit_depth,
                tile_method, meta.get("transform", TRANSFORM_YCOCG),
                meta.get("filter_mode", FILTER_GLOBAL), filter_bits,
                legacy=legacy)
            tile_meta = {"x": tx, "y": ty, "width": tw, "height": th}
            yield tile_meta, tile_pixels
            tile_idx += 1

def encode_animation(frames: list[list[int]], width: int, height: int, fps: int = 30, **kwargs) -> bytes:
    """Encode an animation (multiple frames) into PYRA format."""
    # For animation, we can pack multiple frames or use a container extension.
    # Here we encode frame count and frames sequentially.
    encoded_frames = [encode(frame, width, height, **kwargs) for frame in frames]
    animation_header = struct.pack(">II", len(frames), fps)
    return animation_header + b"".join(encoded_frames)
