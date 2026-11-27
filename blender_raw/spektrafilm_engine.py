# SPDX-License-Identifier: GPL-3.0-or-later
"""Straight linear Rec.709 float RGB interface to the native film renderer."""
import ctypes as ct
from functools import lru_cache
import json
from math import isfinite
from numbers import Integral, Real
from pathlib import Path
import platform
import sys
import tomllib
from typing import NamedTuple

import numpy as np

if __package__:
    from .film_effects import BY_KEY
else:
    from film_effects import BY_KEY


class _Descriptor(ct.Structure):
    _fields_ = [(name, ct.c_char_p) for name in ("name", "label", "group", "option_set")] + [
        ("kind", ct.c_int32), ("visibility", ct.c_int32), ("default_int", ct.c_int32),
        ("defaults", ct.c_double * 3), ("minimum", ct.c_double), ("maximum", ct.c_double)]


class _Parameter(ct.Structure):
    _fields_ = [("name", ct.c_char_p), ("kind", ct.c_int32), ("values", ct.c_double * 3)]


class _StockCalibration(ct.Structure):
    _fields_ = [("dirGammaSameLayerRgb", ct.c_float * 3), ("dirGammaRToGb", ct.c_float * 2),
                ("dirGammaGToRb", ct.c_float * 2), ("dirGammaBToRg", ct.c_float * 2),
                ("halationStrength", ct.c_float * 3)]


class FilmCalibration(NamedTuple):
    reversal: bool
    parameters: dict


# SpektraAppParamKind from the pinned native app bridge.
_SECTIONS = ("ints", "bools", "doubles", "vectors", "vectors", "ints", "ints", "ints")
HOST_PARAMETERS = frozenset(("autoExposure", "autoExposureMethod", "rawWhiteBalanceMode",
                             "rawTemperature", "rawTint", "rawLensCorrection",
                             "inputColorSpace", "outputRole", "outputColorSpace"))

# Stock calibration from the pinned source's tools/generate_profile_curves.py,
# _dir_coupler_defaults(). The app bridge otherwise keeps its negative-film
# defaults when a slide stock is selected, which folds the DIR exposure curve.
# Keep explicit preset matrices intact, including individual channel groups.
_SLIDE_DIR_DEFAULTS = {
    "kodak_ektachrome_100": ((0.12, 0.08, 0.06), (0.12, 0.06), (0.08, 0.06), (0.06, 0.06)),
    "kodak_kodachrome_64": ((0.12, 0.08, 0.06), (0.12, 0.06), (0.08, 0.06), (0.06, 0.06)),
    "fujifilm_velvia_100": ((0.108, 0.072, 0.054), (0.108, 0.054), (0.072, 0.054), (0.054, 0.054)),
    "fujifilm_provia_100f": ((0.156, 0.104, 0.078), (0.156, 0.078), (0.104, 0.078), (0.078, 0.078)),
}
_DIR_MATRIX_PARAMETERS = ("dirGammaSameLayerRgb", "dirGammaRToGb", "dirGammaGToRb", "dirGammaBToRg")


@lru_cache(maxsize=1)
def _library():
    if sys.platform == "win32" and platform.machine().lower() in {"amd64", "x86_64"}:
        filename = "photo_film.dll"
    elif sys.platform == "darwin" and platform.machine() == "arm64":
        filename = "libphoto_film.dylib"
    else:
        raise RuntimeError("Spektrafilm requires Windows x64 with Vulkan or an Apple Silicon Mac")
    path = Path(__file__).resolve().parent / "vendor" / "spektrafilm" / filename
    if not path.is_file():
        raise RuntimeError(f"Missing Spektrafilm library {filename}. Reinstall the extension.")
    try:
        lib = ct.CDLL(str(path))
        render_params = lib.photo_film_render_parameters
        if sys.platform == "win32":
            lib.photo_film_probe.argtypes = [ct.c_char_p, ct.c_size_t]
            lib.photo_film_probe.restype = ct.c_int
    except OSError as error:
        raise RuntimeError(f"Cannot load Spektrafilm: {error}") from error
    except AttributeError as error:
        raise RuntimeError("Restart Blender to load the updated Spektrafilm engine") from error
    for name in ("Film", "Paper"):
        count = getattr(lib, f"SpektraApp{name}Count")
        count.argtypes, count.restype = [], ct.c_uint32
        label = getattr(lib, f"SpektraApp{name}Name")
        label.argtypes, label.restype = [ct.c_uint32], ct.c_char_p
    lib.SpektraAppParamCount.argtypes, lib.SpektraAppParamCount.restype = [], ct.c_uint32
    lib.SpektraAppParamAt.argtypes = [ct.c_uint32]
    lib.SpektraAppParamAt.restype = ct.POINTER(_Descriptor)
    render_params.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_int, ct.c_int,
                              ct.POINTER(_Parameter), ct.c_size_t, ct.c_double, ct.c_char_p, ct.c_size_t]
    render_params.restype = ct.c_int
    return lib


