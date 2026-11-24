# SPDX-License-Identifier: GPL-3.0-or-later
"""Externalize Blender's packed media when the user confirms opening the workspace."""
from pathlib import Path
import ntpath
import os
import re
import tempfile

import bpy

from . import catalog


def _resource_directory():
    if bpy.data.is_saved:
        project = Path(bpy.data.filepath)
        directory = project.parent / (project.stem + " Resources")
    else:
        # A new project has no directory yet. Use persistent storage until Save As.
        location = bpy.utils.user_resource('DATAFILES', path="Blender Photo Editor/Unpacked", create=True)
        if not location:
            raise OSError("Blender's user data directory is unavailable")
        directory = Path(location)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _original_matches(entry):
    path = entry.filepath
    if not path or (path.startswith("//") and not bpy.data.is_saved):
        return False
    try:
        original = catalog.resolved(path)
        return (original.is_file() and original.stat().st_size == entry.packed_file.size
                and catalog._matches_payload(original, memoryview(entry.packed_file.data)))
    except (OSError, ValueError):
        return False


_IMAGE_EXTENSIONS = {
    'JPEG': 'jpg', 'JPEG2000': 'jp2', 'OPEN_EXR': 'exr', 'OPEN_EXR_MULTILAYER': 'exr',
    'CINEON': 'cin', 'TARGA': 'tga', 'TARGA_RAW': 'tga', 'TIFF': 'tif', 'IRIS': 'rgb',
}


def _filename(owner, entry):
    name = entry.filepath.replace("\\", "/").rsplit("/", 1)[-1]
    if not name:
        name = bpy.path.clean_name(owner.name)
        if isinstance(owner, bpy.types.Image):
            if owner.source == 'TILED':
                name += f".{entry.tile_number}"
            name += "." + _IMAGE_EXTENSIONS.get(owner.file_format, owner.file_format.lower())
    # Imported projects can have paths from a different operating system.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).rstrip(' .') or 'resource'
    if ntpath.isreserved(name):
        name = "_" + name
    return name


def _recover_paths(owner, entries, filenames=None):
    # Keep tiles and stereo views together. Prepare every file before detaching any packed bytes.
    folder = Path(tempfile.mkdtemp(prefix=bpy.path.clean_name(owner.name)[:48] + "-",
                                   dir=_resource_directory()))
    paths = []
    for index, entry in enumerate(entries):
        path = folder / (filenames[index] if filenames else _filename(owner, entry))
        if path.exists():
            # Different views can have identical basenames in their original folders.
            path = folder / str(len(paths)) / path.name
            path.parent.mkdir()
        data = entry.packed_file.data
        with path.open("xb") as stream:
            if stream.write(data) != len(data):
                raise OSError(f"Incomplete resource file: {path.name}")
        paths.append(catalog.stored_path(path))
    return paths


def _recover_views(image, entries):
    # The packed-file paths and Blender's per-eye playback paths are separate.
    # Rebuild the latter while the image still has a readable packed buffer.
    ordered = sorted(entries, key=lambda entry: entry.view)
    files = [Path(_filename(image, entry)) for entry in ordered]
    if len({entry.view for entry in ordered}) != len(files):
        raise RuntimeError(f"Cannot identify each packed view of {image.name}")
    count = len(files)
    names = None
    for scene in (bpy.context.scene, *bpy.data.scenes):
        views = [v for v in scene.render.views if v.use and
                 (scene.render.views_format == 'MULTIVIEW' or v.name in {'left', 'right'})]
        if len(views) == count and all(f.stem.endswith(v.file_suffix) for f, v in zip(files, views)):
            names = [v.name for v in views]
            break
    if names is None:
        if count == 2 and image.is_stereo_3d:
            names = ['left', 'right']
        else:
            raise RuntimeError(f"Cannot identify the named render views of {image.name}")

    # A private scene makes the reconstruction independent of the active
    # project's view toggles and suffixes. Keep original names where possible.
    prefix = os.path.commonprefix([f.stem for f in files])
    suffixes = [f.stem[len(prefix):] for f in files]
    extension = files[0].suffix
    if (not prefix or not all(suffixes) or len({s.casefold() for s in suffixes}) != count or
            any(len(s.encode('utf8')) >= 64 for s in suffixes) or any(f.suffix != extension for f in files)):
        prefix = "view"
        suffixes = [f"_{i + 1}" for i in range(count)]
    filenames = {entry.view: prefix + suffix + extension for entry, suffix in zip(ordered, suffixes)}
    paths = _recover_paths(image, entries, [filenames[entry.view] for entry in entries])
    path_by_view = {entry.view: path for entry, path in zip(entries, paths)}
    scene = bpy.data.scenes.new("Unpack image views")
    previous_path = image.filepath_raw
    try:
        scene.render.use_multiview = True
        scene.render.views_format = 'MULTIVIEW'
        for view in scene.render.views:
            view.use = False
        for name, suffix in zip(names, suffixes):
            view = scene.render.views.get(name) or scene.render.views.new(name)
            view.use = True
            view.file_suffix = suffix
        with bpy.context.temp_override(scene=scene):
            image.filepath_raw = path_by_view[ordered[0].view]
            image.use_multiview = True
        if not image.is_multiview:
            raise RuntimeError(f"Blender could not reconnect all views of {image.name}")
    except Exception:
        image.filepath_raw = previous_path
        raise
    finally:
        bpy.data.scenes.remove(scene)
    return paths


def _unpack_media(owner):
    entries = list(owner.packed_files) if isinstance(owner, bpy.types.Image) else [owner]
    if not all(_original_matches(entry) for entry in entries):
        if isinstance(owner, bpy.types.Image) and len({entry.view for entry in entries}) > 1:
            paths = _recover_views(owner, entries)
        else:
            paths = _recover_paths(owner, entries)
        for entry, path in zip(entries, paths):
            entry.filepath = path
    # All destinations now contain the packed bytes. Let Blender update the ID,
    # including UDIM token handling, through the same path as Unpack Resources.
    if isinstance(owner, bpy.types.Volume):
        volume_type = bpy.types.ID.bl_rna.properties["id_type"].enum_items["VOLUME"].value
        bpy.ops.file.unpack_item(method='USE_ORIGINAL', id_name=owner.name, id_type=volume_type)
    else:
        owner.unpack(method='USE_ORIGINAL')
    if owner.packed_file:
        raise RuntimeError(f"{owner.name} is still packed")


def unpack_project(context):
    from . import workspace_bakes

    workspace_bakes.unpack_project_bakes(_resource_directory)
    lib = catalog.library(context)
    catalog.identify_legacy_caches()
    catalog.externalize_caches(lib)
    if lib.cache_storage_error:
        raise RuntimeError(lib.cache_storage_error)
    for collection in (bpy.data.images, bpy.data.fonts, bpy.data.sounds, bpy.data.volumes):
        for owner in collection:
            if not owner.library and owner.packed_file:
                _unpack_media(owner)
    bpy.data.use_autopack = False
    for scene in bpy.data.scenes:
        if not scene.library and scene.blender_raw.get("pack_cache_images"):
            scene.blender_raw["pack_cache_images"] = False
