# SPDX-License-Identifier: GPL-3.0-or-later
"""Move packed Geometry Nodes bakes into the project's workspace."""

from pathlib import Path
import shutil
import tempfile

import bpy


def _set_path(bake, path):
    # Bake directories support Blender path templates. Escape literal braces,
    # or Blender may silently choose its default bake directory instead.
    path = path.replace("{", "{{").replace("}", "}}")
    if len(path.encode("utf-8")) >= 1024:
        raise ValueError("Geometry Nodes bake directory exceeds Blender's path limit")
    bake.use_custom_path = True
    bake.directory = path
    if bake.directory != path:
        raise ValueError("Blender could not store the Geometry Nodes bake directory")


def _unpack(obj, modifier_name, bake_id):
    with bpy.context.temp_override(object=obj, active_object=obj):
        return 'FINISHED' in bpy.ops.object.geometry_node_bake_unpack_single(
            session_uid=obj.session_uid, modifier_name=modifier_name, bake_id=bake_id,
            method='USE_ORIGINAL')


def _unpack_one(obj, modifier, bake, copy, staging, directory):
    # RNA lists potential bakes without exposing whether a packed payload exists.
    # Unpacking a disposable object copy leaves the original intact on I/O failure.
    copied = next(b for b in copy.modifiers[modifier.name].bakes if b.bake_id == bake.bake_id)
    _set_path(copied, str(staging))
    if not _unpack(copy, modifier.name, copied.bake_id):
        return 0
    files = {p.relative_to(staging): p.stat().st_size for p in staging.rglob("*") if p.is_file()}
    if not any(p.parent == Path("meta") and size for p, size in files.items()):
        raise OSError(f"Blender produced no frame metadata for {obj.name} / {modifier.name}")

    parent = Path(directory()) / "bakes"
    parent.mkdir(parents=True, exist_ok=True)
    name = bpy.path.clean_name(obj.name)[:32] or "Object"
    target = Path(tempfile.mkdtemp(prefix=f"{name}-{bake.bake_id}-", dir=parent))
    original = bake.use_custom_path, bake.directory
    try:
        for child in staging.iterdir():
            shutil.move(child, target / child.name)
        if any((target / p).stat().st_size != size for p, size in files.items()):
            raise OSError(f"Incomplete Geometry Nodes bake in {target}")
        # With verified metadata already present, Blender USE_ORIGINAL binds
        # the disk bake and drops only this original's packed data.
        try:
            filepath = bpy.path.relpath(str(target))
        except ValueError:  # Windows cannot express a relative path across drives.
            filepath = str(target)
        _set_path(bake, filepath)
        if not _unpack(obj, modifier.name, bake.bake_id):
            raise RuntimeError(f"Could not bind the unpacked bake for {obj.name} / {modifier.name}")
    except Exception:
        bake.use_custom_path, bake.directory = original
        shutil.rmtree(target)
        raise
    return 1


def unpack_project_bakes(directory):
    """Return the number of packed local bakes moved to directory()/bakes."""
    count = 0
    for obj in list(bpy.data.objects):
        if obj.library or not obj.is_editable:
            continue
        modifiers = [m for m in obj.modifiers if m.type == 'NODES' and m.bakes]
        if not modifiers:
            continue
        copy = obj.copy()
        try:
            for modifier in modifiers:
                for bake in modifier.bakes:
                    with tempfile.TemporaryDirectory(prefix="photo-editor-bake-") as temporary:
                        count += _unpack_one(obj, modifier, bake, copy, Path(temporary), directory)
        finally:
            bpy.data.objects.remove(copy)
    return count
