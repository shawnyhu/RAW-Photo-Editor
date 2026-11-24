"""Check the Apple RAW worker and catalog on a supported RAW original.

blender -b --factory-startup --python-exit-code 1 --python tests/apple_raw.py -- path/to/photo.NEF
"""
from contextlib import closing
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blender_raw
from blender_raw import catalog, compositor, raw_backend, worker


def render(req, path):
    worker.process_image(dict(req, output=str(path)))
    with closing(oiio.ImageInput.open(str(path))) as image:
        spec = image.spec()
        assert spec.get_string_attribute('PhotoEditor:CameraInput') == 'Apple RAW (Core Image)'
        assert str(spec.format) == 'float'
        return ((spec.width, spec.height),
                tuple(spec.get_float_attribute('PhotoEditor:MeterEV1:' + mode) for mode in
                      ('FILM_MEDIAN', 'FILM_CENTER')))


def check(source, folder):
    context = bpy.context
    photo, _ = catalog.add_photo(context, source)
    tree = compositor.ensure_tree(context, photo)
    node = next(node for node in tree.nodes if node.bl_idname == compositor.DEVELOPER_TYPE)
    assert photo.camera_input == node.camera_input == catalog.DEFAULT_CAMERA_INPUT == 'AUTO'
    lib = catalog.library(context)
    lib.preview_resolution = 'PROXY'
    for resolution in ('full', 'preview'):
        reader = catalog.request(photo, resolution)
        assert reader['settings']['camera_input'] == 'AUTO'
        assert catalog.request(photo, resolution, node)['key'] == reader['key']
    baseline = dict(catalog.request(photo, 'preview'), output=str(folder / 'reader.exr'))
    worker.process_image(baseline)
    assert 'Apple' not in raw_backend.camera_input_name(baseline['output'])
    catalog.apply_image(photo, baseline)
    node.camera_input = 'APPLE'
    full = catalog.request(photo, 'full', recipe=node)
    assert full['key'] != catalog.request(photo, 'full')['key']
    assert full['settings']['white_balance'] == 'CAMERA' and not full['settings']['apple_lens_correction']
    shape, meters = render(full, folder / 'full.exr')
    assert all(abs(meter) < 100 for meter in meters)

    proxy = catalog.request(photo, 'preview', recipe=node)
    preview_shape, preview_meters = render(proxy, folder / 'proxy.exr')
    assert max(preview_shape) == min(1600, max(shape)) and preview_meters == meters
    old_job = dict(proxy, settings=dict(proxy['settings'], camera_input='APPLE_AUTO'))
    render(old_job, folder / 'legacy.exr')
    with (
        closing(oiio.ImageInput.open(str(folder / 'proxy.exr'))) as current,
        closing(oiio.ImageInput.open(str(folder / 'legacy.exr'))) as legacy,
    ):
        assert np.array_equal(current.read_image(), legacy.read_image())

    def attach(req, path):
        bound = dict(req, output=str(path))
        catalog.apply_developer_image(photo, node, bound)
        assert np.allclose(catalog.applied_white_balance(node), (node.applied_temperature, node.applied_tint))
        return node.applied_temperature, node.applied_tint

    as_shot = attach(proxy, folder / 'proxy.exr')
    assert as_shot[0] > 0
    for mode in ('AUTO', 'SAMPLE'):
        node.neutral_x, node.neutral_y = .58, .2
        node.apple_white_balance = mode
        req = catalog.request(photo, 'preview', node)
        path = folder / f'{mode}.exr'
        render(req, path)
        measured = attach(req, path)
        assert np.isfinite(measured).all() and measured[0] > 0

    node.apple_white_balance = 'CUSTOM'
    node.temperature = 7000
    node.tint = -10
    node.apple_lens_correction = True
    custom = catalog.request(photo, 'preview', recipe=node)
    assert custom['key'] != proxy['key']
    assert custom['settings']['white_balance'] == 'CUSTOM' and custom['settings']['apple_lens_correction']
    assert (custom['settings']['temperature'], custom['settings']['tint']) == (7000, -10)
    render(custom, folder / 'custom.exr')
    assert np.allclose(attach(custom, folder / 'custom.exr'), (7000, -10), atol=.1)

    # Explicit LibRaw uses the previous Blender calibration and its own white-point coordinates.
    node.camera_input = 'AUTO'
    assert catalog.request(photo, 'preview', node)['key'] == baseline['key']
    node.white_balance = 'SAMPLE'
    req = catalog.request(photo, 'preview', node)
    libraw_path = folder / 'libraw.exr'
    worker.process_image(dict(req, output=str(libraw_path)))
    assert 'Apple' not in raw_backend.camera_input_name(libraw_path)
    assert attach(req, libraw_path)[0] > 0
    node.camera_input = 'APPLE'
    assert node.apple_white_balance == 'CUSTOM' and (node.temperature, node.tint) == (7000, -10)


def check_unsupported(source, folder):
    for kind in ('APPLE', 'APPLE_AUTO'):
        path = folder / f'unsupported-{kind}.exr'
        settings = dict(camera_input=kind, white_balance='CAMERA', look='NONE', preview=True)
        try:
            raw_backend.decode(source, path, settings)
        except raw_backend.AppleRawUnsupported:
            assert not path.exists()
        else:
            raise AssertionError(f'{kind} must report an unsupported camera')


if __name__ == '__main__':
    blender_raw.register()
    try:
        with tempfile.TemporaryDirectory() as temp:
            original, *unsupported = sys.argv[sys.argv.index('--') + 1:]
            check(Path(original).resolve(), Path(temp))
            if unsupported:
                check_unsupported(Path(unsupported[0]).resolve(), Path(temp))
            else:
                # Installed RAW support varies with macOS. Exercise the unsupported result
                # even when Core Image can decode every original in the test set.
                with patch.object(raw_backend, '_apple_raw_bridge',
                                  side_effect=raw_backend.AppleRawUnsupported('Unsupported test camera')):
                    check_unsupported(Path(original).resolve(), Path(temp))
    finally:
        blender_raw.unregister()
