# SPDX-License-Identifier: GPL-3.0-or-later
"""Photographic exposure metering on the reader's scene-linear Rec.709 output."""

from functools import lru_cache
import math
from pathlib import Path
import tempfile

import numpy as np


_PHOTO_MODES = (
    ('AS_SHOT', 'As Shot', 'Keep the original exposure'),
    ('GLOBAL', 'Global', 'Meter the whole frame for middle gray'),
    ('CENTER', 'Center-Weighted', 'Give the center of the frame more weight'),
    ('HIGHLIGHTS', 'Preserve Highlights', 'Limit exposure to retain bright color detail'),
    ('SHADOWS', 'Preserve Shadows', 'Raise exposure when the darker tones need more light'),
)
_FILM_MODES = ('FILM_CENTER', 'FILM_MEDIAN')
MODES = tuple((*entry, number) for number, entry in enumerate(_PHOTO_MODES)) + (
    ('SPOT', 'Spot', 'Meter a circular area of the developed image for 18% gray', 7),)
_LEGACY = (('FILM_CENTER', 'Saved Center-Weighted', 'Retain the exposure in this saved project', 5),
           ('FILM_MEDIAN', 'Saved Median', 'Retain the exposure in this saved project', 6))
STORED_MODES = MODES + _LEGACY
_LEGACY_MENUS = {item[0]: MODES + (item,) for item in _LEGACY}
_NUMBERS = {item[0]: item[3] for item in STORED_MODES}
_IDENTIFIERS = {item[3]: item[0] for item in STORED_MODES}


def mode_items(node, context):
    # Enum numbers 5 and 6 were saved in 0.14. New edits never select them.
    return _LEGACY_MENUS.get(node.auto_exposure, MODES)


def get_mode(node):
    return _NUMBERS[node.auto_exposure]


def set_mode(node, value):
    node.auto_exposure = _IDENTIFIERS[value]


_AUTO_MODES = tuple(item[0] for item in _PHOTO_MODES[1:])
_HEADER_PREFIX = 'PhotoEditor:MeterEV1:'
_CENTER_HEADER = 'PhotoEditor:MeterEV2:CENTER'
_SAMPLE_AXIS = 128


def _sample_positions(length):
    count = min(length, _SAMPLE_AXIS)
    return ((np.arange(count) + 0.5) * length / count).astype(np.intp)


def _film_positions(height, width):
    # Spektrafilm uses the upper-left sample of each cell on a 256-long-edge grid.
    scale = min(1.0, 256 / max(height, width))
    ny, nx = (max(1, math.floor(size * scale + 0.5)) for size in (height, width))
    return np.arange(ny) * height // ny, np.arange(nx) * width // nx


def _film_values(samples):
    # Native BT.709 RGB-to-XYZ Y row. The standalone measures before film exposure.
    luminance = samples[..., :3].astype(np.float32) @ np.array(
        (0.21263901, 0.71516868, 0.07219232), dtype=np.float32)
    height, width = luminance.shape
    edge = max(height, width)
    y = (np.arange(height) - height / 2)[:, None] / edge
    x = (np.arange(width) - width / 2) / edge
    weights = np.exp(-(x*x + y*y) / (2 * 0.2**2))
    levels = np.average(luminance, weights=weights), np.median(luminance)
    return {mode: math.log2(0.184 / level) if level > 0 and math.isfinite(level) else 0.0
            for mode, level in zip(_FILM_MODES, map(float, levels))}


