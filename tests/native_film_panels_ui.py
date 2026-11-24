"""Exercise native Spektrafilm sections and the node-canvas fallback in Blender.

blender --factory-startup --enable-event-simulate -p 50 50 1600 1000 --python tests/native_film_panels_ui.py
"""
from pathlib import Path
import sys
import tempfile
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import blender_raw
from blender_raw import film_node, native_effects

draws = []
errors = []
draw_effects = film_node.BR_NODE_spektrafilm._draw_effects


def record_effects(self, layout, *, native_panels=False):
    try:
        draw_effects(self, layout, native_panels=native_panels)
    except Exception:
        errors.append(traceback.format_exc())
        raise
    draws.append((self.instance_uid, native_panels,
                  tuple(getattr(self, 'show_' + key) for key, *_ in film_node.FLOW_SECTIONS)))


film_node.BR_NODE_spektrafilm._draw_effects = record_effects


def wait_for(predicate, message):
    for _ in range(80):
        assert not errors, errors
        if predicate():
            return
        yield
    raise AssertionError(message)


def click(window, x, y):
    window.event_simulate(type='MOUSEMOVE', value='NOTHING', x=round(x), y=round(y))
    yield
    window.event_simulate(type='LEFTMOUSE', value='PRESS', x=round(x), y=round(y))
    yield
    window.event_simulate(type='LEFTMOUSE', value='RELEASE', x=round(x), y=round(y))
    yield
    yield


