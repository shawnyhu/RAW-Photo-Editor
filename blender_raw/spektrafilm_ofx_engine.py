# SPDX-License-Identifier: GPL-3.0-or-later
"""Render using an unmodified, locally installed official Spektrafilm OFX bundle.

The helper is an isolated OpenFX host. It receives straight float RGBA frames;
this module adapts the plug-in's output encoding to Blender's linear Rec.709.
"""
from functools import lru_cache
import json
import math
from numbers import Integral, Real
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import tempfile

import numpy as np

if __package__:
    from . import ofx_schema
else:
    import ofx_schema


_configured_path = ""
_VENDOR = Path(__file__).resolve().parent / "vendor" / "spektrafilm_ofx"
HOST_PARAMETERS = ofx_schema.HOST_PARAMETERS
ENGINE_VERSION = "official-ofx-0.5.0-host-1"


def configure(path):
    global _configured_path
    _configured_path = str(path or "")


def _bundle_at(path):
    candidate = Path(path).expanduser()
    if candidate.is_file() and candidate.suffix.lower() == ".ofx":
        for parent in candidate.parents:
            if parent.name.casefold() == "spektrafilm.ofx.bundle":
                return parent
    if candidate.name.casefold() == "spektrafilm.ofx.bundle" and candidate.is_dir():
        return candidate
    direct = candidate / "spektrafilm.ofx.bundle"
    if direct.is_dir():
        return direct
    # Official ZIPs may add one enclosing release directory.
    if candidate.is_dir():
        for child in candidate.iterdir():
            direct = child / "spektrafilm.ofx.bundle"
            if child.is_dir() and direct.is_dir():
                return direct
    return None


def discover_plugin(path=""):
    """Resolve a bundle, binary, or complete extracted official release folder."""
    if path:
        found = _bundle_at(path)
        return str(found.resolve()) if found else ""
    candidates = [_configured_path, os.environ.get("BLENDER_PHOTO_SPEKTRAFILM_OFX", "")]
    if sys.platform == "win32":
        candidates.append(str(Path(os.environ.get("CommonProgramFiles", "C:/Program Files/Common Files"))
                              / "OFX" / "Plugins"))
    elif sys.platform == "darwin":
        candidates.extend(["/Library/OFX/Plugins", str(Path.home() / "Library/OFX/Plugins")])
    else:
        candidates.append("/usr/OFX/Plugins")
    for candidate in candidates:
        if candidate:
            found = _bundle_at(candidate)
            if found:
                return str(found.resolve())
    return ""


def _helper():
    if sys.platform == "win32" and platform.machine().lower() in {"amd64", "x86_64"}:
        return _VENDOR / "photo_ofx_host.exe"
    if sys.platform == "darwin" and platform.machine().lower() == "arm64":
        return _VENDOR / "photo_ofx_host"
    return None


def _plugin_binary(bundle):
    """Mirror the native host's platform paths, including Mac architecture folders."""
    root = Path(bundle) / "Contents"
    paths = ("Win64",) if sys.platform == "win32" else (
        "MacOS", "MacOS/arm64", "MacOS/x86_64", "MacOS/universal")
    for folder in paths:
        binary = root / folder / "spektrafilm.ofx"
        if binary.is_file():
            return binary
    return None


def _executable_helper():
    helper = _helper()
    if helper is None or not helper.is_file():
        raise RuntimeError("Spektrafilm OFX host helper is missing. Reinstall the extension.")
    if sys.platform == "darwin" and not os.access(helper, os.X_OK):
        # ZIP extraction can discard POSIX permissions. Restore only the owner's
        # execute bit on our bundled helper; its bytes and code signature stay intact.
        try:
            helper.chmod(stat.S_IMODE(helper.stat().st_mode) | stat.S_IXUSR)
        except OSError as exc:
            raise RuntimeError("Cannot make the Spektrafilm OFX host executable. "
                               "Reinstall the extension in a writable user directory.") from exc
        if not os.access(helper, os.X_OK):
            raise RuntimeError("The Spektrafilm OFX host is not executable. "
                               "Check the extension folder's permissions and reinstall the extension.")
    return helper


