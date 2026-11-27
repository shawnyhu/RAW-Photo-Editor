"""Blender 5.3 background regression for the grouped Developer controls and saved inputs."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

import bpy

if 'bl_ext.user_default.raw_photo_editor' in bpy.context.preferences.addons:
    from bl_ext.user_default.raw_photo_editor import catalog, compositor, metering, native_effects
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from blender_raw import catalog, compositor, metering, native_effects
    catalog.register()
    compositor.register()
    native_effects.register()


class Controls:
    """Capture RNA bindings passed to the shared node/card layout."""
    def __init__(self):
        self.labels = []
        self.props = []
        self.ops = []
        self.items = []

    def column(self, **_kwargs):
        return self

    row = column

    def separator(self):
        pass

    def label(self, *, text, **_kwargs):
        self.labels.append(text)
        self.items.append(text)

    def prop(self, owner, prop, **_kwargs):
        self.props.append((owner, prop))
        self.items.append(prop)

    def operator(self, name, **_kwargs):
        self.ops.append(name)
        return SimpleNamespace()


def draw(node, effect=None):
    controls = Controls()
    node.draw_controls(controls, effect=effect)
    assert [x for x in controls.labels if x in headings] == headings
    return controls


headings = ['Camera & Look', 'White Balance', 'Auto Exposure']
mode_numbers = {'AS_SHOT': 0, 'GLOBAL': 1, 'CENTER': 2, 'HIGHLIGHTS': 3, 'SHADOWS': 4,
                'FILM_CENTER': 5, 'FILM_MEDIAN': 6, 'SPOT': 7}
assert dict((i[0], i[3]) for i in metering.STORED_MODES) == mode_numbers
scene = bpy.context.scene
photo = catalog.library(bpy.context).photos.add()
photo.uid = photo.name = 'Developer controls'
scene[compositor.PHOTO_TAG] = photo.uid
scene[compositor.STUDIO_TAG] = True
photo.scene = scene
native_effects.create_effect(scene, compositor.READER_TYPE, photo=photo)
effect = native_effects.create_effect(scene, compositor.DEVELOPER_TYPE, photo=photo)
node = native_effects.effect_node(effect)
native_value = native_effects.input_property(effect, 'Exposure')
wrapper = effect.node_group.interface.items_tree['Exposure']
assert wrapper.hide_in_modifier and node.inputs['Exposure'].is_linked
controls = draw(node, effect)
group = controls.items.index('Auto Exposure')
assert controls.items[group + 1:] == ['value', 'as_shot']
assert (native_value, 'value') in controls.props
assert node.as_shot and node.meter_mode == 'SPOT'

node.as_shot = False
assert node.auto_exposure == node.meter_mode == 'SPOT'
controls = draw(node, effect)
assert controls.items[group + 1:group + 4] == ['value', 'as_shot', 'meter_mode']
assert 'blender_raw.pick_meter' in controls.ops and (node, 'spot_radius') in controls.props
assert [x[0] for x in metering.mode_items(node, None)] == [
    'SPOT', 'GLOBAL', 'CENTER', 'HIGHLIGHTS', 'SHADOWS']
for mode in ('GLOBAL', 'CENTER', 'HIGHLIGHTS', 'SHADOWS'):
    node.meter_mode = mode
    assert node.auto_exposure == mode and not node.as_shot
    assert 'blender_raw.pick_meter' not in draw(node, effect).ops
node.as_shot = True
node.as_shot = False
assert node.auto_exposure == 'SPOT'

# A free Developer binds to its own socket (including its existing animation).
tree = bpy.data.node_groups.new('Standalone developer', 'CompositorNodeTree')
tree.use_fake_user = True
plain = tree.nodes.new(compositor.DEVELOPER_TYPE)
socket = plain.inputs['Exposure']
assert socket.hide_value and socket.enabled and not socket.hide
assert (socket, 'default_value') in draw(plain).props
socket.default_value = -1.5
assert socket.keyframe_insert(data_path='default_value', frame=1)
source = tree.nodes.new('ShaderNodeValue')
tree.links.new(source.outputs[0], socket)
assert 'Exposure (linked)' in draw(plain).labels

# Simulate the public v1.0.2-beta.2 interface before the grouped GUI was introduced.
wrapper.hide_in_modifier = False
plain.node_tree.interface.items_tree['Exposure'].hide_value = False
identifier = wrapper.identifier
# Changing the interface invalidates Blender's generated RNA property wrappers.
native_value = native_effects.input_property(effect, 'Exposure')
native_path = native_value.path_from_id('value')
native_value.value = -1.25
assert native_value.keyframe_insert(data_path='value', frame=1)
native_value.value = 2
assert native_value.keyframe_insert(data_path='value', frame=2)
node.auto_exposure = 'FILM_MEDIAN'
assert node.meter_mode == 'FILM_MEDIAN'
assert metering.mode_items(node, None)[-1][0] == 'FILM_MEDIAN'
copied = native_effects.duplicate_effect(scene, effect)
copy_node = native_effects.effect_node(copied)
assert copy_node.auto_exposure == node.auto_exposure and copy_node.exposure == node.exposure
copy_node.as_shot = True
copy_node.exposure = 3
assert node.auto_exposure == 'FILM_MEDIAN' and node.exposure == 2

# Round-trip every stored enum, including legacy values that are shown only when active.
for mode in mode_numbers:
    persistent = tree.nodes.new(compositor.DEVELOPER_TYPE)
    persistent.name = mode
    persistent.auto_exposure = mode
names = scene.name, effect.name, tree.name, plain.name
with tempfile.TemporaryDirectory(prefix='raw-developer-controls-') as folder:
    blend = str(Path(folder) / 'controls.blend')
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    bpy.ops.wm.open_mainfile(filepath=blend, load_ui=False)
    scene = bpy.data.scenes[names[0]]
    effect = scene.compositor_effects[names[1]]
    node = native_effects.effect_node(effect)
    wrapper = effect.node_group.interface.items_tree['Exposure']
    native_value = native_effects.input_property(effect, 'Exposure')
    assert wrapper.hide_in_modifier and wrapper.identifier == identifier and node.inputs['Exposure'].is_linked
    assert native_value.path_from_id('value') == native_path
    tree = bpy.data.node_groups[names[2]]
    socket = tree.nodes[names[3]].inputs['Exposure']
    assert socket.hide_value and socket.is_linked and socket.default_value == -1.5
    for mode in mode_numbers:
        saved = tree.nodes[mode]
        assert saved.auto_exposure == mode and saved.as_shot == (mode == 'AS_SHOT')
    for frame, ev in ((1, -1.25), (2, 2)):
        scene.frame_set(frame)
        assert native_value.value == node.exposure == ev, (frame, ev, native_value.value, node.exposure)
    assert node.auto_exposure == 'FILM_MEDIAN' and not node.as_shot

print('DEVELOPER_METER_CONTROLS_PASS: node/card layout, Spot, all eight stored modes, native keys, copies, save/reopen')
