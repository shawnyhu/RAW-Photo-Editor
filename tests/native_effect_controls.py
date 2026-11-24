"""Exercise native Scene Effects against Blender 5.3, including pixel output.

blender -b --factory-startup --python-exit-code 1 --python tests/native_effect_controls.py
"""
from pathlib import Path
import sys
import tempfile
import uuid

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import catalog, compositor, film_node, native_effects


def pixels(path):
    reader = oiio.ImageInput.open(str(path))
    try:
        return reader.read_image(format=oiio.FLOAT)[4, 4, :3]
    finally:
        reader.close()


def render(scene, path):
    # Blender 5.3 render preflight requires a camera for multi-effect stacks.
    # The production export copy owns that helper; the editable scene stays empty.
    scratch = compositor.export_scene(scene)
    try:
        scratch.render.filepath = str(path)
        bpy.ops.render.render(scene=scratch.name, write_still=True)
        return pixels(path)
    finally:
        bpy.data.scenes.remove(scratch)


def mutable_subtree_regressions():
    # A user may add the processing nodes to any ordinary native group. Tags
    # are a convenience for UI, never a prerequisite for cache isolation.
    original = bpy.data.scenes.new('Untagged Original')
    tree = bpy.data.node_groups.new('User Graph', 'CompositorNodeTree')
    developer = tree.nodes.new(compositor.DEVELOPER_TYPE)
    effect = original.compositor_effects.new('User Effect')
    effect.node_group = tree
    native_effects.ensure_independent_effects()
    cloned = original.copy()
    cloned.name = '.Sort Before Original'
    native_effects.ensure_independent_effects()
    cloned_node = next(n for n in cloned.compositor_effects[0].node_group.nodes
                       if n.bl_idname == compositor.DEVELOPER_TYPE)
    assert effect.node_group == tree and developer.id_data == tree
    assert cloned_node.node_tree != developer.node_tree
    cloned_node.temperature = 6100
    assert developer.temperature == 5500
    assert cloned_node.instance_uid != developer.instance_uid

    # Two different roots can still share a mutable branch. Clone the branch
    # containing the processor; leave a pure reusable asset shared.
    second = native_effects.create_effect(original, native_effects.CUSTOM_TYPE)
    branch = second.node_group.nodes.new('CompositorNodeGroup')
    branch.node_tree = tree
    asset = bpy.data.node_groups.new('Pure Reusable Asset', 'CompositorNodeTree')
    tree.nodes.new('CompositorNodeGroup').node_tree = asset
    other_asset = second.node_group.nodes.new('CompositorNodeGroup')
    other_asset.node_tree = asset
    native_effects.ensure_independent_effects()
    assert branch.node_tree != tree
    child = next(n for n in branch.node_tree.nodes if n.bl_idname == compositor.DEVELOPER_TYPE)
    assert child.node_tree != developer.node_tree
    child.temperature = 7200
    assert developer.temperature == 5500
    assert other_asset.node_tree == asset
    assert next(n for n in branch.node_tree.nodes if n.bl_idname == 'CompositorNodeGroup').node_tree == asset
    bpy.data.scenes.remove(cloned)
    bpy.data.scenes.remove(original)


