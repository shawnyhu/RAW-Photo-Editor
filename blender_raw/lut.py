# SPDX-License-Identifier: GPL-3.0-or-later
"""Apply a photographic .cube look using Blender's bundled OpenColorIO."""

from codecs import BOM_UTF8
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import numpy as np
import PyOpenColorIO as ocio


def _srgb_encode(linear):
    return np.where(linear <= .0031308, 12.92 * linear,
                    1.055 * np.maximum(linear, 0) ** (1 / 2.4) - .055)


def _srgb_decode(encoded):
    return np.where(encoded <= .04045, encoded / 12.92,
                    ((np.maximum(encoded, 0) + .055) / 1.055) ** 2.4)


@lru_cache(maxsize=1)
def _compile(contents):
    contents = contents.removeprefix(BOM_UTF8)
    size = re.search(rb"(?:^|[\r\n])[ \t]*LUT_3D_SIZE[ \t]+([0-9]+)[ \t]*(?=[#\r\n]|$)",
                     contents, re.IGNORECASE)
    if not size:
        raise ValueError("Choose a .cube file with a 3D LUT")
    if not 2 <= int(size[1]) <= 129:
        raise ValueError("Blender supports .cube grids from 2 to 129 samples per channel")
    # OCIO caches FileTransforms by filename. A private snapshot also ensures
    # that editing a linked file mid-decode cannot change a running transform.
    with TemporaryDirectory(prefix="photo-editor-cube-") as directory:
        snapshot = Path(directory) / "look.cube"
        snapshot.write_bytes(contents)
        transform = ocio.FileTransform(src=str(snapshot), interpolation=ocio.INTERP_TETRAHEDRAL)
        try:
            processor = ocio.Config.CreateRaw().getProcessor(transform)
        except ocio.Exception as error:
            raise ValueError("Invalid .cube LUT. Check the table size, headers and RGB rows.") from error

    stages = list(processor.createGroupTransform())
    if not stages or not isinstance(stages[-1], ocio.Lut3DTransform):
        raise ValueError("Choose a .cube file with a 3D LUT")
    low, high = np.zeros(3, np.float32), np.ones(3, np.float32)
    for index, stage in enumerate(stages):
        if isinstance(stage, ocio.MatrixTransform):
            # For both Adobe DOMAIN_MIN/MAX and Resolve INPUT_RANGE, OCIO
            # represents each domain as a scale and offset before its table.
            scale = np.diag(np.asarray(stage.getMatrix()).reshape(4, 4))[:3]
            offset = np.asarray(stage.getOffset())[:3]
            if not np.isfinite(scale).all() or not np.isfinite(offset).all() or np.any(scale <= 0):
                raise ValueError("Invalid .cube LUT input range")
            if index == 0:
                low, high = (-offset / scale).astype(np.float32), ((1 - offset) / scale).astype(np.float32)
        elif isinstance(stage, (ocio.Lut1DTransform, ocio.Lut3DTransform)):
            if not np.isfinite(stage.getData()).all():
                raise ValueError("The .cube LUT contains non-finite table entries")
        else:
            raise ValueError("Unsupported transform in .cube LUT")
    return processor.getDefaultCPUProcessor(), low, high


def load(path, space="SRGB", expected_hash=None):
    """Prepare one callable for all metering samples and scanlines in a decode."""
    if not path or Path(path).suffix.lower() != ".cube":
        raise ValueError("Choose a 3D LUT file (.cube)")
    if space not in {"SRGB", "LINEAR"}:
        raise ValueError("Unknown 3D LUT color space")
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"3D LUT file not found: {path.name}")
    contents = path.read_bytes()
    if expected_hash and sha256(contents).hexdigest() != expected_hash:
        raise ValueError("3D LUT changed while loading. Retry development.")
    processor, low, high = _compile(contents)
    encoded = space == "SRGB"
    if encoded:
        low, high = _srgb_decode(low), _srgb_decode(high)
    span = high - low
    if not np.isfinite(span).all() or np.any(span <= 0):
        raise ValueError("Invalid .cube LUT input range")

    def apply(rgb):
        # Carry sub-black offsets and highlight exposure through the look.
        # Staying inside the LUT domain avoids OCIO's hard per-channel clamp.
        unit = (rgb - low) / span
        offset = np.minimum(unit.min(axis=-1, keepdims=True), 0)
        gain = np.maximum(unit.max(axis=-1, keepdims=True) - offset, 1)
        bounded = low + (unit - offset) / gain * span
        values = np.ascontiguousarray(_srgb_encode(bounded) if encoded else bounded, dtype=np.float32)
        processor.applyRGB(values)
        mapped = _srgb_decode(values) if encoded else values
        return low + (mapped - low) * gain + offset * span

    return apply
