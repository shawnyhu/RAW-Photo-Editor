"""Verify photo ownership, scene switching, source modes and scene deletion."""
from pathlib import Path
import sys
import tempfile
import uuid

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blender_raw
from blender_raw import catalog, compositor, edited_preview, film_display, native_effects, naming


def add_photo(lib, folder, name, color):
    path = folder / (name + '.exr')
    pixels = np.tile([*color, 1.0], (24, 32, 1)).astype(np.float32)
    writer = oiio.ImageOutput.create(str(path))
    assert writer.open(str(path), oiio.ImageSpec(32, 24, 4, oiio.FLOAT))
    assert writer.write_image(pixels)
    writer.close()
    photo = lib.photos.add()
    photo.uid, photo.name = uuid.uuid4().hex, name
    photo.filepath = photo.source_absolute = str(path)
    catalog.set_camera_input(photo, 0)
    photo.image = bpy.data.images.load(str(path))
    photo.image.colorspace_settings.name = 'Linear Rec.709'
    photo.decode_key, photo.status = name, 'Ready'
    return photo


def run(folder):
    blender_raw.register()
    owner = bpy.context.scene
    owner.blender_raw.owner = True
    lib = catalog.library(bpy.context)
    first = add_photo(lib, folder, 'First', (.05, .1, .2))
    second = add_photo(lib, folder, 'Second', (.2, .1, .05))
    scene_a = compositor.ensure_photo_scene(bpy.context, first)
    scene_b = compositor.ensure_photo_scene(bpy.context, second)
    assert scene_a != scene_b and not scene_a.camera and not scene_b.camera
    assert len(scene_a.collection.objects) == len(scene_b.collection.objects) == 0
    assert len(scene_a.compositor_effects) == len(scene_b.compositor_effects) == 2
    assert all(a.node_group != b.node_group for a, b in zip(scene_a.compositor_effects, scene_b.compositor_effects))
    assert not scene_a.blender_raw.owner and not scene_a.blender_raw.photos
    assert compositor.ensure_photo_scene(bpy.context, first) == scene_a

    dev_a = native_effects.effect_node(scene_a.compositor_effects[1])
    dev_b = native_effects.effect_node(scene_b.compositor_effects[1])
    assert compositor.developer_source(dev_a) == first
    assert compositor.developer_source(dev_b) == second
    assert compositor._image_predecessor(dev_a).bl_idname == compositor.READER_TYPE
    native_effects.set_input_value(scene_a.compositor_effects[1], 'Exposure', 1.0)
    native_effects.set_input_value(scene_b.compositor_effects[1], 'Exposure', -1.0)
    scene_a.compositor_effects[0].enable_for_preview = False
    assert compositor.developer_source(dev_a) is None
    assert compositor.developer_source(dev_a, mode='RENDER') == first
    scene_a.compositor_effects[0].enable_for_preview = True

    # Photo selection changes only the active window's scene; the library stays global.
    compositor.open_photo(bpy.context, first)
    assert bpy.context.window.scene == scene_a
    compositor.open_photo(bpy.context, second)
    assert bpy.context.window.scene == scene_b
    assert catalog.library(bpy.context) == lib
    assert dev_a.exposure == 1.0 and dev_b.exposure == -1.0
    scene_a.view_settings.exposure = .75
    film_display.sync(scene_a)
    compositor.open_photo(bpy.context, first)
    assert scene_a.view_settings.exposure == .75
    assert scene_b.view_settings.exposure == 0.0
    assert compositor.viewer_image().type == 'COMPOSITING'

    # Native input changes invalidate final-image thumbnails.
    before = edited_preview._signature(scene_a)
    assert before is not None
    native_effects.set_input_value(scene_a.compositor_effects[1], 'Exposure', 2.0)
    assert before != edited_preview._signature(scene_a)
    scene_a.render.image_settings.file_format = 'PNG'
    scene_b.render.image_settings.file_format = 'TIFF'
    names = naming.export_items([first, second], folder,
                               {first.uid: '.png', second.uid: '.tif'}, lib)
    assert names[0]['path'].endswith('.png') and names[1]['path'].endswith('.tif')

    first_uid, second_uid = first.uid, second.uid
    project = folder / 'native-scenes.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(project))
    bpy.ops.wm.open_mainfile(filepath=str(project))
    lib = catalog.library(bpy.context)
    first = catalog.lookup_photo(None, first_uid)
    second = catalog.lookup_photo(None, second_uid)
    assert first.scene != second.scene
    assert first.scene.view_settings.exposure == .75
    assert native_effects.get_input_value(first.scene.compositor_effects[1], 'Exposure') == 2.0
    assert native_effects.get_input_value(second.scene.compositor_effects[1], 'Exposure') == -1.0
    assert not first.scene.camera and not second.scene.camera

    # Removing a photograph must not repurpose another photo's scene or source.
    compositor.open_photo(bpy.context, second)
    lib.active_index = -1  # Native Scene switching need not select a Gallery row.
    removed_name = second.scene.name
    assert bpy.ops.blender_raw.remove_photo(uid=second_uid) == {'FINISHED'}
    assert catalog.lookup_photo(None, second_uid) is None
    assert bpy.data.scenes.get(removed_name) is None
    assert len(lib.photos) == 1 and lib.photos[0].uid == first_uid
    assert bpy.context.scene == first.scene and not first.scene.camera
    print('PASS photo scenes, native source modes, switching, display settings, save/reopen and removal')


with tempfile.TemporaryDirectory(prefix='native-photo-scenes-') as scratch:
    run(Path(scratch))
