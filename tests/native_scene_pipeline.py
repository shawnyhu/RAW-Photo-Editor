"""Exercise native effect inputs, cross-effect film dependencies and render pixels."""
from contextlib import closing, nullcontext
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import catalog, compositor, film_backend, film_bake, film_node, film_preview, scene_pipeline, native_effects, spektrafilm_engine


def write(path, pixels):
    height, width, channels = pixels.shape
    with closing(oiio.ImageOutput.create(str(path))) as out:
        spec = oiio.ImageSpec(width, height, channels, oiio.FLOAT)
        spec.attribute('oiio:ColorSpace', 'Linear Rec.709')
        assert out.open(str(path), spec) and out.write_image(pixels)


def read(path):
    with closing(oiio.ImageInput.open(str(path))) as source:
        return source.read_image(format=oiio.FLOAT)


def add_effect(scene, name, kind, has_input=True):
    tree = bpy.data.node_groups.new(name, 'CompositorNodeTree')
    if has_input:
        tree.interface.new_socket(name='Image', in_out='INPUT', socket_type='NodeSocketColor')
    tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    node = tree.nodes.new(kind)
    if has_input:
        tree.links.new(tree.nodes.new('NodeGroupInput').outputs[0], node.inputs['Image'])
    tree.links.new(node.outputs['Image'], tree.nodes.new('NodeGroupOutput').inputs[0])
    effect = scene.compositor_effects.new(name)
    effect.node_group = tree
    return effect, node


def nest_effect(effect):
    inner = effect.node_group
    outer = bpy.data.node_groups.new('Custom nested compositor', 'CompositorNodeTree')
    for direction in ('INPUT', 'OUTPUT'):
        outer.interface.new_socket(name='Image', in_out=direction, socket_type='NodeSocketColor')
    group = outer.nodes.new('CompositorNodeGroup')
    group.node_tree = inner
    source, sink = outer.nodes.new('NodeGroupInput'), outer.nodes.new('NodeGroupOutput')
    outer.links.new(source.outputs[0], group.inputs[0])
    outer.links.new(group.outputs[0], sink.inputs[0])
    effect.node_group = outer
    return group


