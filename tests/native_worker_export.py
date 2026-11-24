"""Export two native photo scenes through the isolated worker from a real RAW.

blender -b --factory-startup --python-exit-code 1 -P tests/native_worker_export.py -- PHOTO.CR3
Decoder wheels must be installed or available in the local .native-test-deps folder.
Without --factory-startup this tests the enabled installed ZIP instead of the checkout.
"""
from contextlib import closing
from pathlib import Path
import shutil
import importlib
import sys
import tempfile
import time

import bpy
import numpy as np
import OpenImageIO as oiio

installed = 'bl_ext.user_default.raw_photo_editor' in bpy.context.preferences.addons
if installed:
    addon = importlib.import_module('bl_ext.user_default.raw_photo_editor')
else:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    if (root / '.native-test-deps').is_dir():
        sys.path.insert(0, str(root / '.native-test-deps'))
    addon = importlib.import_module('blender_raw')
catalog, compositor, jobs, raw_backend, scene_pipeline = (
    importlib.import_module('.' + name, addon.__name__)
    for name in ('catalog', 'compositor', 'jobs', 'raw_backend', 'scene_pipeline'))
if '--ofx' in sys.argv:
    film_node, native_effects, ofx_node, spektrafilm_ofx_engine = (
        importlib.import_module('.' + name, addon.__name__)
        for name in ('film_node', 'native_effects', 'ofx_node', 'spektrafilm_ofx_engine'))


def samples(path):
    with closing(oiio.ImageInput.open(str(path))) as reader:
        spec = reader.spec()
        size = spec.width, spec.height
        pixels = reader.read_image(format=oiio.FLOAT)
    # Sample the complete image domain without retaining another full frame.
    return size, pixels[::max(1, size[1] // 79), ::max(1, size[0] // 83), :3].copy()


def run(source, folder):
    if not installed:
        addon.register()
    photos = []
    for name, fmt, ev in (('Native A', 'OPEN_EXR', .75), ('Native B', 'TIFF', -.5)):
        original = folder / (name + source.suffix)
        shutil.copyfile(source, original)
        photo, created = catalog.add_photo(bpy.context, original)
        assert created
        scene = compositor.ensure_photo_scene(bpy.context, photo)
        settings = scene.render.image_settings
        settings.file_format = fmt
        settings.color_depth = '32' if fmt == 'OPEN_EXR' else '16'
        settings.color_mode = 'RGB'
        developer = next(node for _, node, _ in scene_pipeline.node_instances(scene, compositor.DEVELOPER_TYPE))
        developer.camera_input, developer.look = 'AUTO', 'NONE'
        developer.white_balance, developer.auto_exposure, developer.exposure = 'CAMERA', 'AS_SHOT', ev
        if '--ofx' in sys.argv and fmt == 'OPEN_EXR':
            film = native_effects.effect_node(native_effects.create_effect(scene, film_node.OFX_TYPE, photo=photo))
            setattr(film, ofx_node.parameter_property('grainEnabled'), False)
            setattr(film, ofx_node.parameter_property('halationEnabled'), False)
        photo.selected = True
        photos.append(photo)
    # Preview toggles must not hide a source from the full-resolution worker.
    photos[0].scene.compositor_effects[0].enable_for_preview = False
    assert photos[0].scene != photos[1].scene
    output = folder / 'output'
    jobs.start_export(bpy.context, photos, output)
    deadline = time.monotonic() + 300
    while jobs.export_running():
        assert time.monotonic() < deadline, jobs.export_status()
        jobs._poll_export()
        time.sleep(.1)
    assert not jobs.export_details(), jobs.export_details()
    expected_files = {photos[0].scene.render.file_extension, photos[1].scene.render.file_extension}
    assert {p.suffix for p in output.iterdir()} == expected_files
    reference = folder / 'reference.exr'
    raw_backend.decode(str(source), str(reference), {'preview': False, 'white_balance': 'CAMERA',
                                                   'camera_input': 'AUTO', 'look': 'NONE'})
    dimensions, base = samples(reference)
    for photo, ev in zip(photos, (.75, -.5)):
        path = output / (Path(photo.filepath).stem + photo.scene.render.file_extension)
        actual_size, actual = samples(path)
        assert actual_size == dimensions
        expected = base * 2 ** ev
        if '--ofx' in sys.argv and path.suffix.lower() == '.exr':
            with closing(oiio.ImageInput.open(str(reference))) as input_image:
                linear = input_image.read_image(format=oiio.FLOAT)[..., :3].copy()
            recipe = film_node.recipe(native_effects.effect_node(photo.scene.compositor_effects[-1]))
            recipe.pop('engine')
            linear = spektrafilm_ofx_engine.render(linear * (2 ** ev), **recipe, frame=photo.scene.frame_current)
            expected = linear[::max(1, dimensions[1] // 79), ::max(1, dimensions[0] // 83), :3].copy()
            del linear
        if path.suffix.lower() in {'.tif', '.tiff'}:
            expected = np.maximum(expected, 0)
            expected = np.where(expected <= .0031308, expected * 12.92,
                                1.055 * expected ** (1 / 2.4) - .055)
            expected = np.clip(expected, 0, 1)
            tolerance = 4e-4
        else:
            tolerance = 4e-4 if '--ofx' in sys.argv else 2e-5
        np.testing.assert_allclose(actual, expected, rtol=tolerance, atol=tolerance)
        print(f'PASS isolated RAW worker {path.name}: {actual_size}, {ev:+.2f} EV, max error {np.max(np.abs(actual - expected)):.6g}', flush=True)
    # The snapshot worker must leave the UI photo scenes and their original flags intact.
    assert not photos[0].scene.compositor_effects[0].enable_for_preview
    assert all(photo.image is None for photo in photos)
    if not installed:
        addon.unregister()
    print('PASS independent per-photo formats, full-resolution RAW and snapshot isolation', flush=True)


source = Path(sys.argv[sys.argv.index('--') + 1]).resolve()
with tempfile.TemporaryDirectory(prefix='native-worker-export-') as temporary:
    run(source, Path(temporary))
