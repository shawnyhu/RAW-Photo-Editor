# SPDX-License-Identifier: GPL-3.0-or-later
"""Whole-frame, linear Rec.709 EXR exchange with the native film renderer."""
from pathlib import Path

import numpy as np
import OpenImageIO as oiio


def process(source, destination, recipe, *, temporary=False, raw_exposure=None):
    recipe = dict(recipe)
    engine = recipe.pop('engine', 'lite')
    if engine not in {'lite', 'ofx'}:
        raise ValueError(f"Unknown film engine: {engine}")
    if __package__:
        if engine == 'ofx':
            from . import spektrafilm_ofx_engine as spektrafilm_engine
        else:
            from . import spektrafilm_engine
    else:
        if engine == 'ofx':
            import spektrafilm_ofx_engine as spektrafilm_engine
        else:
            import spektrafilm_engine

    reader = oiio.ImageInput.open(str(source))
    if reader is None:
        raise OSError(f"Cannot open film input: {oiio.geterror()}")
    try:
        channels = tuple(reader.spec().channelnames)
        if channels not in {("R", "G", "B"), ("R", "G", "B", "A")}:
            raise ValueError(f"Expected an RGB or RGBA film input, got {channels}")
        pixels = reader.read_image(format=oiio.FLOAT)
        if pixels is None:
            raise OSError(reader.geterror())
    finally:
        reader.close()

    if raw_exposure:
        # Match the compositor Exposure node in the working space. Alpha is unchanged.
        pixels[..., :3] *= np.float32(2.0 ** raw_exposure)
    if pixels.shape[2] == 4:
        alpha = pixels[..., 3:4]
        opaque = np.all(alpha == 1)
        if not opaque:
            if raw_exposure is not None and not np.any(alpha):
                raise ValueError('Decoded RAW film input is fully transparent')
            alpha = alpha.copy()
            rgb = pixels[..., :3]
            np.divide(rgb, alpha, out=rgb, where=alpha != 0)
            rgb[alpha[..., 0] == 0] = 0
            if engine == 'lite':
                pixels[..., 3] = 1
        pixels = spektrafilm_engine.render_rgba(pixels, **recipe)
        if engine == 'ofx':
            # Lens/gate/timing controls can transform coverage as well as RGB.
            # Preserve their native straight alpha and return premultiplied pixels.
            pixels[..., :3] *= pixels[..., 3:4]
        elif not opaque:
            pixels[..., :3] *= alpha
        if engine == 'lite':
            pixels[..., 3:4] = 1 if opaque else alpha
    elif raw_exposure is not None:
        rgba = np.empty((*pixels.shape[:2], 4), dtype=np.float32)
        rgba[..., :3] = pixels
        rgba[..., 3] = 1
        del pixels
        pixels = spektrafilm_engine.render_rgba(rgba, **recipe)
        if engine == 'ofx':
            pixels[..., :3] *= pixels[..., 3:4]
        else:
            pixels[..., 3] = 1
    else:
        if engine == 'ofx':
            # An opaque RGB source can acquire transparent borders in OFX.
            rgba = np.ones((*pixels.shape[:2], 4), dtype=np.float32)
            rgba[..., :3] = pixels
            pixels = spektrafilm_engine.render_rgba(rgba, **recipe)
            pixels[..., :3] *= pixels[..., 3:4]
        else:
            pixels = spektrafilm_engine.render(pixels, **recipe)
    if not np.isfinite(pixels).all():
        raise RuntimeError("Spektrafilm returned invalid pixels")

    height, width, count = pixels.shape
    spec = oiio.ImageSpec(width, height, count, oiio.FLOAT)
    spec.attribute("oiio:ColorSpace", "Linear Rec.709")
    spec.attribute("compression", "none" if temporary else "zip")
    writer = oiio.ImageOutput.create(str(destination))
    if writer is None or not writer.open(str(destination), spec):
        raise OSError(f"Cannot open film output: {writer.geterror() if writer else oiio.geterror()}")
    try:
        if not writer.write_image(pixels):
            raise OSError(writer.geterror())
    finally:
        if not writer.close():
            raise OSError(f"Cannot finish {Path(destination).name}: {writer.geterror()}")