def run(folder):
    scene = bpy.data.scenes.new('Native pipeline pixels')
    scene.render.resolution_x, scene.render.resolution_y = 64, 48
    scene.render.resolution_percentage = 100
    scene.render.compositor_device = 'CPU'
    scene.render.compositor_precision = 'FULL'
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_depth = '32'
    scene.render.image_settings.color_mode = 'RGBA'
    chart = np.full((48, 64, 4), (.12, .25, .4, 1), dtype=np.float32)
    source_path = folder / 'source.exr'
    write(source_path, chart)
    image = bpy.data.images.load(str(source_path))
    image.colorspace_settings.name = 'Linear Rec.709'
    source_effect, source = add_effect(scene, 'Source', 'CompositorNodeImage', False)
    source.image = image
    exposure_effect, exposure = add_effect(scene, 'Native Exposure', 'CompositorNodeExposure')
    tree = exposure_effect.node_group
    parameter = tree.interface.new_socket(name='Exposure', in_out='INPUT', socket_type='NodeSocketFloat')
    tree.links.new(next(n for n in tree.nodes if n.type == 'GROUP_INPUT').outputs[parameter.identifier], exposure.inputs['Exposure'])
    getattr(exposure_effect.properties.inputs, parameter.identifier).value = 1.0
    film_effect, film = add_effect(scene, 'Film', film_node.FILM_TYPE)
    film.live_preview = True
    assert scene_pipeline.immediate_image_source(scene, film) == exposure
    dependencies = list(scene_pipeline.upstream(scene))
    assert [node for _, node, _ in dependencies if node.type != 'GROUP_INPUT'] == [source, exposure, film]
    signature = film_preview._signature(film, scene)
    getattr(exposure_effect.properties.inputs, parameter.identifier).value = 2.0
    assert film_preview._signature(film, scene) != signature
    getattr(exposure_effect.properties.inputs, parameter.identifier).value = 1.0
    path = film_bake._capture(scene, film_effect.node_group, film, (), folder, 'PREVIEW')
    np.testing.assert_allclose(read(path), chart * (2, 2, 2, 1), atol=1e-5)
    signature = film_preview._signature(film, scene)
    exposure_effect.enable_for_preview = False
    assert film_preview._signature(film, scene) != signature
    path = film_bake._capture(scene, film_effect.node_group, film, (), folder, 'PREVIEW')
    np.testing.assert_allclose(read(path), chart, atol=1e-5)
    assert exposure_effect.enable_for_render
    path = film_bake._capture(scene, film_effect.node_group, film, (), folder, 'RENDER')
    np.testing.assert_allclose(read(path), chart * (2, 2, 2, 1), atol=1e-5)
    exposure_effect.enable_for_preview = True
    print('PASS native effect parameter and enable flags change actual film input and its signature', flush=True)

    second_effect, second = add_effect(scene, 'Second Film',
                                      film_node.OFX_TYPE if '--ofx' in sys.argv else film_node.FILM_TYPE)
    second.live_preview = True
    calls = []

    def half(source, output, recipe, **kwargs):
        pixels = read(source)
        calls.append(pixels.copy())
        pixels[:, :, :3] *= .5
        write(output, pixels)

    with patch.object(film_backend, 'process', side_effect=half):
        output = folder / 'preview.exr'
        film_bake.preview(scene, second_effect.node_group, second, output)
        assert len(calls) == 2
        np.testing.assert_allclose(calls[0], chart * (2, 2, 2, 1), atol=1e-5)
        np.testing.assert_allclose(calls[1], chart, atol=1e-5)
        np.testing.assert_allclose(read(output), chart * (.5, .5, .5, 1), atol=1e-5)
        calls.clear()
        film.live_preview = False
        with film_bake.prepared(scene, None, folder) as has_film:
            assert has_film and len(calls) == 2
            scene.render.filepath = str(folder / 'final.exr')
            assert bpy.ops.render.render(scene=scene.name, write_still=True) == {'FINISHED'}
        np.testing.assert_allclose(read(folder / 'final.exr'), chart * (.5, .5, .5, 1), atol=1e-5)
    print('PASS two native film effects evaluate in order and final output ignores paused previews', flush=True)
    assert film.node_tree.nodes['Film Result'].image is None
    assert second.node_tree.nodes['Film Result'].image is None
    before = scene_pipeline.stack_signature(scene)
    getattr(exposure_effect.properties.inputs, parameter.identifier).value = 3.0
    assert scene_pipeline.stack_signature(scene) != before
    print('PASS thumbnail signature and temporary film result restoration', flush=True)


