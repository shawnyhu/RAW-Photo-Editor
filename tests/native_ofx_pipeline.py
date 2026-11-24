"""Real official OFX pixels through native effects, workers, alpha and exports.

Set BLENDER_PHOTO_SPEKTRAFILM_OFX to the complete official bundle/extracted ZIP.
blender -b --factory-startup --offline-mode --python-exit-code 1 -P tests/native_ofx_pipeline.py
"""
from contextlib import closing
from pathlib import Path
import sys
import tempfile

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import (catalog, compositor, film_backend, film_bake, film_node,
                         film_preview, native_effects, ofx_node, ofx_schema,
                         scene_pipeline, spektrafilm_engine, spektrafilm_ofx_engine)


def write(path, pixels):
    with closing(oiio.ImageOutput.create(str(path))) as target:
        h, w, c = pixels.shape
        spec = oiio.ImageSpec(w, h, c, oiio.FLOAT)
        spec.attribute('oiio:ColorSpace', 'Linear Rec.709')
        assert target.open(str(path), spec) and target.write_image(pixels)


def read(path):
    with closing(oiio.ImageInput.open(str(path))) as source:
        return source.read_image(format=oiio.FLOAT)


def render_parameters(node):
    recipe = film_node.recipe(node).copy()
    recipe.pop('engine')
    return recipe