def backend_error(plugin_path=""):
    """Cheap UI capability check; probing/rendering never occurs in Blender draw."""
    helper = _helper()
    if helper is None:
        return "Spektrafilm OFX requires Windows x64 or an Apple Silicon Mac"
    if not helper.is_file():
        return "Spektrafilm OFX host helper is unavailable on this platform; see OFX setup"
    bundle = discover_plugin(plugin_path)
    if not bundle:
        return "Install the official Spektrafilm OFX plug-in or select its complete release folder in OFX Setup"
    if _plugin_binary(bundle) is None or not (Path(bundle) / "Contents/Resources").is_dir():
        return "The selected Spektrafilm OFX bundle is incomplete or is for a different platform"
    return ""


def cache_identity(plugin_path=""):
    """Invalidate completed scans when a local plugin/helper is replaced in place."""
    bundle = discover_plugin(plugin_path)
    stamps = []
    if bundle:
        root = Path(bundle)
        binary = _plugin_binary(bundle)
        if binary is not None:
            metadata = binary.stat()
            stamps.append((binary.relative_to(root).as_posix(), metadata.st_size, metadata.st_mtime_ns))
    helper = _helper()
    if helper and helper.is_file():
        stat = helper.stat()
        stamps.append((helper.name, stat.st_size, stat.st_mtime_ns))
    return (ENGINE_VERSION, ofx_schema.SCHEMA_VERSION, bundle, tuple(stamps))


def defaults():
    return {p.key: p.default for p in ofx_schema.PARAMETERS}


def validate_parameter(key, value):
    p = ofx_schema.BY_KEY.get(key)
    if p is None:
        raise ValueError(f"Unknown or host-managed Spektrafilm OFX parameter: {key}")
    if p.kind == "bool":
        valid = isinstance(value, bool)
    elif p.kind in {"int", "choice"}:
        valid = isinstance(value, Integral) and not isinstance(value, bool)
    elif p.kind == "float":
        valid = isinstance(value, Real) and not isinstance(value, bool)
    elif p.kind == "vector":
        valid = (isinstance(value, (list, tuple)) and len(value) == len(p.default)
                 and all(isinstance(v, Real) and not isinstance(v, bool) for v in value))
    else:
        valid = isinstance(value, str)
    if not valid:
        raise ValueError(f"Invalid {p.kind} value for {p.label} ({key})")
    if p.kind not in {"bool", "string"}:
        for number in value if p.kind == "vector" else (value,):
            low_tolerance = max(1.0, abs(p.minimum)) * 1e-7 if p.kind != "choice" else 0
            high_tolerance = max(1.0, abs(p.maximum)) * 1e-7 if p.kind != "choice" else 0
            maximum = p.maximum
            if key in ofx_schema.CHOICE_DEPENDENCIES:
                maximum = max(len(item["options"]) for item in ofx_schema.CHOICE_DEPENDENCIES[key]["values"].values()) - 1
            if not math.isfinite(number) or not p.minimum - low_tolerance <= number <= maximum + high_tolerance:
                raise ValueError(f"{p.label} must be between {p.minimum:g} and {p.maximum:g}")
    return p.kind


def normalize_parameters(parameters=None):
    if parameters is not None and not isinstance(parameters, dict):
        raise ValueError("Spektrafilm OFX parameters must be an object")
    result = defaults()
    for key, value in (parameters or {}).items():
        kind = validate_parameter(key, value)
        result[key] = tuple(value) if kind == "vector" else value
    for key in ofx_schema.CHOICE_DEPENDENCIES:
        if key in (parameters or {}) and result[key] >= len(ofx_schema.choice_options(key, result)):
            raise ValueError(f"{key} choice is not available in the selected category")
    return result


