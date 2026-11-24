# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan export filenames without renaming any Blender data or originals."""
from itertools import islice
from collections.abc import Mapping
import ntpath
import os
from pathlib import Path
import re

import bpy
from .catalog import original_filename


def export_items(photos, directory, ext, lib):
    """Resolve collisions in Gallery order, using identical names for preview and export."""
    project = Path(bpy.data.filepath).stem or "Untitled"
    used = set()
    next_suffix = {}
    items = []
    extensions = ext if isinstance(ext, Mapping) else None
    for number, photo in enumerate(photos, start=1):
        if extensions is not None:
            ext = extensions[photo.uid]
        stem = Path(original_filename(photo)).stem or "Photo"
        if lib.rename_exports:
            stem = f"{project}_{stem}_{number:03d}"
        if not stem.strip() or re.search(r'[<>:"/\\|?*\x00-\x1f]', stem) or ntpath.isreserved(stem + ext):
            raise ValueError(f"Invalid export filename: {stem + ext!r}")
        folded = (stem + ext).casefold()
        suffix = next_suffix.get(folded, 1)
        while True:
            basename = (stem if suffix == 1 else f"{stem}-{suffix}") + ext
            if len(basename.encode("utf-16-le")) > 510:
                raise ValueError("Export filename exceeds 255 characters")
            dest = directory / basename
            if basename.casefold() not in used and (not os.path.lexists(dest) or (lib.overwrite_exports and dest.is_file())):
                break
            suffix += 1
        used.add(basename.casefold())
        next_suffix[folded] = suffix + 1
        items.append(dict(uid=photo.uid, name=photo.name, path=str(dest)))
    return items


def draw(layout, context, photos, lib, directory):
    box = layout.box()
    box.prop(lib, "rename_exports", text="Batch Rename")
    box.prop(lib, "overwrite_exports")
    # File Browser updates its directory while the modal operator is open.
    space = context.space_data
    if space and space.type == "FILE_BROWSER" and space.params and space.params.directory:
        directory = os.fsdecode(space.params.directory)
    target = Path(bpy.path.abspath(directory))
    try:
        sample = list(islice(photos, 3))
        # The export dialog's settings apply to the complete selected batch.
        examples = export_items(sample, target, context.scene.render.file_extension, lib)
        for entry in examples:
            box.label(text=Path(entry["path"]).name, icon="FILE_IMAGE")
    except (OSError, ValueError) as exc:
        box.label(text=str(exc), icon="ERROR")
