"""Lite standalone processing modes, legacy presets and real native slide scans.

blender -b --factory-startup --python-exit-code 1 --python tests/spektrafilm_reversal.py
Use -- --no-metal for RNA/preset/persistence checks without a GPU.
"""
from contextlib import nullcontext
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import bpy
import numpy as np

installed = "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons
if installed:
    from bl_ext.user_default.raw_photo_editor import catalog, compositor, film_effects, film_node, spektrafilm_engine
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from blender_raw import catalog, compositor, film_effects, film_node, spektrafilm_engine


MODES = ("Full Film Sim", "Negative only", "Print only")


def assert_controls(node, mode, invert):
    assert node.process == MODES[mode], (node.process, mode)
    assert node.scanNegativeInvert is invert
    values = film_node.recipe(node)["parameters"]
    assert values["process"] == mode and values["scanNegativeInvert"] is invert, values


def duplicate_node(tree, node):
    window = bpy.context.window
    area = max(window.screen.areas, key=lambda item: item.width * item.height)
    area.ui_type = "CompositorNodeTree"
    region = next(region for region in area.regions if region.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=area, region=region):
        area.spaces.active.pin = True
        area.spaces.active.path.start(tree)
        for item in tree.nodes:
            item.select = item == node
        tree.nodes.active = node
        assert bpy.ops.node.duplicate() == {"FINISHED"}
    return tree.nodes.active


def check_persistence(folder):
    assert film_effects.PROCESS_MODES == MODES
    tree = bpy.data.node_groups.new("Lite reversal modes", "CompositorNodeTree")
    tree.use_fake_user = True
    node = tree.nodes.new(film_node.FILM_TYPE)
    assert node.bl_rna.properties["process"].name == "Mode"
    assert node.bl_rna.properties["scanNegativeInvert"].name == "Positive Output"
    assert tuple(item.name for item in node.bl_rna.properties["process"].enum_items) == MODES
    assert_controls(node, 0, False)

    # Earlier releases stored these imported controls solely in JSON. Reading an
    # old file must expose the original values; later manual edits take priority.
    node.preset_parameters = json.dumps({"process": 1, "scanNegativeInvert": True, "filmGamma": 1.1})
    assert not node.is_property_set("process") and not node.is_property_set("scanNegativeInvert")
    assert_controls(node, 1, True)
    node.process = MODES[0]
    node.scanNegativeInvert = False
    assert_controls(node, 0, False)
    assert film_node.recipe(node)["parameters"]["filmGamma"] == 1.1
    node.property_unset("process")
    node.property_unset("scanNegativeInvert")
    assert_controls(node, 1, True)

    path = folder / "slide.sfpreset"
    films = spektrafilm_engine.stocks()[0]
    velvia = films.index("fujifilm_velvia_100")

    def load(settings):
        path.write_text(json.dumps({"version": 1, "settings": settings}), encoding="utf-8")
        film_node.load_preset(node, path)

    load({"ints": {"film": velvia, "process": 1}, "bools": {"scanNegativeInvert": False}})
    assert_controls(node, 1, False)
    assert node.film == "fujifilm_velvia_100"
    assert "process" not in json.loads(node.preset_parameters)
    assert "scanNegativeInvert" not in json.loads(node.preset_parameters)
    node.scanNegativeInvert = True
    # A partial standalone preset replaces prior controls rather than leaking
    # the old scan/invert settings into the newly imported print recipe.
    load({"ints": {"film": velvia}})
    assert_controls(node, 0, False)
    load({"ints": {"process": 2}, "bools": {"scanNegativeInvert": True}})
    assert_controls(node, 2, True)
    load({"ints": {"film": velvia, "process": 1}})
    assert_controls(node, 1, False)
    node.scanNegativeInvert = True
    original_recipe = film_node.recipe(node)
    duplicate = duplicate_node(tree, node)
    assert duplicate.instance_uid != node.instance_uid
    assert duplicate.node_tree != node.node_tree
    assert film_node.recipe(duplicate) == original_recipe
    duplicate.process = MODES[2]
    assert_controls(duplicate, 2, True)
    assert_controls(node, 1, True)

    tree_name, node_name, duplicate_name = tree.name, node.name, duplicate.name
    blend = str(folder / "reversal.blend")
    assert bpy.ops.wm.save_as_mainfile(filepath=blend) == {"FINISHED"}
    assert bpy.ops.wm.open_mainfile(filepath=blend) == {"FINISHED"}
    tree = bpy.data.node_groups[tree_name]
    node, duplicate = tree.nodes[node_name], tree.nodes[duplicate_name]
    assert film_node.recipe(node) == original_recipe
    assert_controls(duplicate, 2, True)
    film_node.reset_recipe(duplicate)
    assert_controls(duplicate, 0, False)
    assert not duplicate.preset_parameters and not duplicate.film
    assert_controls(node, 1, True)
    print("PASS Lite modes: legacy presets, explicit override, partial import, duplicate, .blend, reset", flush=True)
    return node


