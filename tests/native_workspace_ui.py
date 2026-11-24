"""Verify native Photo Workspace controls in a real Blender 5.3 window.

blender --factory-startup --enable-event-simulate -p 50 50 1600 1000 --python tests/native_workspace_ui.py
"""
from pathlib import Path
import sys
import tempfile
import traceback

import bpy
import numpy as np
import OpenImageIO as oiio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import blender_raw
from blender_raw import catalog, compositor, film_node, gallery, jobs, native_effects, node_status

temporary = tempfile.TemporaryDirectory(prefix='native-workspace-ui-')
draws = {}
effect_draws = {}
menu_draws = []
status_draws = []
actions = []
draw_controls = native_effects.BR_PT_native_effect_controls.draw
draw_menu = native_effects._add_menu
status_icon = node_status.icon_id
reset_effect = native_effects.BR_OT_reset_native_effect.execute


def record_controls(self, context):
    if not native_effects._ui_panel_ready(context):
        return
    effect = self.custom_data
    node = native_effects.effect_node(effect)
    draw_controls(self, context)
    if node:
        draws[node.bl_idname] = draws.get(node.bl_idname, 0) + 1
        effect_draws[effect.name] = effect_draws.get(effect.name, 0) + 1


def record_status_icon(phase):
    result = status_icon(phase)
    status_draws.append((phase, result))
    return result


def record_reset(self, context):
    actions.append(('RESET', self.effect_name))
    return reset_effect(self, context)


def record_menu(self, context):
    menu_draws.append(context.scene.name)
    draw_menu(self, context)


native_effects.BR_PT_native_effect_controls.draw = record_controls
native_effects._add_menu = record_menu
node_status.icon_id = record_status_icon
native_effects.BR_OT_reset_native_effect.execute = record_reset


def click(window, x, y, *, settle=2):
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=round(x), y=round(y))
    yield
    window.event_simulate(type='LEFTMOUSE', value='PRESS', x=round(x), y=round(y))
    yield
    window.event_simulate(type='LEFTMOUSE', value='RELEASE', x=round(x), y=round(y))
    for _ in range(settle):
        yield


def wait_for(predicate, message):
    for _ in range(80):
        if predicate():
            return
        yield
    raise AssertionError(message)


def record(name, offset):
    photo = catalog.library(bpy.context).photos.add()
    photo.uid = name
    photo.name = name
    photo.width = 480
    photo.height = 320
    y, x = np.mgrid[0:320, 0:480]
    image = np.empty((320, 480, 4), np.float32)
    image[:, :, 0] = .05 + .5 * x / 480
    image[:, :, 1] = .03 + .3 * y / 320
    image[:, :, 2] = .1 + offset
    image[:, :, 3] = 1
    path = Path(temporary.name) / (name + '.exr')
    output = oiio.ImageOutput.create(str(path))
    output.open(str(path), oiio.ImageSpec(480, 320, 4, oiio.FLOAT))
    output.write_image(image)
    output.close()
    photo.image = bpy.data.images.load(str(path))
    photo.image.colorspace_settings.name = 'Linear Rec.709'
    photo.filepath = str(path)
    photo.source_absolute = str(path)
    photo.thumbnail_path = str(path)
    photo.status = 'Ready'
    photo['capture_info'] = {'camera': 'UI Test Camera', 'lens': '50 mm', 'iso': '100',
                             'shutter': '1/125 s', 'aperture': 'f/4'}
    return photo


def photo(name):
    return catalog.lookup_photo(None, name)


