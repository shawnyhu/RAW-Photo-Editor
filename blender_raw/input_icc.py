# SPDX-License-Identifier: GPL-3.0-or-later
"""White-balanced camera RGB to scene-linear Rec.709 through an ICC input profile."""
from contextlib import contextmanager, ExitStack
import ctypes as ct
from functools import lru_cache
from pathlib import Path
import sys

import numpy as np


# Little CMS pixel format constants from lcms2.h (interleaved, 3-channel float32).
_RGB_FLOAT = (1 << 22) | (4 << 16) | (3 << 3) | 4
_XYZ_FLOAT = (1 << 22) | (9 << 16) | (3 << 3) | 4
_RELATIVE_COLORIMETRIC = 1
_NO_OPTIMIZE = 0x0100

# Bradford-adapted D50 PCS XYZ to D65 linear Rec.709/sRGB. Keeping this
# stage floating point preserves colors outside the working RGB gamut.
_PCS_TO_709 = np.array(((3.1338561, -1.6168667, -0.4906146),
                        (-0.9787684, 1.9161415, 0.0334540),
                        (0.0719453, -0.2289914, 1.4052427)), dtype=np.float32)


@lru_cache(maxsize=1)
def _library():
    if sys.platform == 'darwin':
        filename = 'liblcms2.2.dylib'
    elif sys.platform == 'win32':
        filename = 'lcms2.dll'
    else:
        raise ValueError("Camera ICC processing is unavailable on this platform")
    try:
        lib = ct.CDLL(str(Path(__file__).parent / 'vendor' / 'lcms2' / filename))
    except OSError as exc:
        raise ValueError("Could not load the bundled Little CMS color engine") from exc
    ptr, uint = ct.c_void_p, ct.c_uint32
    for name, result, args in (
        ('cmsOpenProfileFromMem', ptr, (ptr, uint)),
        ('cmsCreateXYZProfile', ptr, ()),
        ('cmsCloseProfile', ct.c_int, (ptr,)),
        ('cmsCreateTransform', ptr, (ptr, uint, ptr, uint, uint, uint)),
        ('cmsDeleteTransform', None, (ptr,)),
        ('cmsDoTransform', None, (ptr, ptr, ptr, uint)),
    ):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, args
    return lib


def _profile_data(path):
    with Path(path).open('rb') as stream:
        header = stream.read(128)
        if len(header) != 128 or header[36:40] != b'acsp':
            raise ValueError("Invalid ICC profile header")
        if header[8] not in (2, 4):
            raise ValueError("Choose an ICC v2 or v4 camera input profile")
        if header[12:20] != b'scnrRGB ' or header[20:24] not in (b'XYZ ', b'Lab '):
            raise ValueError("Choose a camera input ICC with RGB device data and XYZ or Lab PCS")
        size = int.from_bytes(header[:4], 'big')
        if not 132 <= size <= stream.seek(0, 2):
            raise ValueError("Incomplete ICC profile")
        stream.seek(0)
        return stream.read(size)


@contextmanager
def open_transform(path):
    """Yield an RGB-array transform. Input is normalized, white-balanced camera RGB.

    Supply a camera profile authored for this linear RAW input encoding. The
    relative-colorimetric input transform may contain an A2B LUT or matrix/TRCs.
    """
    profile_data = _profile_data(path)
    lib = _library()
    with ExitStack() as cleanup:
        profile = lib.cmsOpenProfileFromMem(profile_data, len(profile_data))
        if not profile:
            raise ValueError("Little CMS could not read this ICC profile")
        cleanup.callback(lib.cmsCloseProfile, profile)
        xyz = lib.cmsCreateXYZProfile()
        if not xyz:
            raise ValueError("Little CMS could not create its XYZ working profile")
        cleanup.callback(lib.cmsCloseProfile, xyz)
        transform = lib.cmsCreateTransform(profile, _RGB_FLOAT, xyz, _XYZ_FLOAT,
                                           _RELATIVE_COLORIMETRIC, _NO_OPTIMIZE)
        if not transform:
            raise ValueError("The camera ICC has no usable RGB input transform")
        cleanup.callback(lib.cmsDeleteTransform, transform)

        def apply(camera_rgb):
            # Camera ICCs define RGB inside the unit cube. Preserve values above
            # sensor white by mapping chromaticity at the boundary and restoring
            # exposure; carry any sub-black neutral offset through separately.
            samples = np.asarray(camera_rgb, dtype=np.float32)
            if samples.ndim == 0 or samples.shape[-1] != 3:
                raise ValueError("Camera ICC processing requires RGB pixels")
            offset = np.minimum(samples.min(axis=-1, keepdims=True), 0)
            nonnegative = samples - offset
            scale = np.maximum(nonnegative.max(axis=-1, keepdims=True), 1)
            bounded = np.ascontiguousarray(nonnegative / scale)
            pcs = np.empty_like(bounded)
            lib.cmsDoTransform(transform, bounded.ctypes.data, pcs.ctypes.data, bounded.size // 3)
            return (pcs @ _PCS_TO_709.T) * scale + offset

        yield apply