def _choice_label(key, values, default=""):
    param = ofx_schema.ALL_BY_KEY.get(key)
    if param is None or not param.options:
        return default
    index = int(values.get(key, param.default))
    return param.options[index] if 0 <= index < len(param.options) else default


def _linear_input(values):
    for key in ("inputColorSpace", "rcmInputColorSpace"):
        parameter = ofx_schema.ALL_BY_KEY.get(key)
        if parameter:
            for index, label in enumerate(parameter.options):
                normalized = label.lower().replace(" ", "").replace(".", "")
                if "linear" in normalized and ("rec709" in normalized or "srgb" in normalized):
                    values[key] = index
                    break
            else:
                raise RuntimeError(f"The OFX plug-in has no linear Rec.709 {key} option")
    return values


def _run(arguments, timeout=600):
    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        config = Path(tempfile.gettempdir()) / "Blender Photo Editor" / "ofx-host" / ofx_schema.SCHEMA_VERSION
        config.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ, BPE_OFX_PARENT_PID=str(os.getpid()))
        # Keep a private persistent config: v0.5 also stores its shader/update
        # cache under APPDATA. Emptying it per job recompiled pipelines and made
        # native updater teardown block short metadata calls.
        environment["APPDATA"] = str(config)
        environment["XDG_CONFIG_HOME"] = str(config)
        if sys.platform != "win32":
            environment["HOME"] = str(config)
        result = subprocess.run([str(_executable_helper()), *map(str, arguments)], timeout=timeout, env=environment, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Spektrafilm OFX did not finish within {timeout} seconds") from exc
    except PermissionError as exc:
        raise RuntimeError("Cannot start the Spektrafilm OFX host. Check the extension folder's "
                           "permissions and any operating-system security notice, then reinstall if needed.") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-3000:]
        raise RuntimeError(f"Spektrafilm OFX host failed ({result.returncode}): {detail}")
    return result


def describe(plugin_path="", parameters=None, *, _builtin=False):
    error = backend_error(plugin_path)
    if error:
        raise RuntimeError(error)
    with tempfile.TemporaryDirectory(prefix="photo-ofx-describe-") as folder:
        destination = Path(folder) / "parameters.json"
        arguments = ["describe", discover_plugin(plugin_path), destination]
        if parameters is not None:
            values = dict(parameters) if _builtin else _validated_sparse(parameters)
            request = Path(folder) / "request.json"
            request.write_text(json.dumps(dict(parameters=_linear_input(values))), encoding="utf-8")
            arguments.append(request)
        _run(arguments, timeout=60)
        metadata = json.loads(destination.read_text(encoding="utf-8"))
        _check_metadata(metadata)
        return metadata


def _check_metadata(metadata):
    version = metadata.get("version")
    if not version:
        version = next((p.get("value") for p in metadata.get("parameters", ()) if p["name"] == "infoVersion"), "unknown")
    if version != ofx_schema.SCHEMA_VERSION:
        raise RuntimeError(f"Spektrafilm OFX {version} requires matching interface metadata; this build supports {ofx_schema.SCHEMA_VERSION}")


def _validated_sparse(parameters):
    normalize_parameters(parameters)
    # JSON snapshots may sort object keys. Restore native descriptor order so
    # categories/modes/presets run before the controls they reconfigure.
    supplied = parameters or {}
    return {p.key: supplied[p.key] for p in ofx_schema.PARAMETERS if p.key in supplied}


def effective_parameters(parameters, plugin_path=""):
    """Resolve OFX change callbacks before exporting a portable preset snapshot."""
    metadata = describe(plugin_path, parameters)
    return _effective_values(metadata)


