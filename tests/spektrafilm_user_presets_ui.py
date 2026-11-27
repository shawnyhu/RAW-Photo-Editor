# SPDX-License-Identifier: GPL-3.0-or-later
"""Click the Lite preset popover on a non-active native effect card in a real window.

blender --factory-startup --enable-event-simulate -p 50 50 1600 1000 \
  --python-exit-code 1 --python tests/spektrafilm_user_presets_ui.py
"""
from importlib import import_module
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

import bpy

if '--installed' in sys.argv:
    from bl_ext.user_default import raw_photo_editor as addon
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import blender_raw as addon

ui = import_module(addon.__name__ + '.film_user_presets')
draws = []
deadline = time.monotonic() + 40


def inspect_popover(self, context):
    node = getattr(context, 'spektrafilm_preset_node', None)
    draws.append((node and node.as_pointer(), str(self.layout.introspect())))


def click(window, x, y):
    for kind, value in (('MOUSEMOVE', 'NOTHING'), ('LEFTMOUSE', 'PRESS'), ('LEFTMOUSE', 'RELEASE')):
        window.event_simulate(type=kind, value=value, x=round(x), y=round(y))
        yield
    yield


def probe_screenshot(label):
    if output := os.environ.get('PHOTO_UI_TEST_SCREENSHOTS'):
        bpy.ops.screen.screenshot(filepath=str(Path(output) / (label + '.png')))


def test():
    if '--installed' not in sys.argv:
        addon.register()
    window = bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.blender_raw.new_workspace() == {'FINISHED'}
    while not window.screen.get('photo_editor_navigation_default') or addon.gallery._timers:
        yield
    panel = next(area for area in window.screen.areas if area.type == 'PROPERTIES')
    region = next(region for region in panel.regions if region.type == 'WINDOW')
    first, second = [addon.native_effects.create_effect(window.scene, addon.film_node.FILM_TYPE)
                     for _ in range(2)]
    target, active = (addon.native_effects.effect_node(effect) for effect in (first, second))
    first.show_expanded, second.show_expanded = True, False
    window.scene.compositor_effects.active = second
    active.grainAmount = .345
    preset_name = 'Night portrait'
    with tempfile.TemporaryDirectory() as temp, patch.object(ui.film_user_library, '_directory',
                                                             return_value=Path(temp)):
        preset_id = ui.film_user_library.save(preset_name, ui.film_user_settings.snapshot(active))
        active.grainAmount = .765
        assert abs(target.grainAmount - .345) > .01
        ui.BR_PT_spektrafilm_presets.append(inspect_popover)
        panel.tag_redraw()
        yield
        yield
        scale = bpy.context.preferences.system.ui_scale / 1.25
        top = panel.y + panel.height
        # Values below use the factory 5.3 compositor Properties panel at 125%.
        x = region.x + 123 * scale
        yield from click(window, region.x + 43 * scale, top - 172 * scale)
        yield
        probe_screenshot('before-preset')
        yield from click(window, x, top - 200 * scale)
        probe_screenshot('after-preset')
        assert draws, 'Click did not open the Spektrafilm preset popover'
        pointer, layout = draws[-1]
        assert pointer == target.as_pointer(), (pointer, target.as_pointer(), layout)
        for action in ('save', 'apply', 'rename', 'delete'):
            assert f'blender_raw.{action}_film_preset' in layout, (action, layout)
        assert preset_name in layout and preset_id in layout, layout
        assert 'Built-in Presets' not in layout
        assert window.scene.compositor_effects.active == second
        yield from click(window, x, top - 301 * scale)
        assert abs(target.grainAmount - .345) < 1e-5, target.grainAmount
        assert abs(active.grainAmount - .765) < 1e-5, active.grainAmount
        ui.BR_PT_spektrafilm_presets.remove(inspect_popover)
    print('PASS Lite preset popover: four actions, named preset applied by click to the non-active card', flush=True)


steps = test()


def tick():
    try:
        assert time.monotonic() < deadline, 'Timed out waiting for the Photo Workspace'
        next(steps)
        return .2
    except StopIteration:
        bpy.ops.wm.quit_blender()
    except BaseException:
        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    return None


bpy.app.timers.register(tick, first_interval=1)
