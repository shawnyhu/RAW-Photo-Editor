# SPDX-License-Identifier: GPL-3.0-or-later
"""Read and write Spektrafilm's native .spkpreset snapshot format.

The numeric snapshot kinds, SPKDFLT2 framing and reversible byte transform are
defined in upstream SpektraFilmPlugin.cpp (86476af), not the Mac .sfpreset format.
"""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile

from . import ofx_schema, spektrafilm_ofx_engine as engine


_LIMIT = 4 * 1024 * 1024
_FORMAT = "spektrafilm-preset-v1"
_ENCODING = "obfuscated-snapshot-hex"
_KEY = b"SpektraFilmOFX1!"
_COMPONENTS = {1: 1, 2: 1, 3: 1, 4: 2, 5: 3}


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Repeated OFX preset key {key!r}")
        result[key] = value
    return result


def _transform(payload):
    return bytes(value ^ ((_KEY[index % len(_KEY)] + index * 37) & 255)
                 for index, value in enumerate(payload))


def _snapshot(payload):
    lines = payload.decode("utf-8").splitlines()
    if not lines or lines[0] != "SPKDFLT2":
        raise ValueError("Unsupported Spektrafilm OFX snapshot version")
    values = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < 4:
            raise ValueError("Incomplete OFX preset parameter")
        key = fields[0]
        if key in values:
            raise ValueError(f"Repeated OFX preset parameter {key!r}")
        kind, count = int(fields[1]), int(fields[2])
        if kind not in _COMPONENTS or _COMPONENTS[kind] != count or len(fields) != count + 3:
            raise ValueError(f"Invalid OFX preset parameter shape for {key}")
        numbers = [float(value) if kind >= 3 else int(value) for value in fields[3:]]
        if not all(math.isfinite(value) for value in numbers):
            raise ValueError(f"Non-finite OFX preset value for {key}")
        if kind == 2 and numbers[0] not in (0, 1):
            raise ValueError(f"Invalid OFX preset switch for {key}")
        value = tuple(numbers) if count > 1 else bool(numbers[0]) if kind == 2 else numbers[0]
        values[key] = (kind, value)
    if not values:
        raise ValueError("The Spektrafilm OFX preset has no parameters")
    return values


def _expected_kind(parameter):
    if parameter.kind == "bool":
        return 2
    if parameter.kind in {"int", "choice", "stock"}:
        return 1
    if parameter.kind == "float":
        return 3
    if parameter.kind == "vector" and len(parameter.default) in (2, 3):
        return len(parameter.default) + 2
    return None


def read(path):
    """Return validated native values and explicit notices for omitted host fields."""
    path = Path(path)
    with path.open("rb") as source:
        raw = source.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ValueError("The Spektrafilm OFX preset exceeds 4 MiB")
    document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_keys)
    if not isinstance(document, dict) or document.get("format") != _FORMAT:
        raise ValueError("Expected a Spektrafilm OFX .spkpreset; Mac .sfpreset files belong to Spektrafilm Lite")
    if document.get("payload_encoding") != _ENCODING:
        raise ValueError("Unsupported Spektrafilm OFX preset payload encoding")
    name = document.get("name", path.stem)
    if not isinstance(name, str) or not name.strip() or "\0" in name:
        raise ValueError("Invalid OFX preset name")
    name.encode("utf-8")
    payload = document.get("payload_hex")
    if not isinstance(payload, str) or not payload or len(payload) % 2:
        raise ValueError("Invalid Spektrafilm OFX preset payload")
    snapshot = _snapshot(_transform(bytes.fromhex(payload)))
    values, ignored, host_fields = {}, [], []
    for key, (kind, value) in snapshot.items():
        if key in ofx_schema.HOST_PARAMETERS:
            host_fields.append(key)
            continue
        parameter = ofx_schema.BY_KEY.get(key)
        if parameter is None:
            ignored.append(key)
            continue
        if kind != _expected_kind(parameter):
            raise ValueError(f"OFX preset parameter type does not match {key}")
        if parameter.kind == "stock":
            if value < 0 or value >= len(parameter.options):
                raise ValueError(f"Unknown stock index in OFX preset parameter {key}")
            value = parameter.options[value]
        engine.validate_parameter(key, value)
        values[key] = value
    if not values:
        raise ValueError("The preset contains no supported OFX processing settings")
    engine.normalize_parameters(values)
    notices = []
    if host_fields:
        notices.append("Input encoding follows Blender's linear compositor image. Preset input color-space settings were adapted.")
    if ignored:
        names = ", ".join(ignored[:8]) + (f" and {len(ignored) - 8} more" if len(ignored) > 8 else "")
        notices.append("Preset parameters unavailable in this OFX schema were skipped: " + names)
    return name.strip(), values, notices


def write(path, name, parameters):
    """Write a native OFX snapshot atomically after validating every value."""
    if not isinstance(name, str) or not name.strip() or "\0" in name:
        raise ValueError("Invalid OFX preset name")
    name.encode("utf-8")
    engine.normalize_parameters(parameters)
    lines = ["SPKDFLT2"]
    for parameter in ofx_schema.PARAMETERS:
        kind = _expected_kind(parameter)
        if kind is None:
            continue  # OFX snapshots contain numeric processing settings only.
        value = parameters.get(parameter.key, parameter.default)
        if parameter.kind == "stock":
            value = parameter.options.index(value)
        values = tuple(value) if kind in (4, 5) else (value,)
        encoded = " ".join(format(float(item), ".17g") if kind >= 3 else str(int(item)) for item in values)
        lines.append(f"{parameter.key} {kind} {len(values)} {encoded}")
    payload = _transform(("\n".join(lines) + "\n").encode("utf-8")).hex()
    document = {"format": _FORMAT, "name": name.strip(),
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "plugin": "Spektrafilm OFX", "version": "RAW Photo Editor",
                "payload_encoding": _ENCODING, "payload_hex": payload}
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=path.name + ".", suffix=".tmp",
                                         delete=False) as target:
            temporary = Path(target.name)
            json.dump(document, target, ensure_ascii=False, indent=2, allow_nan=False)
            target.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
