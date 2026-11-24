"""Real-window regression for adding Python controls to existing native cards.

blender --factory-startup --enable-event-simulate --python tests/native_effect_panels_ui.py
"""
from pathlib import Path
import sys
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import blender_raw
from blender_raw import compositor, native_effects

records = set()


def record_header(self, context):
    if not native_effects._ui_panel_ready(context):
        return
    effect = self.custom_data
    records.add((context.screen.name, effect.name))


# Test-only header instrumentation also runs for collapsed Settings children.
# Production has no card-header action; each record proves Blender supplied the
# correct native effect custom_data after lifecycle transitions.
native_effects.BR_PT_native_effect_controls.draw_header = record_header


def make_effect(scene, name):
    group = bpy.data.node_groups.new(name, 'CompositorNodeTree')
    group.interface.new_socket(name='Image', in_out='INPUT', socket_type='NodeSocketColor')
    group.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
    source = group.nodes.new('NodeGroupInput')
    output = group.nodes.new('NodeGroupOutput')
    group.links.new(source.outputs['Image'], output.inputs['Image'])
    effect = scene.compositor_effects.new(name)
    effect.node_group = group
    effect.show_node_group_selector = False


def compositor_area(window):
    area = max(window.screen.areas, key=lambda item: item.width * item.height)
    area.type = 'PROPERTIES'
    area.spaces.active.context = 'COMPOSITOR'
    return area


def redraw(area, count=8):
    for _ in range(count):
        area.tag_redraw()
        yield


def check_screen(screen):
    assert {(screen.name, 'Alpha'), (screen.name, 'Beta')} <= records, records
    assert native_effects._ui_reset_scene_get() is None
    assert not any(scene.name.startswith('.Photo Editor UI Refresh') for scene in bpy.data.scenes)


def test():
    window = bpy.context.window_manager.windows[0]
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    scene = window.scene
    scene[compositor.STUDIO_TAG] = 'lifecycle-probe'
    make_effect(scene, 'Alpha')
    make_effect(scene, 'Beta')
    first = window.workspace
    first_name = first.name
    second = next(workspace for workspace in bpy.data.workspaces if workspace != first)
    area_a = compositor_area(window)
    screen_a = window.screen
    yield from redraw(area_a)
    window.workspace = second
    yield
    area_b = compositor_area(window)
    screen_b = window.screen
    yield from redraw(area_b)
    window.workspace = first
    yield
    original_draw = bpy.types.SCENE_PT_compositor_effects.draw
    blender_raw.register()
    yield from redraw(area_a, 12)
    check_screen(screen_a)
    print('NATIVE_UI_ENABLE_EXISTING_PASS', flush=True)
    window.workspace = second
    yield from redraw(area_b, 12)
    check_screen(screen_b)
    print('NATIVE_UI_INACTIVE_WORKSPACE_PASS', flush=True)
    window.workspace = first
    yield from redraw(area_a)
    native_effects.unregister()
    assert bpy.types.SCENE_PT_compositor_effects.draw is original_draw
    assert native_effects._ui_reset_scene_get() is None
    yield from redraw(area_a)
    records.clear()
    native_effects.register()
    yield from redraw(area_a, 12)
    check_screen(screen_a)
    window.workspace = second
    yield from redraw(area_b, 12)
    check_screen(screen_b)
    print('NATIVE_UI_DISABLE_REENABLE_PASS', flush=True)
    bpy.context.preferences.edit.use_global_undo = True
    region = next(item for item in area_b.regions if item.type == 'WINDOW')
    with bpy.context.temp_override(window=window, area=area_b, region=region):
        bpy.ops.ed.undo_push(message='Before UI helper')
        native_effects._ui_reset_scene_create()
        bpy.ops.ed.undo_push(message='With UI helper')
        native_effects._ui_reset_scene_remove()
        bpy.ops.ed.undo_push(message='After UI helper')
        assert bpy.ops.ed.undo() == {'FINISHED'}
    yield from redraw(area_b)
    assert not any(scene.get(native_effects._UI_RESET_TAG) for scene in bpy.data.scenes)
    with bpy.context.temp_override(window=window, area=area_b, region=region):
        assert bpy.ops.ed.redo() == {'FINISHED'}
    yield from redraw(area_b)
    assert not any(scene.get(native_effects._UI_RESET_TAG) for scene in bpy.data.scenes)
    print('NATIVE_UI_UNDO_HELPER_CLEANUP_PASS', flush=True)
    saved = ROOT / '.native-test-output/native-panels-lifecycle.blend'
    native_effects._ui_reset_scene_create()
    bpy.ops.wm.save_as_mainfile(filepath=str(saved))
    assert not any(scene.get(native_effects._UI_RESET_TAG) for scene in bpy.data.scenes)
    records.clear()
    bpy.ops.wm.open_mainfile(filepath=str(saved))
    window = bpy.context.window_manager.windows[0]
    area_b = next(area for area in window.screen.areas
                  if area.type == 'PROPERTIES' and area.spaces.active.context == 'COMPOSITOR')
    yield from redraw(area_b, 12)
    check_screen(window.screen)
    print('NATIVE_UI_SAVE_REOPEN_HELPER_CLEANUP_PASS', flush=True)
    # Whole-extension teardown must clear native panel animations before
    # unregistering any of the other Python panels.
    blender_raw.unregister()
    assert bpy.types.SCENE_PT_compositor_effects.draw is original_draw
    yield from redraw(area_b)
    window.workspace = bpy.data.workspaces.get(first_name)
    area_a = next(area for area in window.screen.areas
                  if area.type == 'PROPERTIES' and area.spaces.active.context == 'COMPOSITOR')
    yield from redraw(area_a)
    assert not any(scene.name.startswith('.Photo Editor UI Refresh') for scene in bpy.data.scenes)
    records.clear()
    blender_raw.register()
    yield from redraw(area_a, 12)
    check_screen(window.screen)
    blender_raw.unregister()
    yield from redraw(area_a)
    assert bpy.types.SCENE_PT_compositor_effects.draw is original_draw
    print('NATIVE_UI_FULL_ADDON_REENABLE_PASS', flush=True)
    print('NATIVE_UI_LIFECYCLE_PASS', flush=True)


iterator = test()


def tick():
    try:
        next(iterator)
        return .2
    except StopIteration:
        bpy.ops.wm.quit_blender()
    except BaseException:
        traceback.print_exc()
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(tick, first_interval=.5, persistent=True)