def test():
    blender_raw.register()
    (ROOT / '.native-test-output').mkdir(exist_ok=True)
    window = bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash = False
    bpy.context.preferences.edit.use_global_undo = True
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    first, second = record('Photo A', 0), record('Photo B', .15)
    initial_scene = window.scene
    initial_scene.blender_raw.owner = True
    compositor.ensure_photo_scene(bpy.context, first)
    compositor.ensure_photo_scene(bpy.context, second)
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.blender_raw.new_workspace() == {'FINISHED'}
    yield from wait_for(lambda: window.workspace.get('blender_raw_workspace')
                       and not blender_raw.gallery._timers, 'Photo Workspace layout did not finish')
    panel = next(a for a in window.screen.areas if a.type == 'PROPERTIES')
    assert panel.spaces.active.context == 'COMPOSITOR'
    assert len(window.screen.areas) == 3
    assert any(a.ui_type == blender_raw.gallery.GALLERY_TYPE for a in window.screen.areas)
    assert any(a.type == 'IMAGE_EDITOR' for a in window.screen.areas)
    region = next(r for r in panel.regions if r.type == 'WINDOW')
    yield from wait_for(lambda: window.screen.get('photo_editor_navigation_default'),
                       'Properties navigation default was not applied')
    assert next(r for r in panel.regions if r.type == 'NAVIGATION_BAR').width <= 1
    compositor.open_photo(bpy.context, photo('Photo A'))
    yield
    assert window.scene == photo('Photo A').scene
    first_scene_name = window.scene.name
    native_effects.set_input_value(window.scene.compositor_effects[1], 'Exposure', 1)
    compositor.open_photo(bpy.context, photo('Photo B'))
    yield
    assert window.scene == photo('Photo B').scene and window.scene.name != first_scene_name
    assert native_effects.get_input_value(window.scene.compositor_effects[1], 'Exposure') == 0
    compositor.open_photo(bpy.context, photo('Photo A'))
    yield
    assert native_effects.get_input_value(window.scene.compositor_effects[1], 'Exposure') == 1
    # Each native effect instance must draw its own controls, independent of
    # which effect happens to be active in the Scene or Compositor.
    for effect in list(window.scene.compositor_effects):
        effect.show_expanded = True
        panel.tag_redraw()
        yield
        yield
    film = native_effects.create_effect(window.scene, film_node.FILM_TYPE, photo=photo('Photo A'))
    film.enable_for_preview = False
    native_effects.effect_node(film).live_preview = False
    film.show_expanded = True
    window.scene.compositor_effects.active = window.scene.compositor_effects[0]
    panel.tag_redraw()
    yield
    yield
    bpy.ops.screen.screenshot(filepath=str(ROOT / '.native-test-output/inline-controls-initial.png'))
    # Native effect bodies and their Settings children have independent fold
    # states. Open real Settings disclosure buttons, bottom first so expanding
    # one does not move the controls we still need to click.
    # Reference geometry uses Windows' 125% display scale (1600 x 1000).
    scale = bpy.context.preferences.system.ui_scale / 1.25
    # The one global Compositor button sits directly below Add Scene Effect.
    # Exercise its actual hit target and verify that the current card remains
    # selected on both open and close.
    selected = window.scene.compositor_effects.active
    yield from click(window, region.x + 200 * scale,
                     panel.y + panel.height - 134 * scale)
    yield from wait_for(lambda: gallery.viewer_column(window.screen)[1]
                       and not gallery._compositor_layout_timers,
                       'Global Compositor button did not open the node pane')
    viewer, nodes = gallery.viewer_column(window.screen)
    assert nodes.y < viewer.y
    assert window.scene.compositor_effects.active == selected
    assert nodes.spaces.active.node_tree == selected.node_group
    yield from click(window, region.x + 200 * scale,
                     panel.y + panel.height - 134 * scale)
    yield from wait_for(lambda: not gallery.viewer_column(window.screen)[1]
                       and not gallery._compositor_layout_timers,
                       'Global Compositor button did not close the node pane')
    assert window.scene.compositor_effects.active == selected
    assert len(window.screen.areas) == 3
    for offset in (378, 314, 204):
        yield from click(window, region.x + 43 * scale,
                         panel.y + panel.height - offset * scale)
    bpy.ops.screen.screenshot(filepath=str(ROOT / '.native-test-output/inline-controls-open.png'))
    assert all(draws.get(kind, 0) for kind in (compositor.READER_TYPE, compositor.DEVELOPER_TYPE, film_node.FILM_TYPE)), draws
    assert status_draws and all(icon for _, icon in status_draws), status_draws
    assert all(status_icon(phase) for phase in ('IDLE', 'QUEUED', 'PROCESSING', 'READY', 'ERROR'))
    for phase in ('IDLE', 'QUEUED', 'PROCESSING', 'READY', 'ERROR'):
        pixels = np.array(node_status._icons[phase].icon_pixels_float).reshape((32, 32, 4))
        # Status icons are thin colored bars, with transparent surroundings.
        assert np.count_nonzero(pixels[:, :, 3]) == 4 * 28
        assert np.allclose(pixels[2:30, 14:18], node_status._COLORS[phase], atol=1 / 255)
    before = effect_draws.copy()
    window.scene.compositor_effects[1].show_expanded = False
    panel.tag_redraw()
    yield
    yield
    # Ignore the closing animation, then verify the collapsed card's controls
    # stop drawing while its expanded neighbors still draw.
    before = effect_draws.copy()
    panel.tag_redraw()
    yield
    yield
    assert effect_draws.get('Raw Developer', 0) == before.get('Raw Developer', 0)
    assert effect_draws.get('Raw Reader', 0) > before.get('Raw Reader', 0)
    window.scene.compositor_effects[1].show_expanded = True
    # Use the actual native action and incremental GUI Undo/Redo.
    window.scene.compositor_effects.active = window.scene.compositor_effects[1]
    with bpy.context.temp_override(window=window, area=panel, region=region):
        assert bpy.ops.ed.undo_push(message='Before native effect duplicate') == {'FINISHED'}
        assert bpy.ops.scene.duplicate_compositor_effect('EXEC_DEFAULT', True, name='Raw Developer') == {'FINISHED'}
    yield
    yield
    effects = window.scene.compositor_effects
    assert len(effects) == 4
    copies = [e for e in effects if native_effects.effect_node(e)
              and native_effects.effect_node(e).bl_idname == compositor.DEVELOPER_TYPE]
    assert len(copies) == 2 and copies[0].node_group != copies[1].node_group
    assert native_effects.effect_node(copies[0]).node_tree != native_effects.effect_node(copies[1]).node_tree
    with bpy.context.temp_override(window=window, area=panel, region=region):
        assert bpy.ops.ed.undo() == {'FINISHED'}
    yield
    assert len(window.scene.compositor_effects) == 3
    with bpy.context.temp_override(window=window, area=panel, region=region):
        assert bpy.ops.ed.redo() == {'FINISHED'}
    yield
    yield
    assert len(window.scene.compositor_effects) == 4
    copies = [e for e in window.scene.compositor_effects if native_effects.effect_node(e)
              and native_effects.effect_node(e).bl_idname == compositor.DEVELOPER_TYPE]
    assert copies[0].node_group != copies[1].node_group
    assert native_effects.effect_node(copies[0]).node_tree != native_effects.effect_node(copies[1]).node_tree
    # Card actions get a separate Undo baseline: Reset must not consume the
    # native duplicate step that the preceding assertions are intended to undo.
    native_effects.set_input_value(copies[0], 'Exposure', .5)
    native_effects.set_input_value(copies[1], 'Exposure', 1.5)
    target_name = copies[0].name
    for effect in window.scene.compositor_effects:
        effect.show_expanded = effect.name == target_name
    window.scene.compositor_effects.active = window.scene.compositor_effects[0]
    panel.tag_redraw()
    yield
    yield
    bpy.ops.screen.screenshot(filepath=str(ROOT / '.native-test-output/inline-controls-actions.png'))
    # Press the icon on the right of a non-active card's Settings header.
    # Its target must remain this card, rather than the selected Reader.
    yield from click(window, panel.x + panel.width - 50 * scale,
                     panel.y + panel.height - 282 * scale, settle=1)
    assert actions[-1:] == [('RESET', target_name)], actions
    assert native_effects.get_input_value(copies[0], 'Exposure') == 0
    assert native_effects.get_input_value(copies[1], 'Exposure') == 1.5
    catalog.set_camera_input(native_effects.effect_node(copies[0]), 0)
    jobs.stop_previews()
    native_effects.set_input_value(copies[0], 'Exposure', .75)
    window.scene.compositor_effects.active = window.scene.compositor_effects[0]
    panel.tag_redraw()
    yield
    yield
    bpy.ops.screen.screenshot(filepath=str(ROOT / '.native-test-output/inline-controls-after-reset.png'))
    # Duplicate remains Blender's native action in the card dropdown. There is
    # no duplicate control in the Python Settings body.
    with bpy.context.temp_override(window=window, area=panel, region=region):
        assert bpy.ops.scene.duplicate_compositor_effect('EXEC_DEFAULT', True, name=target_name) == {'FINISHED'}
    yield
    yield
    developers = [e for e in window.scene.compositor_effects if native_effects.effect_node(e)
                  and native_effects.effect_node(e).bl_idname == compositor.DEVELOPER_TYPE]
    assert len(developers) == 3
    assert sorted(native_effects.get_input_value(e, 'Exposure') for e in developers) == [.75, .75, 1.5]
    assert len({e.node_group.as_pointer() for e in developers}) == 3
    # Draw Blender's real add menu; the appended provider must be invoked.
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=panel.x + 80, y=panel.y + panel.height - 100)
    with bpy.context.temp_override(window=window, area=panel, region=region):
        assert bpy.ops.wm.call_menu(name='SCENE_MT_add_compositor_effect') == {'INTERFACE'}
    yield
    yield
    assert menu_draws
    bpy.ops.screen.screenshot(filepath=str(ROOT / 'docs/native-scene-menu.png'))
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    window.scene.compositor_effects.active = copies[0]
    panel.tag_redraw()
    yield
    # Explicit UI feasibility probe: the unlinked input should be gray, while
    # Exposure participates in actual compositor evaluation and remains active.
    custom = native_effects.create_effect(window.scene, native_effects.CUSTOM_TYPE)
    tree = custom.node_group
    used = tree.interface.new_socket(name='Used Exposure', in_out='INPUT', socket_type='NodeSocketFloat')
    tree.interface.new_socket(name='Unused Backend Knob', in_out='INPUT', socket_type='NodeSocketFloat')
    source = tree.nodes['Effect Input']
    output = tree.nodes['Effect Output']
    exposure = tree.nodes.new('CompositorNodeExposure')
    tree.links.new(source.outputs['Image'], exposure.inputs['Image'])
    tree.links.new(source.outputs[used.name], exposure.inputs['Exposure'])
    tree.links.new(exposure.outputs['Image'], output.inputs['Image'])
    for effect in window.scene.compositor_effects:
        effect.show_expanded = effect == custom
    panel.tag_redraw()
    yield
    yield
    bpy.ops.screen.screenshot(filepath=str(ROOT / 'docs/native-effect-input-probe.png'))
    # Exercise the timer's source reconciliation using real native enable/reorder
    # actions. The fixture already contains developed pixels, so skip RAW decoding.
    second = photo('Photo B')
    compositor.open_photo(bpy.context, second)
    reader = second.scene.compositor_effects[0]
    developer = second.scene.compositor_effects[1]
    node = native_effects.effect_node(developer)
    catalog.set_camera_input(node, 0)
    reader.enable_for_preview = False
    yield from wait_for(lambda: node.photo_uid == '', 'Disabling Reader did not clear Developer source')
    reader.enable_for_preview = True
    yield from wait_for(lambda: node.photo_uid == 'Photo B', 'Enabling Reader did not restore Developer source')
    with bpy.context.temp_override(window=window, area=panel, region=region):
        assert bpy.ops.scene.move_compositor_effect_to_index(name=reader.name, index=1) == {'FINISHED'}
    yield from wait_for(lambda: node.photo_uid == '', 'Moving Reader below Developer did not clear source')
    with bpy.context.temp_override(window=window, area=panel, region=region):
        assert bpy.ops.scene.move_compositor_effect_to_index(name=reader.name, index=0) == {'FINISHED'}
    yield from wait_for(lambda: node.photo_uid == 'Photo B', 'Restoring Reader order did not restore source')
    # Removing an active photo also removes its Scene. Incremental Undo must
    # restore the record, native effects and private node groups together.
    removed_scene_name = second.scene.name
    gallery_area = next(a for a in window.screen.areas if a.ui_type == blender_raw.gallery.GALLERY_TYPE)
    gallery_region = next(r for r in gallery_area.regions if r.type == 'WINDOW')
    with bpy.context.temp_override(window=window, area=gallery_area, region=gallery_region):
        assert bpy.ops.ed.undo_push(message='Before removing photo scene') == {'FINISHED'}
        assert bpy.ops.blender_raw.remove_photo('EXEC_DEFAULT', True, uid='Photo B') == {'FINISHED'}
    yield
    yield
    assert photo('Photo B') is None
    assert removed_scene_name not in bpy.data.scenes
    assert window.scene == photo('Photo A').scene
    with bpy.context.temp_override(window=window, area=gallery_area, region=gallery_region):
        assert bpy.ops.ed.undo() == {'FINISHED'}
    yield
    yield
    restored = photo('Photo B')
    assert restored and restored.scene and restored.scene.name == removed_scene_name
    assert len(restored.scene.compositor_effects) == 2
    restored_reader = native_effects.effect_node(restored.scene.compositor_effects[0])
    restored_developer = native_effects.effect_node(restored.scene.compositor_effects[1])
    assert restored_reader.photo_uid == restored.uid
    assert restored_reader.node_tree.nodes['Decoded Image'].image == restored.image
    assert restored_developer.node_tree and compositor.developer_source(restored_developer) == restored
    with bpy.context.temp_override(window=window, area=gallery_area, region=gallery_region):
        assert bpy.ops.ed.redo() == {'FINISHED'}
    yield
    yield
    assert photo('Photo B') is None
    assert removed_scene_name not in bpy.data.scenes
    assert window.scene == photo('Photo A').scene
    # Finish with the actual two processing cards visible together. Drop only
    # the test copies after all Undo assertions, preserving the tested owner.
    effects = window.scene.compositor_effects
    for effect in list(effects):
        node = native_effects.effect_node(effect)
        if not node or (node.bl_idname == compositor.DEVELOPER_TYPE and effect.name != 'Raw Developer'):
            effects.remove(effect)
    reader, developer, film = effects
    native_effects.set_input_value(developer, 'Exposure', 0)
    catalog.set_camera_input(native_effects.effect_node(developer), 0)
    jobs.stop_previews()
    # Reset briefly selects LibRaw. Our source is an already-decoded EXR, so
    # remove that fixture-only decode diagnostic after returning to passthrough.
    photo('Photo A').error = ''
    native_effects.effect_node(developer).error = ''
    reader.show_expanded = False
    developer.show_expanded = film.show_expanded = True
    effects.active = developer
    viewer = next(a for a in window.screen.areas if a.type == 'IMAGE_EDITOR')
    viewer.spaces.active.show_region_ui = False
    panel.tag_redraw()
    yield
    yield
    with bpy.context.temp_override(window=window, area=panel, region=region):
        bpy.ops.view2d.zoom_out()
        bpy.ops.view2d.zoom_out()
    yield
    yield
    assert compositor.developer_status(native_effects.effect_node(developer)) == ('READY', 'Preview ready')
    bpy.ops.screen.screenshot(filepath=str(ROOT / 'docs/native-scene-ui.png'))
    print('NATIVE_WORKSPACE_UI_PASS: independent inline controls and folds, real non-active-card Reset, global Compositor clicks, status bar icons, native duplicate Undo/Redo and add menu', flush=True)
    print('NATIVE_STACK_SOURCE_SYNC_PASS: native preview enable and reorder refresh Developer source', flush=True)
    print('NATIVE_PHOTO_REMOVE_UNDO_PASS: active photo Scene removal, next-photo switching, Undo restoration and Redo removal', flush=True)


steps = test()


def tick():
    try:
        next(steps)
        return .3
    except StopIteration:
        jobs.stop_previews()
        temporary.cleanup()
        bpy.ops.wm.quit_blender()
    except BaseException:
        traceback.print_exc()
        jobs.stop_previews()
        sys.stdout.flush()
        sys.stderr.flush()
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(tick, first_interval=1.0)
