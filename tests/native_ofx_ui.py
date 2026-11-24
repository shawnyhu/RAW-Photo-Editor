"""Draw both film nodes and every OFX settings section in an isolated GUI.

blender --factory-startup --offline-mode --enable-event-simulate -p 40 40 980 1100 --python tests/native_ofx_ui.py
Use a separate BLENDER_USER_RESOURCES directory when launching this test.
"""
import json
from pathlib import Path
import sys
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import blender_raw
from blender_raw import compositor, film_node, native_effects, ofx_node, ofx_schema

OUTPUT = ROOT / ".native-test-output" / "ofx-ui"
OUTPUT.mkdir(parents=True, exist_ok=True)
seen_groups = set()
seen_parameters = set()
draw_failures = []
draws = 0
original_draw = ofx_node.BR_NODE_spektrafilm_ofx._draw_parameters
original_control = ofx_node._draw_control


def record_control(node, layout, parameter):
    original_control(node, layout, parameter)
    seen_parameters.add(parameter.key)


ofx_node._draw_control = record_control


def record_draw(self, context, layout):
    global draws
    try:
        original_draw(self, context, layout)
        draws += 1
        for key, _label in ofx_schema.GROUPS:
            if getattr(self, "show_ofx_" + key):
                seen_groups.add(key)
    except BaseException:
        draw_failures.append(traceback.format_exc())
        raise


ofx_node.BR_NODE_spektrafilm_ofx._draw_parameters = record_draw
def redraw(area, count=5):
    for _ in range(count):
        area.tag_redraw()
        yield


def test():
    blender_raw.register()
    window = bpy.context.window_manager.windows[0]
    window.event_simulate(type="ESC", value="PRESS")
    window.event_simulate(type="ESC", value="RELEASE")
    yield
    scene = window.scene
    scene[compositor.STUDIO_TAG] = "ofx-interface-check"
    area = max(window.screen.areas, key=lambda item: item.width * item.height)
    area.type = "PROPERTIES"
    area.spaces.active.context = "COMPOSITOR"
    lite = native_effects.create_effect(scene, film_node.FILM_TYPE)
    ofx = native_effects.create_effect(scene, ofx_node.FILM_TYPE)
    lite.show_expanded = False
    ofx.show_expanded = True
    node = native_effects.effect_node(ofx)
    scene.compositor_effects.active = ofx
    yield from redraw(area, 12)
    bpy.ops.screen.screenshot(filepath=str(OUTPUT / "ofx-before-settings.png"))
    # Native Settings has independent fold state, even when the card is open.
    # Coordinates are taken from this test's 980 x 1100, 125% UI layout.
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=area.x + 80, y=area.y + area.height - 234)
    yield
    window.event_simulate(type="LEFTMOUSE", value="PRESS", x=area.x + 80, y=area.y + area.height - 234)
    yield
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=area.x + 80, y=area.y + area.height - 234)
    yield from redraw(area, 8)
    assert draws and not draw_failures, draw_failures
    bpy.ops.screen.screenshot(filepath=str(OUTPUT / "ofx-initial.png"))

    populated = [(key, label) for key, label in ofx_schema.GROUPS
                 if any(parameter.group == key for parameter in ofx_schema.PARAMETERS)]
    for key, label in populated:
        ancestors = {key}
        parent = ofx_node._GROUP_PARENTS.get(key)
        while parent:
            ancestors.add(parent)
            parent = ofx_node._GROUP_PARENTS.get(parent)
        for other, _ in ofx_schema.GROUPS:
            setattr(node, "show_ofx_" + other, other in ancestors)
            setattr(node, "show_ofx_advanced_" + other, other == key)
        yield from redraw(area, 3)
        assert key in seen_groups and not draw_failures, (key, draw_failures)
        if any(word in label.casefold() for word in ("film", "grain", "scanner", "diffusion", "print")):
            bpy.ops.screen.screenshot(filepath=str(OUTPUT / ("ofx-" + (key or "root") + ".png")))

    for key, _ in ofx_schema.GROUPS:
        setattr(node, "show_ofx_" + key, False)
    yield from redraw(area)
    bpy.ops.screen.screenshot(filepath=str(OUTPUT / "ofx-all-sections.png"))
    # Actual native duplicate and Reset must address the selected OFX instance.
    setattr(node, ofx_node.parameter_property("filmExposureEv"), 1.5)
    bpy.ops.scene.duplicate_compositor_effect(name=ofx.name)
    native_effects.ensure_independent_effects()
    duplicate = scene.compositor_effects[-1]
    copied = native_effects.effect_node(duplicate)
    assert copied.instance_uid != node.instance_uid and copied.node_tree != node.node_tree
    ofx_node.reset_recipe(copied)
    assert ofx_node.parameters(node)["filmExposureEv"] == 1.5
    assert ofx_node.parameters(copied)["filmExposureEv"] == ofx_schema.BY_KEY["filmExposureEv"].default
    assert not draw_failures
    toggles = {ofx_node._group_toggle(key) for key, _label in ofx_schema.GROUPS}
    assert {p.key for p in ofx_schema.PARAMETERS} - toggles <= seen_parameters
    result = {"passed": True, "draws": draws, "groups": len(populated),
              "parameters": len(ofx_schema.PARAMETERS), "drawn_parameters": len(seen_parameters),
              "screenshots": str(OUTPUT)}
    (OUTPUT / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("NATIVE_OFX_UI_PASS", json.dumps(result), flush=True)


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
        (OUTPUT / "result.json").write_text(json.dumps({"passed": False, "error": error,
                                                        "draw_failures": draw_failures}, indent=2), encoding="utf-8")
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(tick, first_interval=.5, persistent=True)
