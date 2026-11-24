"""Draw the Lite standalone modes in an isolated Blender compositor window.

blender --factory-startup --offline-mode --enable-event-simulate -p 40 40 1500 950 --python tests/spektrafilm_reversal_ui.py
"""
import json
from pathlib import Path
import sys
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import blender_raw
from blender_raw import film_node
from blender_raw.film_effects import PROCESS_MODES

OUTPUT = ROOT / '.native-test-output' / 'lite-reversal-ui'
OUTPUT.mkdir(parents=True, exist_ok=True)
drawn = {}
original = film_node.BR_NODE_spektrafilm._draw_stocks


def record(self, layout):
    names = []

    class Probe:
        def __getattr__(self, name):
            return getattr(layout, name)

        def prop(self, node, name, **kwargs):
            names.append(name)
            return layout.prop(node, name, **kwargs)

    original(self, Probe())
    drawn[self.process] = names


film_node.BR_NODE_spektrafilm._draw_stocks = record


def test():
    blender_raw.register()
    window = bpy.context.window_manager.windows[0]
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    area = max(window.screen.areas, key=lambda a: a.width * a.height)
    area.ui_type = 'CompositorNodeTree'
    area.spaces.active.show_region_ui = False
    area.spaces.active.show_region_asset_shelf = False
    graph = bpy.data.node_groups.new('Lite film modes', 'CompositorNodeTree')
    for index, mode in enumerate(PROCESS_MODES):
        node = graph.nodes.new(film_node.FILM_TYPE)
        node.film = 'fujifilm_velvia_100'
        node.process = mode
        node.show_dir = index == 1
        node.location = (index * 340, 0)
        node.width = 300
    area.spaces.active.pin = True
    area.spaces.active.path.start(graph)
    region = next(r for r in area.regions if r.type == 'WINDOW')
    for _ in range(8):
        area.tag_redraw()
        yield
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.node.view_all()
    for _ in range(8):
        area.tag_redraw()
        yield
    assert drawn == {
        PROCESS_MODES[0]: ['process', 'film', 'paper'],
        PROCESS_MODES[1]: ['process', 'film', 'scanNegativeInvert'],
        PROCESS_MODES[2]: ['process', 'paper'],
    }, drawn
    bpy.ops.screen.screenshot(filepath=str(OUTPUT / 'lite-modes.png'))
    (OUTPUT / 'result.json').write_text(json.dumps({'passed': True, 'drawn': drawn}, indent=2))
    print('PASS Lite modes draw with standalone labels and mode-specific controls', flush=True)


iterator = test()


def tick():
    try:
        next(iterator)
        return .15
    except StopIteration:
        bpy.ops.wm.quit_blender()
    except BaseException:
        error = traceback.format_exc()
        print(error, flush=True)
        (OUTPUT / 'result.json').write_text(json.dumps({'passed': False, 'error': error}))
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(tick, first_interval=.5, persistent=True)