def run(folder):
    assert not spektrafilm_ofx_engine.backend_error(), spektrafilm_ofx_engine.backend_error()
    scene = bpy.context.scene
    scene.render.resolution_x, scene.render.resolution_y = 96, 64
    scene.render.resolution_percentage = 100
    scene.render.compositor_device = 'CPU'
    scene.render.compositor_precision = 'FULL'
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_depth = '32'
    scene.render.image_settings.color_mode = 'RGBA'
    x, y = np.meshgrid(np.linspace(.01, .8, 96), np.linspace(.02, .6, 64))
    chart = np.stack((x, y, (x + y) * .35, np.ones_like(x)), axis=-1).astype(np.float32)
    path = folder / 'input.exr'
    write(path, chart)
    image = bpy.data.images.load(str(path))
    image.colorspace_settings.name, image.alpha_mode = 'Linear Rec.709', 'PREMUL'
    source = native_effects.create_effect(scene, native_effects.CUSTOM_TYPE)
    source_image = source.node_group.nodes.new('CompositorNodeImage')
    source_image.image = image
    source.node_group.links.new(source_image.outputs['Image'],
                                scene_pipeline.output_socket(source.node_group))
    effect = native_effects.create_effect(scene, film_node.OFX_TYPE)
    node = native_effects.effect_node(effect)
    assert effect.name == node.bl_label == 'Spektrafilm OFX'
    assert film_node.BR_NODE_spektrafilm.bl_label == 'Spektrafilm Lite'
    for key in ('grainEnabled', 'halationEnabled'):
        if key in ofx_schema.BY_KEY:
            setattr(node, ofx_node.parameter_property(key), False)
    node.live_preview = True
    before = film_preview._signature(node, scene)
    setattr(node, ofx_node.parameter_property('filmExposureEv'), .65)
    setattr(node, ofx_node.parameter_property('printExposureEv'), -.2)
    assert film_preview._signature(node, scene) != before
    assert film_node.recipe(node)['engine'] == 'ofx'
    expected = spektrafilm_ofx_engine.render_rgba(chart, **render_parameters(node), frame=scene.frame_current)
    output = folder / 'preview.exr'
    film_bake.preview(scene, effect.node_group, node, output)
    np.testing.assert_allclose(read(output), expected, rtol=3e-3, atol=4e-4)
    print('PASS official OFX pixels through native Scene Effects and active film/print EV', flush=True)

    # Background snapshot worker must find the same plugin and restore its RNA.
    key = film_preview._key(node)
    film_preview._seen[key] = film_preview._signature(node, scene)[0]
    job = film_preview._start(key, scene, node)
    try:
        assert job.process.wait(timeout=180) == 0, (Path(job.worker_dir.name) / 'worker.log').read_text(errors='replace')
        np.testing.assert_allclose(read(Path(job.snapshot_dir.name) / 'result.exr'), expected,
                                   rtol=3e-3, atol=4e-4)
        film_preview._finish(job, node)
    finally:
        job.worker_dir.cleanup()
        job.snapshot_dir.cleanup()
        film_preview._seen.pop(key, None)
    print('PASS isolated OFX preview worker and result binding', flush=True)

    # Copying the effect isolates its parameters and nested cache group.
    duplicate = native_effects.duplicate_effect(scene, effect)
    copied = native_effects.effect_node(duplicate)
    assert copied != node and copied.node_tree != node.node_tree and copied.instance_uid != node.instance_uid
    setattr(copied, ofx_node.parameter_property('filmExposureEv'), -1.0)
    assert abs(getattr(node, ofx_node.parameter_property('filmExposureEv')) - .65) < 1e-6
    duplicate.enable_for_render = duplicate.enable_for_preview = False
    print('PASS OFX duplicate settings and cache isolation', flush=True)

    # A paused OFX stage still evaluates its current parameters for final exports.
    node.live_preview = False
    lite_effect = native_effects.create_effect(scene, film_node.FILM_TYPE)
    lite = native_effects.effect_node(lite_effect)
    lite.live_preview = True
    mixed_expected = spektrafilm_engine.render_rgba(expected.copy(), **film_node.recipe(lite), frame=scene.frame_current)
    export_scene = compositor.export_scene(scene)
    try:
        export_scene.render.filepath = str(folder / 'mixed-export.exr')
        with film_bake.prepared(export_scene, None, folder) as has_film:
            assert has_film
            assert bpy.ops.render.render(scene=export_scene.name, write_still=True) == {'FINISHED'}
        np.testing.assert_allclose(read(folder / 'mixed-export.exr'), mixed_expected, rtol=5e-3, atol=6e-4)
    finally:
        bpy.data.scenes.remove(export_scene)
    print('PASS mixed OFX + Lite full export with paused preview and disabled duplicate', flush=True)

    # Color is unpremultiplied for OFX, then restored without corrupting alpha.
    alpha = np.linspace(0, 1, 96, dtype=np.float32)[None, :, None]
    transparent = chart.copy()
    transparent[..., :3] *= alpha
    transparent[..., 3:4] = alpha
    write(folder / 'alpha.exr', transparent)
    film_backend.process(folder / 'alpha.exr', folder / 'alpha-out.exr', film_node.recipe(node))
    actual = read(folder / 'alpha-out.exr')
    np.testing.assert_allclose(actual[..., 3:4], np.broadcast_to(alpha, actual[..., 3:4].shape), atol=1e-7)
    assert np.isfinite(actual).all() and not np.any(actual[:, 0, :3])
    print('PASS transparent OFX processing preserves finite premultiplied RGBA', flush=True)

    spatial_recipe = film_node.recipe(node)
    spatial_recipe['parameters'].update(lensDistortionEnabled=True, lensDistortionAffectAlpha=True,
                                         lensDistortionRadialPercent=10.0, lensDistortionAutoCrop=False)
    straight = chart.copy()
    straight[..., 3:4] = alpha
    straight[:, 0, :3] = 0
    native_recipe = {k: v for k, v in spatial_recipe.items() if k != 'engine'}
    warped = spektrafilm_ofx_engine.render_rgba(straight, **native_recipe)
    assert np.max(np.abs(warped[..., 3:4] - alpha)) > 1e-4, 'Native lens alpha test did not warp coverage'
    warped[..., :3] *= warped[..., 3:4]
    film_backend.process(folder / 'alpha.exr', folder / 'warped-alpha.exr', spatial_recipe)
    np.testing.assert_allclose(read(folder / 'warped-alpha.exr'), warped, rtol=4e-3, atol=4e-4)
    print('PASS OFX lens Affect Alpha changes coverage through premultiplied EXR exchange', flush=True)


catalog.register()
compositor.register()
native_effects.register()
try:
    with tempfile.TemporaryDirectory(prefix='native-ofx-pipeline-') as tmp:
        run(Path(tmp))
finally:
    native_effects.unregister()
    compositor.unregister()
    catalog.unregister()
