# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared RGB levels and whole-frame measurements, independent of Blender."""
from dataclasses import dataclass
import math

import numpy as np


ALPHA_EPSILON = 1e-6
_BLOCK_PIXELS = 262144


@dataclass(frozen=True)
class LevelSample:
    low: float
    high: float
    values: np.ndarray
    width: int
    height: int
    count: int


def _rgba(pixels):
    array = np.asarray(pixels)
    if array.ndim != 3 or array.shape[2] != 4 or not array.shape[0] or not array.shape[1]:
        raise ValueError('Levels requires a non-empty RGBA image')
    return array


def points(black, white):
    """Validate the values the compositor can actually store as float32."""
    if not math.isfinite(black) or not math.isfinite(white):
        raise ValueError('Black and white points must be finite')
    limit = np.finfo(np.float32).max
    if abs(black) > limit or abs(white) > limit:
        raise ValueError('Black and white points exceed the supported range')
    black, white = float(np.float32(black)), float(np.float32(white))
    if white <= black:
        raise ValueError('The white point must be above the black point')
    if white - black > limit:
        raise ValueError('The levels range exceeds the supported range')
    return black, white


def auto_points(low, high):
    """Stretch the visible 0–1 interval without lifting black or pulling white down.

    Negative film/grain values and scene-linear highlights above one are outside
    the target display interval. Including them as endpoints normalizes HDR and
    compresses visible contrast. Manual levels may still use an arbitrary range.
    """
    if not math.isfinite(low) or not math.isfinite(high):
        return None
    black, white = max(0., min(1., low)), max(0., min(1., high))
    return (black, white) if white > black else None


def analyze(rgba, max_values=1_000_000):
    """Measure every visible finite channel; sample only the histogram payload.

    Input is scene-linear premultiplied RGBA. A common RGB range preserves the
    channel relationship; no individual channel receives its own endpoints.
    """
    rgba = _rgba(rgba)
    if max_values is not None and max_values < 1:
        raise ValueError('The histogram sample limit must be positive')
    height, width = rgba.shape[:2]
    pixels = rgba.reshape(-1, 4)
    step = max(1, math.ceil(pixels.shape[0] * 3 / max_values)) if max_values else 1
    low, high, count = math.inf, -math.inf, 0
    histogram = []
    for start in range(0, len(pixels), _BLOCK_PIXELS):
        block = pixels[start:start + _BLOCK_PIXELS]
        alpha = block[:, 3]
        visible = np.isfinite(alpha) & (alpha > ALPHA_EPSILON)
        # Keep memory bounded even when the source is a full-resolution RAW.
        rgb = np.zeros((len(block), 3), dtype=np.float32)
        with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
            np.divide(block[:, :3], alpha[:, None], out=rgb, where=visible[:, None])
        valid = visible[:, None] & np.isfinite(rgb)
        size = int(np.count_nonzero(valid))
        if size:
            low = min(low, float(np.min(rgb, where=valid, initial=np.inf)))
            high = max(high, float(np.max(rgb, where=valid, initial=-np.inf)))
            count += size
        offset = (-start * 3) % step
        selected = rgb.reshape(-1)[offset::step]
        keep = valid.reshape(-1)[offset::step]
        if np.any(keep):
            histogram.append(selected[keep])
    if not count:
        raise ValueError('The image has no visible finite RGB values to measure')
    values = np.concatenate(histogram) if histogram else np.empty(0, dtype=np.float32)
    return LevelSample(low, high, values, width, height, count)


def samples(rgba, max_values=1_000_000):
    """Return bounded straight-RGB histogram samples from premultiplied RGBA."""
    return analyze(rgba, max_values=max_values).values


def apply(rgba, black, white):
    """Apply one shared RGB range, clamp to 0–1, and preserve source alpha."""
    rgba = _rgba(rgba)
    black, white = points(black, white)
    result = np.array(rgba, dtype=np.float32, copy=True)
    alpha = result[..., 3:4]
    rgb = result[..., :3]
    with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
        np.divide(rgb, alpha, out=rgb, where=alpha != 0)
        rgb[alpha[..., 0] == 0] = 0
        rgb -= np.float32(black)
        rgb /= np.float32(white - black)
        np.clip(rgb, 0, 1, out=rgb)
        rgb *= alpha
    return result
