# SPDX-License-Identifier: GPL-3.0-or-later
"""Stock picker regression: explicit non-active-card targets and stale choices.

blender -b --offline-mode --python-exit-code 1 -P tests/spektrafilm_stock_picker_ui.py -- --installed --require-bw

Omit -b, enable --enable-event-simulate and add --click to exercise the actual
card buttons and menu selections. Tested at -p 40 40 1600 1000 and 1200 850,
with factory 125% UI scaling. --window separately draws/introspects the menus.
PHOTO_UI_TEST_SCREENSHOTS optionally receives each stage's screenshot.
"""
from importlib import import_module
import os
from pathlib import Path
import sys
import time
import traceback
from unittest.mock import patch

import bpy


if "--installed" in sys.argv:
    from bl_ext.user_default import raw_photo_editor as addon
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import blender_raw as addon

ui = import_module(addon.__name__ + ".film_stock_ui")
engine = import_module(addon.__name__ + ".spektrafilm_engine")
film_node = import_module(addon.__name__ + ".film_node")
effects = import_module(addon.__name__ + ".native_effects")


def rejected(**kwargs):
    try:
        result = bpy.ops.blender_raw.choose_film_stock(**kwargs)
    except RuntimeError as error:
        assert "unavailable" in str(error), str(error)
    else:
        assert result == {"CANCELLED"}, result


def prepare(*, register=True):
    if register and "--installed" not in sys.argv:
        addon.register()
    scene = bpy.context.scene
    with patch.object(engine, "backend_error", return_value=""):
        first, second = [effects.create_effect(scene, film_node.FILM_TYPE) for _ in range(2)]
    target, active = [effects.effect_node(effect) for effect in (first, second)]
    scene.compositor_effects.active = second
    target.live_preview = False
    active.film = "fujifilm_provia_100f"
    active.grainAmount = .345
    active_before = film_node.recipe(active)
    names = dict(tree_name=target.id_data.name, node_name=target.name)

    assert bpy.ops.blender_raw.choose_film_stock(**names, kind="film", stock="kodak_portra_800") == {"FINISHED"}
    assert target.film == "kodak_portra_800" and target.paper == "kodak_portra_endura"
    assert target.process == "Full Film Sim" and not target.live_preview
    assert bpy.ops.blender_raw.choose_film_stock(**names, kind="paper", stock="kodak_2383") == {"FINISHED"}
    assert target.paper == "kodak_2383"
    assert bpy.ops.blender_raw.choose_film_stock(**names, kind="film", stock="fujifilm_velvia_100") == {"FINISHED"}
    assert target.process == "Negative only"
    assert film_node.recipe(active) == active_before
    assert scene.compositor_effects.active == second

    # Renamed/removed targets must never fall back to the active effect.
    assert bpy.ops.blender_raw.choose_film_stock(tree_name=target.id_data.name, node_name="missing node",
                                               kind="film", stock="kodak_portra_400") == {"CANCELLED"}
    assert bpy.ops.blender_raw.choose_film_stock(tree_name="", node_name="",
                                               kind="film", stock="kodak_portra_400") == {"CANCELLED"}
    assert film_node.recipe(active) == active_before
    target_before = film_node.recipe(target)
    rejected(**names, kind="film", stock="unlisted_material")
    assert film_node.recipe(target) == target_before

    films, papers = engine.stocks()
    if "--require-bw" in sys.argv:
        assert (len(films), len(papers)) == (64, 14), (len(films), len(papers))
    if "kodak_doublex" in films:
        assert bpy.ops.blender_raw.choose_film_stock(**names, kind="film", stock="kodak_doublex") == {"FINISHED"}
        assert target.paper == "kodak_2302" and target.process == "Full Film Sim"
        # A colour-paper button retained from an earlier menu is stale now.
        before = film_node.recipe(target)
        rejected(**names, kind="paper", stock="kodak_2383")
        assert film_node.recipe(target) == before
        assert bpy.ops.blender_raw.choose_film_stock(**names, kind="paper",
                                                    stock="ilford_mgrc_deluxe_grade2") == {"FINISHED"}
        assert bpy.ops.blender_raw.choose_film_stock(**names, kind="film", stock="kodak_trix") == {"FINISHED"}
        assert target.process == "Negative only" and not engine.compatible_papers(target.film)
        rejected(**names, kind="paper", stock="kodak_2302")
    assert film_node.recipe(active) == active_before and scene.compositor_effects.active == second
    # The checkmark should identify this readable Kodak name on the film menu.
    target.film = "kodak_portra_800"
    print(f"PASS stock picker target/compatibility: {len(films)} films, {len(papers)} prints; inactive card and Live Preview preserved", flush=True)
    return target, active


