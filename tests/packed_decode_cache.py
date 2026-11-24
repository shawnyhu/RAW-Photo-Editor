"""Blender 5.3: reload packed reader/developer caches and export with no RAW decoder.

blender -b --factory-startup --python-exit-code 1 --python tests/packed_decode_cache.py
"""

from contextlib import closing
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import catalog, compositor, jobs, raw_backend


def developer(photo):
    return next(n for n in photo.tree.nodes if n.bl_idname == compositor.DEVELOPER_TYPE)


def write_cache(state, rgb):
    pixels = np.full((48, 64, 3), rgb, dtype=np.float32)
    raw_backend._write_image(state['output'], pixels, np.eye(3))


def run(root):
    catalog.register()
    compositor.register()
    jobs.register()
    library = catalog.library(bpy.context)
    library.owner = True
    library.preview_resolution = '100'
    source = root / 'fixture.dng'
    # A decoder cannot process this sentinel. Successful export therefore
    # demonstrates that the child Blender process also used both packed EXRs.
    source.write_bytes(b'packed cache test sentinel')
    photo = library.photos.add()
    photo.name, photo.uid, photo.filepath = source.name, 'packed-cache-fixture', str(source)
    catalog.pack_photo(photo)
    compositor.ensure_tree(bpy.context, photo)
    node = developer(photo)
    node.white_balance = 'CUSTOM'
    node.temperature = 4400
    node.exposure = 1.0
    base_req = catalog.request(photo, 'full')
    variant_req = catalog.request(photo, 'full', node)
    assert base_req['key'] != variant_req['key']
    calibrated = [0.0625, 0.25, 0.375]
    write_cache(base_req, [0.25, 0.125, 0.0625])
    write_cache(variant_req, calibrated)
    catalog.apply_image(photo, base_req)
    catalog.apply_developer_image(photo, node, variant_req)
    # A background rebuild can restore an EXR after Blender has attempted to
    # display its missing file. The image ID keeps reporting 0x0 until reloaded.
    variant = node.node_tree.nodes["Developed RAW"].image
    for image, req in ((photo.image, base_req), (variant, variant_req)):
        path = Path(req["output"])
        payload = path.read_bytes()
        image.buffers_free()
        path.unlink()
        assert tuple(image.size) == (0, 0)
        path.write_bytes(payload)
    with patch.object(jobs, "preview_later", side_effect=AssertionError("queued a warm Reader")):
        jobs.open_preview(photo)
    assert not jobs.developing(photo.uid)
    assert tuple(photo.image.size) == tuple(variant.size) == (64, 48)
    np.testing.assert_allclose(variant.pixels[:3], calibrated, atol=2e-6)
    scene = bpy.context.scene
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_depth = '32'
    assert bpy.ops.file.pack_all() == {'FINISHED'}
    project = root / 'packed.blend'
    assert bpy.ops.wm.save_as_mainfile(filepath=str(project)) == {'FINISHED'}

    # Remove only the test original, its extracted copy and these two known caches.
    for path in (source, catalog.source_file(photo), Path(base_req['output']), Path(variant_req['output'])):
        assert path.resolve().is_relative_to(root.resolve())
        path.unlink()
    assert bpy.ops.wm.open_mainfile(filepath=str(project), load_ui=False) == {'FINISHED'}
    library = catalog.library(bpy.context)
    photo = library.photos[0]
    node = developer(photo)
    for recipe in (None, node):
        req = catalog.request(photo, 'preview', recipe)
        assert not Path(req['output']).is_file()
        assert catalog.packed_decode(photo, req, recipe)

    with patch.object(jobs, '_start', side_effect=AssertionError('unexpected decoder job')) as launch:
        jobs.preview_later(photo, immediate=True)
        jobs.developer_later(photo, node)
        time.sleep(.26)
        jobs.tick()
        launch.assert_not_called()
    assert photo.status == 'Ready' and not photo.error and not node.error
    assert tuple(photo.image.size) == (64, 48)
    assert node.node_tree.nodes['Developed RAW'].image.packed_file

    export_dir = root / 'exports'
    jobs.start_export(bpy.context, [photo], export_dir)
    deadline = time.monotonic() + 120
    while jobs.export_running() and time.monotonic() < deadline:
        jobs.tick()
        time.sleep(.1)
    assert not jobs.export_running(), 'Background export timed out'
    assert not jobs.export_details(), jobs.export_details()
    output = export_dir / (source.stem + '.exr')
    with closing(oiio.ImageInput.open(str(output))) as reader:
        rgb = reader.read_image(format=oiio.FLOAT)
    assert rgb.shape[:2] == (48, 64)
    np.testing.assert_allclose(rgb[24, 32, :3], np.multiply(calibrated, 2), atol=2e-6)
    assert not Path(base_req['output']).exists() and not Path(variant_req['output']).exists()

    node.temperature = 6800
    assert catalog.packed_decode(photo, catalog.request(photo, 'preview', node), node) is None
    library.preview_resolution = '50'
    assert catalog.packed_decode(photo, catalog.request(photo, 'preview')) is None
    print('PACKED READER AND DEVELOPER RELOAD, PREVIEW, EXPORT AND INVALIDATION PASSED', flush=True)


with tempfile.TemporaryDirectory(prefix='photo-editor-packed-test-') as folder:
    # Isolate the cache of the host and its spawned Blender exporter, including
    # fallback lookup of caches left by previous releases, from existing files.
    env = dict(TEMP=folder, TMP=folder, TMPDIR=folder)
    with patch.object(tempfile, 'tempdir', folder), patch.dict(os.environ, env):
        try:
            run(Path(folder))
        finally:
            if jobs.export_running():
                jobs.cancel_export()