def real_film(folder):
    if error := spektrafilm_engine.backend_error():
        raise RuntimeError(error)
    chart = np.full((48, 64, 4), (.12, .25, .4, 1), dtype=np.float32)
    source_path = folder / 'raw-linear.exr'
    write(source_path, chart)
    image = bpy.data.images.load(str(source_path))
    image.colorspace_settings.name = 'Linear Rec.709'
    image.alpha_mode = 'PREMUL'
    image['photo_editor_decode_cache'] = source_path.stem
    photo = catalog.library(bpy.context).photos.add()
    photo.uid, photo.name = 'native-real-film', 'Native real film'
    photo.filepath, photo.image = str(source_path), image
    scene = compositor.ensure_photo_scene(bpy.context, photo)
    scene.render.compositor_device = 'CPU'
    scene.render.compositor_precision = 'FULL'
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_depth = '32'
    scene.render.image_settings.color_mode = 'RGBA'
    developer = next(node for _, node, _ in scene_pipeline.node_instances(scene, compositor.DEVELOPER_TYPE))
    compositor.apply_developer_image(developer, photo, image)
    developer.exposure = 1.0
    film_effect = native_effects.create_effect(scene, film_node.FILM_TYPE, photo=photo)
    film = native_effects.effect_node(film_effect)
    if '--reversal' in sys.argv:
        signature = film_preview._signature(film, scene)
        film.film = 'fujifilm_velvia_100'
        assert film.process == film_node.PROCESS_MODES[1]
        assert film_preview._signature(film, scene) != signature
        film.process = film_node.PROCESS_MODES[0]
        signature = film_preview._signature(film, scene)
        film.process = film_node.PROCESS_MODES[1]
        assert film_preview._signature(film, scene) != signature
        signature = film_preview._signature(film, scene)
        film.scanNegativeInvert = True
        assert film_preview._signature(film, scene) != signature
        film.scanNegativeInvert = False
    film.live_preview = True
    assert film_bake.direct_raw_image(scene, film) == (image, 1.0)
    output = folder / 'native-direct.exr'
    film_bake.preview(scene, film_effect.node_group, film, output)
    expected = spektrafilm_engine.render(chart[:, :, :3] * 2, **film_node.recipe(film), frame=scene.frame_current)
    np.testing.assert_allclose(read(output)[:, :, :3], expected, rtol=8e-4, atol=2e-4)
    print('PASS real Spektrafilm native Reader/Developer fast path with effect Exposure', flush=True)

    # Inserting an ordinary adjustment must fall back to the actual compositor input.
    exposure_effect, exposure = add_effect(scene, 'Extra Exposure', 'CompositorNodeExposure')
    exposure.inputs['Exposure'].default_value = 1
    with bpy.context.temp_override(scene=scene):
        assert bpy.ops.scene.move_compositor_effect_to_index(name=exposure_effect.name, index=2) == {'FINISHED'}
    assert film_bake.direct_raw_image(scene, film) is None
    output = folder / 'native-slow.exr'
    film_bake.preview(scene, film_effect.node_group, film, output)
    expected = spektrafilm_engine.render(chart[:, :, :3] * 4, **film_node.recipe(film), frame=scene.frame_current)
    np.testing.assert_allclose(read(output)[:, :, :3], expected, rtol=8e-4, atol=2e-4)
    second_effect = native_effects.create_effect(scene, film_node.FILM_TYPE, photo=photo)
    second = native_effects.effect_node(second_effect)
    second.live_preview = True
    wrapper = nest_effect(second_effect)
    assert scene_pipeline.instance_for_node(scene, second)[1] == (wrapper,)
    output = folder / 'native-dual.exr'
    film_bake.preview(scene, second_effect.node_group, second, output)
    expected = spektrafilm_engine.render(expected, **film_node.recipe(second), frame=scene.frame_current)
    np.testing.assert_allclose(read(output)[:, :, :3], expected, rtol=2e-3, atol=3e-4)
    export_scene = compositor.export_scene(scene)
    compositor.export_photo(bpy.context, photo, str(folder / 'native-real-final.exr'), export_scene)
    bpy.data.scenes.remove(export_scene)
    np.testing.assert_allclose(read(folder / 'native-real-final.exr')[:, :, :3], expected, rtol=2e-3, atol=3e-4)
    print('PASS real Spektrafilm native adjustment slow path, nested custom group, two film stages and full render', flush=True)
    key = film_preview._key(second)
    film_preview._seen[key] = film_preview._signature(second, scene)[0]
    job = film_preview._start(key, scene, second)
    try:
        assert job.process.wait(timeout=90) == 0, (Path(job.worker_dir.name) / 'worker.log').read_text(errors='replace')
        np.testing.assert_allclose(read(Path(job.snapshot_dir.name) / 'result.exr')[:, :, :3], expected,
                                   rtol=2e-3, atol=3e-4)
        image = film_preview._finish(job, second)
        assert second.node_tree.nodes['Film Result'].image == image
    finally:
        job.worker_dir.cleanup()
        job.snapshot_dir.cleanup()
        film_preview._seen.pop(key, None)
    print('PASS real Spektrafilm snapshot preview worker and main-thread result binding', flush=True)


catalog.register()
compositor.register()
try:
    # The default suite mocks Film processing and needs only node construction;
    # hosted runners may have no Vulkan/Metal device for the node's poll callback.
    with tempfile.TemporaryDirectory(prefix='native-scene-pipeline-') as temporary, (
        nullcontext() if '--real-film' in sys.argv else
        patch.object(spektrafilm_engine, 'backend_error', return_value='')
    ):
        run(Path(temporary))
        if '--real-film' in sys.argv:
            real_film(Path(temporary))
finally:
    compositor.unregister()
    catalog.unregister()
