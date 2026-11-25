"""Regress nested source reconciliation and render-only Developer controls.

blender -b --factory-startup --offline-mode --python-exit-code 1 -P tests/native_source_binding.py
"""
from pathlib import Path
import sys
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blender_raw import catalog, compositor, native_effects, spot_meter


def photograph(library, name):
    photo = library.photos.add()
    photo.uid = photo.name = name
    photo.image = bpy.data.images.new(name, width=4, height=4, float_buffer=True)
    return photo


def nested_developer(scene, photo):
    child = bpy.data.node_groups.new("Nested Developer", "CompositorNodeTree")
    for direction in ("INPUT", "OUTPUT"):
        child.interface.new_socket(name="Image", in_out=direction, socket_type="NodeSocketColor")
    source, output = child.nodes.new("NodeGroupInput"), child.nodes.new("NodeGroupOutput")
    node = child.nodes.new(compositor.DEVELOPER_TYPE)
    child.links.new(source.outputs["Image"], node.inputs["Image"])
    child.links.new(node.outputs["Image"], output.inputs["Image"])
    effect = native_effects.create_effect(scene, native_effects.CUSTOM_TYPE, photo=photo)
    root = effect.node_group
    group = root.nodes.new("CompositorNodeGroup")
    group.node_tree = child
    root.links.new(root.nodes["Effect Input"].outputs["Image"], group.inputs["Image"])
    root.links.new(group.outputs["Image"], root.nodes["Effect Output"].inputs["Image"])
    return effect, node


def check_nested_reconciliation(photo, other):
    scene = compositor.ensure_photo_scene(bpy.context, photo)
    scene.compositor_effects.remove(scene.compositor_effects[1])
    reader_effect = scene.compositor_effects[0]
    effect, node = nested_developer(scene, photo)
    reader = native_effects.effect_node(reader_effect)
    root_id = effect.node_group.session_uid
    assert node.id_data != effect.node_group
    assert compositor.developer_source(node) == photo
    compositor.apply_developer_image(node, photo, photo.image)
    assert node.node_tree.nodes["Developed RAW"].image == photo.image

    # Structural effects changes enqueue root groups only. The synchronizer must
    # visit a Developer inside a child group and invalidate its cached source.
    reader_effect.enable_for_preview = False
    reader_effect.enable_for_render = False
    compositor._sync_developer_sources({root_id})
    assert compositor.developer_source(node) is None
    assert node.photo_uid == ""
    assert node.node_tree.nodes["Developed RAW"].image is None
    assert node.node_tree.nodes["Calibration"].inputs[0].links[0].from_node.name == "Developer Input"

    reader_effect.enable_for_preview = True
    reader_effect.enable_for_render = True
    compositor._sync_developer_sources({root_id})
    assert node.photo_uid == photo.uid
    compositor.apply_developer_image(node, photo, photo.image)
    with bpy.context.temp_override(scene=scene):
        assert bpy.ops.scene.move_compositor_effect_to_index(name=reader_effect.name, index=1) == {"FINISHED"}
    compositor._sync_developer_sources({root_id})
    assert node.photo_uid == ""
    assert node.node_tree.nodes["Developed RAW"].image is None
    with bpy.context.temp_override(scene=scene):
        assert bpy.ops.scene.move_compositor_effect_to_index(name=reader_effect.name, index=0) == {"FINISHED"}
    compositor._sync_developer_sources({root_id})
    assert node.photo_uid == photo.uid

    compositor.apply_developer_image(node, photo, photo.image)
    compositor._bind_node(reader, other)
    compositor._sync_developer_sources({root_id})
    assert compositor.developer_source(node) == other
    assert node.photo_uid == other.uid
    assert node.node_tree.nodes["Developed RAW"].image is None

    # Export the first photo with a render-only Reader bound to the second.
    # File paths must travel with that Reader, even inside a nested Developer.
    from blender_raw import jobs
    reader_effect.enable_for_preview = False
    other.filepath = str(Path(__file__).resolve())
    node.camera_input = 'ICC'
    node.profile_path = str(Path(__file__).with_name('external.icc'))
    node.look = 'LUT'
    node.lut_path = str(Path(__file__).parent / 'fixtures' / 'half-green.cube')
    with patch.object(compositor, 'developer_source', side_effect=AssertionError('Global source search')):
        bindings = jobs._export_bindings([photo], catalog.library(bpy.context))
    assert set(bindings) == {photo.uid, other.uid}
    assert bindings[other.uid] == {
        'source': str(catalog.linked_file(other)),
        'profiles': {node.instance_uid: str(catalog.linked_profile(node))},
        'luts': {node.instance_uid: str(catalog.linked_lut(node))},
    }
    effect.enable_for_render = False
    bindings = jobs._export_bindings([photo], catalog.library(bpy.context))
    assert not bindings[other.uid]['profiles'] and not bindings[other.uid]['luts']


def check_render_only_controls(photo):
    scene = compositor.ensure_photo_scene(bpy.context, photo)
    effect = scene.compositor_effects[1]
    node = native_effects.effect_node(effect)
    effect.enable_for_preview = False
    assert effect.enable_for_render
    assert compositor.developer_source(node) is None
    assert compositor.developer_source(node, mode="RENDER") == photo

    class Layout:
        def __init__(self):
            self.labels = []

        def label(self, *, text, **_kwargs):
            self.labels.append(text)

    shown = []
    layout = Layout()
    with patch.object(catalog, "draw_develop", side_effect=lambda _layout, _node, source: shown.append(source.uid)), \
            patch.object(spot_meter, "draw_controls"):
        node.draw_controls(layout, show_source=True)
    assert shown == [photo.uid], "Render-only Developer must retain its camera/white-balance controls"
    assert photo.name in layout.labels
    assert not any("Connect a Raw Reader" in label for label in layout.labels)


catalog.register()
compositor.register()
bpy.context.scene.blender_raw.owner = True
library = catalog.library(bpy.context)
first, second, third = (photograph(library, name) for name in ("First", "Second", "Render Only"))
check_nested_reconciliation(first, second)
check_render_only_controls(third)
print("PASS nested Developer source invalidation/rebind on native disable/reorder and render-only controls", flush=True)
