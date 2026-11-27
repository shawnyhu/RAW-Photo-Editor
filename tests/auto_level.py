# SPDX-License-Identifier: GPL-3.0-or-later
"""Whole-frame levels, actual compositor pixels, copies and saved settings.

Source: blender -b --factory-startup --offline-mode --python-exit-code 1 -P tests/auto_level.py
Installed: omit --factory-startup and append -- --installed.
GPU compositor: omit -b and append -- --gpu; the test closes its own window.
"""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

import bpy
import numpy as np
import OpenImageIO as oiio

if '--installed' in sys.argv:
    from bl_ext.user_default.raw_photo_editor import (
        adjustments, catalog, compositor, levels_math, levels_node, native_effects)
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import blender_raw
    from blender_raw import adjustments, catalog, compositor, levels_math, levels_node, native_effects
    if not hasattr(bpy.types.Scene, 'blender_raw'):
        blender_raw.register()


def close(actual, expected, label):
    np.testing.assert_allclose(actual, expected, atol=4e-6, rtol=2e-5, err_msg=label)


def reference(rgba, black=None, white=None):
    """Independent scalar definition, using float64 for the test oracle."""
    output = np.array(rgba, dtype=np.float64, copy=True)
    rgb, alpha = output[..., :3], output[..., 3:4]
    np.divide(rgb, alpha, out=rgb, where=alpha != 0)
    rgb[alpha[..., 0] == 0] = 0
    if black is None:
        visible = rgb[alpha[..., 0] > 1e-6]
        black, white = max(0., min(1., float(visible.min()))), max(0., min(1., float(visible.max())))
        if white <= black:
            return np.array(rgba, copy=True)
    rgb[:] = np.clip((rgb - black) / (white - black), 0, 1) * alpha
    return output


def measurements():
    # Endpoints sit outside the tiny display sample, including the last pixel.
    rgba = np.full((1027, 1021, 4), .25, np.float32)
    rgba[..., 3] = 1
    rgba[0, 1, 0], rgba[-1, -1, 1] = -.5, 3.5
    sample = levels_math.analyze(rgba, max_values=17)
    assert (sample.low, sample.high) == (-.5, 3.5)
    assert (sample.width, sample.height, sample.count) == (1021, 1027, 1021 * 1027 * 3)
    assert len(sample.values) <= 17 and np.isfinite(sample.values).all()
    assert sample.low not in sample.values and sample.high not in sample.values
    del rgba

    rgba = np.array([[[.1, .2, .3, .25], [-999, 999, 999, 0],
                      [.3, .4, .5, .5], [np.nan, np.inf, .45, 1],
                      [-999, 999, 999, 1e-7]]], dtype=np.float32)
    sample = levels_math.analyze(rgba)
    close((sample.low, sample.high), (.4, 1.2), 'alpha and finite-channel statistics')
    assert sample.count == 7 and np.isfinite(sample.values).all()
    for array in (np.zeros((2, 3, 4), np.float32), np.full((2, 3, 4), np.nan, np.float32)):
        try:
            levels_math.analyze(array)
        except ValueError:
            pass
        else:
            raise AssertionError('Invisible/invalid input yielded endpoints')
    constant = np.full((3, 4, 4), .375, np.float32)
    constant[..., 3] = 1
    assert levels_math.analyze(constant).low == levels_math.analyze(constant).high == .375
    good = np.array([[[.05, .1, .2, .25], [-.5, .5, 2, 1], [0, 0, 0, 0]]], np.float32)
    actual = levels_math.apply(good, -.25, 1.25)
    close(actual, reference(good, -.25, 1.25), 'manual numerical definition')
    np.testing.assert_array_equal(actual[..., 3], good[..., 3])
    for black, white in ((1, 1), (2, 1), (np.nan, 1), (0, np.inf)):
        try:
            levels_math.apply(good, black, white)
        except ValueError:
            pass
        else:
            raise AssertionError(('Invalid endpoints accepted', black, white))
    print('PASS whole-frame extrema, bounded histogram samples, alpha, nonfinite and HDR math', flush=True)


def write_image(path, rgba):
    with closing(oiio.ImageOutput.create(str(path))) as writer:
        assert writer.open(str(path), oiio.ImageSpec(rgba.shape[1], rgba.shape[0], 4, oiio.FLOAT))
        assert writer.write_image(rgba.astype(np.float32))
    image = bpy.data.images.load(str(path), check_existing=False)
    image.colorspace_settings.name = 'Linear Rec.709'
    image.alpha_mode = 'PREMUL'
    return image