def measure(samples, image_size=None):
    """Return EV offsets for the four automatic modes from a spatial RGB grid."""
    rgb = np.asarray(samples[..., :3], dtype=np.float64)
    luminance = rgb @ (0.2126, 0.7152, 0.0722)
    valid = np.isfinite(rgb).all(axis=-1) & (luminance > 1e-8)
    if samples.shape[-1] == 4:
        valid &= samples[..., 3] > 0
    if not valid.any():
        return dict.fromkeys(_AUTO_MODES, 0.0)

    log_y = np.log2(luminance[valid])
    # A log-average luminance meter (Reinhard et al., 2002). Winsorizing
    # the tails reduces the influence of isolated hot pixels and deep noise.
    lo, hi = np.percentile(log_y, (1, 99))
    robust = np.clip(log_y, lo, hi)
    gray = math.log2(0.18)
    global_ev = gray - robust.mean()

    height, width = valid.shape
    w, h = image_size or (width, height)
    y = (((np.arange(height) + 0.5) / height - 0.5) * h)[:, None]
    x = ((np.arange(width) + 0.5) / width - 0.5) * w
    central = (x*x + y*y <= (min(w, h) * 0.25)**2)[valid]
    center_ev = global_ev
    if central.any() and not central.all():
        center_ev = gray - (0.75 * robust[central].mean() + 0.25 * robust[~central].mean())

    # Like darktable's percentile metering, place a measured percentile
    # at a target level. Brightest-channel metering also protects saturated colors.
    highlight = np.percentile(rgb[valid].max(axis=-1), 99.5)
    bright_ev = min(global_ev, math.log2(0.90 / highlight))
    shadow = np.percentile(luminance[valid], 10)
    shadow_ev = max(global_ev, math.log2(0.03 / shadow))
    values = np.clip([global_ev, center_ev, bright_ev, shadow_ev], -6.0, 6.0)
    return dict(zip(_AUTO_MODES, map(float, values)))


def _linear_samples(pixels, positions, matrix, divisor, appearance):
    y, x = positions
    samples = (pixels[y[:, None], x, :3].astype(np.float32) / divisor) @ matrix.T
    return appearance.apply(samples) if appearance and appearance.enabled else samples


def film_metadata(pixels, matrix, divisor=1.0, appearance=None):
    positions = _film_positions(*pixels.shape[:2])
    return _film_values(_linear_samples(pixels, positions, matrix, divisor, appearance))


def write_exr_metadata(spec, pixels, matrix, divisor, appearance, *, film_meter=None):
    """Store metering on the decoder worker while the camera pixels are available."""
    height, width = pixels.shape[:2]
    positions = _sample_positions(height), _sample_positions(width)
    values = measure(_linear_samples(pixels, positions, matrix, divisor, appearance), (width, height))
    values.update(film_meter if film_meter is not None else film_metadata(pixels, matrix, divisor, appearance))
    for mode, ev in values.items():
        spec.attribute(_CENTER_HEADER if mode == 'CENTER' else _HEADER_PREFIX + mode, ev)


def _read_exr(path, film=False):
    import OpenImageIO as oiio

    reader = oiio.ImageInput.open(str(path))
    if not reader:
        raise OSError(oiio.geterror())
    try:
        spec = reader.spec()
        modes = _FILM_MODES if film else _AUTO_MODES
        header = {mode: spec.get_float_attribute(_CENTER_HEADER if mode == 'CENTER' else _HEADER_PREFIX + mode, math.nan)
                  for mode in modes}
        if all(math.isfinite(value) for value in header.values()):
            return header
        # Pre-existing photo caches have no metering attributes. Read only
        # sampled scanlines, keeping memory bounded even for high-resolution NEFs.
        if spec.nchannels < 3:
            return dict.fromkeys(modes, 0.0)
        ys, xs = (_film_positions(spec.height, spec.width) if film else
                  (_sample_positions(spec.height), _sample_positions(spec.width)))
        grid = np.empty((len(ys), len(xs), 3), dtype=np.float32)
        for index, y in enumerate(ys):
            start = int(y) + spec.y
            row = reader.read_scanlines(0, 0, start, start + 1, spec.z, 0, 3, oiio.FLOAT)
            if row is None:
                raise OSError(reader.geterror())
            grid[index] = row.reshape(-1, 3)[xs]
        return _film_values(grid) if film else measure(grid, (spec.width, spec.height))
    finally:
        reader.close()


