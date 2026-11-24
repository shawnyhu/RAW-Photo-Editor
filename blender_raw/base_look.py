# SPDX-License-Identifier: GPL-3.0-or-later
"""RAW Photo Editor's photographic look in scene-linear Rec.709."""
import numpy as np


def apply(rgb):
    """Shape luminance and chroma without a display transform or channel clipping."""
    red, green, blue = np.moveaxis(rgb, -1, 0)
    # Difference form gives the exact input value on the neutral axis.
    luminance = green + .2126 * (red - green) + .0722 * (blue - green)
    light = np.maximum(luminance, 0)
    # Modest midtone lift, with a darker toe and a smooth return toward
    # unit gain in bright values. Above white the response keeps increasing.
    shadow = light / (light + .055)
    highlight = light / (light + .65)
    gain = .94 + .48 * shadow - .46 * highlight
    # Bring out subtler colors; highly separated channels already have color.
    spread = np.max(rgb, axis=-1) - np.min(rgb, axis=-1)
    chroma_gain = 1 + .055 * shadow * (1 - highlight) / (1 + spread / (light + .055))
    return (luminance[..., None] + chroma_gain[..., None] * (rgb - luminance[..., None])) * gain[..., None]