def window_checks():
    target, active = prepare()
    window = bpy.context.window_manager.windows[0]
    area = window.screen.areas[0]
    draws = []
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type="ESC", value="PRESS")
    window.event_simulate(type="ESC", value="RELEASE")
    yield

    def inspect(self, context):
        node = getattr(context, "spektrafilm_stock_node", None)
        draws.append((node.as_pointer() if node else None, str(self.layout.introspect())))

    for kind, panel in (("film", ui.BR_MT_film_stock_picker), ("paper", ui.BR_MT_print_stock_picker)):
        draws.clear()
        panel.append(inspect)
        try:
            with bpy.context.temp_override(window=window, area=area, spektrafilm_stock_node=target):
                bpy.ops.wm.call_menu("INVOKE_DEFAULT", name=panel.bl_idname)
            for _ in range(4):
                yield
            assert draws, "Stock menu did not draw"
            pointer, layout = draws[-1]
            assert pointer == target.as_pointer(), (pointer, target.as_pointer())
            assert ui.BR_OT_choose_film_stock.bl_idname in layout, layout
            assert target.id_data.name in layout and target.name in layout, layout
            # UILayout.introspect omits icons; the saved screenshot exposes
            # the current-selection checkmark for visual review.
            assert "KODAK" in layout and "FUJIFILM" in layout, layout
            if kind == "film":
                assert "Color Negative" in layout and "Slide" in layout, layout
                if "kodak_doublex" in engine.stocks()[0]:
                    assert all(text in layout for text in ("B&W Negative", "B&W Reversal", "experimental")), layout
            if output := os.environ.get("PHOTO_UI_TEST_SCREENSHOTS"):
                path = Path(output)
                path.mkdir(parents=True, exist_ok=True)
                bpy.ops.screen.screenshot(filepath=str(path / ("stock-picker-" + kind + ".png")))
            window.event_simulate(type="ESC", value="PRESS")
            window.event_simulate(type="ESC", value="RELEASE")
            yield
        finally:
            panel.remove(inspect)
    assert active.film == "fujifilm_provia_100f" and abs(active.grainAmount - .345) < 1e-6
    print("PASS real stock menu layouts, brand/category labels and explicit target bindings", flush=True)


def click(window, x, y):
    for kind, value in (("MOUSEMOVE", "NOTHING"), ("LEFTMOUSE", "PRESS"), ("LEFTMOUSE", "RELEASE")):
        window.event_simulate(type=kind, value=value, x=round(x), y=round(y))
        yield
    for _ in range(3):
        yield


def screenshot(label):
    if output := os.environ.get("PHOTO_UI_TEST_SCREENSHOTS"):
        path = Path(output)
        path.mkdir(parents=True, exist_ok=True)
        bpy.ops.screen.screenshot(filepath=str(path / (label + ".png")))


