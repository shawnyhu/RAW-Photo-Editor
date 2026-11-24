"""Build the native Photo Workspace seed in a real Blender window.

The saved Workspace supplies native Scopes panel folds and graph heights that
Blender 5.3 does not expose through RNA. It contains one Image Editor; the add-on
splits it using the user's window dimensions and thumbnail preferences.

Build with factory 125% UI at the reference window size:
blender --factory-startup --enable-event-simulate -p 50 50 1400 1400 -P tools/build_workspace_template.py
"""
from pathlib import Path
import sys
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / '.native-test-output'


def click(window, x, y):
    for kind, value in (('MOUSEMOVE', 'NOTHING'), ('LEFTMOUSE', 'PRESS'), ('LEFTMOUSE', 'RELEASE')):
        window.event_simulate(type=kind, value=value, x=round(x), y=round(y))
        yield


def drag(window, x, y, delta):
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=round(x), y=round(y))
    yield
    window.event_simulate(type='LEFTMOUSE', value='PRESS', x=round(x), y=round(y))
    yield
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=round(x), y=round(y + delta))
    yield
    window.event_simulate(type='LEFTMOUSE', value='RELEASE', x=round(x), y=round(y + delta))
    yield
    yield


def build():
    assert not bpy.app.background, 'Native panel layout requires a real window'
    OUTPUT.mkdir(exist_ok=True)
    window = bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    protected = max(window.screen.areas, key=lambda area: area.width * area.height)
    while len(window.screen.areas) > 1:
        target = min((a for a in window.screen.areas if a != protected), key=lambda a: a.width * a.height)
        with bpy.context.temp_override(window=window, area=target):
            assert bpy.ops.screen.area_close() == {'FINISHED'}
        yield
    area = window.screen.areas[0]
    area.type = 'IMAGE_EDITOR'
    area.spaces.active.mode = 'VIEW'
    area.spaces.active.show_region_ui = True
    image = bpy.data.images.new('Scopes template preview', width=64, height=64)
    area.spaces.active.image = image
    yield
    yield
    sidebar = next(r for r in area.regions if r.type == 'UI')
    sidebar.active_panel_category = 'Scopes'
    yield
    yield
    print('TEMPLATE_GEOMETRY', (area.x, area.y, area.width, area.height),
          (sidebar.x, sidebar.y, sidebar.width, sidebar.height),
          'SCALE', bpy.context.preferences.system.ui_scale, flush=True)
    bpy.ops.screen.screenshot(filepath=str(OUTPUT / 'scopes-template-initial.png'))
    # The factory 125% UI has 284 px wide graphs with 100 px default heights.
    # Resize using the native grips, from bottom to top so earlier controls do
    # not move. These are build-time gestures, never simulated on user windows.
    assert abs(bpy.context.preferences.system.ui_scale - 1.25) < .01
    assert sidebar.width == 350
    top = sidebar.y + sidebar.height
    yield from click(window, sidebar.x + 25, top - 590)  # Sample Line
    for offset in (529, 337, 146):
        yield from drag(window, sidebar.x + 162, top - offset, -184)
    bpy.ops.screen.screenshot(filepath=str(OUTPUT / 'scopes-template-final.png'))
    area.spaces.active.image = None
    bpy.data.images.remove(image)
    # Remove inactive startup editors so the asset has no camera/object/scene
    # dependencies. Only this Workspace is appended from the saved asset.
    with bpy.context.temp_override(window=window, area=area):
        bpy.ops.screen.spacedata_cleanup()
    workspace = window.workspace
    workspace.name = 'Photo Editor Scopes'
    workspace.use_pin_scene = False
    workspace.sequencer_scene = None
    path = ROOT / 'blender_raw' / 'assets' / 'photo_workspace.blend'
    path.parent.mkdir(exist_ok=True)
    # libraries.write() copies Workspace/Screen before writing, which resets
    # Scopes heights in image_duplicate(). A normal save preserves the preset.
    bpy.data.batch_remove(ids=tuple(w for w in bpy.data.workspaces if w != workspace))
    bpy.data.batch_remove(ids=tuple(bpy.data.objects))
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(path), compress=True)
    print('WORKSPACE_TEMPLATE_BUILT', path, flush=True)


steps = build()


def tick():
    try:
        next(steps)
        return .3
    except StopIteration:
        bpy.ops.wm.quit_blender()
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(tick, first_interval=1)
