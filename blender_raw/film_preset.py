# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the Mac app's version 1 JSON preset and map its native controls to the node."""
import json
from pathlib import Path

from . import spektrafilm_engine as engine
from .film_effects import BY_KEY, DIR_MATRIX_PARAMETERS, FAMILIES, FIXED_EXPOSURES, GRAIN_MODELS, PROCESS_MODES


NODE_PROPERTIES = {
    "film": "film", "paper": "paper", "grainEnabled": "grain", "grainModel": "grain_model",
    "halationEnabled": "halation",
    "process": "process", "scanNegativeInvert": "scanNegativeInvert",
    **{key: key for key in BY_KEY},
}
_SECTIONS = ("bools", "ints", "doubles", "vectors")


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Repeated preset key {key!r}")
        result[key] = value
    return result


def read(path):
    """Return the display name, original typed settings and validated node/native fields."""
    path = Path(path)
    with path.open("rb") as file:
        data = file.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("The Spektrafilm preset exceeds 1 MiB")
    document = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_keys)
    if not isinstance(document, dict) or type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError("Expected a version 1 Spektrafilm preset")
    name = document.get("name", path.stem)
    if not isinstance(name, str) or not name.strip() or "\0" in name:
        raise ValueError("Invalid Spektrafilm preset name")
    name.encode("utf-8")  # Validate before touching either Blender node.
    settings = document.get("settings")
    if not isinstance(settings, dict) or set(settings).difference(_SECTIONS):
        raise ValueError("Invalid preset settings sections")
    fields = {}
    parameters = {}
    for section in _SECTIONS:
        entries = settings.get(section, {})
        if not isinstance(entries, dict):
            raise ValueError(f"Expected an object in {section}")
        for key, value in entries.items():
            if key in FIXED_EXPOSURES:
                continue
            engine.validate_parameter(key, value, section)
            if key == "outputRole" and value != 0:
                raise ValueError("This node supports Display Out SDR presets")
            if key == "outputColorSpace" and value not in (15, 17, 24, 25):
                raise ValueError("This node supports SDR presets with Rec.709 or sRGB output primaries")
            if key == "printTiming" and value == 1:
                raise ValueError("APD printer density requires SMPTE calibration data unavailable in this build")
            if key in engine.HOST_PARAMETERS:
                continue
            # Keep imported stock calibration distinct from subsequent manual RNA edits.
            if key not in NODE_PROPERTIES or key in DIR_MATRIX_PARAMETERS:
                parameters[key] = value
                continue
            if key in ("film", "paper"):
                value = engine.stocks()[0 if key == "film" else 1][value]
            elif key == "grainModel":
                value = GRAIN_MODELS[value]
            elif key == "process":
                value = PROCESS_MODES[value]
            elif key in ("cameraDiffusionFamily", "printDiffusionFamily"):
                value = FAMILIES[value]
            fields[NODE_PROPERTIES[key]] = value
    if not any(settings.values()):
        raise ValueError("The Spektrafilm preset has no settings")
    return name, settings, fields, parameters
