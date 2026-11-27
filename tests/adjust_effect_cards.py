"""Exercise Adjust menu actions, native settings and private scene effects in Blender 5.3.

blender -b --factory-startup --offline-mode --python-exit-code 1 -P tests/adjust_effect_cards.py
After installing the ZIP, omit --factory-startup and append -- --installed.
To inspect the native controls in a real window, omit -b and append -- --ui.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sys

import bpy

if '--installed' in sys.argv:
    from bl_ext.user_default.raw_photo_editor import adjust_effects, adjustments, catalog, compositor, native_effects
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import blender_raw
    from blender_raw import adjust_effects, adjustments, catalog, compositor, native_effects
    blender_raw.register()


class MenuLayout:
    operator_context = 'EXEC_REGION_WIN'

    def __init__(self):
        self.items = []

    def separator(self):
        self.items.append('|')

    def menu(self, name):
        self.items.append(name)

    def menu_contents(self, name):
        self.items.append(name)

    def operator(self, name, *, text, icon):
        op = SimpleNamespace(operator=name, text=text)
        self.items.append(op)
        return op


def menus():
    menu = bpy.types.SCENE_MT_add_compositor_effect
    assert native_effects._add_menu in menu.draw._draw_funcs
    fake = SimpleNamespace(layout=MenuLayout())
    native_effects._add_menu(fake, SimpleNamespace(workspace={}, scene={compositor.STUDIO_TAG: True}))
    assert [getattr(item, 'text', item) for item in fake.layout.items] == [
        'Search...', '|', 'BR_MT_adjust_effects', 'SCENE_MT_add_compositor_effect_root_catalogs',
        '|', 'Spektrafilm Lite', 'Spektrafilm OFX', '|', 'Raw Reader', 'Raw Developer',
        'Crop & Perspective', 'Custom Compositor']
    assert fake.layout.items[0].menu_idname == menu.__name__
    fake.layout = MenuLayout()
    native_effects._add_menu(fake, SimpleNamespace(workspace={}, scene={}))
    assert 'Scene Effect' in [getattr(item, 'text', item) for item in fake.layout.items]
    fake.layout = MenuLayout()
    native_effects.BR_MT_adjust_effects.draw(fake, None)
    items = fake.layout.items
    assert [op.text for op in items] == ['Brightness/Contrast', 'Color Balance', 'Color Correction', 'Exposure',
                                         'Gamma', 'Hue Correct', 'Hue/Saturation/Value', 'RGB Curves', 'Tonemap']
    return items


def run(folder):
    items = menus()
    context = bpy.context
    context.scene.blender_raw.owner = True
    library = catalog.library(context)
    source, target = library.photos.add(), library.photos.add()
    source.name = source.uid = 'Source'
    target.name = target.uid = 'Target'
    scene = compositor.ensure_photo_scene(context, source)
    with context.temp_override(scene=scene):
        for op in items:
            assert op.operator == 'blender_raw.add_native_effect'
            assert bpy.ops.blender_raw.add_native_effect(kind=op.kind) == {'FINISHED'}
            effect = scene.compositor_effects.active
            node = native_effects.effect_node(effect)
            assert node.bl_idname == op.kind and effect.name == op.text and not effect.show_node_group_selector
            incoming = adjust_effects.image_socket(node.inputs, op.kind)
            outgoing = adjust_effects.image_socket(node.outputs, op.kind)
            assert incoming.links[0].from_node.type == 'GROUP_INPUT'
            assert outgoing.links[0].to_node.type == 'GROUP_OUTPUT'

    def node(scene, name):
        return native_effects.effect_node(scene.compositor_effects[name])

    values = (('Brightness/Contrast', 'Bright', 12.5), ('Color Balance', 'Base Gain', .75),
              ('Color Correction', 'Master Contrast', 1.5), ('Exposure', 'Exposure', 1.25),
              ('Gamma', 'Gamma', 2.5), ('Hue/Saturation/Value', 'Saturation', .5))
    for label, identifier, value in values:
        node(scene, label).inputs[identifier].default_value = value
    for label, index in (('Hue Correct', 1), ('RGB Curves', 3)):
        mapping = node(scene, label).mapping
        mapping.curves[index].points.new(.4375, .625)
        mapping.update()
    tonemap = node(scene, 'Tonemap')
    assert tonemap.inputs['Type'].default_value == 'R/D Photoreceptor'
    tonemap.inputs['Intensity'].default_value = 1.75
    tonemap.inputs['Type'].default_value = 'Rh Simple'
    tonemap.inputs['Key'].default_value = .25
    duplicate = native_effects.duplicate_effect(scene, scene.compositor_effects['RGB Curves'])
    copied_mapping = native_effects.effect_node(duplicate).mapping
    copied_mapping.curves[3].points.new(.75, .125)
    copied_mapping.update()
    assert len(copied_mapping.curves[3].points) == len(node(scene, 'RGB Curves').mapping.curves[3].points) + 1
    # The same Blender node inside an ordinary custom group keeps its original UI.
    custom = native_effects.create_effect(scene, native_effects.CUSTOM_TYPE)
    custom.node_group.nodes.new('CompositorNodeCurveRGB')
    assert custom.show_node_group_selector and native_effects.effect_node(custom) is None

    adjustments.copy(context, source)
    assert adjustments.paste(context, [target]) == 1
    adjustments.clear()
    source_name, target_name = scene.name, target.scene.name
    path = folder / 'adjust-cards.blend'
    assert bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {'FINISHED'}
    assert bpy.ops.wm.open_mainfile(filepath=str(path)) == {'FINISHED'}
    source_scene, target_scene = bpy.data.scenes[source_name], bpy.data.scenes[target_name]
    for label, identifier, value in values:
        original, pasted = node(source_scene, label), node(target_scene, label)
        assert original.id_data != pasted.id_data
        assert original.inputs[identifier].default_value == pasted.inputs[identifier].default_value == value
    for label, index in (('Hue Correct', 1), ('RGB Curves', 3)):
        pasted = node(target_scene, label).mapping
        assert (.4375, .625) in [tuple(point.location) for point in pasted.curves[index].points]
    assert node(target_scene, 'Tonemap').inputs['Type'].default_value == 'Rh Simple'
    assert node(target_scene, 'Tonemap').inputs['Key'].default_value == .25
    node(target_scene, 'Tonemap').inputs['Type'].default_value = 'R/D Photoreceptor'
    assert node(target_scene, 'Tonemap').inputs['Intensity'].default_value == 1.75
    assert node(source_scene, 'Tonemap').inputs['Type'].default_value == 'Rh Simple'


def run_window():
    """Inspect the actual Settings child for each card, including curve widgets."""
    import traceback

    seen = {}

    class TEST_PT_adjust_settings(bpy.types.Panel):
        bl_space_type = 'PROPERTIES'
        bl_region_type = 'WINDOW'
        bl_context = 'compositor'
        bl_label = 'Adjust test'

        def draw(self, context):
            # Exercise the production Settings body in a real panel. Native
            # instance disclosure triangles keep their own per-region state.
            effect = context.scene.compositor_effects.active
            node = native_effects.effect_node(effect)
            if node and native_effects._ui_panel_ready(context):
                panel = SimpleNamespace(layout=self.layout, custom_data=effect)
                native_effects.BR_PT_native_effect_controls.draw(panel, context)
                seen[node.bl_idname] = str(self.layout.introspect())

    def inspect():
        window = bpy.context.window_manager.windows[0]
        area = max(window.screen.areas, key=lambda candidate: candidate.width * candidate.height)
        area.type = 'PROPERTIES'
        area.spaces.active.context = 'COMPOSITOR'
        window.scene[compositor.STUDIO_TAG] = True
        expected = (('CompositorNodeBrightContrast', 'Contrast'), ('CompositorNodeColorBalance', 'Lift'),
                    ('CompositorNodeColorCorrection', 'Master'), ('CompositorNodeExposure', 'Exposure'),
                    ('ShaderNodeGamma', 'Gamma'), ('CompositorNodeHueCorrect', 'Edit Curve Map'),
                    ('CompositorNodeHueSat', 'Saturation'), ('CompositorNodeCurveRGB', 'Edit Curve Map'),
                    ('CompositorNodeTonemap', 'Intensity'))
        for kind, marker in expected:
            effect = native_effects.create_effect(window.scene, kind)
            effect.show_expanded = True
            for _ in range(30):
                area.tag_redraw()
                yield
                if marker in seen.get(kind, ''):
                    break
            assert marker in seen.get(kind, ''), (kind, marker, seen.get(kind, 'not drawn'))
            if kind == 'CompositorNodeColorBalance':
                mode = native_effects.effect_node(effect).inputs['Type']
                for method, marker in (('Offset/Power/Slope (ASC-CDL)', 'Slope'), ('White Point', 'Temperature')):
                    mode.default_value = method
                    for _ in range(20):
                        area.tag_redraw()
                        yield
                        if method in seen[kind] and marker in seen[kind]:
                            break
                    assert method in seen[kind] and marker in seen[kind], seen[kind]
            if kind == 'CompositorNodeTonemap':
                node = native_effects.effect_node(effect)
                node.inputs['Type'].default_value = 'Rh Simple'
                for _ in range(20):
                    area.tag_redraw()
                    yield
                    if 'Key' in seen[kind]:
                        break
                assert 'Key' in seen[kind] and 'Intensity' not in seen[kind], seen[kind]
            effect.show_expanded = False
        print('ADJUST_EFFECT_CARD_UI_PASS', flush=True)

    bpy.utils.register_class(TEST_PT_adjust_settings)
    steps = inspect()

    def tick():
        try:
            next(steps)
            return .15
        except StopIteration:
            pass
        except Exception:
            traceback.print_exc()
            sys.stdout.flush()
            sys.stderr.flush()
            import os
            os._exit(1)
        bpy.ops.wm.quit_blender()
        return None

    bpy.app.timers.register(tick, first_interval=.5)


if '--ui' in sys.argv:
    run_window()
else:
    with TemporaryDirectory(prefix='adjust-effects-') as scratch:
        run(Path(scratch))
    print('ADJUST_EFFECT_CARDS_PASS', flush=True)
