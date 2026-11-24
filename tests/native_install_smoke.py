"""Smoke test the installed ZIP, never the checkout on sys.path."""
from pathlib import Path
import sys
import tempfile
import uuid

import bpy
import numpy as np
import OpenImageIO as oiio
import rawpy
import pyexiv2
from bl_ext.user_default.raw_photo_editor import catalog, compositor, native_effects, spektrafilm_engine


assert 'bl_ext.user_default.raw_photo_editor' in bpy.context.preferences.addons
runtime = Path(spektrafilm_engine.__file__).parent / 'vendor' / 'spektrafilm'
library = runtime / ('photo_film.dll' if sys.platform == 'win32' else 'libphoto_film.dylib')
assert library.is_file() and library.stat().st_size > 0, library
# Hosted CI can verify the installer and CPU compositor without a graphics device.
# Local GPU acceptance adds --require-gpu and exercises the real backend separately.
if '--require-gpu' in sys.argv:
    assert not spektrafilm_engine.backend_error(), spektrafilm_engine.backend_error()
assert not hasattr(bpy.types, 'BlenderPhotoStackTree')
bpy.context.scene.blender_raw.owner = True
lib = catalog.library(bpy.context)
with tempfile.TemporaryDirectory(prefix='photo-native-install-') as scratch:
    folder = Path(scratch)
    source = folder / 'source.exr'
    writer = oiio.ImageOutput.create(str(source))
    assert writer.open(str(source), oiio.ImageSpec(16, 16, 4, oiio.FLOAT))
    assert writer.write_image(np.tile([.0625, .125, .25, 1.0], (16, 16, 1)).astype(np.float32))
    writer.close()
    photo = lib.photos.add()
    photo.uid, photo.name = uuid.uuid4().hex, 'Installed Native Photo'
    photo.filepath = photo.source_absolute = str(source)
    catalog.set_camera_input(photo, 0)
    photo.image = bpy.data.images.load(str(source))
    photo.image.colorspace_settings.name = 'Linear Rec.709'
    scene = compositor.open_photo(bpy.context, photo)
    assert photo.scene == scene and not scene.camera
    assert len(scene.compositor_effects) == 2
    native_effects.set_input_value(scene.compositor_effects[1], 'Exposure', 1.0)
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_depth = '32'
    scene.render.compositor_device = 'CPU'
    export = compositor.export_scene(scene)
    destination = folder / 'installed.exr'
    compositor.export_photo(bpy.context, photo, str(destination), export)
    reader = oiio.ImageInput.open(str(destination))
    pixels = reader.read_image(format=oiio.FLOAT)
    reader.close()
    np.testing.assert_allclose(pixels[8, 8, :3], [.125, .25, .5], atol=2e-6)
    assert not scene.camera
    assert native_effects.get_input_value(scene.compositor_effects[1], 'Exposure') == 1.0
print('PASS installed ZIP: registration, offline dependencies, native scene and export pixels')
