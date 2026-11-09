from .constants import *

class Tile:
    def __init__(self, x: int, y: int, width: int, height: int, data: list[int]):
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.data = data # Flat pixel values for this tile

def split_into_tiles(pixels: list[int], width: int, height: int, channels: int, tile_size: int = 256) -> list[Tile]:
    tiles = []
    for ty in range(0, height, tile_size):
        th = min(tile_size, height - ty)
        for tx in range(0, width, tile_size):
            tw = min(tile_size, width - tx)
            
            # Extract tile pixels
            tile_pixels = []
            for y in range(th):
                row_start = ((ty + y) * width + tx) * channels
                row_end = row_start + tw * channels
                tile_pixels.extend(pixels[row_start:row_end])
                
            tiles.append(Tile(tx, ty, tw, th, tile_pixels))
    return tiles

def assemble_from_tiles(tiles: list[Tile], width: int, height: int, channels: int) -> list[int]:
    pixels = [0] * (width * height * channels)
    for tile in tiles:
        for y in range(tile.height):
            src_start = y * tile.width * channels
            src_end = src_start + tile.width * channels
            dst_start = ((tile.y + y) * width + tile.x) * channels
            pixels[dst_start : dst_start + (tile.width * channels)] = tile.data[src_start:src_end]
    return pixels
