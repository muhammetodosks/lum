from .constants import *
from . import native as _native

def rgb_to_ycocg(r: int, g: int, b: int) -> tuple[int, int, int]:
    """Reversible RGB to YCoCg transform."""
    co = r - b
    t = b + (co >> 1)
    cg = g - t
    y = t + (cg >> 1)
    return y, co, cg

def ycocg_to_rgb(y: int, co: int, cg: int) -> tuple[int, int, int]:
    """Reversible YCoCg to RGB transform."""
    t = y - (cg >> 1)
    g = cg + t
    b = t - (co >> 1)
    r = co + b
    return r, g, b

def transform_pixels_forward(pixels: list[int], color_space: int) -> list[int]:
    native_result = _native.transform_forward(pixels, color_space)
    if native_result is not None:
        return native_result

    if color_space not in (CS_RGB, CS_RGBA):
        return pixels
    
    has_alpha = (color_space == CS_RGBA)
    channels = 4 if has_alpha else 3
    n = len(pixels)
    out = [0] * n
    
    for i in range(0, n, channels):
        r = pixels[i]
        g = pixels[i+1]
        b = pixels[i+2]
        y, co, cg = rgb_to_ycocg(r, g, b)
        out[i] = y
        out[i+1] = co
        out[i+2] = cg
        if has_alpha:
            out[i+3] = pixels[i+3]
            
    return out

def transform_pixels_inverse(pixels: list[int], color_space: int) -> list[int]:
    native_result = _native.transform_inverse(pixels, color_space)
    if native_result is not None:
        return native_result

    if color_space not in (CS_RGB, CS_RGBA):
        return pixels
    
    has_alpha = (color_space == CS_RGBA)
    channels = 4 if has_alpha else 3
    n = len(pixels)
    out = [0] * n
    
    for i in range(0, n, channels):
        y = pixels[i]
        co = pixels[i+1]
        cg = pixels[i+2]
        r, g, b = ycocg_to_rgb(y, co, cg)
        out[i] = r
        out[i+1] = g
        out[i+2] = b
        if has_alpha:
            out[i+3] = pixels[i+3]
            
    return out


# ---------------------------------------------------------------------------
# Explicit-transform variants (v2 format).
#
# TRANSFORM_YCOCG decorrelates the colour channels, which needs a wider
# residual alphabet (2^(bit_depth+3)). TRANSFORM_NONE predicts the channels
# as they are: the alphabet shrinks to 2^bit_depth, which usually codes
# better because the entropy coder sees a much denser symbol distribution.
# The encoder picks whichever wins by measuring both.
# ---------------------------------------------------------------------------

def transform_pixels_forward_ex(pixels: list[int], color_space: int, transform: int) -> list[int]:
    """Forward transform with an explicit transform selector."""
    if transform == TRANSFORM_NONE:
        return list(pixels)
    return transform_pixels_forward(pixels, color_space)


def transform_pixels_inverse_ex(pixels: list[int], color_space: int, transform: int) -> list[int]:
    """Inverse transform with an explicit transform selector."""
    if transform == TRANSFORM_NONE:
        return list(pixels)
    return transform_pixels_inverse(pixels, color_space)
