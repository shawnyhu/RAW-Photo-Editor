"""Recover two packed image views and check Blender can repack both after reopening.

blender -b --offline-mode --python-exit-code 1 --python tests/workspace_multiview.py
"""
from hashlib import sha256
from pathlib import Path
import sys
import tempfile

import bpy
import numpy as np
import OpenImageIO as oiio

if "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons:
    from bl_ext.user_default.raw_photo_editor import workspace_resources
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from blender_raw import workspace_resources


def write_png(path, color):
    image = oiio.ImageOutput.create(str(path))
    assert image.open(str(path), oiio.ImageSpec(12, 8, 3, oiio.UINT8))
    assert image.write_image(np.full((8, 12, 3), color, dtype=np.uint8))
    assert image.close()


def verify(image, expected, folder):
    assert not image.packed_files and image.is_stereo_3d
    image.buffers_free()
    image.reload()
    assert tuple(image.size) == (12, 8)
    image.pack()
    # This reads Blender's internal per-eye paths. Checking only Image.filepath
    # misses the case where both files exist but the right eye still uses its old path.
    actual = {entry.view: sha256(entry.packed_file.data).digest() for entry in image.packed_files}
    assert actual == expected
    for entry in image.packed_files:
        path = Path(bpy.path.abspath(entry.filepath)).resolve()
        assert path.is_relative_to(folder)
        assert sha256(path.read_bytes()).digest() == expected[entry.view]
    image.unpack(method='USE_ORIGINAL')


with tempfile.TemporaryDirectory(prefix="Photo Editor stereo ") as tmp:
    folder = Path(tmp).resolve()
    sources = (folder / "stereo-first.png", folder / "stereo-second.png")
    for path, color in zip(sources, ((215, 3, 20), (7, 71, 193))):
        write_png(path, color)
    expected = {index: sha256(path.read_bytes()).digest() for index, path in enumerate(sources)}
    assert expected[0] != expected[1]
    render = bpy.context.scene.render
    render.use_multiview = True
    for view, suffix in zip(render.views, ("-first", "-second")):
        view.file_suffix = suffix
    image = bpy.data.images.load(str(sources[0]))
    image.name, image.use_fake_user = "Packed stereo", True
    image.use_multiview = True
    image.views_format = 'INDIVIDUAL'
    image.pack()
    assert image.is_stereo_3d and len(image.packed_files) == 2
    collision = image.copy()
    collision.name, collision.use_fake_user = "Case-sensitive file names", True
    for entry, filename in zip(collision.packed_files, ("eyes_l.png", "eyes_L.png")):
        entry.filepath = str(folder / str(entry.view) / filename)
    image_names = (image.name, collision.name)
    project = folder / "Recovered.blend"
    assert bpy.ops.wm.save_as_mainfile(filepath=str(project)) == {'FINISHED'}
    render.use_multiview = False
    for view, suffix in zip(render.views, ("_L", "_R")):
        view.file_suffix = suffix
    for path in sources:
        path.unlink()

    assert bpy.ops.wm.save_as_mainfile(filepath=str(project)) == {'FINISHED'}
    assert bpy.ops.wm.open_mainfile(filepath=str(project)) == {'FINISHED'}
    render = bpy.context.scene.render
    for name in image_names:
        image = bpy.data.images[name]
        assert image.is_stereo_3d and len(image.packed_files) == 2
        workspace_resources._unpack_media(image)
        verify(image, expected, folder)
    assert not render.use_multiview and [view.file_suffix for view in render.views] == ["_L", "_R"]
    assert bpy.ops.wm.save_as_mainfile(filepath=str(project)) == {'FINISHED'}
    assert bpy.ops.wm.open_mainfile(filepath=str(project)) == {'FINISHED'}
    for name in image_names:
        verify(bpy.data.images[name], expected, folder)
    print("PASS both external image views repacked with their original bytes after save and reopen", flush=True)