def make_photo(folder, name, pixels):
    path = folder / (name + '.exr')
    image = write_image(path, pixels)
    photo = catalog.library(bpy.context).photos.add()
    photo.name = photo.uid = name
    photo.filepath = photo.source_absolute = str(path)
    photo.image = image
    catalog.set_camera_input(photo, 0)
    scene = compositor.ensure_photo_scene(bpy.context, photo)
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_depth = '32'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.compositor_device = 'GPU' if '--gpu' in sys.argv else 'CPU'
    scene.render.compositor_precision = 'FULL'
    scene.render.resolution_percentage = 100
    return photo, scene


def render(folder, name, photo, scene):
    path = folder / (name + '.exr')
    export = compositor.export_scene(scene)
    try:
        compositor.export_photo(bpy.context, photo, path, export)
    finally:
        bpy.data.scenes.remove(export)
    with closing(oiio.ImageInput.open(str(path))) as reader:
        assert reader is not None, path
        pixels = reader.read_image(format=oiio.FLOAT)
    assert pixels.shape == (photo.image.size[1], photo.image.size[0], 4), pixels.shape
    assert np.isfinite(pixels).all(), name
    return pixels


def levels(scene):
    return [native_effects.effect_node(effect) for effect in scene.compositor_effects
            if native_effects.effect_node(effect)
            and native_effects.effect_node(effect).bl_idname == levels_node.TYPE]


def actual_pixels(folder):
    bpy.context.scene.blender_raw.owner = True
    y, x = np.indices((23, 37))
    pixels = np.ones((23, 37, 4), np.float32)
    pixels[..., 0] = .125 + .375 * x / 36
    pixels[..., 1] = .2 + .25 * y / 22
    pixels[..., 2] = .3
    pixels[0, 0, :3] = -.25
    pixels[-1, -1, :3] = 2
    pixels[4:8, 9:15, 3] = .25
    pixels[..., :3] *= pixels[..., 3:4]
    pixels[10:13, 20:25] = 0
    photo, scene = make_photo(folder, 'levels-source', pixels)
    exposure = native_effects.create_effect(scene, 'CompositorNodeExposure')
    upstream = native_effects.effect_node(exposure)
    effect = native_effects.create_effect(scene, levels_node.TYPE)
    node = native_effects.effect_node(effect)
    assert node.auto_level and node.inputs.get('Image') and node.outputs.get('Image')
    assert not effect.show_node_group_selector

    # Native graph evaluates Auto on every render; no capture/apply call precedes this.
    close(render(folder, 'native-auto', photo, scene), reference(pixels), 'automatic native pixels')
    sample = levels_node.capture(scene, node)
    close((sample.low, sample.high), (-.25, 2), 'full compositor input measurement')
    assert (sample.width, sample.height) == (37, 23)
    upstream.inputs['Exposure'].default_value = 1
    brighter = pixels.copy()
    brighter[..., :3] *= 2
    close(render(folder, 'native-auto-exposure', photo, scene), reference(brighter), 'live upstream exposure')
    sample = levels_node.capture(scene, node)
    close((sample.low, sample.high), (-.5, 4), 'updated upstream measurement')
    downstream = native_effects.create_effect(scene, 'CompositorNodeExposure')
    native_effects.effect_node(downstream).inputs['Exposure'].default_value = 2
    later = levels_node.capture(scene, node)
    close((later.low, later.high), (sample.low, sample.high), 'later card excluded from histogram')
    downstream.enable_for_preview = downstream.enable_for_render = False
    upstream.inputs['Exposure'].default_value = 0

    # Manual endpoints are stored; neither rendering nor upstream changes resets them.
    node.auto_level = False
    levels_node.apply_points(node, .1, .8)
    close(render(folder, 'native-manual', photo, scene), reference(pixels, .1, .8), 'manual native pixels')
    upstream.inputs['Exposure'].default_value = 1
    brighter = pixels.copy()
    brighter[..., :3] *= 2
    close(render(folder, 'native-manual-exposure', photo, scene), reference(brighter, .1, .8), 'manual retained')
    close((node.black_point, node.white_point), (.1, .8), 'manual endpoint persistence')
    upstream.inputs['Exposure'].default_value = 0
    node.auto_level = True

    # A copied adjustment must derive new endpoints from the destination photo.
    target_pixels = pixels.copy()
    target_pixels[..., :3] = pixels[..., :3] * .2 + pixels[..., 3:4] * .3
    target, target_scene = make_photo(folder, 'levels-target', target_pixels)
    original_source = target.image
    with bpy.context.temp_override(scene=scene):
        adjustments.copy(bpy.context, photo)
        assert adjustments.paste(bpy.context, [target]) == 1
        adjustments.clear()
    copied = levels(target_scene)[0]
    assert copied.auto_level and copied.node_tree != node.node_tree and copied.id_data != node.id_data
    assert target.image == original_source
    close(render(folder, 'native-copied-auto', target, target_scene), reference(target_pixels),
          'Auto adapts after copying to a different photo')
    target_sample = levels_node.capture(target_scene, copied)
    close((target_sample.low, target_sample.high), (.25, .7), 'destination endpoints')
    levels_node.apply_points(copied, .2, .75)
    copied.auto_level = False
    assert node.auto_level
    close(render(folder, 'native-copied-manual', target, target_scene), reference(target_pixels, .2, .75),
          'private copied manual graph')
    close((node.black_point, node.white_point), (.1, .8), 'original graph untouched')

    # Native duplicate gets a private internal graph, even when appended after the source.
    duplicate_effect = native_effects.duplicate_effect(scene, effect)
    duplicate = native_effects.effect_node(duplicate_effect)
    assert duplicate.node_tree != node.node_tree and duplicate.auto_level
    levels_node.apply_points(duplicate, .15, .65)
    duplicate.auto_level = False
    duplicate_effect.enable_for_preview = duplicate_effect.enable_for_render = False
    assert node.auto_level

    scene_name, target_name = scene.name, target_scene.name
    blend = folder / 'levels-saved.blend'
    assert bpy.ops.wm.save_as_mainfile(filepath=str(blend)) == {'FINISHED'}
    assert bpy.ops.wm.open_mainfile(filepath=str(blend)) == {'FINISHED'}
    scene, target_scene = bpy.data.scenes[scene_name], bpy.data.scenes[target_name]
    saved, dup = levels(scene)
    copied = levels(target_scene)[0]
    assert saved.auto_level and not dup.auto_level and not copied.auto_level
    close((dup.black_point, dup.white_point), (.15, .65), 'saved duplicate')
    close((copied.black_point, copied.white_point), (.2, .75), 'saved manual')
    assert saved.node_tree != dup.node_tree != copied.node_tree
    photo = catalog.lookup_photo(None, 'levels-source')
    target = catalog.lookup_photo(None, 'levels-target')
    close(render(folder, 'native-reopened-auto', photo, scene), reference(pixels), 'reopened automatic graph')
    close(render(folder, 'native-reopened-manual', target, target_scene), reference(target_pixels, .2, .75),
          'reopened manual graph')
    levels_node.reset(copied, bpy.context)
    assert copied.auto_level and copied.black_point == 0 and copied.white_point == 1
    print('PASS native EXR pixels, upstream-only capture, persistent manual endpoints, copy isolation and reopen', flush=True)