@lru_cache(maxsize=1)
def backend_error():
    """Probe once per process. A missing GPU must leave ordinary RAW editing available."""
    try:
        lib = _library()
        if sys.platform == "win32":
            message = ct.create_string_buffer(2048)
            if not lib.photo_film_probe(message, len(message)):
                return message.value.decode(errors="replace") or "No compatible Vulkan device"
        else:
            lib.SpektraRendererCreate.argtypes, lib.SpektraRendererCreate.restype = [], ct.c_void_p
            lib.SpektraRendererDestroy.argtypes, lib.SpektraRendererDestroy.restype = [ct.c_void_p], None
            lib.SpektraRendererIsAvailable.argtypes, lib.SpektraRendererIsAvailable.restype = [ct.c_void_p], ct.c_int
            lib.SpektraRendererLastError.argtypes, lib.SpektraRendererLastError.restype = [ct.c_void_p], ct.c_char_p
            handle = lib.SpektraRendererCreate()
            try:
                if not lib.SpektraRendererIsAvailable(handle):
                    error = lib.SpektraRendererLastError(handle)
                    return error.decode(errors="replace") if error else "No compatible Metal device"
            finally:
                lib.SpektraRendererDestroy(handle)
    except (AttributeError, OSError, RuntimeError) as error:
        return str(error)
    return ""


@lru_cache(maxsize=1)
def stocks():
    """Identifiers follow the bundled native profile tables, in engine order."""
    lib = _library()
    return (tuple(lib.SpektraAppFilmName(i).decode() for i in range(lib.SpektraAppFilmCount())),
            tuple(lib.SpektraAppPaperName(i).decode() for i in range(lib.SpektraAppPaperCount())))


def default_film():
    return stocks()[0][_descriptors()["film"].default_int]


@lru_cache(maxsize=1)
def _stock_catalog():
    path = Path(__file__).resolve().parent / "vendor" / "spektrafilm" / "stock_defaults.json"
    catalog = json.loads(path.read_bytes())
    if catalog.get("version") != 1:
        raise ValueError("Unsupported bundled stock defaults")
    return catalog


def stock_defaults(film):
    """Source-backed settings for an explicit stock choice, never a file-load migration."""
    stock = film or default_film()
    try:
        return _stock_catalog()["films"][stock]
    except KeyError as error:
        raise ValueError(f"Unknown film stock {stock!r}") from error


def compatible_papers(film):
    """Keep native ordering while excluding uncalibrated mixed color/silver paths."""
    stock = film or default_film()
    stock_defaults(stock)
    papers = _stock_catalog()["papers"]
    return tuple(paper for paper in stocks()[1] if stock in papers[paper]["compatible_films"])


def validate_stock_pair(film, paper, process=0):
    if process != 0:
        return
    stock = film or default_film()
    material = paper or stocks()[1][_descriptors()["paper"].default_int]
    if material not in compatible_papers(stock):
        raise ValueError(f"{stock} and {material} are not a calibrated Film/Print pair; "
                         "choose a compatible print material or scan the film directly")