def _effective_values(metadata):
    result = {}
    for p in metadata["parameters"]:
        key = p["name"]
        if key not in ofx_schema.BY_KEY:
            continue
        value = p.get("value", p["default"])
        kind = ofx_schema.BY_KEY[key].kind
        if kind == "vector":
            value = tuple(value)
        elif isinstance(value, list):
            value = value[0] if value else ofx_schema.BY_KEY[key].default
        if kind == "bool":
            value = bool(value)
        validate_parameter(key, value)
        result[key] = value
    return result


@lru_cache(maxsize=2)
def _local_descriptors(identity):
    return {p["name"]: p for p in describe(identity[2])["parameters"]}


def parameter_help(key, plugin_path=""):
    error = backend_error(plugin_path)
    if error:
        raise RuntimeError(error)
    parameter = _local_descriptors(cache_identity(plugin_path)).get(key, {})
    return parameter.get("hint", "")


@lru_cache(maxsize=2)
def _local_looks(identity):
    native = _local_descriptors(identity)
    looks = []
    for category, group in enumerate(native["quickPresetCategory"]["options"]):
        if category == native["quickPresetCategory"]["value"]:
            choices = native["quickPresetSelection"]["options"]
        else:
            result = describe(identity[2], {"quickPresetCategory": category}, _builtin=True)
            choices = next(p["options"] for p in result["parameters"] if p["name"] == "quickPresetSelection")
        looks.extend((category, index, group, label) for index, label in enumerate(choices))
    return tuple(looks)


def builtin_looks(plugin_path=""):
    error = backend_error(plugin_path)
    if error:
        raise RuntimeError(error)
    return _local_looks(cache_identity(plugin_path))


def builtin_preset(category, index, plugin_path=""):
    """Apply an official bundled look through the plug-in's own callbacks."""
    category, index = int(category), int(index)
    if not any((c, i) == (category, index) for c, i, _, _ in builtin_looks(plugin_path)):
        raise ValueError("Unknown official Spektrafilm preset")
    metadata = describe(plugin_path, {"quickPresetCategory": category, "quickPresetSelection": index}, _builtin=True)
    return _effective_values(metadata)


def export_lut(path, parameters=None, size=33, plugin_path="", *, overwrite=False):
    """Delegate CUBE export and its license text to the official OFX exporter."""
    if size not in {33, 65}:
        raise ValueError("Spektrafilm OFX exports 33 or 65 point CUBE LUTs")
    values = _linear_input(_validated_sparse(parameters))
    if int(values.get("outputRole", 0)) != 0 or output_is_data(values):
        raise ValueError("LUT export requires SDR display output")
    destination = Path(path).expanduser().resolve()
    if destination.suffix.lower() != ".cube":
        raise ValueError("Choose a .cube output filename")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"LUT already exists: {destination.name}")
    describe(plugin_path)  # Check the installed interface version before writing.
    with tempfile.TemporaryDirectory(prefix="photo-ofx-lut-") as folder:
        request = Path(folder) / "request.json"
        request.write_text(json.dumps(dict(parameters=values, lut_size=size, frame=0.0,
                                          overwrite=bool(overwrite))), encoding="utf-8")
        _run(["export-lut", discover_plugin(plugin_path), request, destination])
    if not destination.is_file():
        raise RuntimeError("The OFX exporter did not create the requested LUT")
    return str(destination)


def _rgb_to_xyz(primaries, white):
    xy = np.array(primaries, dtype=np.float64)
    xyz = np.stack((xy[:, 0] / xy[:, 1], np.ones(3), (1 - xy.sum(axis=1)) / xy[:, 1]))
    w = np.array([white[0] / white[1], 1, (1 - sum(white)) / white[1]])
    return xyz * np.linalg.solve(xyz, w)


