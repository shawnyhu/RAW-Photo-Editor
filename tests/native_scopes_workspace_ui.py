"""Verify the native Scopes preset and preserved user edits in a real window.

blender --factory-startup --enable-event-simulate -p 50 50 1600 1400 -P tests/native_scopes_workspace_ui.py
"""
from pathlib import Path
import sys
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import blender_raw
from blender_raw import catalog, compositor, gallery, jobs

draws = set()
callbacks = []


def observe(panel_name):
    def draw(self, context):
        if context.workspace.get('blender_raw_workspace'):
            draws.add(panel_name)
    return draw


def wait_for(predicate, message):
    for _ in range(100):
        if predicate():
            return
        yield
    raise AssertionError(message)


def click(window, x, y):
    for kind, value in (('MOUSEMOVE', 'NOTHING'), ('LEFTMOUSE', 'PRESS'), ('LEFTMOUSE', 'RELEASE')):
        window.event_simulate(type=kind, value=value, x=round(x), y=round(y))
        yield
    yield
    yield


def steady_draw(viewer):
    yield
    yield
    draws.clear()
    viewer.tag_redraw()
    yield
    yield


def test():
    assert not bpy.app.background
    blender_raw.register()
    window = bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    initial_workspace = window.workspace
    for name in ('view_histogram', 'view_waveform', 'view_vectorscope', 'sample_line', 'scope_sample'):
        cls = getattr(bpy.types, 'IMAGE_PT_' + name)
        draw = observe(name)
        cls.append(draw)
        callbacks.append((cls, draw))
    window.scene.blender_raw.owner = True
    photo = catalog.library(bpy.context).photos.add()
    photo.uid = photo.name = 'Scopes preset photo'
    catalog.set_camera_input(photo, 0)
    photo.image = bpy.data.images.new('Scopes test image', width=64, height=64, float_buffer=True)
    photo.image.pixels = [component for y in range(64) for x in range(64)
                          for component in (x / 64, y / 64, .2, 1)]
    compositor.ensure_photo_scene(bpy.context, photo)
    before = {kind: {item.as_pointer() for item in getattr(bpy.data, kind)}
              for kind in ('scenes', 'objects', 'images')}
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.blender_raw.new_workspace() == {'FINISHED'}
    yield from wait_for(lambda: window.workspace.get('blender_raw_workspace') and not gallery._timers,
                       'Workspace template did not finish loading')
    yield from wait_for(lambda: not any(scene.get('photo_editor_ui_refresh') for scene in bpy.data.scenes),
                       'Native effect panel preparation did not finish')
    for kind, pointers in before.items():
        added = [item for item in getattr(bpy.data, kind) if item.as_pointer() not in pointers]
        # Preview's built-in Viewer image may be created while activating the photo.
        assert not [item for item in added if kind != 'images' or item.type != 'COMPOSITING'], (kind, added)
    workspace = window.workspace
    viewer, nodes = gallery.viewer_column(window.screen)
    assert viewer and not nodes
    compositor.open_photo(bpy.context, photo)
    sidebar = next(r for r in viewer.regions if r.type == 'UI')
    yield from wait_for(lambda: sidebar.active_panel_category == 'Scopes', 'Scopes tab was not selected')
    assert viewer.spaces.active.show_region_ui and sidebar.width > 1
    yield from steady_draw(viewer)
    assert draws == {'view_histogram', 'view_waveform', 'view_vectorscope'}, draws
    bpy.ops.screen.screenshot(filepath=str(ROOT / 'docs/native-scopes-ui.png'))
    print('PASS Scopes visible; only the top three native panels expanded; template has no data dependencies', flush=True)

    # Exercise a real native fold, then preserve it through workspace changes.
    yield from click(window, sidebar.x + 25, sidebar.y + sidebar.height - 18)
    yield from steady_draw(viewer)
    assert 'view_histogram' not in draws and 'view_waveform' in draws, draws
    window.workspace = initial_workspace
    yield
    window.workspace = workspace
    yield
    yield from steady_draw(viewer)
    assert 'view_histogram' not in draws, 'Workspace activation reset the user fold'
    panel = next(a for a in window.screen.areas if a.type == 'PROPERTIES')
    window.screen['photo_editor_node_height'] = .72
    for _ in range(2):
        with bpy.context.temp_override(window=window, area=panel):
            assert bpy.ops.blender_raw.toggle_compositor() == {'FINISHED'}
        yield from wait_for(lambda: gallery.viewer_column(window.screen)[1] and not gallery._compositor_layout_timers,
                           'Tall Compositor did not open')
        viewer, nodes = gallery.viewer_column(window.screen)
        assert nodes.y < viewer.y
        with bpy.context.temp_override(window=window, area=panel):
            assert bpy.ops.blender_raw.toggle_compositor() == {'FINISHED'}
        yield from wait_for(lambda: not gallery.viewer_column(window.screen)[1] and not gallery._compositor_layout_timers,
                           'Compositor did not close')
    viewer, _ = gallery.viewer_column(window.screen)
    yield from steady_draw(viewer)
    assert 'view_histogram' not in draws, 'Compositor split reset the native panel fold'
    print('NATIVE_SCOPES_WORKSPACE_UI_PASS: default preset; user folds survive workspace changes and Compositor toggles', flush=True)


steps = test()


def tick():
    try:
        next(steps)
        return .3
    except StopIteration:
        pass
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
    else:
        return .3
    for cls, draw in callbacks:
        cls.remove(draw)
    jobs.stop_previews()
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(tick, first_interval=1)
