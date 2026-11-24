# SPDX-License-Identifier: GPL-3.0-or-later
"""RAW decoding for Blender's bundled Python. No bpy or UI state is required."""

import ctypes as ct
import math
import os
from pathlib import Path
import platform
import struct
import sys
import tempfile
from contextlib import contextmanager
from functools import lru_cache

import numpy as np
import OpenImageIO as oiio

if __package__:
    from . import base_look, camera_profiles, canon_wb, metering
else:
    import base_look
    import camera_profiles
    import canon_wb
    import metering


# All matrices operate on XYZ and camera samples as column vectors.
_XYZ_TO_709 = np.array(((3.2404542, -1.5371385, -0.4985314),
                        (-0.9692660, 1.8760108, 0.0415560),
                        (0.0556434, -0.2040259, 1.0572252)))
_D65 = np.array((0.95047, 1.0, 1.08883))
_D50 = np.array((0.96422, 1.0, 0.82521))
_PROPHOTO_TO_XYZ50 = np.array(((0.7976749, 0.1351917, 0.0313534),
                               (0.2880402, 0.7118741, 0.0000857),
                               (0.0, 0.0, 0.8252100)))
_BRADFORD = np.array(((0.8951, 0.2664, -0.1614),
                      (-0.7502, 1.7135, 0.0367),
                      (0.0389, -0.0685, 1.0296)))
_ILLUMINANTS = {1: 5500, 2: 4150, 3: 2856, 4: 5500, 9: 5500,
                10: 6500, 11: 7500, 12: 6500, 13: 5000, 14: 4150,
                15: 3450, 17: 2856, 18: 4874, 19: 6774, 20: 5503,
                21: 6504, 22: 7504, 23: 5003, 24: 3200}
_CAMERA_TAGS = {271, 272, 50708, 50721, 50722, 50723, 50724, 50727,
                50728, 50778, 50779, 50931, 50932, 50964, 50965, 52531}
_COLOR_TABLE_TAGS = {50937, 50938, 50939, 50981, 50982, 51107, 51108}
_HUE_SAT_TAGS = {50937, 50938, 50939, 51107}
_CAMERA_INPUT_ATTRIBUTE = 'PhotoEditor:CameraInput'
# LibRaw's linear 16-bit scaling can round a sensor-white sample down by one.
_CAMERA_CLIP = 65534
# Ease into recovery over the top 1/16 of the unbalanced camera range.
_HIGHLIGHT_START = _CAMERA_CLIP * (15 / 16)
_REC2020_TO_709 = np.array(((1.6604910, -0.5876411, -0.0728499),
                            (-0.1245505, 1.1328999, -0.0083494),
                            (-0.0181508, -0.1005789, 1.1187297)), dtype=np.float32)
_TYPES = {1: ('B', 1), 2: ('s', 1), 3: ('H', 2), 4: ('I', 4),
          5: ('II', 8), 8: ('h', 2), 9: ('i', 4), 10: ('ii', 8),
          11: ('f', 4), 12: ('d', 8)}


def _read_tags(path, profile=False, optional_tags=(), *, camera_tags=_CAMERA_TAGS):
    """Read only calibration metadata from TIFF IFD0, or standalone DCP IFD."""
    with open(path, 'rb') as handle:
        length = os.fstat(handle.fileno()).st_size
        header = handle.read(8)
        if len(header) != 8 or header[:2] not in (b'II', b'MM'):
            if profile:
                raise ValueError("Invalid DCP file header")
            return {}
        endian = '<' if header[:2] == b'II' else '>'
        magic, ifd = struct.unpack(endian + 'HI', header[2:])
        if magic != (0x4352 if profile else 42) or ifd + 2 > length:
            if profile:
                raise ValueError("Not a standalone DNG camera profile (.dcp)")
            return {}
        handle.seek(ifd)
        count = struct.unpack(endian + 'H', handle.read(2))[0]
        directory = handle.read(count * 12)
        if len(directory) != count * 12:
            raise ValueError("Incomplete camera metadata directory")
        entries = list(struct.iter_unpack(endian + 'HHI4s', directory))
        # Proprietary TIFF-family files may reuse private DNG tag numbers.
        # Only interpret DNG calibration fields in actual DNGs or DCP profiles.
        dng_tags = profile or any(entry[0] == 50706 for entry in entries)
        tags = camera_tags.union(optional_tags) if dng_tags else {271, 272}
        result = {}
        for tag, typ, n, field in entries:
            if tag not in tags or typ not in _TYPES:
                continue
            if tag in (50730, 51109) and (typ != 10 or n != 1):
                continue
            fmt, size = _TYPES[typ]
            if n > (4_000_000 if tag in _COLOR_TABLE_TAGS else 65536) or n < 1:
                raise ValueError("Invalid camera calibration field size")
            size *= n
            data = field[:size]
            if size > 4:
                offset = struct.unpack(endian + 'I', field)[0]
                if offset + size > length:
                    raise ValueError("Camera calibration field is outside its file")
                handle.seek(offset)
                data = handle.read(size)
            if typ == 2:
                result[tag] = data.rstrip(b'\0').decode('utf-8', 'replace')
                continue
            if typ in (5, 10):
                values = tuple(struct.iter_unpack(endian + fmt, data))
                if tag in (50730, 51109) and not values[0][1]:
                    continue
                result[tag] = np.array([a / b for a, b in values])
            else:
                result[tag] = np.frombuffer(data, dtype=endian + fmt).astype(
                    np.float32 if tag in _COLOR_TABLE_TAGS else float)
            if not np.isfinite(result[tag]).all():
                raise ValueError("Invalid camera calibration values")
        return result


def _planck_uv(k):
    # Planckian locus approximation in CIE 1960 uv.
    k = float(np.clip(k, 1667, 25000))
    if k < 4000:
        x = -0.2661239e9/k**3 - 0.2343580e6/k**2 + 0.8776956e3/k + 0.179910
    else:
        x = -3.0258469e9/k**3 + 2.1070379e6/k**2 + 0.2226347e3/k + 0.240390
    if k <= 2222:
        y = -1.1063814*x**3 - 1.34811020*x**2 + 2.18555832*x - 0.20219683
    elif k < 4000:
        y = -0.9549476*x**3 - 1.37418593*x**2 + 2.09137015*x - 0.16748867
    else:
        y = 3.0817580*x**3 - 5.87338670*x**2 + 3.75112997*x - 0.37001483
    den = -2*x + 12*y + 3
    return np.array([4*x/den, 6*y/den])