def test():
    blender_raw.register()
    output = ROOT / '.native-test-output'
    output.mkdir(exist_ok=True)
    window = bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    yield
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.blender_raw.new_workspace() == {'FINISHED'}
    yield from wait_for(lambda: window.workspace.get('blender_raw_workspace')
                       and not blender_raw.gallery._timers
                       and window.screen.get('photo_editor_navigation_default'),
                       'Photo Workspace did not finish')
    panel = next(a for a in window.screen.areas if a.type == 'PROPERTIES')
    region = next(r for r in panel.regions if r.type == 'WINDOW')
    scene = window.scene
    asset = Path(bpy.utils.resource_path('LOCAL')) / 'datafiles/assets/nodes/compositing_nodes_essentials.blend'
    with bpy.data.libraries.load(str(asset)) as (source, target):
        assert 'Bloom' in source.node_groups
        target.node_groups = ['Bloom']
    bloom = scene.compositor_effects.new('Bloom')
    bloom.node_group = target.node_groups[0]
    bloom.show_expanded = False
    first = native_effects.create_effect(scene, film_node.FILM_TYPE)
    first.name = 'Film A'
    second = native_effects.create_effect(scene, film_node.FILM_TYPE)
    second.name = 'Film B'
    a, b = native_effects.effect_node(first), native_effects.effect_node(second)
    first.show_expanded = second.show_expanded = True
    scene.compositor_effects.active = bloom
    panel.tag_redraw()
    yield
    yield
    bpy.ops.screen.screenshot(filepath=str(output / 'film-panels-initial.png'))
    scale = bpy.context.preferences.system.ui_scale / 1.25
    # Settings is the native effect's own child panel; open both instances from
    # bottom to top before testing the nested section panels.
    if not any(uid == a.instance_uid and native for uid, native, _ in draws):
        for offset in (264, 200):
            yield from click(window, region.x + 43 * scale,
                             panel.y + panel.height - offset * scale)
    yield from wait_for(lambda: all(any(uid == node.instance_uid and native for uid, native, _ in draws)
                                   for node in (a, b)), 'Both native Film controls must draw')
    assert not errors, errors
    bpy.ops.screen.screenshot(filepath=str(output / 'film-panels-open.png'))
    recipe_a, recipe_b = film_node.recipe(a), film_node.recipe(b)
    # Use the actual native disclosure buttons. The second Film must retain
    # its own state while the first card opens and closes above it.
    yield from click(window, region.x + 43 * scale,
                     panel.y + panel.height - 841 * scale)
    assert b.show_grain and not a.show_grain
    yield from click(window, region.x + 43 * scale,
                     panel.y + panel.height - 396 * scale)
    assert a.show_grain and b.show_grain
    yield from click(window, region.x + 43 * scale,
                     panel.y + panel.height - 396 * scale)
    assert not a.show_grain and b.show_grain
    yield from click(window, region.x + 43 * scale,
                     panel.y + panel.height - 841 * scale)
    assert not a.show_grain and not b.show_grain
    assert film_node.recipe(a) == recipe_a and film_node.recipe(b) == recipe_b
    yield from click(window, region.x + 63 * scale,
                     panel.y + panel.height - 396 * scale)
    assert a.grain and not b.grain
    assert not a.show_grain and not b.show_grain
    recipe_a = film_node.recipe(a)
    for key, _label, _enabled, _properties in film_node.FLOW_SECTIONS:
        setattr(a, 'show_' + key, True)
    panel.tag_redraw()
    yield
    yield
    assert all(getattr(a, 'show_' + key) for key, *_ in film_node.FLOW_SECTIONS)
    assert not any(getattr(b, 'show_' + key) for key, *_ in film_node.FLOW_SECTIONS)
    assert film_node.recipe(a) == recipe_a and film_node.recipe(b) == recipe_b
    a.grain = True
    a.grainAmount = .37
    a.halationStrength = (.01, .02, .03)
    b.scannerEnabled = True
    b.show_scanner = True
    assert not b.grain and not a.scannerEnabled
    assert b.grainAmount != a.grainAmount
    # Section state and settings belong to the node and survive .blend storage.
    with tempfile.TemporaryDirectory(prefix='native-film-panels-') as directory:
        path = Path(directory) / 'sections.blend'
        names = (first.node_group.name, second.node_group.name)
        bpy.data.libraries.write(str(path), {first.node_group, second.node_group})
        with bpy.data.libraries.load(str(path)) as (source, target):
            target.node_groups = list(names)
        restored = [next(n for n in tree.nodes if n.bl_idname == film_node.FILM_TYPE)
                    for tree in target.node_groups]
        assert [film_node.recipe(n) for n in restored] == [film_node.recipe(a), film_node.recipe(b)]
        assert [tuple(getattr(n, 'show_' + key) for key, *_ in film_node.FLOW_SECTIONS) for n in restored] == [
            (True, True, True, True), (False, False, False, True)]
    # Node drawing runs through a canvas-safe fallback, including all expanded
    # controls, rather than calling region-wide panels from an inline node.
    viewer = next(area for area in window.screen.areas if area.type == 'IMAGE_EDITOR')
    viewer.type = 'NODE_EDITOR'
    viewer.ui_type = 'CompositorNodeTree'
    viewer.spaces.active.pin = True
    viewer.spaces.active.node_tree = first.node_group
    viewer.spaces.active.show_region_ui = False
    viewer.spaces.active.show_region_asset_shelf = False
    a.width = 290
    viewer.tag_redraw()
    yield from wait_for(lambda: any(uid == a.instance_uid and not native for uid, native, _ in draws),
                       'Compositor inline Film controls did not draw')
    assert not errors, errors
    for key, *_ in film_node.FLOW_SECTIONS:
        setattr(a, 'show_' + key, key == 'grain')
    for node in first.node_group.nodes:
        node.select = node == a
    first.node_group.nodes.active = a
    viewer.tag_redraw()
    yield
    yield
    with bpy.context.temp_override(window=window, area=viewer,
                                   region=next(r for r in viewer.regions if r.type == 'WINDOW')):
        assert bpy.ops.node.view_selected() == {'FINISHED'}
    yield
    yield
    bpy.ops.screen.screenshot(filepath=str(output / 'film-panels-node-fallback.png'))
    # Compact comparison: native Bloom and Film section disclosures in one area.
    for key, *_ in film_node.FLOW_SECTIONS:
        setattr(a, 'show_' + key, False)
    second.show_expanded = False
    bloom.show_expanded = True
    panel.tag_redraw()
    yield
    yield
    bpy.ops.screen.screenshot(filepath=str(output / 'film-panels-bloom.png'))
    print('NATIVE_FILM_PANELS_UI_PASS: real native disclosure/enable clicks, independent Film state and recipes, persisted folds, Bloom-aligned sections, safe Compositor canvas fallback', flush=True)


steps = test()


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