@lru_cache(maxsize=1)
def _stock_grain_table():
    folder = Path(__file__).resolve().parent / "vendor" / "spektrafilm"
    with (folder / "stock_grain.toml").open("rb") as file:
        presets = tomllib.load(file)
    areas = json.loads((folder / "stock_grain_areas.json").read_bytes())
    return presets, areas


@lru_cache(maxsize=256)
def added_stock_calibration(film):
    """Read the new stock's polarity and defaults from its compiled spectral record.

    The first twenty entries retain the historical RNA defaults used by saved
    nodes and legacy presets. New film IDs are appended after them on both GPUs.
    """
    if not film:
        return None
    try:
        index = stocks()[0].index(film)
    except (ValueError, OSError, RuntimeError):
        return None
    if index < 20:
        return None
    if index >= 56:
        record = stock_defaults(film)
        return FilmCalibration(record["type"] == "positive", dict(record["parameters"]))
    read = _library().photo_film_stock_calibration
    read.argtypes = [ct.c_int32, ct.POINTER(_StockCalibration)]
    read.restype = ct.c_char_p
    result = _StockCalibration()
    polarity = read(index, ct.byref(result))
    if polarity not in (b"negative", b"positive"):
        raise ValueError(f"Invalid calibration for film stock {film!r}")
    parameters = {name: tuple(getattr(result, name)) for name, _kind in _StockCalibration._fields_}
    table, areas = _stock_grain_table()
    grain = table["defaults"]["color"][polarity.decode()] | table.get(film, {})
    # Lite multiplies the single editable area by separate RGB factors.
    # Their product equals the area derived from the stock RMS and curves.
    area = sum(areas[film]) / 3
    parameters["grainParticleAreaUm2"] = area
    parameters["grainParticleScale"] = tuple(channel / area for channel in areas[film])
    for key, source in (("grainUniformity", "uniformity"), ("grainDensityMin", "density_min"),
                        ("grainParticleScaleLayers", "particle_scale_sublayers")):
        parameters[key] = tuple(grain[source])
    return FilmCalibration(polarity == b"positive", parameters)


def is_slide(film):
    calibration = added_stock_calibration(film)
    return calibration.reversal if calibration else film in _SLIDE_DIR_DEFAULTS


def dir_defaults(film):
    calibration = added_stock_calibration(film)
    if calibration:
        return {key: calibration.parameters[key] for key in _DIR_MATRIX_PARAMETERS}
    values = _SLIDE_DIR_DEFAULTS.get(film, tuple(BY_KEY[key].default for key in _DIR_MATRIX_PARAMETERS))
    return dict(zip(_DIR_MATRIX_PARAMETERS, values))


def known_dir_calibration(values):
    """Identify a complete stock-default matrix without guessing a preset's name."""
    for stock in ("", *_SLIDE_DIR_DEFAULTS):
        defaults = dir_defaults(stock)
        if all(key in values and len(values[key]) == len(expected)
               and all(abs(a - b) <= 1e-6 for a, b in zip(values[key], expected))
               for key, expected in defaults.items()):
            return defaults
    return None


@lru_cache(maxsize=1)
def _descriptors():
    lib = _library()
    entries = (lib.SpektraAppParamAt(i).contents for i in range(lib.SpektraAppParamCount()))
    return {entry.name.decode(): entry for entry in entries}


def validate_parameter(name, value, section=None):
    """Check the preset type and native range before forwarding it to the renderer."""
    param = _descriptors().get(name)
    if param is None:
        raise ValueError(f"Unknown Spektrafilm parameter {name!r}")
    kind = param.kind
    expected = _SECTIONS[kind]
    if section is not None and section != expected:
        raise ValueError(f"{name} belongs in {expected}")
    if expected == "bools":
        valid = isinstance(value, bool)
    elif expected == "ints":
        valid = isinstance(value, Integral) and not isinstance(value, bool)
    elif expected == "doubles":
        valid = isinstance(value, Real) and not isinstance(value, bool)
    else:
        valid = (isinstance(value, (list, tuple)) and len(value) == kind - 1
                 and all(isinstance(v, Real) and not isinstance(v, bool) for v in value))
    if not valid:
        raise ValueError(f"Invalid {expected} value for {name}")
    minimum, maximum = param.minimum, param.maximum
    if kind in (6, 7):
        maximum = len(stocks()[kind - 6]) - 1
    values = value if expected == "vectors" else (value,)
    # RNA rounds float properties to single precision on assignment.
    for number in values:
        if not (minimum - max(1, abs(minimum)) * 1e-7 <= number <= maximum + max(1, abs(maximum)) * 1e-7
                and isfinite(number)):
            raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return kind


