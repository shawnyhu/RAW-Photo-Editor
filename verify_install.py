"""Check the active extension in the isolated profile before running regression tests."""
import json
import os
import platform
import struct
import tomllib
from pathlib import Path

import bpy
from bl_ext.user_default import raw_photo_editor

module = Path(raw_photo_editor.__file__).resolve()
profile = Path(os.environ["BLENDER_USER_RESOURCES"]).resolve()
assert module.is_relative_to(profile), module
assert raw_photo_editor.__name__ in bpy.context.preferences.addons
manifest = tomllib.loads(module.with_name("blender_manifest.toml").read_text(encoding="utf-8"))
assert (manifest["id"], manifest["version"]) == ("raw_photo_editor", "1.1.1"), manifest
assert bpy.app.version[:2] == (5, 3) and struct.calcsize("P") == 8, bpy.app.version
info = {"blender_version": bpy.app.version_string, "blender_build": bpy.app.build_hash.decode(),
        "os": platform.platform(), "architecture": platform.machine(), "extension": manifest["version"]}
Path(os.environ["RPE_RUNTIME_INFO"]).write_text(json.dumps(info, indent=2), encoding="utf-8")
print("PASS isolated 1.1.1 install", flush=True)