@lru_cache(maxsize=12)
def _to_rec709(gamut):
    d65 = (0.3127, 0.3290)
    primaries = {
        "709": (((0.64, 0.33), (0.30, 0.60), (0.15, 0.06)), d65),
        "2020": (((0.708, 0.292), (0.170, 0.797), (0.131, 0.046)), d65),
        "p3": (((0.680, 0.320), (0.265, 0.690), (0.150, 0.060)), d65),
        "dci": (((0.680, 0.320), (0.265, 0.690), (0.150, 0.060)), (0.314, 0.351)),
        "adobe": (((0.640, 0.330), (0.210, 0.710), (0.150, 0.060)), d65),
        "prophoto": (((0.7347, 0.2653), (0.1596, 0.8404), (0.0366, 0.0001)), (0.3457, 0.3585)),
        "dwg": (((0.8000, 0.3130), (0.1682, 0.9877), (0.0790, -0.1155)), d65),
    }
    p, white = primaries["709" if gamut == "srgb" else gamut]
    source = _rgb_to_xyz(p, white)
    # The native Colour tables use these published rounded XYZ-to-RGB matrices.
    # Invert those exact output matrices instead of substituting derived primaries.
    published = {
        "srgb": ((3.2406, -1.5372, -0.4986), (-0.9689, 1.8758, 0.0415), (0.0557, -0.204, 1.057)),
        "adobe": ((2.04159, -0.56501, -0.34473), (-0.96924, 1.87597, 0.04156), (0.01344, -0.11836, 1.01517)),
        "prophoto": ((1.346, -0.2556, -0.0511), (-0.5446, 1.5082, 0.0205), (0.0, 0.0, 1.2123)),
    }
    if gamut in published:
        source = np.linalg.inv(np.array(published[gamut]))
    if white != d65:
        cat02 = np.array(((0.7328, 0.4296, -0.1624), (-0.7036, 1.6975, 0.0061), (0.0030, 0.0136, 0.9834)))
        def xyz(w):
            return np.array((w[0] / w[1], 1, (1 - sum(w)) / w[1]))
        source = np.linalg.inv(cat02) @ np.diag((cat02 @ xyz(d65)) / (cat02 @ xyz(white))) @ cat02 @ source
    return (np.linalg.inv(_rgb_to_xyz(*primaries["709"])) @ source).astype(np.float32)


def _signed_power(rgb, gamma):
    return np.copysign(np.power(np.abs(rgb), np.float32(gamma)), rgb)


def decode_output(rgb, parameters):
    """Decode the selected OFX output into Blender's linear Rec.709 contract."""
    if output_is_data(parameters):
        return rgb
    role = _choice_label("outputRole", parameters).lower()
    if "hdr" in role:
        white = max(float(parameters.get("hdrReferenceWhiteNits", 203)), 1)
        peak = max(float(parameters.get("hdrPeakNits", 1000)), white + 1)
        transfer = _choice_label("hdrTransfer", parameters).lower()
        encoded = np.maximum(rgb, 0)
        if "hlg" in transfer:
            a, b, c = 0.17883277, 1 - 4 * 0.17883277, 0.55991073
            linear = np.where(encoded <= 0.5, encoded ** 2 / 3, (np.exp((encoded - c) / a) + b) / 12)
            # Official 0.5.0 uses the BT.2100 luminance OOTF. The old public
            # 0.2 source used a different per-channel curve; do not reuse it.
            gamma = max(1.2 + 0.42 * math.log10(peak / 1000), 1e-6)
            luminance = linear @ np.array([0.2627, 0.6780, 0.0593], dtype=np.float32)
            linear = linear * np.power(np.maximum(luminance, 1e-12)[..., None], gamma - 1) * (peak / white)
        else:
            m1, m2, c1, c2, c3 = 2610 / 16384, 2523 / 32, 3424 / 4096, 2413 / 128, 2392 / 128
            value = np.power(encoded, 1 / m2)
            linear = np.power(np.maximum(value - c1, 0) / np.maximum(c2 - c3 * value, 1e-12), 1 / m1) * (10000 / white)
        return linear @ _to_rec709("2020").T
    if "rcm" in role or "aces" in role:
        # OFX RCM follows its input timeline color space. We supply linear Rec.709.
        return rgb
    encoding = _choice_label("sdrOutputColorSpace", parameters, "Rec.709 Gamma 2.4").lower()
    absolute = np.abs(rgb)
    if "prophoto" in encoding:
        linear = np.copysign(np.where(absolute < 1 / 32, absolute / 16, absolute ** 1.8), rgb)
        gamut = "prophoto"
    elif "srgb" in encoding or "display p3" in encoding:
        linear = np.copysign(np.where(absolute <= 0.04045, absolute / 12.92, ((absolute + 0.055) / 1.055) ** 2.4), rgb)
        gamut = "p3" if "p3" in encoding else "srgb"
    else:
        gamma = 2.6 if "2.6" in encoding or "dci" in encoding else (563 / 256 if "adobe" in encoding else (2.2 if "2.2" in encoding else 2.4))
        linear = rgb if "linear" in encoding else _signed_power(rgb, gamma)
        gamut = "adobe" if "adobe" in encoding else ("dci" if "dci" in encoding else ("p3" if "p3" in encoding else "709"))
    return linear if gamut == "709" else linear @ _to_rec709(gamut).T


