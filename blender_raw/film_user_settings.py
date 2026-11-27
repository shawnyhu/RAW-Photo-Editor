# SPDX-License-Identifier: GPL-3.0-or-later
"""Transfer a Spektrafilm Lite look between nodes without touching the photo or preview."""
import json

from . import film_node, spektrafilm_engine as engine
from .film_effects import BY_KEY, DIR_MATRIX_PARAMETERS, FAMILIES, FIXED_EXPOSURES, GRAIN_MODELS, PROCESS_MODES


_BASE = ("film", "paper", "process", "scanNegativeInvert", "grain", "grain_model", "halation")
_CONTROLS = (*_BASE, *BY_KEY)
_METADATA = ("preset_parameters", "preset_film", "use_stock_calibration", "preset_name")
_NATIVE_KEYS = {"grain": "grainEnabled", "grain_model": "grainModel", "halation": "halationEnabled"}
_CHOICES = {"process": PROCESS_MODES, "grain_model": GRAIN_MODELS,
            "cameraDiffusionFamily": FAMILIES, "printDiffusionFamily": FAMILIES}
_VECTORS = {key for key, effect in BY_KEY.items() if isinstance(effect.default, tuple)}
# The four stock-bound DIR curves can live in an old preset even when a manual
# edit overrides one of them. Keep that inheritance when selecting stocks later.
_CONTROLLED_NATIVE = {_NATIVE_KEYS.get(key, key) for key in _CONTROLS} - set(DIR_MATRIX_PARAMETERS)
_EXCLUDED_NATIVE = _CONTROLLED_NATIVE | engine.HOST_PARAMETERS | FIXED_EXPOSURES


def _value(node, key):
    value = getattr(node, key)
    return list(value) if key in _VECTORS else value


def snapshot(node):
    controls = {key: _value(node, key) for key in _CONTROLS
                if key not in DIR_MATRIX_PARAMETERS or node.is_property_set(key)}
    original = json.loads(node.preset_parameters or "{}")
    if not isinstance(original, dict):
        raise ValueError("Invalid stored Spektrafilm settings")
    settings = {
        "controls": controls,
        "parameters": {key: value for key, value in original.items() if key not in _EXCLUDED_NATIVE},
        "calibration_film": node.preset_film,
        "use_stock_calibration": node.use_stock_calibration,
    }
    _validated_values(settings)
    return settings


def _validated_values(settings):
    if not isinstance(settings, dict):
        raise ValueError("Invalid Spektrafilm Lite settings")
    controls = settings.get("controls")
    if not isinstance(controls, dict) or not set(_BASE).issubset(controls) or controls.keys() - _CONTROLS:
        raise ValueError("Invalid Spektrafilm Lite controls")
    for key, value in controls.items():
        if key in ("film", "paper"):
            choices = engine.stocks()[0 if key == "film" else 1]
            if not isinstance(value, str) or (value and value not in choices):
                raise ValueError(f"Unknown {key} stock {value!r}")
            continue
        if key in _CHOICES:
            if value not in _CHOICES[key]:
                raise ValueError(f"Unknown {key} option {value!r}")
            value = _CHOICES[key].index(value)
        engine.validate_parameter(_NATIVE_KEYS.get(key, key), value)
    engine.validate_stock_pair(controls["film"], controls["paper"], PROCESS_MODES.index(controls["process"]))

    parameters = settings.get("parameters")
    if not isinstance(parameters, dict) or _EXCLUDED_NATIVE.intersection(parameters):
        raise ValueError("Invalid Spektrafilm Lite native parameters")
    for key, value in parameters.items():
        engine.validate_parameter(key, value)
    if parameters.get("printTiming") == 1:
        raise ValueError("APD printer density requires SMPTE calibration data unavailable in this build")
    film = settings.get("calibration_film")
    if not isinstance(film, str) or "\0" in film or type(settings.get("use_stock_calibration")) is not bool:
        raise ValueError("Invalid Spektrafilm Lite stock calibration")
    film.encode("utf-8")

    values = dict(controls)
    if parameters:
        values["preset_parameters"] = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    if film:
        values["preset_film"] = film
    if settings["use_stock_calibration"]:
        values["use_stock_calibration"] = True
    return values


def _replace(node, values):
    for key in (*_METADATA, *_CONTROLS):
        if key in values:
            setattr(node, key, values[key])
        elif node.is_property_set(key):
            node.property_unset(key)


def apply(node, settings):
    values = _validated_values(settings)
    # Capture explicitly stored properties so rollback also restores unset DIR
    # coefficients and the name of any legacy imported look.
    previous = {key: _value(node, key) for key in (*_METADATA, *_CONTROLS) if node.is_property_set(key)}
    pointer = node.as_pointer()
    film_node._applying_user_preset.add(pointer)
    try:
        _replace(node, values)
    except Exception:
        _replace(node, previous)
        raise
    finally:
        film_node._applying_user_preset.discard(pointer)
    film_node._request(node)
