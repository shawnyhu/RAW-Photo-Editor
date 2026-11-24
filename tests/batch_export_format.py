"""The export dialog applies one output encoding to every selected photo.

blender -b --factory-startup --offline-mode --python-exit-code 1 -P tests/batch_export_format.py
"""
from contextlib import closing
import gzip
import importlib
from pathlib import Path
import sys
import tempfile
import time

import bpy
import numpy as np
import OpenImageIO as oiio

ROOT = Path(__file__).resolve().parents[1]
INSTALLED = '--installed' in sys.argv
if INSTALLED:
    package = 'bl_ext.user_default.raw_photo_editor'
    assert package in bpy.context.preferences.addons, 'Install and enable the ZIP before running this test'
else:
    package = 'blender_raw'
    sys.path.insert(0, str(ROOT))
    if (ROOT / '.native-test-deps').is_dir():
        sys.path.insert(0, str(ROOT / '.native-test-deps'))
addon = importlib.import_module(package)
if INSTALLED:
    assert Path(addon.__file__).resolve().parent != ROOT / 'blender_raw', addon.__file__
catalog, compositor, jobs, naming, scene_pipeline = (
    importlib.import_module(f'{package}.{name}')
    for name in ('catalog', 'compositor', 'jobs', 'naming', 'scene_pipeline'))


def finish():
    deadline = time.monotonic() + 120
    while jobs.export_running():
        assert time.monotonic() < deadline, jobs.export_status()
        jobs._poll_export()
        time.sleep(.05)
    assert not jobs.export_details(), jobs.export_details()


def image(path):
    with closing(oiio.ImageInput.open(str(path))) as reader:
        assert reader, path
        return reader.format_name(), reader.spec().nchannels, reader.read_image(format=oiio.FLOAT)


def state(scene):
    s = scene.render.image_settings
    return (s.file_format, s.color_mode, s.color_depth, s.quality, s.compression, s.color_management,
            scene.view_settings.view_transform, scene.view_settings.exposure)


def run(folder):
    data = gzip.decompress((ROOT / 'tests/fixtures/highlight-chart.dng.gz').read_bytes())
    photos = []
    for index in range(3):
        original = folder / f'Photo-{index + 1}.dng'
        original.write_bytes(data)
        photo, created = catalog.add_photo(bpy.context, original)
        assert created
        scene = compositor.ensure_photo_scene(bpy.context, photo)
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.color_mode = 'RGBA'
        scene.view_settings.exposure = index * .8
        dev = next(node for _, node, _ in scene_pipeline.node_instances(scene, compositor.DEVELOPER_TYPE))
        dev.camera_input, dev.look = 'AUTO', 'NONE'
        dev.auto_exposure, dev.white_balance = 'AS_SHOT', 'CAMERA'
        photo.selected = True
        photos.append(photo)
    selected = photos[0].scene
    options = selected.render.image_settings
    options.file_format, options.color_mode, options.quality = 'JPEG', 'BW', 87
    options.color_management = 'OVERRIDE'
    options.view_settings.view_transform = 'Standard'
    options.view_settings.look = 'None'
    options.view_settings.exposure = .25
    original_states = [state(photo.scene) for photo in photos]

    # The same settings presented by the dialog must drive its filename preview.
    class Layout:
        def __init__(self): self.labels = []
        def box(self): return self
        def prop(self, *args, **kwargs): pass
        def label(self, *, text, **kwargs): self.labels.append(text)

    preview = Layout()
    with bpy.context.temp_override(scene=selected):
        naming.draw(preview, bpy.context, photos, catalog.library(bpy.context), str(folder / 'batch'))
        assert all(name.endswith('.jpg') for name in preview.labels), preview.labels
        assert bpy.ops.blender_raw.export_photos(directory=str(folder / 'batch')) == {'FINISHED'}
    # The worker uses the already-saved choice even if the UI changes meanwhile.
    options.file_format = 'PNG'
    options.quality = 25
    finish()
    outputs = sorted((folder / 'batch').iterdir())
    assert [p.name for p in outputs] == [f'Photo-{i + 1}.jpg' for i in range(3)]
    pixels = []
    for path in outputs:
        kind, channels, result = image(path)
        assert kind == 'jpeg' and channels == 1, (path, kind, channels)
        pixels.append(result)
    # Explicit output color override applies equally despite different scene exposures.
    for result in pixels[1:]:
        np.testing.assert_array_equal(result, pixels[0])
    assert [state(p.scene) for p in photos[1:]] == original_states[1:]
    print('PASS all three JPEG files, batch BW/color override, naming, snapshot isolation and untouched photo settings', flush=True)

    # Programmatic callers can still request independent per-photo output settings.
    options.file_format, options.color_mode, options.quality = 'JPEG', 'BW', 87
    with bpy.context.temp_override(scene=selected):
        jobs.start_export(bpy.context, photos, folder / 'per-photo')
    finish()
    assert {p.suffix for p in (folder / 'per-photo').iterdir()} == {'.jpg', '.png'}

    # Copy all chosen JPEG settings and a nontrivial output color curve into only
    # the temporary render scene; do not replace the photo's view settings.
    options.view_settings.use_curve_mapping = True
    options.view_settings.curve_mapping.initialize()
    options.view_settings.curve_mapping.curves[3].points.new(.4, .7)
    options.view_settings.curve_mapping.update()
    temporary = compositor.export_scene(photos[1].scene, output_settings=options)
    try:
        actual = temporary.render.image_settings
        assert actual.file_format == 'JPEG' and actual.quality == 87 and actual.color_mode == 'BW'
        assert actual.view_settings.use_curve_mapping and actual.view_settings.exposure == .25
        np.testing.assert_allclose([p.location for p in actual.view_settings.curve_mapping.curves[3].points],
                                   [p.location for p in options.view_settings.curve_mapping.curves[3].points])
        assert temporary.view_settings.exposure == photos[1].scene.view_settings.exposure
    finally:
        bpy.data.scenes.remove(temporary)
    assert [state(p.scene) for p in photos[1:]] == original_states[1:]
    print('PASS programmatic per-photo formats, quality and custom output curve settings', flush=True)


if not INSTALLED:
    addon.register()
try:
    with tempfile.TemporaryDirectory(prefix='batch-output-settings-') as temporary:
        run(Path(temporary))
finally:
    if not INSTALLED:
        addon.unregister()
