# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned public parameter descriptors read from the official OFX plug-in.

The JSON is host-observed interface metadata, not extracted shader/profile data.
Keeping it here lets Blender register and reopen a project before OFX is installed.
"""
from dataclasses import dataclass
import json
from pathlib import Path


HOST_PARAMETERS = frozenset({"inputColorSpace", "rcmInputColorSpace", "sceneOutputColorSpace",
                             "quickInputColorSpace", "playbackInputColorSpace"})
# These controls operate the host's preset browser, not its image processor.
MANAGEMENT_PARAMETERS = frozenset({
    "presetSelection", "presetName", "presetCategory", "presetSearch", "presetPath",
    "lutSize", "lutExportSize", "lutExportPath", "lutExportName", "lutName", "lutPath",
})
LEGACY_PARAMETERS = frozenset({"instanceSchemaVersion", "negativeLeucoCyanCoupling", "printCurveSource",
                               "dirSafeControlEnvelope", "halationHotHighlights"})


@dataclass(frozen=True)
class Parameter:
    key: str
    label: str
    group: str
    kind: str
    default: object
    minimum: float = 0.0
    maximum: float = 1.0
    options: tuple = ()
    secret: bool = False
    enabled: bool = True


def _kind(value):
    value = value.removeprefix("OfxParamType").lower()
    return {"boolean": "bool", "integer": "int", "double": "float", "rgb": "vector",
            "rgba": "vector", "double2d": "vector", "double3d": "vector",
            "integer2d": "vector", "integer3d": "vector", "choice": "choice",
            "string": "string", "group": "group", "pushbutton": "button"}.get(value, value)


def _scalar(value, fallback):
    if isinstance(value, list):
        return value[0] if value else fallback
    return fallback if value is None else value


def _read():
    path = Path(__file__).parent / "vendor" / "spektrafilm_ofx" / "parameters.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data["parameters"]
    groups = [(p["name"], p.get("label") or p["name"]) for p in entries if _kind(p["type"]) == "group"]
    params = []
    for p in entries:
        kind = _kind(p["type"])
        if kind not in {"bool", "int", "float", "choice", "vector", "string"}:
            continue
        default = p.get("value", p.get("default"))
        if kind == "vector":
            default = tuple(default or (0.0, 0.0, 0.0))
        elif kind == "bool":
            default = bool(_scalar(default, False))
        elif kind in {"int", "choice"}:
            default = int(_scalar(default, 0))
        elif kind == "float":
            default = float(_scalar(default, 0.0))
        else:
            default = str(_scalar(default, ""))
        if p["name"] in {"grainSeed", "glareSeed"}:
            # A still-image recipe needs a stable seed across isolated workers.
            default = 0
        options = tuple(p.get("options") or ())
        minimum = _scalar(p.get("minimum", p.get("min")), -1.0e6)
        maximum = _scalar(p.get("maximum", p.get("max")), 1.0e6)
        if kind == "choice":
            minimum, maximum = 0, len(options) - 1
        group = "scannerGroup" if p["name"] == "glareSeed" else p.get("group") or ""
        label = "Glare Seed" if p["name"] == "glareSeed" else p.get("label") or p["name"]
        params.append(Parameter(p["name"], label, group,
                                kind, default, minimum, maximum, options,
                                bool(p.get("secret", False)), bool(p.get("enabled", True))))
    if any(not p.group for p in params):
        groups.insert(0, ("", "Processing"))
    return data, tuple(groups), tuple(params)


METADATA, GROUPS, ALL_PARAMETERS = _read()
ALL_BY_KEY = {p.key: p for p in ALL_PARAMETERS}
CHOICE_DEPENDENCIES = METADATA.get("choice_dependencies", {})


def _editable(p):
    if p.key in {"filmProfileId", "paperProfileId"}:
        return True
    if p.key in HOST_PARAMETERS | MANAGEMENT_PARAMETERS | LEGACY_PARAMETERS or p.kind == "string":
        return False
    if p.key.startswith(("playback", "quick")):
        return False
    if p.group.startswith(("telecine", "sprocketsWriting")):
        return False  # Explicitly reserved/non-rendering controls in the official descriptor.
    if p.key in {"quality", "gpuRenderTiling"}:
        return True
    return not p.group.lower().startswith(("manage", "info", "lut"))


PARAMETERS = tuple(p for p in ALL_PARAMETERS if _editable(p))
BY_KEY = {p.key: p for p in PARAMETERS}
SCHEMA_VERSION = str(METADATA.get("version", f"{METADATA.get('version_major', 0)}.{METADATA.get('version_minor', 5)}"))


def choice_options(key, values=None):
    dependency = CHOICE_DEPENDENCIES.get(key)
    if dependency:
        control = dependency["control"]
        index = str((values or {}).get(control, ALL_BY_KEY[control].default))
        return tuple(dependency["values"][index]["options"])
    return ALL_BY_KEY[key].options