@lru_cache(maxsize=512)
def _file_values(path, mtime_ns, size, film):
    # The stat arguments invalidate metering if an external cache is replaced.
    return _read_exr(path, film)


@lru_cache(maxsize=3)
def _packed_meter_file(image, packed_id, size):
    # The packed EXR can be hundreds of MB. Materialize it once while the
    # user moves or resizes the spot. Evicted temporary directories clean up.
    folder = tempfile.TemporaryDirectory(prefix='photo-editor-spot-')
    (Path(folder.name) / 'packed.exr').write_bytes(image.packed_file.data)
    return folder


@lru_cache(maxsize=128)
def _spot_value(path, mtime_ns, size, u, v, radius_percent):
    import OpenImageIO as oiio

    reader = oiio.ImageInput.open(str(path))
    if not reader:
        raise OSError(oiio.geterror())
    try:
        spec = reader.spec()
        w, h = spec.width, spec.height
        cx, cy = np.clip(float(u)*w - 0.5, 0, w-1), np.clip(float(v)*h - 0.5, 0, h-1)
        radius = max(math.sqrt(0.5), min(w, h) * radius_percent / 100)
        x0, x1 = max(0, math.ceil(cx-radius)), min(w-1, math.floor(cx+radius))
        y0, y1 = max(0, math.ceil(cy-radius)), min(h-1, math.floor(cy+radius))
        xs = x0 + _sample_positions(x1-x0+1)
        ys = y0 + _sample_positions(y1-y0+1)
        total = count = 0
        channels = min(spec.nchannels, 4)
        if channels < 3:
            return 0.
        for y in ys:
            inside = (xs-cx)**2 + (y-cy)**2 <= radius**2
            if not inside.any():
                continue
            start = int(y) + spec.y
            row = reader.read_scanlines(0, 0, start, start + 1, spec.z, 0, channels, oiio.FLOAT)
            if row is None:
                raise OSError(reader.geterror())
            rgb = row.reshape(-1, channels)[xs[inside]]
            values = rgb[:, :3] @ (0.2126, 0.7152, 0.0722)
            valid = np.isfinite(values)
            if channels == 4:
                valid &= rgb[:, 3] > 0
            total += np.maximum(values[valid], 0).sum()
            count += np.count_nonzero(valid)
        return float(np.clip(math.log2(0.18 * count / total), -6, 6)) if total > 0 else 0.
    finally:
        reader.close()


def exposure_ev(mode, image, *, spot=(0.5, 0.5, 8.0)):
    """Add this EV to the manual exposure. As Shot never loads pixels."""
    film = mode in _FILM_MODES
    if (not film and mode not in _AUTO_MODES and mode != 'SPOT') or image is None:
        return 0.0
    import bpy

    path = Path(bpy.path.abspath(image.filepath, library=image.library))
    if mode == 'SPOT':
        if image.packed_file:
            packed = image.packed_file
            folder = _packed_meter_file(image, packed.as_pointer(), packed.size)
            path = Path(folder.name) / 'packed.exr'
        if not path.is_file():
            return 0.0
        stat = path.stat()
        return _spot_value(path, stat.st_mtime_ns, stat.st_size, *spot)
    if image.filepath and path.is_file():
        stat = path.stat()
        return _file_values(path, stat.st_mtime_ns, stat.st_size, film)[mode]
    if image.packed_file:
        # Pack Resources can place a decoded EXR solely in the .blend.
        # An image data-block can be reused by all Developer nodes for a photo.
        key = 'photo_editor_packed_film_meter_v1' if film else 'photo_editor_packed_meter_v2'
        stored = image.get(key)
        if not stored or stored.get('path') != image.filepath or stored.get('size') != image.packed_file.size:
            with tempfile.TemporaryDirectory() as folder:
                local = Path(folder) / 'reader.exr'
                local.write_bytes(image.packed_file.data)
                stored = _read_exr(local, film)
            stored.update(path=image.filepath, size=image.packed_file.size)
            image[key] = stored
        return float(stored[mode])
    return 0.0