def output_is_data(parameters):
    """Cineon/ADX negative outputs retain their native numeric density encoding."""
    return int(parameters.get("process", 0)) == 1 and int(parameters.get("negativeScanOutput", 0)) != 0


def render_rgba(rgba, *, parameters=None, frame=0.0, plugin_path=""):
    if rgba.ndim != 3 or rgba.shape[2] != 4 or min(rgba.shape[:2]) <= 0:
        raise ValueError("Spektrafilm OFX expects an H × W × 4 image")
    if rgba.dtype != np.float32 or not rgba.flags.c_contiguous:
        raise ValueError("Expected a contiguous float32 RGBA buffer")
    if not math.isfinite(frame) or not np.isfinite(rgba).all():
        raise ValueError("Spektrafilm OFX requires finite pixels and frame time")
    error = backend_error(plugin_path)
    if error:
        raise RuntimeError(error)
    values = _linear_input(_validated_sparse(parameters))
    for seed in ("grainSeed", "glareSeed"):
        if seed in ofx_schema.BY_KEY:
            values.setdefault(seed, 0)
    height, width = rgba.shape[:2]
    with tempfile.TemporaryDirectory(prefix="photo-ofx-render-") as folder:
        folder = Path(folder)
        source, destination, request = folder / "input.f32", folder / "output.f32", folder / "request.json"
        rgba.tofile(source)
        metadata_path = folder / "effective.json"
        request.write_text(json.dumps(dict(width=width, height=height, frame=float(frame), parameters=values,
                                          result_metadata=str(metadata_path))), encoding="utf-8")
        _run(["render", discover_plugin(plugin_path), request, source, destination])
        if not destination.is_file() or destination.stat().st_size != rgba.nbytes:
            raise RuntimeError("Spektrafilm OFX returned an incomplete frame")
        output = np.fromfile(destination, dtype=np.float32).reshape(rgba.shape)
        if not metadata_path.is_file():
            raise RuntimeError("The OFX host did not return effective output-encoding metadata")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        _check_metadata(metadata)
        for p in metadata.get("parameters", ()):
            value = p.get("value", p.get("default"))
            values[p["name"]] = value[0] if isinstance(value, list) and len(value) == 1 else value
    output[..., :3] = decode_output(output[..., :3], values)
    if not np.isfinite(output).all():
        raise RuntimeError("Spektrafilm OFX returned invalid pixels")
    return output


def render(rgb, **recipe):
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("Spektrafilm OFX expects an H × W × 3 image")
    rgba = np.empty((*rgb.shape[:2], 4), dtype=np.float32)
    rgba[..., :3], rgba[..., 3] = rgb, 1
    return render_rgba(rgba, **recipe)[..., :3]