def _frame_size(pixels, channels):
    if pixels.ndim != 3 or pixels.shape[2] != channels:
        raise ValueError(f"Spektrafilm expects an H × W × {channels} image")
    height, width = pixels.shape[:2]
    if min(height, width) <= 0 or width * 16 >= 2**31 or width * height > (2**32 - 1) // 16:
        raise ValueError("Image dimensions exceed the film renderer's supported buffer range")
    return height, width


def render(rgb, **recipe):
    """Render a full RGB frame, preserving the film scan in linear Rec.709 encoding."""
    height, width = _frame_size(rgb, 3)
    rgba = np.empty((height, width, 4), dtype=np.float32)
    rgba[..., :3] = rgb
    rgba[..., 3] = 1
    return render_rgba(rgba, **recipe)[..., :3]


def render_rgba(rgba, *, film="", paper="", exposure=0.0, print_exposure=0.0,
                grain=False, grain_model=None, halation=False, frame=0.0, effects=None, parameters=None):
    """Native buffers contain contiguous float32, straight RGB and an opaque fourth channel."""
    height, width = _frame_size(rgba, 4)
    if rgba.dtype != np.float32 or not rgba.flags.c_contiguous:
        raise ValueError("Expected a contiguous float32 RGBA buffer")
    settings = dict(parameters or {})
    if HOST_PARAMETERS.intersection(settings):
        raise ValueError("The compositor and Raw Developer own color encoding, RAW decode and metering")
    for name, value in (effects or {}).items():
        if name not in BY_KEY:
            raise ValueError(f"Unknown Spektrafilm effect parameter {name!r}")
        settings[name] = value
    # Accept legacy recipes but always send neutral film and print EV.
    settings.update(filmExposureEv=0.0, printExposureEv=0.0,
                    grainEnabled=grain, halationEnabled=halation)
    if grain_model is not None:
        settings["grainModel"] = grain_model
    for key, identifier, choices in zip(("film", "paper"), (film, paper), stocks()):
        if identifier:
            if identifier not in choices:
                raise ValueError(f"Unknown {key} stock {identifier!r}; choose a stock from the list")
            settings[key] = choices.index(identifier)
    for key in ("film", "paper"):
        if key in settings:
            validate_parameter(key, settings[key])
    validate_stock_pair(stocks()[0][settings.get("film", _descriptors()["film"].default_int)],
                        stocks()[1][settings.get("paper", _descriptors()["paper"].default_int)],
                        settings.get("process", 0))
    if "film" in settings and settings.get("process", 0) != 2:
        validate_parameter("film", settings["film"])
        stock = stocks()[0][settings["film"]]
        if calibration := added_stock_calibration(stock):
            for name, value in calibration.parameters.items():
                settings.setdefault(name, value)
        else:
            for name, value in zip(_DIR_MATRIX_PARAMETERS, _SLIDE_DIR_DEFAULTS.get(stock, ())):
                settings.setdefault(name, value)
    values = []
    for name, value in settings.items():
        kind = validate_parameter(name, value)
        components = value if kind in (3, 4) else (value,)
        values.append(_Parameter(name.encode("ascii"), kind, (ct.c_double * 3)(*components)))
    params = (_Parameter * len(values))(*values)
    out = np.empty_like(rgba)
    error = ct.create_string_buffer(2048)
    if not _library().photo_film_render_parameters(rgba.ctypes.data, out.ctypes.data, width, height,
                                                  params, len(params), frame, error, len(error)):
        raise RuntimeError(error.value.decode(errors="replace") or "Spektrafilm rendering failed")
    return out
