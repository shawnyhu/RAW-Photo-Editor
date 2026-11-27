# SPDX-License-Identifier: GPL-3.0-or-later
"""Named Spektrafilm Lite presets shared by Blender versions for this OS user."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile
import unicodedata
from uuid import uuid4


def _directory():
    if sys.platform == "win32":
        roaming = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        blender = Path(roaming) / "Blender Foundation" / "Blender"
    elif sys.platform == "darwin":
        blender = Path.home() / "Library" / "Application Support" / "Blender"
    else:
        config = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
        blender = Path(config) / "blender"
    return blender / "RAW Photo Editor" / "Spektrafilm Lite"


def _name(value):
    if not isinstance(value, str):
        raise ValueError("Enter a preset name")
    value = unicodedata.normalize("NFC", value.strip())
    if not value or any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError("Enter a non-empty preset name without control characters")
    return value


def _path(preset_id):
    if not isinstance(preset_id, str) or not re.fullmatch("[0-9a-f]{32}", preset_id):
        raise ValueError("Invalid preset ID")
    return _directory() / (preset_id + ".json")


@contextmanager
def _writing():
    """Serialize name checks and mutations across Blender instances. Closing releases the lock."""
    directory = _directory()
    directory.mkdir(mode=0o700, exist_ok=True, parents=True)
    with (directory / ".lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError) as exc:
            raise OSError("The preset library is busy. Please try again") from exc
        yield


def _read(path):
    with path.open(encoding="utf-8") as stream:
        doc = json.load(stream)
    if (not isinstance(doc, dict) or type(doc.get("version")) is not int or doc["version"] != 1
            or not isinstance(doc.get("settings"), dict)):
        raise ValueError("Unsupported Spektrafilm Lite user preset")
    return _name(doc.get("name")), doc["settings"]


def list_presets():
    """Return (persistent ID, display name) pairs. A damaged file cannot hide other presets."""
    presets = []
    for path in _directory().glob("*.json"):
        if not re.fullmatch("[0-9a-f]{32}", path.stem):
            continue
        try:
            name, _ = _read(path)
        except (FileNotFoundError, ValueError):
            continue
        presets.append((path.stem, name))
    return sorted(presets, key=lambda item: (item[1].casefold(), item[0]))


def _write(preset_id, name, settings):
    name = _name(name)
    if any(other != preset_id and label.casefold() == name.casefold() for other, label in list_presets()):
        raise ValueError("A preset with this name already exists")
    if not isinstance(settings, dict):
        raise ValueError("Expected a dictionary of Spektrafilm Lite settings")
    doc = {"version": 1, "name": name, "settings": settings}
    data = json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False)
    path = _path(preset_id)
    stream = NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", suffix=".tmp", delete=False)
    temp_path = Path(stream.name)
    try:
        with stream:
            stream.write(data)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def save(name, settings):
    preset_id = uuid4().hex
    with _writing():
        _write(preset_id, name, settings)
    return preset_id


def load(preset_id):
    return _read(_path(preset_id))[1]


def rename(preset_id, name):
    with _writing():
        _write(preset_id, name, load(preset_id))


def delete(preset_id):
    with _writing():
        _path(preset_id).unlink()
