"""Verify 16-bit TIFF and JPEG chroma/metadata with an active native film node.

blender -b --factory-startup --python-exit-code 1 --python tests/film_jpeg.py
"""
from contextlib import closing
from pathlib import Path
import sys
import tempfile

import bpy
import numpy as np
import OpenImageIO as oiio

installed = "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons
if installed:
    from bl_ext.user_default import raw_photo_editor as addon
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import blender_raw as addon
catalog, compositor = addon.catalog, addon.compositor


def check(folder):
    if not installed:
        addon.register()
    scene = bpy.context.scene
    photo = catalog.library(bpy.context).photos.add()
    photo.name, photo.uid = 'Chroma test', 'jpeg-test'
    path = folder / 'input.exr'
    x = np.linspace(.05, .75, 64, dtype='f')
    rgb = np.tile(np.stack((x, x/2, x[::-1]/3), axis=-1), (48, 1, 1))
    with closing(oiio.ImageOutput.create(str(path))) as writer:
        assert writer.open(str(path), oiio.ImageSpec(64, 48, 3, oiio.FLOAT)) and writer.write_image(rgb)
    photo.image = bpy.data.images.load(str(path))
    photo.image.colorspace_settings.name = 'Linear Rec.709'
    tree = compositor.ensure_tree(bpy.context, photo)
    developer = next(node for node in tree.nodes if node.bl_idname == compositor.DEVELOPER_TYPE)
    assert bpy.ops.blender_raw.add_spektrafilm(tree_name=tree.name, node_name=developer.name) == {'FINISHED'}
    film = tree.nodes.active
    film.grain = False
    scene.render.use_stamp_note = True
    scene.render.stamp_note_text = 'Film stamp'
    export = compositor.export_scene(scene)
    fmt = export.render.image_settings
    fmt.color_management = 'OVERRIDE'
    fmt.view_settings.view_transform = 'Standard'
    fmt.view_settings.exposure = .7
    for mode, quality, muted, expected in [('RGB', 100, False, '4:4:4'), ('RGB', 90, False, '4:2:0'),
                                            ('BW', 100, False, ''), ('RGB', 100, True, '4:2:0')]:
        fmt.file_format, fmt.color_mode, fmt.quality = 'JPEG', mode, quality
        film.mute = muted
        result = folder / f'{mode}-{quality}-{muted}.jpg'
        compositor.export_photo(bpy.context, photo, result, export)
        assert (fmt.file_format, fmt.quality, fmt.color_mode) == ('JPEG', quality, mode)
        with closing(oiio.ImageInput.open(str(result))) as image:
            spec = image.spec()
            assert spec.nchannels == (1 if mode == 'BW' else 3)
            if mode == 'RGB':
                assert spec.get_string_attribute('jpeg:subsampling') == expected
            assert spec.get_string_attribute('Blender:Note') == 'Film stamp'
            if expected == '4:4:4':
                assert spec.get_string_attribute('ResolutionUnit') == 'in'
                jpeg_rgb = image.read_image(format=oiio.FLOAT)
                profile = bytes(spec.getattribute('ICCProfile'))

    film.mute = False
    fmt.file_format, fmt.color_mode, fmt.color_depth = 'PNG', 'RGB', '16'
    png = folder / 'override.png'
    compositor.export_photo(bpy.context, photo, png, export)
    with closing(oiio.ImageInput.open(str(png))) as image:
        assert bytes(image.spec().getattribute('ICCProfile')) == profile
        png_rgb = image.read_image(format=oiio.FLOAT)
        assert np.mean(np.abs(png_rgb - jpeg_rgb)) < .005

    fmt.file_format, fmt.color_depth = 'TIFF', '16'
    tiff = folder / 'film.tif'
    compositor.export_photo(bpy.context, photo, tiff, export)
    with closing(oiio.ImageInput.open(str(tiff))) as image:
        spec = image.spec()
        assert (spec.width, spec.height, spec.nchannels, str(spec.format)) == (64, 48, 3, 'uint16')
        np.testing.assert_allclose(image.read_image(format=oiio.FLOAT), png_rgb, atol=1 / 65535)
    print('PASS active film export: 16-bit RGB TIFF, quality-100 JPEG 4:4:4, ICC and stamp')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as folder:
        check(Path(folder))