def _white_xyz(kelvin, tint):
    k = float(np.clip(kelvin, 1668, 24999))
    du, dv = _planck_uv(k + 1) - _planck_uv(k - 1)
    normal = np.array([dv, -du]) / math.hypot(du, dv)
    u, v = _planck_uv(k) + np.clip(tint, -150, 150) * 0.00016 * normal
    den = 2*u - 8*v + 4
    x, y = 3*u/den, 2*v/den
    return np.array([x/y, 1, (1-x-y)/y])


def _kelvin(xyz):
    x, y, _ = xyz / xyz.sum()
    if abs(y - 0.1858) < 1e-5:
        return 6500
    n = (x - 0.3320) / (y - 0.1858)
    return float(np.clip(-449*n**3 + 3525*n**2 - 6823.3*n + 5520.33, 1667, 25000))


def _white_controls(xyz):
    """Invert the developer's temperature/tint coordinates for the applied illuminant."""
    x, y, z = xyz
    den = x + 15*y + 3*z
    if den <= 0 or min(x, y, z) <= 0 or not np.isfinite(xyz).all():
        return ()
    uv = np.array((4*x/den, 6*y/den))
    lo, hi = 1e6/24999, 1e6/1668
    for _ in range(32):
        a, b = (2*lo + hi)/3, (lo + 2*hi)/3
        if np.linalg.norm(uv - _planck_uv(1e6/a)) < np.linalg.norm(uv - _planck_uv(1e6/b)):
            hi = b
        else:
            lo = a
    k = 2e6/(lo + hi)
    du, dv = _planck_uv(k + 1) - _planck_uv(k - 1)
    tint = (uv - _planck_uv(k)) @ np.array([dv, -du]) / (math.hypot(du, dv) * .00016)
    return float(k), float(tint)


def _adapt(source, destination):
    scale = (_BRADFORD @ destination) / (_BRADFORD @ source)
    return np.linalg.solve(_BRADFORD, np.diag(scale) @ _BRADFORD)


def _camera_metadata(path, optional_tags=()):
    camera = _read_tags(path, optional_tags=optional_tags)
    if not camera.get(272):
        # CR3 stores EXIF in ISO BMFF, outside the TIFF IFD read above.
        import pyexiv2
        try:
            with pyexiv2.Image(os.fspath(path)) as image:
                exif = image.read_exif(encoding="latin-1")
            camera[271] = exif.get("Exif.Image.Make", "")
            camera[272] = exif.get("Exif.Image.Model", "")
        except (OSError, RuntimeError, ValueError, OverflowError):
            pass  # An unidentified camera can still use the decoder's calibration.
    return camera


def _auto_record(camera):
    return camera_profiles.match(camera.get(271, ""), camera.get(272, ""))


@lru_cache(maxsize=512)
def _profile_name(path, mtime_ns, size):
    camera = _camera_metadata(path)
    record = _auto_record(camera)
    model = (record or {}).get("model") or camera.get(272) or camera.get(50708) or "Camera"
    source = (record or {}).get("source")
    if source == "matrix":
        origin = "Bundled matrix"
    elif 50721 in camera:
        origin = "Embedded calibration"
    else:
        origin = "Decoder calibration"
    return f"{model} · {origin}"


def auto_profile_name(path):
    """Describe the installed Auto selection without reopening the RAW on every redraw."""
    path = Path(path)
    stat = path.stat()
    return _profile_name(str(path), stat.st_mtime_ns, stat.st_size)


def decode_metadata(exr_path):
    """Read the camera input and applied white balance from a development cache."""
    reader = oiio.ImageInput.open(os.fspath(exr_path))
    if not reader:
        return '', ()
    try:
        spec = reader.spec()
        wb = tuple(spec.get_float_attribute('PhotoEditor:' + k, math.nan) for k in ('Temperature', 'Tint'))
        return spec.get_string_attribute(_CAMERA_INPUT_ATTRIBUTE, ''), wb if all(map(math.isfinite, wb)) else ()
    finally:
        reader.close()


def camera_input_name(exr_path):
    return decode_metadata(exr_path)[0]


def _load_profile(path, metadata, optional_tags):
    profile = _read_tags(path, profile=True, optional_tags=optional_tags)
    model = profile.get(50708, '')
    candidates = [metadata.get(50708, ''), metadata.get(272, ''),
                  metadata.get(271, '') + ' ' + metadata.get(272, '')]
    if model and camera_profiles.normalized(model) not in {camera_profiles.normalized(s) for s in candidates}:
        raise ValueError(f"DCP is for {model}; this RAW does not identify as that camera")
    if 50721 not in profile or profile[50721].size != 9 or 52531 in profile:
        raise ValueError("Choose a three-channel, single- or dual-illuminant matrix DCP")
    return profile


def _blend(profile, a, b, weight, default=None):
    first = profile.get(a, default)
    if first is None:
        return None
    first = np.asarray(first).reshape(3, 3)
    second = np.asarray(profile.get(b, first)).reshape(3, 3)
    return first + (second - first) * weight