def run(folder):
    catalog.register()
    compositor.register()
    native_effects.register()
    mutable_subtree_regressions()
    scene = bpy.context.scene
    original = scene.blender_raw.photos.add()
    original.uid = uuid.uuid4().hex
    original.name = 'Native Scene Effects Test'
    original.width = original.height = 16
    values = np.tile([.0625, .125, .25, 1.0], (16, 16, 1)).astype(np.float32)
    path = folder / 'source.exr'
    writer = oiio.ImageOutput.create(str(path))
    writer.open(str(path), oiio.ImageSpec(16, 16, 4, oiio.FLOAT))
    writer.write_image(values)
    writer.close()
    original.image = bpy.data.images.load(str(path))
    original.image.colorspace_settings.name = 'Linear Rec.709'
    scene[compositor.PHOTO_TAG] = original.uid
    scene[compositor.STUDIO_TAG] = True
    if hasattr(original, 'scene'):
        original.scene = scene
    scene.render.resolution_x = scene.render.resolution_y = 16
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'OPEN_EXR'
    scene.render.image_settings.color_depth = '32'
    scene.render.engine = 'BLENDER_EEVEE'
    scene.camera = None
    scene.render.use_compositing = True
    scene.compositor_effects.clear()
    reader = native_effects.create_effect(scene, compositor.READER_TYPE, photo=original)
    developer = native_effects.create_effect(scene, compositor.DEVELOPER_TYPE, photo=original)
    node = native_effects.effect_node(developer)
    assert native_effects.owner_effect(node) == (scene, developer)
    assert compositor.developer_source(node) == original
    assert list(native_effects.iter_effects(scene)) == [reader, developer]
    assert not reader.show_node_group_selector
    np.testing.assert_allclose(render(scene, folder / 'baseline.exr'), values[0, 0, :3], atol=2e-6)
    assert scene.camera is None
    native_effects.set_input_value(developer, 'Exposure', 1.0)
    assert node.exposure == 1.0
    np.testing.assert_allclose(render(scene, folder / 'exposure.exr'), values[0, 0, :3] * 2, atol=2e-6)
    node.exposure = -1
    assert native_effects.get_input_value(developer, 'Exposure') == -1
    np.testing.assert_allclose(render(scene, folder / 'proxy-set.exr'), values[0, 0, :3] / 2, atol=2e-6)
    developer.enable_for_render = False
    np.testing.assert_allclose(render(scene, folder / 'disabled.exr'), values[0, 0, :3], atol=2e-6)
    developer.enable_for_render = True
    # Blender's built-in action shares mutable groups. Our lifecycle hook must
    # isolate the root and all private nested processing groups before editing.
    bpy.ops.scene.duplicate_compositor_effect(name=developer.name)
    bpy.context.view_layer.update()
    duplicate = scene.compositor_effects[-1]
    copied_node = native_effects.effect_node(duplicate)
    assert duplicate.node_group != developer.node_group
    assert copied_node.node_tree != node.node_tree
    assert copied_node.instance_uid != node.instance_uid
    assert duplicate.node_group[native_effects.UID_TAG] != developer.node_group[native_effects.UID_TAG]
    native_effects.set_input_value(duplicate, 'Exposure', 2)
    assert node.exposure == -1 and copied_node.exposure == 2
    scene.compositor_effects.remove(duplicate)
    custom = native_effects.create_effect(scene, native_effects.CUSTOM_TYPE)
    assert custom.show_node_group_selector
    assert native_effects.effect_node(custom) is None
    np.testing.assert_allclose(render(scene, folder / 'custom.exr'), values[0, 0, :3] / 2, atol=2e-6)
    copied_scene = scene.copy()
    native_effects.ensure_independent_effects()
    assert {e.node_group for e in copied_scene.compositor_effects}.isdisjoint(
        e.node_group for e in scene.compositor_effects)
    copied_developer = copied_scene.compositor_effects[1]
    native_effects.set_input_value(copied_developer, 'Exposure', 3)
    assert node.exposure == -1
    # Save/reopen exercises real native input serialization and custom group
    # ownership, not a hand-built serialization of our own implementation.
    scene_name, effect_name = scene.name, developer.name
    blend = folder / 'native-effects.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False)
    scene = bpy.data.scenes[scene_name]
    developer = scene.compositor_effects[effect_name]
    assert native_effects.get_input_value(developer, 'Exposure') == -1
    node = native_effects.effect_node(developer)
    assert node and node.node_tree and node.exposure == -1
    np.testing.assert_allclose(render(scene, folder / 'reopened.exr'), values[0, 0, :3] / 2, atol=2e-6)
    print('NATIVE_EFFECT_CONTROLS_PASS: native exposure, pixel output, mute, duplication, custom group, scene copy, save/reopen')


with tempfile.TemporaryDirectory(prefix='native-effect-controls-') as directory:
    run(Path(directory))