def degenerate_pixels(folder):
    for name, pixels in (
            ('constant', np.tile([.375, .375, .375, 1], (19, 29, 1)).astype(np.float32)),
            ('constant-hdr', np.tile([2.5, 2.5, 2.5, 1], (19, 29, 1)).astype(np.float32)),
            ('transparent', np.zeros((19, 29, 4), np.float32)),
            ('transparent-hidden-rgb', np.tile([7, 3, 2, 0], (19, 29, 1)).astype(np.float32))):
        photo, scene = make_photo(folder, name, pixels)
        native_effects.create_effect(scene, levels_node.TYPE)
        close(render(folder, 'native-' + name, photo, scene), pixels, name + ' passes through')
    print('PASS constant and transparent native inputs remain finite and unchanged', flush=True)


def signed_ranges(folder):
    for name, low, high in (('positive-range', .2, .8), ('negative-range', -.8, -.2)):
        pixels = np.ones((21, 31, 4), np.float32)
        pixels[..., :3] = np.linspace(low, high, 31, dtype=np.float32)[None, :, None]
        pixels[2:4, 3:8, 3] = .25
        pixels[..., :3] *= pixels[..., 3:4]
        pixels[9:11, 11:13] = [999, -999, 999, 0]
        pixels[12, 16] = [9, -9, 9, 1e-7]
        photo, scene = make_photo(folder, name, pixels)
        effect = native_effects.create_effect(scene, levels_node.TYPE)
        sample = levels_node.capture(scene, native_effects.effect_node(effect))
        close((sample.low, sample.high), (low, high), name + ' transparent exclusion')
        close(render(folder, 'native-' + name, photo, scene), reference(pixels),
              name + ' transparent pixels cannot supply an endpoint')
    print('PASS strictly positive/negative ranges, hidden RGB and almost-transparent pixels', flush=True)