def click_checks():
    if "--installed" not in sys.argv:
        addon.register()
    window = bpy.context.window_manager.windows[0]
    bpy.context.preferences.view.show_splash = False
    window.event_simulate(type="ESC", value="PRESS")
    window.event_simulate(type="ESC", value="RELEASE")
    yield
    with bpy.context.temp_override(window=window, area=window.screen.areas[0]):
        assert bpy.ops.blender_raw.new_workspace() == {"FINISHED"}
    while not window.screen.get("photo_editor_navigation_default") or addon.gallery._timers:
        yield
    target, active = prepare(register=False)
    first = next(effect for effect in window.scene.compositor_effects if effects.effect_node(effect) == target)
    second = next(effect for effect in window.scene.compositor_effects if effects.effect_node(effect) == active)
    first.show_expanded, second.show_expanded = True, False
    window.scene.compositor_effects.active = second
    panel = next(area for area in window.screen.areas if area.type == "PROPERTIES")
    region = next(region for region in panel.regions if region.type == "WINDOW")
    draws = []

    def inspect(self, context):
        node = getattr(context, "spektrafilm_stock_node", None)
        draws.append((node.as_pointer() if node else None, str(self.layout.introspect())))

    ui.BR_MT_film_stock_picker.append(inspect)
    try:
        panel.tag_redraw()
        yield
        yield
        scale = bpy.context.preferences.system.ui_scale / 1.25
        top = panel.y + panel.height
        yield from click(window, region.x + 43 * scale, top - 172 * scale)
        screenshot("click-before-film")
        window.scene.compositor_effects.active = second
        yield from click(window, region.x + 180 * scale, top - 366 * scale)
        screenshot("click-film-open")
        assert draws, "Click did not open the Film picker"
        assert draws[-1][0] == target.as_pointer()
        assert all(stock in draws[-1][1] for stock in engine.stocks()[0]), draws[-1][1]
        assert "KODAK" in draws[-1][1] and "FUJIFILM" in draws[-1][1]
        assert window.scene.compositor_effects.active == second
        # Fifth column, first B&W item: HP5 Plus. At factory 125% scale the
        # compact menu is clamped to the right window edge and fits below
        # the Film button at both tested laptop/desktop window sizes.
        yield from click(window, window.width - 150 * scale, top - 431 * scale)
        screenshot("click-after-film")
        assert target.film == "ilford_hp5_plus", target.film
        assert target.paper == "ilford_mgrc_deluxe_grade2", target.paper
        assert active.film == "fujifilm_provia_100f" and abs(active.grainAmount - .345) < 1e-6
        assert window.scene.compositor_effects.active == second and not target.live_preview
        draws.clear()
        ui.BR_MT_print_stock_picker.append(inspect)
        try:
            yield from click(window, region.x + 180 * scale, top - 428 * scale)
            screenshot("click-print-open")
            assert draws and draws[-1][0] == target.as_pointer()
            yield from click(window, window.width - 850 * scale, top - 499 * scale)
            screenshot("click-after-print")
            assert target.paper == "kodak_2302", target.paper
            assert target.film == "ilford_hp5_plus" and not target.live_preview
            assert active.film == "fujifilm_provia_100f" and abs(active.grainAmount - .345) < 1e-6
            assert window.scene.compositor_effects.active == second
            target.grainAmount = .2
            assert abs(target.grainAmount - .2) < 1e-6
            # Hit the right end of the parameter column. An icon-sized button
            # at the left cannot pass this full-width hit-area regression.
            restore_x = region.x + region.width - 32 * scale
            yield from click(window, restore_x, top - 395 * scale)
            screenshot("click-after-restore")
            assert abs(target.grainAmount - 1.) < 1e-6, target.grainAmount
            assert target.film == "ilford_hp5_plus" and target.paper == "ilford_mgrc_deluxe_grade2"
            assert not target.live_preview and window.scene.compositor_effects.active == second
            assert active.film == "fujifilm_provia_100f" and abs(active.grainAmount - .345) < 1e-6
            print(f"PASS actual stock clicks at {window.width}x{window.height}: non-active Film -> HP5 -> Print -> 2302 -> restore icon; active card unchanged", flush=True)
        finally:
            ui.BR_MT_print_stock_picker.remove(inspect)
    finally:
        ui.BR_MT_film_stock_picker.remove(inspect)


if "--window" in sys.argv or "--click" in sys.argv:
    assert not bpy.app.background
    steps = click_checks() if "--click" in sys.argv else window_checks()
    deadline = time.monotonic() + 60

    def tick():
        try:
            assert time.monotonic() < deadline, "Timed out waiting for stock menus"
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
else:
    prepare()