def _camera_transform(white_balance, camera, calibration, settings, samples, *, wb_only=False, applied_wb=None,
                      camera_neutral=None):
    """Implement the matrix portion of DNG's reference-camera conversion."""
    mode = settings.get('white_balance', 'CAMERA').upper()
    temp = float(settings.get('temperature', 6500.0))
    desired_white = _white_xyz(temp, float(settings.get('tint', 0))) if mode == 'CUSTOM' else None
    # DNG allows the capture illuminant to be recorded in CIE xy instead
    # of AsShotNeutral. Retain the old white balance on legacy recipes.
    if mode == 'CAMERA' and settings.get('camera_input') and 50728 not in camera and 50721 in calibration:
        xy = camera.get(50729)
        if xy is not None and xy.size == 2 and min(xy) > 0 and xy.sum() < 1:
            x, y = xy
            desired_white = np.array((x / y, 1.0, (1 - x - y) / y))
    if mode == 'SAMPLE':
        # Relative coordinates refer to the oriented image, independent of proxy size.
        height, width = samples.shape[:2]
        x = round(float(np.clip(settings['neutral_x'], 0, 1)) * (width - 1))
        y = round(float(np.clip(settings['neutral_y'], 0, 1)) * (height - 1))
        radius = max(1, round(min(width, height) * 0.004))
        patch = samples[max(0,y-radius):y+radius+1, max(0,x-radius):x+radius+1, :3]
        values = patch.reshape(-1, 3)
        usable = values[(values.min(axis=1) > 64) & (values.max(axis=1) < 64224)]
        if len(usable) * 2 < len(values):
            raise ValueError("Choose a neutral, well-exposed area for white balance")
        neutral = usable.mean(axis=0)
    elif mode == 'AUTO':
        stride = max(1, max(samples.shape[:2]) // 256)
        pixels = samples[::stride, ::stride, :3].reshape(-1, 3)
        valid = pixels[(pixels.min(axis=1) > 64) & (pixels.max(axis=1) < 62500)]
        neutral = valid.mean(axis=0) if len(valid) else None
    else:
        neutral = camera.get(50728)
        if neutral is None:
            mult = np.asarray(white_balance[:3], dtype=float)
            neutral = 1 / mult if np.all(mult > 0) else None
    if neutral is not None and (len(neutral) != 3 or np.any(neutral <= 0) or not np.isfinite(neutral).all()):
        neutral = None
    if desired_white is None and neutral is None:
        desired_white = _D65
    if neutral is not None:
        neutral = neutral / neutral[1]
        if wb_only and desired_white is None and (50721 not in calibration or applied_wb is None):
            if camera_neutral is not None:
                camera_neutral.extend(neutral)
            return np.diag(1 / neutral).astype(np.float32), 0.0
    if wb_only and 50721 not in calibration:
        raise ValueError("The selected white balance needs a camera matrix or a recorded white point")

    ab = np.diag(camera.get(50727, np.ones(3)))
    match = calibration.get(50932, '') == camera.get(50931, '')
    t1 = _ILLUMINANTS.get(int(calibration.get(50778, [21])[0]), 6504)
    t2 = _ILLUMINANTS.get(int(calibration.get(50779, [21])[0]), 6504)
    current = temp if mode == 'CUSTOM' else 5500
    if mode == 'CAMERA' and settings.get('camera_input') and desired_white is not None:
        current = _kelvin(desired_white)
    for _ in range(10):
        weight = (np.clip((1/current - 1/t1) / (1/t2 - 1/t1), 0, 1)
                  if t1 != t2 and 50722 in calibration else 0.0)
        cc = _blend(camera, 50723, 50724, weight, np.eye(3)) if match else np.eye(3)
        individual = ab @ cc
        xyz_to_camera = individual @ _blend(calibration, 50721, 50722, weight)
        if desired_white is not None:
            source_white = desired_white
            neutral = xyz_to_camera @ source_white
            neutral /= neutral[1]
            break
        source_white = np.linalg.solve(xyz_to_camera, neutral)
        source_white /= source_white[1]
        new_temp = _kelvin(source_white)
        if abs(new_temp - current) < 1:
            break
        current = new_temp

    if applied_wb is not None:
        applied_wb.extend(_white_controls(source_white))
    if camera_neutral is not None:
        camera_neutral.extend(neutral)
    if wb_only:
        return np.diag(1 / neutral).astype(np.float32), weight
    forward = _blend(calibration, 50964, 50965, weight)
    if forward is not None:
        inv = np.linalg.inv(individual)
        to_xyz = _adapt(_D50, _D65) @ forward @ np.diag(1/(inv @ neutral)) @ inv
    else:
        inv = np.linalg.inv(xyz_to_camera)
        to_xyz = _adapt(source_white, _D65) @ inv / (inv @ neutral)[1]
    return (_XYZ_TO_709 @ to_xyz).astype(np.float32), weight


def _table(dims, entries):
    h, s, v = map(int, dims)
    if h < 1 or s < 2 or v < 1 or h*s*v*3 > 4_000_000:
        raise ValueError("Invalid dimensions in DCP color table")
    if entries.size == h*s*v*3:
        return entries.reshape(v, h, s, 3)
    # DNG also permits omission of the neutral (zero saturation) plane.
    if entries.size == h*(s-1)*v*3:
        data = np.empty((v, h, s, 3), dtype=np.float32)
        data[:, :, 0, :] = (0.0, 1.0, 1.0)
        data[:, :, 1:, :] = entries.reshape(v,h,s-1,3)
        return data
    raise ValueError("Invalid DCP color table length")


def _map_hsv(rgb, table, encoded):
    """Trilinear interpolation in linear ProPhoto HSV, including wrapped hue."""
    nv, nh, ns = table.shape[:3]
    r, g, b = np.moveaxis(rgb, -1, 0)
    value = rgb.max(axis=-1)
    span = value - rgb.min(axis=-1)
    nonzero = np.maximum(span, 1e-12)
    hue = np.where(value == r, (g-b)/nonzero,
                   np.where(value == g, 2+(b-r)/nonzero, 4+(r-g)/nonzero))
    hue = np.mod(hue / 6, 1)
    saturation = np.clip(span / np.maximum(value, 1e-12), 0, 1)
    if encoded and nv > 1:
        value = np.where(value <= .0031308, value * 12.92,
                         1.055 * np.power(np.maximum(value, 0), 1/2.4) - .055)
    x, y, z = hue * nh, saturation * (ns-1), np.clip(value, 0, 1) * (nv-1)
    x0 = np.minimum(x.astype(np.int32), nh-1)
    y0 = np.minimum(y.astype(np.int32), ns-2)
    z0 = np.minimum(z.astype(np.int32), max(0,nv-2))
    x1, y1, z1 = (x0+1) % nh, y0+1, np.minimum(z0+1,nv-1)
    fx, fy, fz = (a[...,None] for a in (x-x0, y-y0, z-z0))

    def plane(index):
        a = table[index,x0,y0] * (1-fy) + table[index,x0,y1] * fy
        b = table[index,x1,y0] * (1-fy) + table[index,x1,y1] * fy
        return a*(1-fx) + b*fx

    delta = plane(z0)
    if nv > 1:
        delta = delta*(1-fz) + plane(z1)*fz
    hue = (hue + delta[...,0]/360) % 1
    saturation = np.clip(saturation * delta[...,1], 0, 1)
    value = np.maximum(0, value * delta[...,2])
    if encoded and nv > 1:
        value = np.where(value <= .04045, value/12.92, ((value+.055)/1.055)**2.4)
    wave = np.abs((hue[...,None]*6 + np.array([0,4,2])) % 6 - 3) - 1
    return (value[...,None] * (1-saturation[...,None]*(1-np.clip(wave,0,1)))).astype(np.float32)


def _curve_lut(entries):
    """Sample a natural cubic spline once, then evaluate it per scanline."""
    points = np.asarray(entries).reshape(-1,2)
    x, y = points.T
    widths = np.diff(x)
    if (len(x) < 2 or len(x) > 8192 or np.any(widths <= 0)
            or np.any(points < 0) or np.any(points > 1)
            or x[0] != 0 or x[-1] != 1):
        raise ValueError("Invalid DCP tone curve control points")
    slopes = np.diff(y)/widths
    diag = 2*(widths[:-1] + widths[1:])
    rhs = 6*np.diff(slopes)
    for i in range(1, len(diag)):
        f = widths[i]/diag[i-1]
        diag[i] -= f*widths[i]
        rhs[i] -= f*rhs[i-1]
    second = np.zeros_like(x)
    for i in range(len(diag)-1, -1, -1):
        second[i+1] = (rhs[i] - widths[i+1]*second[i+2])/diag[i]
    t = np.linspace(0,1,16385)
    idx = np.clip(np.searchsorted(x,t,side='right')-1, 0, len(x)-2)
    width = widths[idx]
    f = (t-x[idx])/width
    val = ((1-f)*y[idx] + f*y[idx+1] + width**2/6 *
           (((1-f)**3-(1-f))*second[idx] + (f**3-f)*second[idx+1]))
    return t, np.clip(val,0,1)


class _ProfileAppearance:
    def __init__(self, profile, weight, gain=1.0):
        self.calibration_table = self.look_table = None
        if 50937 in profile and 50938 in profile:
            data = _table(profile[50937], profile[50938])
            if 50939 in profile:
                data = data*(1-weight) + _table(profile[50937],profile[50939])*weight
            self.calibration_table = (data, int(profile.get(51107,[0])[0]) == 1)
        if 50981 in profile and 50982 in profile:
            self.look_table = (_table(profile[50981], profile[50982]), int(profile.get(51108,[0])[0]) == 1)
        self.curve = _curve_lut(profile[50940]) if 50940 in profile else None
        self.gain = gain
        self.enabled = bool(self.calibration_table or self.look_table or self.curve or gain != 1.0)
        self.to_709 = (_XYZ_TO_709 @ _adapt(_D50, _D65) @ _PROPHOTO_TO_XYZ50).astype(np.float32)
        self.to_prophoto = np.linalg.inv(self.to_709)

    def apply(self, rgb):
        if not (self.calibration_table or self.look_table or self.curve):
            return rgb * self.gain
        wide = rgb @ self.to_prophoto.T
        # Keep out-of-domain residuals and headroom outside a profile's [0,1] cube.
        residual = np.minimum(wide, 0)
        headroom = np.maximum(1, wide.max(axis=-1, keepdims=True))
        wide = np.maximum(wide, 0) / headroom
        if self.calibration_table:
            wide = _map_hsv(wide, *self.calibration_table)
        if self.gain != 1.0:
            # DNG's HueSatMap calibrates camera color before exposure; the
            # LookTable and tone curve use the exposure-adjusted values.
            wide = (wide * headroom + residual) * self.gain
            residual = np.minimum(wide, 0)
            headroom = np.maximum(1, wide.max(axis=-1, keepdims=True))
            wide = np.maximum(wide, 0) / headroom
        if self.look_table:
            wide = _map_hsv(wide, *self.look_table)
        if self.curve is not None:
            lo, hi = wide.min(axis=-1), wide.max(axis=-1)
            fraction = (wide-lo[...,None]) / np.maximum(hi-lo,1e-12)[...,None]
            bottom, top = (np.interp(v, *self.curve) for v in (lo,hi))
            wide = bottom[...,None] + fraction*(top-bottom)[...,None]
        return ((wide*headroom + residual) @ self.to_709.T).astype(np.float32)


def _sony_frame(pixels, path, sizes, half_size):
    """Trim a Sony ARW to its declared image area in the oriented output."""
    if Path(path).suffix.lower() != '.arw':
        return pixels
    # LibRaw returns crop coordinates relative to the entire sensor. The RGB
    # bitmap has already discarded the optical margins and applied orientation.
    left = sizes.crop_left_margin - sizes.left_margin
    top = sizes.crop_top_margin - sizes.top_margin
    right = left + sizes.crop_width
    bottom = top + sizes.crop_height
    if not (0 <= left < right <= sizes.width and 0 <= top < bottom <= sizes.height):
        return pixels

    step = 2 if half_size else 1
    width, height = ((v + step - 1) // step for v in (sizes.width, sizes.height))
    left, top = left // step, top // step
    right, bottom = (right + step - 1) // step, (bottom + step - 1) // step
    if sizes.flip & 1:
        left, right = width - right, width - left
    if sizes.flip & 2:
        top, bottom = height - bottom, height - top
    if sizes.flip & 4:
        width, height = height, width
        left, top, right, bottom = top, left, bottom, right
    if pixels.shape[:2] != (height, width):
        # Some decoders already crop or rescale the output.
        return pixels
    return pixels[top:bottom, left:right]


def _resized(pixels, longest=None, *, percent=100):
    buffer = isinstance(pixels, oiio.ImageBuf)
    height, width = (pixels.spec().height, pixels.spec().width) if buffer else pixels.shape[:2]
    factor = min(1.0, longest / max(width, height)) if longest else percent / 100
    if factor == 1:
        return pixels
    w, h = max(1, round(width*factor)), max(1, round(height*factor))
    source = pixels if buffer else oiio.ImageBuf(np.ascontiguousarray(pixels))
    result = oiio.ImageBufAlgo.resize(source, filtername='triangle', roi=oiio.ROI(0,w,0,h,0,1,0,3))
    if result.has_error:
        raise RuntimeError(result.geterror())
    return result.get_pixels(source.spec().format)


class _CameraHighlights:
    """Blend clipped camera samples at the selected WB before calibration or filtering."""

    def __init__(self, pixels, neutral):
        self.pixels = pixels[..., :3]
        self.shape = self.pixels.shape
        self.gains = 1 / np.asarray(neutral, dtype=np.float32)
        self.common_white = 65535 * self.gains.min()

    def _correction(self, tile):
        peak = tile.max(axis=-1)
        near_white = peak > _HIGHLIGHT_START
        if not near_white.any():
            return near_white, None
        camera = tile[near_white].astype(np.float32)
        balanced = camera * self.gains
        # As in LibRaw Blend, keep the sum of the balanced channels and the
        # hue, and use the chroma of the channels clipped to their common white.
        limited = np.minimum(balanced, self.common_white)
        mean = balanced.mean(axis=1, keepdims=True)
        chroma = balanced - mean
        limited -= limited.mean(axis=1, keepdims=True)
        length = np.sum(chroma * chroma, axis=1, keepdims=True)
        ratio = np.sqrt(np.sum(limited * limited, axis=1, keepdims=True) / np.maximum(length, 1e-20))
        # A single saturated red or blue lamp still has measurable color in
        # the weaker channels. Only trust the neutral estimate as those also
        # approach the common white. Both shoulders have a continuous slope.
        shoulder = np.minimum(1, (peak[near_white, None] - _HIGHLIGHT_START)
                              / (_CAMERA_CLIP - _HIGHLIGHT_START))
        support = np.clip((balanced.min(axis=1, keepdims=True) / self.common_white - .25) / .5, 0, 1)
        strength = (shoulder**2 * (3 - 2*shoulder)) * (support**2 * (3 - 2*support))
        return near_white, ((mean + chroma * ratio) / self.gains - camera) * strength

    def __getitem__(self, key):
        # Metering samples and EXR scanlines share the same bounded-memory conversion.
        tile = self.pixels[key]
        mask, delta = self._correction(tile)
        if delta is None:
            return tile
        tile = tile.astype(np.float32)
        tile[mask] += delta
        return tile

    def resized(self, longest, percent):
        if (not longest or max(self.shape[:2]) <= longest) and percent == 100:
            return self
        height, width = self.shape[:2]
        base = self.pixels
        difference = oiio.ImageBuf(oiio.ImageSpec(width, height, 3, oiio.FLOAT))
        for y in range(0, height, 128):
            tile = base[y:y + 128]
            mask, delta = self._correction(tile)
            if delta is not None:
                block = np.zeros(tile.shape, dtype=np.float32)
                block[mask] = delta
                roi = oiio.ROI(0, width, y, y + len(tile), 0, 1, 0, 3)
                if not difference.set_pixels(roi, block):
                    raise RuntimeError(difference.geterror())
        # Resize the correction separately: every unaffected proxy pixel keeps
        # the existing 16-bit resampler's exact rounding. The delta is float
        # because a reconstructed camera channel can exceed its input ceiling.
        if longest and max(self.shape[:2]) > longest:
            base = _resized(base, longest)
            difference = _resized(difference, longest)
        base = _resized(base, percent=percent)
        difference = _resized(difference, percent=percent)
        return base + difference


def _write_image(path, pixels, matrix=None, divisor=1.0, appearance=None, input_name=None, *, film_meter=None, wb=None,
                 temporary=False):
    height, width = pixels.shape[:2]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=path.suffix, dir=path.parent)
    os.close(fd)
    try:
        output = oiio.ImageOutput.create(tmp)
        linear = matrix is not None
        spec = oiio.ImageSpec(width, height, 3, oiio.FLOAT if linear else oiio.UINT8)
        spec.attribute('oiio:ColorSpace', 'Linear Rec.709' if linear else 'sRGB')
        if linear:
            spec.attribute('compression', 'none' if temporary else 'zip')
            if input_name:
                spec.attribute(_CAMERA_INPUT_ATTRIBUTE, input_name)
            if wb and all(math.isfinite(v) for v in wb) and wb[0] > 0:
                spec.attribute('PhotoEditor:Temperature', float(wb[0]))
                spec.attribute('PhotoEditor:Tint', float(wb[1]))
            metering.write_exr_metadata(spec, pixels, matrix, divisor, appearance, film_meter=film_meter)
        if not output or not output.open(tmp, spec):
            raise OSError(output.geterror() if output else oiio.geterror())
        try:
            lines = 32 if appearance and appearance.enabled else 128
            for y in range(0, height, lines):
                chunk = pixels[y:y+lines, :, :3]
                if linear:
                    chunk = (chunk.astype(np.float32) / divisor) @ matrix.T
                    if appearance and appearance.enabled:
                        chunk = appearance.apply(chunk)
                if not output.write_scanlines(y, y + len(chunk), 0, np.ascontiguousarray(chunk)):
                    raise OSError(output.geterror())
        finally:
            if not output.close():
                raise OSError(output.geterror())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return width, height


@lru_cache(maxsize=1)
def _libraw_bridge():
    folder = Path(__file__).parent / 'vendor' / 'nikon_he'
    if os.name == 'nt':
        filename, path_type = folder / 'blenderraw_nikon_he.dll', ct.c_wchar_p
    elif sys.platform == 'darwin':
        filename, path_type = folder / 'libblenderraw_nikon_he.dylib', ct.c_char_p
    else:
        raise ValueError("The Nikon High Efficiency RAW decoder is unavailable on this platform")
    if not filename.is_file():
        raise ValueError("The Nikon High Efficiency RAW decoder is unavailable on this installation")
    return ct.CDLL(str(filename)), path_type


def _decoder_matrix(path):
    """Query the newer bundled LibRaw's metadata without unpacking sensor pixels."""
    try:
        lib, path_type = _libraw_bridge()
    except (OSError, ValueError):
        return None
    read = getattr(lib, 'br_camera_matrix', None)
    if read is None:
        return None
    read.argtypes = (path_type, ct.POINTER(ct.c_float))
    read.restype = ct.c_int
    matrix = (ct.c_float * 9)()
    if read(str(path) if os.name == 'nt' else os.fsencode(path), matrix):
        return None
    matrix = np.asarray(matrix, dtype=float).reshape(3, 3)
    return matrix if _valid_matrix(matrix) else None


class _CameraFrame(ct.Structure):
    _fields_ = [("owner", ct.c_void_p), ("pixels", ct.POINTER(ct.c_uint16)),
                ("width", ct.c_uint32), ("height", ct.c_uint32),
                ("xyz_to_camera", ct.c_float * 9), ("multipliers", ct.c_float * 4)]


@contextmanager
def _nikon_he_frame(path, half_size):
    """Borrow the camera-linear bitmap from the private, separately licensed decoder."""
    lib, path_type = _libraw_bridge()
    native_path = str(path) if os.name == 'nt' else os.fsencode(path)
    lib.br_he_read.argtypes = (path_type, ct.c_int, ct.POINTER(_CameraFrame))
    lib.br_he_read.restype = ct.c_int
    lib.br_he_free.argtypes = (ct.POINTER(_CameraFrame),)
    lib.br_he_free.restype = None
    lib.br_he_error.argtypes = (ct.c_int,)
    lib.br_he_error.restype = ct.c_char_p
    frame = _CameraFrame()
    code = lib.br_he_read(native_path, half_size, ct.byref(frame))
    if code:
        detail = lib.br_he_error(code).decode('utf-8', 'replace')
        raise ValueError(f"Cannot develop this Nikon RAW: {detail}")
    try:
        # OIIO accepts the native 'H' Python buffer format; ctypes exposes '<H'.
        pixels = np.ctypeslib.as_array(frame.pixels, shape=(frame.height, frame.width, 3)).view(np.uint16)
        calibration = np.ctypeslib.as_array(frame.xyz_to_camera).reshape(3, 3)
        yield pixels, calibration, frame.multipliers[:]
    finally:
        lib.br_he_free(ct.byref(frame))


class _Rendering:
    """Camera input conversion, baseline exposure, then the chosen scene look."""

    def __init__(self, calibration, weight, gain, settings, icc=None):
        self.profile = _ProfileAppearance(calibration, weight, gain)
        self.icc = icc
        look = settings.get('look', 'NONE')
        self.look = base_look.apply if look == 'BASELOOK' else None
        if look == 'LUT':
            if __package__:
                from . import lut
            else:
                import lut
            self.look = lut.load(settings.get('lut_path'), settings.get('lut_space', 'SRGB'),
                                 settings.get('lut_sha256'))
        self.enabled = bool(icc or self.profile.enabled or self.look)

    def apply(self, rgb):
        if self.icc:
            rgb = self.icc(rgb)
        if self.profile.enabled:
            rgb = self.profile.apply(rgb)
        if self.look:
            rgb = self.look(rgb)
        return rgb


def _write_camera_rgb(output_path, pixels, white_balance, camera, calibration, settings, sampled_frame=None,
                      input_name=None):
    longest = 1600 if settings.get('preview') else None
    percent = settings.get('preview_percent') or 100
    samples = sampled_frame if sampled_frame is not None else pixels
    if longest and sampled_frame is None and settings.get('white_balance') in {'SAMPLE', 'AUTO'}:
        samples = _resized(pixels, longest)
    use_icc = settings.get('camera_input') == 'ICC'
    wb = []
    neutral = [] if settings.get('camera_input') in {'AUTO', 'DCP', 'ICC'} else None
    matrix, weight = _camera_transform(white_balance, camera, calibration, settings, samples,
                                       wb_only=use_icc, applied_wb=wb, camera_neutral=neutral)
    blend = (neutral and min(neutral) > 0 and np.isfinite(neutral).all() and
             max(neutral) != min(neutral) and pixels[..., :3].max() > _HIGHLIGHT_START)
    if blend:
        pixels = _CameraHighlights(pixels, neutral).resized(longest, percent)
    else:
        if longest:
            pixels = _resized(pixels, longest)
        pixels = _resized(pixels, percent=percent)
    if use_icc:
        if __package__:
            from . import input_icc
        else:
            import input_icc
        with input_icc.open_transform(settings['profile_path']) as transform:
            appearance = _Rendering({}, weight, _baseline_gain(camera, {}), settings, transform)
            return _write_image(output_path, pixels, matrix, 65535.0, appearance, input_name, wb=wb,
                                temporary=settings.get('temporary_output', False))

    appearance = None
    if input_name:
        appearance = _Rendering(calibration, weight, _baseline_gain(camera, calibration), settings)
    elif settings.get('profile_mode') == 'AUTO':
        appearance = _ProfileAppearance(calibration, weight, _baseline_gain(camera, calibration))
    elif settings.get('use_dcp_look') or settings.get('use_dcp_curve'):
        appearance = _ProfileAppearance(calibration, weight)
    return _write_image(output_path, pixels, matrix, 65535.0, appearance, input_name, wb=wb,
                        temporary=settings.get('temporary_output', False))


def _baseline_gain(camera, calibration):
    ev = float(camera.get(50730, (0,))[0]) + float(calibration.get(51109, (0,))[0])
    if abs(ev) > 126:
        raise ValueError("Camera baseline exposure exceeds the floating-point image range")
    return 2.0 ** ev


def _valid_matrix(values):
    return (values is not None and values.size == 9 and np.isfinite(values).all()
            and abs(np.linalg.det(values.reshape(3, 3))) > 1e-8)


def _input_calibration(source, settings):
    kind = settings['camera_input']
    look = settings.get('look', 'NONE')
    if kind not in {'AUTO', 'DCP', 'ICC'} or look not in {'NONE', 'LUT', 'BASELOOK', 'PROFILE'}:
        raise ValueError("Unknown camera input or look")
    if kind != 'DCP' and look == 'PROFILE':
        raise ValueError("The profile look requires a custom DCP")
    if kind != 'AUTO' and not settings.get('profile_path'):
        raise ValueError("Choose a camera input profile file")

    # HueSatMap is a calibration table. LookTable and ProfileToneCurve are
    # appearance data and are only imported on explicit request.
    calibration_tags = _HUE_SAT_TAGS | {51109}
    if kind == 'DCP':
        camera = _camera_metadata(source, optional_tags=(50729, 50730))
        profile_tags = calibration_tags | (_COLOR_TABLE_TAGS | {50940} if look == 'PROFILE' else set())
        return camera, _load_profile(settings['profile_path'], camera, profile_tags), 'Custom DCP'
    tags = calibration_tags | {50729, 50730} if kind == 'AUTO' else {50729, 50730}
    camera = _read_tags(source, optional_tags=tags)
    # The embedded input only supports single- and dual-illuminant RGB. Other
    # sensor layouts are left to LibRaw's output conversion.
    compatible = (52531 not in camera and _valid_matrix(camera.get(50721)) and
                  all(_valid_matrix(camera[tag]) for tag in (50722, 50723, 50724, 50964, 50965) if tag in camera))
    if compatible:
        return camera, camera, 'Custom ICC' if kind == 'ICC' else 'Embedded DNG calibration'
    # Calibration matrices describe the embedded reference camera. When its
    # input is incompatible, they cannot be combined with a decoder matrix.
    if 50721 in camera:
        camera = {tag: value for tag, value in camera.items() if tag not in (50723, 50724, 50727)}
    return camera, {}, 'Custom ICC' if kind == 'ICC' else 'LibRaw calibration'


def _legacy_calibration(source, settings):
    """Resolve explicit external profiles and remaining legacy calibration sources."""
    auto_profile = settings.get('profile_mode') == 'AUTO'
    external = None if auto_profile else settings.get('profile_path')
    optional = set()
    if auto_profile or settings.get('use_dcp_look'):
        optional.update(_COLOR_TABLE_TAGS)
    if auto_profile or settings.get('use_dcp_curve'):
        optional.add(50940)
    if auto_profile:
        optional.update((50730, 51109))
    if auto_profile or external:
        camera = _camera_metadata(source, optional_tags=() if external else optional)
    else:
        camera = _read_tags(source, optional_tags=optional)
    record = _auto_record(camera) if auto_profile else None
    calibration = _load_profile(external, camera, optional) if external else camera
    if record and record['source'] == 'matrix':
        calibration = {50721: np.asarray(record['color_matrix'], dtype=float)}
        if 'illuminant' in record:
            calibration[50778] = [record['illuminant']]
    return camera, calibration, bool(external)


class AppleRawUnsupported(ValueError):
    """Core Image has no usable decoder for this camera or format."""


@lru_cache(maxsize=512)
def _sigma_fp2_dng(path, mtime_ns, size):
    try:
        camera = _read_tags(path, camera_tags={271, 272, 50706, 50708})
    except (ValueError, struct.error):
        # Core Image can also read DNG variants outside our TIFF metadata reader.
        return False
    make = str(camera.get(271, '')).strip().casefold()
    model = camera.get(50708)
    if not isinstance(model, str) or not model.strip():
        model = str(camera.get(272, ''))
    model = model.strip().casefold()
    return 50706 in camera and make == 'sigma' and model in {'sigma fp2', 'fp2'}


def apple_zero_shadow_bias(source):
    path = Path(source)
    if path.suffix.casefold() != '.dng':
        return False
    stat = path.stat()
    return _sigma_fp2_dng(str(path), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=1)
def _apple_raw_bridge():
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise AppleRawUnsupported('Apple RAW requires macOS on Apple Silicon')
    path = Path(__file__).parent / 'vendor' / 'apple_raw' / 'libphoto_apple_raw.dylib'
    lib = ct.CDLL(str(path))
    image = ct.POINTER(ct.c_float)
    lib.br_apple_raw_read_v3.argtypes = (ct.c_char_p, ct.c_int, ct.c_float, ct.c_float, ct.c_int,
                                         ct.c_float, ct.c_float, ct.c_int, ct.c_int, ct.POINTER(image),
                                         ct.POINTER(ct.c_int), ct.POINTER(ct.c_int), image,
                                         ct.POINTER(ct.c_char), ct.c_size_t)
    lib.br_apple_raw_read_v3.restype = ct.c_int
    lib.br_apple_raw_free.argtypes = (image,)
    lib.br_apple_raw_free.restype = None
    return lib


@contextmanager
def _apple_frame(source, settings, mode, temperature=5500, tint=0, max_size=0):
    modes = {'CAMERA': 0, 'DAYLIGHT': 1, 'CUSTOM': 2, 'SAMPLE': 3}
    lib = _apple_raw_bridge()
    address, width, height = ct.POINTER(ct.c_float)(), ct.c_int(), ct.c_int()
    wb = (ct.c_float * 2)()
    error = ct.create_string_buffer(1024)
    result = lib.br_apple_raw_read_v3(os.fsencode(source), modes[mode], temperature, tint,
                                      bool(settings.get('apple_lens_correction')), settings.get('neutral_x', .5),
                                      settings.get('neutral_y', .5), max_size, apple_zero_shadow_bias(source),
                                      ct.byref(address), ct.byref(width),
                                      ct.byref(height), wb, error, len(error))
    if result != 1:
        message = error.value.decode('utf-8', 'replace')
        raise AppleRawUnsupported(message) if result == -1 else ValueError(message)
    try:
        # OpenImageIO's Python image buffer accepts native 'f' but not ctypes' '<f'.
        rgba = np.ctypeslib.as_array(address, shape=(height.value, width.value, 4)).view(np.float32)
        yield rgba, tuple(wb)
    finally:
        lib.br_apple_raw_free(address)


def _apple_auto_white(source, settings):
    """Find native Kelvin/tint that neutralize the gray-world mean of usable RAW samples."""
    with _apple_frame(source, settings, 'CAMERA', max_size=256) as (image, as_shot):
        rgb = image[..., :3]
        valid = (rgb.min(axis=2) > .002) & (rgb.max(axis=2) < .95) & (image[..., 3] > .99)
        if not valid.any():
            return as_shot

    def error(mired, tint):
        with _apple_frame(source, settings, 'CUSTOM', 1e6 / mired, tint, 256) as (image, _):
            means = image[valid, :3].mean(axis=0, dtype=np.float64)
            return np.log(np.maximum(means[[0, 2]], 1e-8) / max(means[1], 1e-8))

    values = np.array((1e6 / as_shot[0], as_shot[1]))
    for _ in range(3):
        residual = error(*values)
        if np.max(np.abs(residual)) < .002:
            break
        # Measure this camera's response to temperature and tint, through the same decoder.
        dm = 5 if values[0] <= 495 else -5
        dt = 4 if values[1] <= 146 else -4
        jacobian = np.column_stack(((error(values[0] + dm, values[1]) - residual) / dm,
                                    (error(values[0], values[1] + dt) - residual) / dt))
        if not np.isfinite(jacobian).all() or abs(np.linalg.det(jacobian)) < 1e-9:
            break
        values = np.clip(values - np.linalg.solve(jacobian, residual), (20, -150), (500, 150))
    return 1e6 / values[0], values[1]


def _decode_apple(source, output_path, settings):
    mode = settings.get('white_balance', 'CAMERA')
    if settings.get('look', 'NONE') not in {'NONE', 'BASELOOK', 'LUT'}:
        raise ValueError('Unsupported Apple RAW look')
    temperature, tint = settings.get('temperature', 5500), settings.get('tint', 0)
    if mode == 'AUTO':
        temperature, tint = _apple_auto_white(source, settings)
        mode = 'CUSTOM'
    with _apple_frame(source, settings, mode, temperature, tint) as (rgba, wb):
        # Apple has already applied white balance, camera calibration and baseline exposure.
        appearance = _Rendering({}, 0, 1.0, settings)
        percent = settings.get('preview_percent', 100)
        film_meter = None
        if settings.get('preview') or percent != 100:
            film_meter = metering.film_metadata(rgba, _REC2020_TO_709, appearance=appearance)
        rgb = _resized(rgba[..., :3], 1600 if settings.get('preview') else None, percent=percent)
        return _write_image(output_path, rgb, _REC2020_TO_709, appearance=appearance,
                            input_name='Apple RAW (Core Image)', film_meter=film_meter, wb=wb,
                            temporary=settings.get('temporary_output', False))


def decode(source_path, output_path, settings):
    """Decode to a scene-linear Rec.709 float EXR and return (width, height).

    Matrix DCP calibration uses raw, unbalanced camera RGB. Blender can grade
    the resulting negative and super-white values without clipping them.
    """
    source = str(source_path)
    input_kind = settings.get('camera_input')
    # A worker request saved by an older version can still carry APPLE_AUTO.
    if input_kind in {'APPLE', 'APPLE_AUTO'}:
        return _decode_apple(source, output_path, settings)
    import rawpy
    if input_kind is not None:
        camera, calibration, input_name = _input_calibration(source, settings)
        requires_rgb = input_kind != 'AUTO'
    else:
        camera, calibration, requires_rgb = _legacy_calibration(source, settings)
        input_name = None
    preview = bool(settings.get('preview'))
    with rawpy.RawPy() as raw:
        try:
            raw.open_file(source)
            raw.unpack()
        except rawpy.LibRawFileUnsupportedError as error:
            if Path(source).suffix.lower() != '.nef' and 'NIKON' not in camera.get(271, '').upper():
                raise ValueError("The camera or RAW compression is unsupported by this decoder") from error
            # LibRaw 0.22 identifies HE/HE* files and their previews but cannot unpack them.
            # Only the HE decoder in the newer snapshot can accept this fallback request.
            with _nikon_he_frame(source, preview) as (pixels, matrix, white_balance):
                native_calibration = calibration if 50721 in calibration else {50721: matrix}
                return _write_camera_rgb(output_path, pixels, white_balance, camera, native_calibration, settings,
                                         input_name=input_name)
        params = dict(output_bps=16, gamma=(1, 1), no_auto_bright=True,
                      half_size=preview, highlight_mode=rawpy.HighlightMode.Ignore)
        # LibRaw mutates sizes.width/height during half-size postprocessing.
        sensor_sizes = raw.sizes
        if 50721 not in calibration or calibration[50721].size != 9:
            matrix = _decoder_matrix(source) if input_kind in {'AUTO', 'ICC'} else None
            if matrix is None:
                matrix = np.asarray(raw.rgb_xyz_matrix[:3], dtype=float)
            if matrix.shape == (3,3) and abs(np.linalg.det(matrix)) > 1e-8:
                calibration = {50721:matrix}
            else:
                calibration = {}
        rgb_input = raw.color_desc.startswith(b'RGB')
        if input_kind:
            if raw.num_colors != 3:
                rgb_input = raw.num_colors == 4 and raw.color_desc.rstrip(b'\0') == b'RGBG'
            if 50721 in camera and camera[50721].size != 9:
                rgb_input = False
        if (calibration or input_kind == 'ICC') and rgb_input:
            pixels = raw.postprocess(output_color=rawpy.ColorSpace.raw,
                                     user_wb=[1,1,1,1], **params)
            sampled_frame = None
            if settings.get('legacy_sample_frame') and settings.get('white_balance') == 'SAMPLE':
                # Neutral coordinates saved before ARW cropping refer to the padded frame.
                sampled_frame = _resized(pixels, 1600) if preview else pixels
            pixels = _sony_frame(pixels, source, sensor_sizes, preview)
            white_balance = raw.camera_whitebalance
            if ((input_kind or (settings.get('profile_mode') == 'AUTO' and camera.get(272) == 'Canon EOS R8 Mark II'))
                    and Path(source).suffix.lower() == '.cr3'
                    and settings.get('white_balance', 'CAMERA') == 'CAMERA'
                    and not all(channel > 0 for channel in white_balance[:3])):
                white_balance = canon_wb.as_shot(source) or white_balance
            return _write_camera_rgb(output_path, pixels, white_balance, camera,
                                     calibration, settings, sampled_frame, input_name)
        else:
            if requires_rgb or settings.get('white_balance') in ('CUSTOM', 'SAMPLE'):
                raise ValueError("This camera has no compatible three-channel calibration matrix")
            auto = settings.get('white_balance') == 'AUTO'
            pixels = raw.postprocess(output_color=rawpy.ColorSpace.sRGB,
                                     use_camera_wb=not auto, use_auto_wb=auto, **params)
            pixels = _sony_frame(pixels, source, sensor_sizes, preview)
            pixels = _resized(pixels, 1600 if preview else None,
                              percent=settings.get('preview_percent', 100))
            matrix = np.eye(3, dtype=np.float32)
    if input_kind:
        appearance = _Rendering({}, 0, _baseline_gain(camera, {}), settings)
        return _write_image(output_path, pixels, matrix, 65535.0, appearance, 'LibRaw RGB conversion',
                            temporary=settings.get('temporary_output', False))
    gain = _baseline_gain(camera, calibration) if settings.get('profile_mode') == 'AUTO' else 1.0
    return _write_image(output_path, pixels, matrix, 65535.0 / gain, temporary=settings.get('temporary_output', False))


def _import_thumbnail_pixels(image):
    """Bound filtering and orientation work to the File Browser's 256px preview."""
    limit = 256
    width, height = image.spec().width, image.spec().height
    if min(width, height) < 1:
        raise ValueError("Cannot read camera thumbnail")
    longest = max(width, height)
    if longest > 4 * limit:
        # Some cameras embed a 45MP JPEG. Bilinearly sample a 4x work image
        # before the final triangle filter instead of filtering all 45MP.
        w = max(1, round(width * (4 * limit) / longest))
        h = max(1, round(height * (4 * limit) / longest))
        image = oiio.ImageBufAlgo.resample(image, interpolate=True, roi=oiio.ROI(0, w, 0, h, 0, 1, 0, 3))
    w = max(1, round(width * min(1.0, limit / longest)))
    h = max(1, round(height * min(1.0, limit / longest)))
    if (image.spec().width, image.spec().height) != (w, h):
        image = oiio.ImageBufAlgo.resize(image, filtername='triangle', roi=oiio.ROI(0, w, 0, h, 0, 1, 0, 3))
    pixels = image.get_pixels(oiio.UINT8)
    if image.has_error or pixels is None:
        raise ValueError("Cannot read camera thumbnail")
    return pixels


def thumbnail(source_path, output_path, *, import_preview=False):
    """Write an upright JPEG using the camera's embedded data when available."""
    import rawpy
    with rawpy.RawPy() as raw:
        raw.open_file(str(source_path))
        try:
            embedded = raw.extract_thumb()
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            embedded = None
        if embedded is None:
            raw.unpack()
            sensor_sizes = raw.sizes
            pixels = raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8)
            pixels = _sony_frame(pixels, source_path, sensor_sizes, True)
            if import_preview:
                pixels = _import_thumbnail_pixels(oiio.ImageBuf(np.ascontiguousarray(pixels)))
        else:
            if embedded.format == rawpy.ThumbFormat.JPEG:
                with tempfile.TemporaryDirectory() as folder:
                    jpeg = Path(folder) / 'preview.jpg'
                    jpeg.write_bytes(embedded.data)
                    img = oiio.ImageBuf(str(jpeg))
                    orientation = img.spec().get_int_attribute('Orientation', 1)
                    stored_portrait = img.spec().height > img.spec().width
                    pixels = _import_thumbnail_pixels(img) if import_preview else img.get_pixels(oiio.UINT8)
                    if img.has_error or pixels is None:
                        raise ValueError("Cannot read embedded camera preview")
            else:
                orientation = 1
                pixels = embedded.data
                stored_portrait = pixels.shape[0] > pixels.shape[1]
                if import_preview:
                    pixels = _import_thumbnail_pixels(oiio.ImageBuf(np.ascontiguousarray(pixels)))
            if orientation == 1:
                # Embedded JPEGs may already be physically rotated.
                sizes = raw.sizes
                upright_swapped = stored_portrait != (sizes.height > sizes.width)
                orientation = {3:3, 5:8, 6:6}.get(sizes.flip, 1)
                if orientation in (6, 8) and upright_swapped:
                    orientation = 1
            if orientation != 1:
                image = oiio.ImageBuf(np.ascontiguousarray(pixels))
                image.orientation = orientation
                pixels = oiio.ImageBufAlgo.reorient(image).get_pixels(oiio.UINT8)
    if not import_preview:
        pixels = _resized(pixels, 320)
    return bool(_write_image(output_path, pixels))