def check_native(node):
    # Gray patches isolate polarity from film hue and spatial effects. Use the
    # actual recipe produced by the node, including the engine's native DIR.
    levels = np.array([0.01, 0.04, 0.18, 0.5, 0.9], dtype=np.float32)
    chart = np.broadcast_to(np.repeat(levels, 32)[None, :, None], (32, 160, 3)).copy()
    films, papers = spektrafilm_engine.stocks()

    def render(stock, mode=1, invert=False, paper=None):
        film_node.reset_recipe(node)
        node.film = stock
        node.process = MODES[mode]
        node.scanNegativeInvert = invert
        node.paper = paper or papers[0]
        recipe = film_node.recipe(node)
        output = spektrafilm_engine.render(chart, **recipe)
        assert output.shape == chart.shape and np.isfinite(output).all()
        luma = output[16, 16::32] @ np.array([0.2126, 0.7152, 0.0722])
        return output, luma, recipe

    slide_stocks = [s for s in films if any(name in s for name in ("ektachrome", "kodachrome", "velvia", "provia"))]
    assert "fujifilm_velvia_100" in slide_stocks and len(slide_stocks) >= 4
    for stock in slide_stocks:
        output, luma, recipe = render(stock)
        assert np.all(np.diff(luma) > 0), (stock, "slide scan polarity", luma)
        assert luma[-1] - luma[0] > 0.1, (stock, luma)
        # Native parameter forwarding must preserve the standalone scan result,
        # with no node-side inversion or a second trip through the print stage.
        native = spektrafilm_engine.render(chart, **{**recipe, "effects": {
            key: value for key, value in recipe["effects"].items()
            if key not in film_effects.DIR_MATRIX_PARAMETERS}, "parameters": {
            "process": 1, "scanNegativeInvert": False}})
        np.testing.assert_allclose(output, native, rtol=0, atol=1e-7)
        alternative, _, _ = render(stock, paper=papers[-1])
        np.testing.assert_allclose(output, alternative, rtol=0, atol=1e-6)
        indexed = spektrafilm_engine.render(chart, **{**recipe, "film": "", "parameters": {
            **recipe["parameters"], "film": films.index(stock)}})
        np.testing.assert_allclose(output, indexed, rtol=0, atol=1e-7)
        _, normalized, _ = render(stock, invert=True)
        assert np.all(np.diff(normalized) > 0), (stock, "positive normalization polarity", normalized)
        print(f"PASS {stock}: scan luma {luma.round(5).tolist()}", flush=True)

    # An imported/manual channel-group override must win over stock defaults.
    # Fully explicit calibration is an independent native call through the
    # public parameter interface, without relying on engine private constants.
    normal, _, recipe = render("fujifilm_velvia_100")
    calibration = {"dirGammaSameLayerRgb": (0.108, 0.072, 0.054),
                   "dirGammaRToGb": (0.108, 0.054), "dirGammaGToRb": (0.072, 0.054),
                   "dirGammaBToRg": (0.054, 0.054)}
    # Direct parameter callers have no node RNA. Remove its effective matrices
    # so the explicit native parameters are not masked by the effects payload.
    direct_recipe = {**recipe, "effects": {key: value for key, value in recipe["effects"].items()
                                          if key not in film_effects.DIR_MATRIX_PARAMETERS}}
    explicit = spektrafilm_engine.render(chart, **{**direct_recipe, "parameters": {
        **recipe["parameters"], **calibration}})
    np.testing.assert_allclose(normal, explicit, rtol=0, atol=1e-7)
    override = {"dirGammaSameLayerRgb": (0.01, 0.02, 0.03)}
    partial = spektrafilm_engine.render(chart, **{**direct_recipe, "parameters": {
        **recipe["parameters"], **override}})
    full = spektrafilm_engine.render(chart, **{**direct_recipe, "parameters": {
        **recipe["parameters"], **calibration, **override}})
    np.testing.assert_allclose(partial, full, rtol=0, atol=1e-7)
    assert np.max(np.abs(partial - normal)) > 1e-3

    negative = "kodak_portra_800"
    _, scan, _ = render(negative)
    _, normalized, _ = render(negative, invert=True)
    _, printed, _ = render(negative, mode=0)
    assert np.all(np.diff(scan) < 0), ("negative raw scan", scan)
    assert np.all(np.diff(normalized) > 0), ("negative inverted scan", normalized)
    assert np.all(np.diff(printed) > 0), ("negative print", printed)
    print("PASS native negative scan/inversion/print polarity and slide direct-scan normalization", flush=True)


if not installed:
    catalog.register()
    compositor.register()
try:
    with TemporaryDirectory() as folder, (
        patch.object(spektrafilm_engine, "backend_error", return_value="") if "--no-metal" in sys.argv else nullcontext()
    ):
        node = check_persistence(Path(folder))
        if "--no-metal" not in sys.argv:
            check_native(node)
finally:
    if not installed:
        compositor.unregister()
        catalog.unregister()