def capture_socket_selection(folder):
    pixels = np.ones((11, 17, 4), np.float32)
    pixels[..., :3] = (.1, .7, 1.5)
    pixels[..., 3] = np.linspace(.2, .8, 17, dtype=np.float32)[None, :]
    pixels[..., :3] *= pixels[..., 3:4]
    image = write_image(folder / 'capture-sockets.exr', pixels)
    scene = bpy.data.scenes.new('Levels socket capture')
    source = native_effects.create_effect(scene, native_effects.CUSTOM_TYPE)
    tree = source.node_group
    image_node = tree.nodes.new('CompositorNodeImage')
    image_node.image = image
    output = tree.nodes['Effect Output'].inputs['Image']
    tree.links.new(image_node.outputs['Image'], output)
    effect = native_effects.create_effect(scene, levels_node.TYPE)
    node = native_effects.effect_node(effect)
    sample = levels_node.capture(scene, node)
    close((sample.low, sample.high), (.1, 1.5), 'direct Image premultiplied capture')
    tree.links.new(image_node.outputs['Alpha'], output)
    sample = levels_node.capture(scene, node)
    close((sample.low, sample.high), (.2, .8), 'Alpha output converted to grayscale input')
    assert (sample.width, sample.height) == (17, 11)
    bpy.data.scenes.remove(scene)
    print('PASS capture follows the selected Image/Alpha output socket', flush=True)


def viewer_pixels_matches(scene, expected, *, timeout=15):
    """Yield to Blender until actual AUTO-precision GPU Viewer pixels match.

    A real-window test can import this helper and ``yield from`` it after making
    its scene visible in an Image Editor. ``expected`` uses Blender's bottom-up
    Image.pixels order. This exercises the preview's half-float intermediates;
    an AUTO-precision final render alone cannot provide that evidence.
    """
    import time
    import gpu
    assert not bpy.app.background
    expected = np.asarray(expected, dtype=np.float32)
    scene.render.compositor_device = 'GPU'
    scene.render.compositor_precision = 'AUTO'
    scene.render.resolution_x, scene.render.resolution_y = expected.shape[1], expected.shape[0]
    scene.render.resolution_percentage = 100
    completed = []

    def post(owner, *_):
        if owner == scene:
            completed.append(True)

    bpy.app.handlers.composite_post.append(post)
    actual = None
    try:
        scene.compositor_effects.active = scene.compositor_effects.active
        scene.update_tag()
        for area in bpy.context.screen.areas:
            area.tag_redraw()
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            viewer = compositor.viewer_image()
            if viewer.has_data and tuple(viewer.size) == (expected.shape[1], expected.shape[0]):
                data = np.empty(expected.size, dtype=np.float32)
                viewer.pixels.foreach_get(data)
                actual = data.reshape(expected.shape)
                if completed and np.isfinite(actual).all() and np.allclose(actual, expected, atol=2e-3, rtol=2e-3):
                    print('PASS actual GPU AUTO Viewer pixels:', gpu.platform.renderer_get(),
                          'max_abs', float(np.max(np.abs(actual - expected))), flush=True)
                    return
            yield
        assert completed, 'Automatic Viewer did not recompose the scene'
        assert actual is not None, 'Viewer has the wrong image domain or no pixels'
        assert np.isfinite(actual).all(), 'GPU AUTO Viewer contains nonfinite pixels'
        np.testing.assert_allclose(actual, expected, atol=2e-3, rtol=2e-3,
                                   err_msg='Actual GPU AUTO Viewer differs from shared-range levels')
    finally:
        bpy.app.handlers.composite_post.remove(post)


def main():
    if '--gpu' in sys.argv:
        assert not bpy.app.background, 'Use a real Blender window for GPU compositor acceptance'
    measurements()
    with TemporaryDirectory(prefix='photo-auto-level-') as directory:
        folder = Path(directory)
        actual_pixels(folder)
        degenerate_pixels(folder)
        signed_ranges(folder)
        capture_socket_selection(folder)
    print('AUTO_LEVEL_PASS', flush=True)
    if '--gpu' in sys.argv:
        bpy.ops.wm.quit_blender()


if __name__ == '__main__':
    main()
