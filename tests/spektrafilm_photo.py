"""Develop a real RAW, preview, edit, and batch export with the installed extension.

blender -b --offline-mode --python-exit-code 1 --python tests/spektrafilm_photo.py -- PHOTO.NEF
"""
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import sys
import tempfile
import time

import bpy
import numpy as np
import OpenImageIO as oiio

from bl_ext.user_default.raw_photo_editor import catalog, compositor, film_bake, film_node, film_preview, jobs, worker


def wait_for_preview(node):
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        jobs.tick()
        film_preview._tick()
        if not film_preview._pending and not film_preview._job:
            assert node.status == "Ready", node.status
            return node.node_tree.nodes["Film Result"].image
        time.sleep(.2)
    raise TimeoutError(node.status)


source = Path(sys.argv[-1]).resolve()
photo, _ = catalog.add_photo(bpy.context, source)
tree = compositor.ensure_tree(bpy.context, photo)
developer = next(n for n in tree.nodes if n.bl_idname == compositor.DEVELOPER_TYPE)
developer.auto_exposure = "CENTER"
for recipe in (None, developer):
    req = catalog.request(photo, "preview", recipe=recipe)
    if not Path(req["output"]).exists():
        worker.process_image(req)
    if recipe is None:
        catalog.apply_image(photo, req)
    else:
        catalog.apply_developer_image(photo, developer, req)
compositor.open_photo(bpy.context, photo)
scene = bpy.context.scene
assert scene.view_settings.view_transform == "Standard"
assert bpy.ops.blender_raw.add_spektrafilm(tree_name=tree.name, node_name=developer.name) == {"FINISHED"}
film = tree.nodes.active
assert film.bl_idname == film_node.FILM_TYPE
assert not film.live_preview
film.film, film.paper = "kodak_portra_800", "kodak_2383"
film.live_preview = True
first = wait_for_preview(film)
assert tuple(first.size) == tuple(photo.image.size)
assert scene.view_settings.view_transform == "Standard"
with closing(oiio.ImageInput.open(first.filepath)) as reader:
    reference = reader.read_image(format=oiio.FLOAT)[::43, ::59, :3].copy()
developer.exposure += 1
film.cameraDiffusionEnabled = True
film.printDiffusionEnabled = True
film.scannerEnabled = True
second = wait_for_preview(film)
with closing(oiio.ImageInput.open(second.filepath)) as reader:
    revised = reader.read_image(format=oiio.FLOAT)[::43, ::59, :3]
assert np.isfinite(revised).all() and np.max(np.abs(revised - reference)) > .01
proxy = tuple(second.size)
with tempfile.TemporaryDirectory() as temp:
    folder = Path(temp)
    # Compare a metered, manually adjusted Developer against Blender's actual compositor.
    assert film_bake.direct_raw_input(scene, film, folder)
    captured = folder / "captured.exr"
    with patch.object(film_bake, 'direct_raw_input', return_value=None):
        film_bake.preview(scene, tree, film, str(captured))
    with closing(oiio.ImageInput.open(str(captured))) as reader, closing(oiio.ImageInput.open(second.filepath)) as direct:
        np.testing.assert_allclose(direct.read_image(format=oiio.FLOAT), reader.read_image(format=oiio.FLOAT),
                                   rtol=8e-4, atol=2e-4)
    extra = tree.nodes.new('CompositorNodeExposure')
    tree.links.new(developer.outputs['Image'], extra.inputs['Image'])
    tree.links.new(extra.outputs['Image'], film.inputs['Image'])
    assert film_bake.direct_raw_input(scene, film, folder) is None
    tree.nodes.remove(extra)
    tree.links.new(developer.outputs['Image'], film.inputs['Image'])
film.live_preview = False

def export(workspace, suffix):
    jobs.start_export(bpy.context, [photo], workspace)
    deadline = time.monotonic() + 600
    while jobs.export_running() and time.monotonic() < deadline:
        jobs.tick()
        time.sleep(.2)
    assert not jobs.export_running() and not jobs.export_details(), (jobs.export_status(), jobs.export_details())
    result, = Path(workspace).glob("*" + suffix)
    return result


with tempfile.TemporaryDirectory(prefix="photo-editor-film-export-") as workspace:
    fmt = scene.render.image_settings
    fmt.file_format, fmt.color_mode, fmt.quality = "JPEG", "RGB", 100
    result = export(workspace, ".jpg")
    with closing(oiio.ImageInput.open(str(result))) as reader:
        spec = reader.spec()
        size = spec.width, spec.height
        assert max(size) > max(proxy) and spec.get_string_attribute('jpeg:subsampling') == '4:4:4'
        assert np.ptp(reader.read_image(format=oiio.UINT8)[::89, ::79, :3]) > 16
    fmt.file_format, fmt.color_depth = "TIFF", "16"
    result = export(workspace, ".tif")
    with closing(oiio.ImageInput.open(str(result))) as reader:
        spec = reader.spec()
        assert (spec.width, spec.height) == size and str(spec.format) == 'uint16'
        assert np.ptp(reader.read_image(format=oiio.FLOAT)[::89, ::79, :3]) > .05
    print(f"PASS {source.name}: two {proxy} film previews, Standard view, Developer exposure, "
          f"camera/print diffusion and scanner, full-resolution {size} JPEG 100 (4:4:4) and 16-bit TIFF")
