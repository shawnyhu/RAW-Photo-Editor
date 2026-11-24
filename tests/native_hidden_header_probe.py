"""Diagnostic for headerless children of native Scene Effect cards.

Run in a disposable real Blender window (not background mode):
    blender --factory-startup --enable-event-simulate -p 50 50 1200 850 \
        --python tests/native_hidden_header_probe.py -- --seconds 5

This script changes only its test process. It never changes add-on source.
Output defaults to .native-test-output/hidden-header-probe under the repository;
--output-dir selects a different directory. MEASURE lines and measurements.json
record idle layout calls and process CPU time after a settling period. No
tag_redraw is issued during measurement. CPU includes other Blender activity,
so compare it with the same process's NORMAL_COLLAPSED baseline.

A sustained rate of >=10 main layouts/second, >=5/second above baseline and
>3 times baseline is reported as CONTINUOUS_LAYOUT_OBSERVED. This is a diagnostic
heuristic, not a performance acceptance threshold. A future Blender fix should
change the result without failing this script. The normal header's body starts
collapsed; count main layouts to compare it with the always-visible hidden body.

HIDDEN_CHILD_SAFE_UNREGISTER_PASS only means that the add-on's public-API panel
cleanup completed and Blender processed further UI events without crashing.
It does not mean that headerless cards are idle or suitable for production.
Blender 5.3 commit 02f0899d78e3 continuously redraws HIDE_HEADER children whose
native child expansion bit remains zero. Do not unregister these panels directly:
native_effects must clear native panel instances/animation handlers first.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import traceback

import bpy

if bpy.app.background:
    raise RuntimeError('native_hidden_header_probe requires a real Blender window; remove --background/-b')

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import blender_raw
from blender_raw import compositor, native_effects

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--seconds', type=float, default=5.0, help='Idle measurement duration per phase')
parser.add_argument('--settle-seconds', type=float, default=2.0, help='Wait after a UI change')
parser.add_argument('--output-dir', type=Path, default=ROOT / '.native-test-output/hidden-header-probe')
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
if args.seconds < 1 or args.settle_seconds < .5:
    parser.error('--seconds must be >=1 and --settle-seconds must be >=0.5')
output_dir = args.output_dir.resolve()
output_dir.mkdir(parents=True, exist_ok=True)

counts = {'main': 0, 'child': 0}
instances = []
measurements = []
addon_registered = False
recorder_registered = False
panel_class = native_effects.BR_PT_native_effect_controls
original_draw = panel_class.draw
original_header = panel_class.__dict__.get('draw_header_preset')


def record_main(_self, _context):
    counts['main'] += 1


def record_child(self, context):
    # An existing C++ panel can initially lack child custom_data. Reading it
    # before native_effects rebuilds the region can crash in Blender's getter.
    if not native_effects._ui_panel_ready(context):
        return
    counts['child'] += 1
    instances[:] = [self]
    original_draw(self, context)


def record_header(self, context):
    if not native_effects._ui_panel_ready(context):
        return
    instances[:] = [self]
    if original_header:
        original_header(self, context)


panel_class.draw = record_child
panel_class.draw_header_preset = record_header


def idle(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        yield


def measure(label):
    started = time.monotonic()
    cpu_started = time.process_time()
    before = counts.copy()
    yield from idle(args.seconds)
    elapsed = time.monotonic() - started
    cpu_seconds = time.process_time() - cpu_started
    row = {
        'phase': label,
        'wall_seconds': elapsed,
        'cpu_seconds': cpu_seconds,
        'cpu_percent_one_core': 100 * cpu_seconds / elapsed,
        'main_draws': counts['main'] - before['main'],
        'child_draws': counts['child'] - before['child'],
    }
    row['main_draws_per_second'] = row['main_draws'] / elapsed
    measurements.append(row)
    print('MEASURE', json.dumps(row, sort_keys=True), flush=True)


def cleanup():
    global addon_registered, recorder_registered
    if addon_registered:
        # Production unregister starts with native_effects, which uses an
        # empty native template and DRAW_WIN_SWAP while context is valid.
        # This cancels animation handlers before freeing instanced panels.
        blender_raw.unregister()
        addon_registered = False
    if recorder_registered:
        bpy.types.SCENE_PT_compositor_effects.remove(record_main)
        recorder_registered = False
    instances.clear()
    panel_class.draw = original_draw
    if original_header is not None:
        panel_class.draw_header_preset = original_header
    elif 'draw_header_preset' in panel_class.__dict__:
        del panel_class.draw_header_preset


def run():
    global addon_registered, recorder_registered
    assert not bpy.app.background, 'This diagnostic requires a real window'
    assert not hasattr(bpy.types.Scene, 'blender_raw'), 'Start with --factory-startup'
    window = bpy.context.window_manager.windows[0]
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    blender_raw.register()
    addon_registered = True
    bpy.types.SCENE_PT_compositor_effects.append(record_main)
    recorder_registered = True
    print('BLENDER_BUILD', bpy.app.version_string, bpy.app.build_hash.decode(), flush=True)
    print('PUBLIC_PANEL_OPS', [
        f'{space}.{name}' for space in ('ui', 'screen', 'wm')
        for name in dir(getattr(bpy.ops, space))
        if any(word in name for word in ('panel', 'collapse', 'expand'))
    ], flush=True)
    window.scene[compositor.STUDIO_TAG] = 'hidden-child-probe'
    first = native_effects.create_effect(window.scene, native_effects.CUSTOM_TYPE)
    first.name = 'Alpha'
    second = native_effects.create_effect(window.scene, native_effects.CUSTOM_TYPE)
    second.name = 'Beta'
    area = max(window.screen.areas, key=lambda item: item.width * item.height)
    area.type = 'PROPERTIES'
    area.spaces.active.context = 'COMPOSITOR'
    yield from idle(args.settle_seconds)
    assert instances, 'Native child header did not draw after region initialization'
    yield from measure('NORMAL_COLLAPSED')

    # Panel.bl_options on this live RNA instance changes the shared Python
    # PanelType options. It does not patch C++ memory or replace its parent.
    instances[0].bl_options = {'HIDE_HEADER'}
    area.tag_redraw()
    yield from idle(args.settle_seconds)
    yield from measure('HIDE_HEADER_1')
    yield from measure('HIDE_HEADER_2')

    instances[0].bl_options = {'HIDE_HEADER', 'INSTANCED'}
    area.tag_redraw()
    yield from idle(args.settle_seconds)
    yield from measure('HIDE_HEADER_INSTANCED')
    bpy.ops.screen.screenshot(filepath=str(output_dir / 'hidden-child.png'))

    first.show_expanded = False
    yield from idle(args.settle_seconds)
    yield from measure('HIDE_HEADER_ALPHA_COLLAPSED')
    first.show_expanded = True
    yield from idle(args.settle_seconds)
    yield from measure('HIDE_HEADER_REOPENED')

    baseline = measurements[0]['main_draws_per_second']
    continuous = [row['phase'] for row in measurements[1:]
                  if row['main_draws_per_second'] >= 10
                  and row['main_draws_per_second'] >= baseline + 5
                  and row['main_draws_per_second'] > baseline * 3]
    result = {
        'blender_version': bpy.app.version_string,
        'build_hash': bpy.app.build_hash.decode(),
        'measurements': measurements,
        'continuous_layout_phases': continuous,
    }
    (output_dir / 'measurements.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('CONTINUOUS_LAYOUT_OBSERVED' if continuous else 'NO_CONTINUOUS_LAYOUT_OBSERVED',
          continuous, flush=True)
    cleanup()
    yield from idle(args.settle_seconds)
    assert not native_effects._registered
    assert native_effects._ui_reset_scene_get() is None
    assert not any(scene.get(native_effects._UI_RESET_TAG) for scene in bpy.data.scenes)
    print('HIDDEN_CHILD_SAFE_UNREGISTER_PASS', flush=True)
    print('DIAGNOSTIC_OUTPUT', output_dir, flush=True)


iterator = run()


def tick():
    try:
        next(iterator)
        return .1
    except StopIteration:
        bpy.ops.wm.quit_blender()
    except BaseException:
        traceback.print_exc()
        try:
            cleanup()
        except BaseException:
            traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
    return None


bpy.app.timers.register(tick, first_interval=.5)
