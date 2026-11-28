"""Stock-bound Lite DIR calibration, legacy repair and the Portra-to-Velvia case.

blender -b --factory-startup --python-exit-code 1 --python tests/spektrafilm_calibration.py
Use -- --no-metal to omit GPU renders. A local Portra800 + 2383.sfpreset is
used when present; the portable fallback contains its original DIR vectors.
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
    from bl_ext.user_default.raw_photo_editor import catalog, compositor, film_effects, film_node, spektrafilm_engine as engine
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from blender_raw import catalog, compositor, film_effects, film_node, spektrafilm_engine as engine


MATRICES = film_effects.DIR_MATRIX_PARAMETERS
MODES = film_effects.PROCESS_MODES
PORTRA = "kodak_portra_800"
VELVIA = "fujifilm_velvia_100"
PROVIA = "fujifilm_provia_100f"
NEGATIVE = {"dirGammaSameLayerRgb": (0.336, 0.319, 0.273),
            "dirGammaRToGb": (0.353, 0.302), "dirGammaGToRb": (0.154, 0.353),
            "dirGammaBToRg": (0.168, 0.226)}


def assert_calibration(node, expected):
    recipe = film_node.recipe(node)
    for key in MATRICES:
        assert len(getattr(node, key)) == len(expected[key]), key
        np.testing.assert_allclose(getattr(node, key), expected[key], rtol=0, atol=1e-7, err_msg=key)
        np.testing.assert_allclose(recipe["effects"][key], expected[key], rtol=0, atol=1e-7, err_msg=key)
        assert key not in recipe["parameters"], key


def noncalibration_recipe(node):
    recipe = film_node.recipe(node)
    recipe.pop("film")
    for key in MATRICES:
        recipe["effects"].pop(key)
    recipe["parameters"].pop("process")
    recipe["parameters"].pop("scanNegativeInvert")
    return recipe


def assert_recommendation(node, stock):
    record = engine.stock_defaults(stock)
    recipe = film_node.recipe(node)
    assert node.preset_film == stock and node.use_stock_calibration
    assert node.paper == (record["target_print"] or "")
    assert node.process == MODES[1 if record["type"] == "positive" else 0]
    assert not node.scanNegativeInvert and not node.preset_name
    for key, expected in record["parameters"].items():
        values = recipe["effects"] if key in recipe["effects"] else recipe["parameters"]
        np.testing.assert_allclose(values[key], expected, rtol=0, atol=1e-6, err_msg=key)


def preset_path(folder):
    repo = Path(__file__).resolve().parents[1]
    for root in (repo, repo.parent.parent):
        actual = root / "DNG for Test" / "Portra800 + 2383.sfpreset"
        if actual.is_file():
            print(f"Testing actual preset: {actual}", flush=True)
            return actual
    films, papers = engine.stocks()
    settings = {"ints": {"film": films.index(PORTRA), "paper": papers.index("kodak_2383"),
                         "process": 0, "grainModel": 1},
                "bools": {"scanNegativeInvert": False, "scannerEnabled": True},
                "doubles": {"dirCouplersAmount": 1.0, "dirCouplersDiffusionUm": 20.0,
                            "dirCouplersInhibitionSameLayer": 1.0, "cameraDiffusionStrength": 0.25},
                "vectors": NEGATIVE}
    path = folder / "Portra800 + 2383.sfpreset"
    path.write_text(json.dumps({"version": 1, "name": "Portra800 + 2383", "settings": settings}), encoding="utf-8")
    print("Testing portable fixture with original Portra DIR vectors", flush=True)
    return path


def duplicate(tree, node):
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


def check_node(folder):
    tree = bpy.data.node_groups.new("Stock calibration regression", "CompositorNodeTree")
    tree.use_fake_user = True
    node = tree.nodes.new(film_node.FILM_TYPE)
    path = preset_path(folder)
    film_node.load_preset(node, path)
    assert node.preset_film == PORTRA and node.film == PORTRA and not node.use_stock_calibration
    assert_calibration(node, NEGATIVE)
    assert not any(node.is_property_set(key) for key in MATRICES)
    original_json = node.preset_parameters
    artistic = noncalibration_recipe(node)

    # Switching after a preset import replaces its print and calibration.
    node.film = VELVIA
    assert node.process == MODES[1] and not node.scanNegativeInvert
    assert node.preset_parameters != original_json
    assert_calibration(node, engine.dir_defaults(VELVIA))
    assert_recommendation(node, VELVIA)
    assert node.scannerEnabled == artistic["effects"]["scannerEnabled"]
    assert film_node.recipe(node)["grain_model"] == artistic["grain_model"]
    assert noncalibration_recipe(node) != artistic
    fixed_recipe = film_node.recipe(node)
    node.film = PORTRA
    assert node.process == MODES[0]
    assert_calibration(node, NEGATIVE)
    assert_recommendation(node, PORTRA)
    node.film = VELVIA

    # Component edits are especially important with get_transform: untouched
    # components must start from the selected stock, not the negative RNA default.
    expected = {k: list(v) for k, v in engine.dir_defaults(VELVIA).items()}
    node.dirGammaRToGb[1] = 0.025
    expected["dirGammaRToGb"][1] = 0.025
    assert_calibration(node, expected)
    node.dirGammaSameLayerRgb[2] = 0.035
    expected["dirGammaSameLayerRgb"][2] = 0.035
    assert_calibration(node, expected)
    node.dirCouplersAmount = 0.75
    node.dirCouplersDiffusionUm = 13.0
    node.dirCouplersInhibitionInterlayer = 0.65
    before_reset = noncalibration_recipe(node)
    film_node.reset_calibration(node)
    assert node.use_stock_calibration and not any(node.is_property_set(k) for k in MATRICES)
    assert_calibration(node, engine.dir_defaults(VELVIA))
    assert noncalibration_recipe(node) == before_reset
    # Explicit edits bind to the current stock and are cleared on a new stock.
    node.dirGammaBToRg = (0.02, 0.03)
    node.film = PROVIA
    assert_calibration(node, engine.dir_defaults(PROVIA))
    assert all(node.is_property_set(key) for key in MATRICES)
    assert_recommendation(node, PROVIA)
    assert noncalibration_recipe(node) != before_reset
    node.process = MODES[2]
    node.scanNegativeInvert = True
    node.film = PORTRA
    assert node.process == MODES[0] and not node.scanNegativeInvert
    assert_recommendation(node, PORTRA)

    # A real slide preset may intentionally customize just one matrix group.
    custom_path = folder / "custom-slide.sfpreset"
    custom_values = {**engine.dir_defaults(VELVIA), "dirGammaRToGb": (0.07, 0.02)}
    custom_path.write_text(json.dumps({"version": 1, "settings": {
        "ints": {"film": engine.stocks()[0].index(VELVIA), "process": 1},
        "vectors": custom_values, "doubles": {"dirCouplersAmount": 0.8}}}), encoding="utf-8")
    film_node.load_preset(node, custom_path)
    assert not node.use_stock_calibration and node.preset_film == VELVIA
    assert_calibration(node, custom_values)
    node.film = PROVIA
    assert_calibration(node, engine.dir_defaults(PROVIA))
    node.film = VELVIA
    assert_calibration(node, engine.dir_defaults(VELVIA))
    assert_recommendation(node, VELVIA)
    film_node.load_preset(node, custom_path)
    assert_calibration(node, custom_values)

    # Saved 0.20.1 nodes have no provenance. Recognize a complete known factory
    # matrix, but do not silently discard an unknown legacy custom calibration.
    node.property_unset("preset_film")
    node.use_stock_calibration = False
    for key in MATRICES:
        node.property_unset(key)
    node.preset_parameters = json.dumps(NEGATIVE)
    assert_calibration(node, engine.dir_defaults(VELVIA))
    custom_legacy = {**NEGATIVE, "dirGammaRToGb": (0.15, 0.08)}
    node.preset_parameters = json.dumps(custom_legacy)
    assert_calibration(node, custom_legacy)
    node.film = "fujifilm_provia_100f"
    assert node.use_stock_calibration
    assert_calibration(node, engine.dir_defaults(node.film))
    film_node.reset_calibration(node)
    assert_calibration(node, engine.dir_defaults(node.film))

    # Preserve provenance, reset flag and explicit component edits through copies
    # and saved .blend files. A reset of one copy must not mutate the other.
    film_node.load_preset(node, custom_path)
    node.dirGammaGToRb[0] = 0.03
    expected = {**custom_values, "dirGammaGToRb": (0.03, custom_values["dirGammaGToRb"][1])}
    assert_calibration(node, expected)
    copied = duplicate(tree, node)
    assert copied.instance_uid != node.instance_uid and copied.node_tree != node.node_tree
    assert copied.preset_film == node.preset_film and copied.preset_parameters == node.preset_parameters
    assert_calibration(copied, expected)
    film_node.reset_calibration(copied)
    assert_calibration(copied, engine.dir_defaults(VELVIA))
    assert_calibration(node, expected)
    names = tree.name, node.name, copied.name
    blend = str(folder / "calibration.blend")
    assert bpy.ops.wm.save_as_mainfile(filepath=blend) == {"FINISHED"}
    assert bpy.ops.wm.open_mainfile(filepath=blend) == {"FINISHED"}
    tree = bpy.data.node_groups[names[0]]
    node, copied = tree.nodes[names[1]], tree.nodes[names[2]]
    assert node.preset_film == VELVIA and not node.use_stock_calibration
    assert copied.use_stock_calibration
    assert_calibration(node, expected)
    assert_calibration(copied, engine.dir_defaults(VELVIA))

    partial = folder / "partial.sfpreset"
    partial.write_text(json.dumps({"version": 1, "settings": {"bools": {"grainEnabled": True}}}), encoding="utf-8")
    film_node.load_preset(copied, partial)
    assert not copied.use_stock_calibration and copied.preset_film == engine.default_film()
    assert_calibration(copied, engine.dir_defaults(engine.default_film()))
    film_node.reset_recipe(copied)
    assert copied.film == engine.default_film()
    assert_recommendation(copied, engine.default_film())
    assert_calibration(copied, engine.dir_defaults(engine.default_film()))
    print("PASS stock calibration, actual preset switch, component edits, mode, legacy repair, copy/reset/persistence", flush=True)
    return fixed_recipe


def check_render(recipe):
    levels = np.array([0.01, 0.04, 0.18, 0.5, 0.9], dtype=np.float32)
    chart = np.broadcast_to(np.repeat(levels, 64)[None, :, None], (64, 320, 3)).copy()
    # Isolate stock calibration from stochastic grains and spatial optics while
    # retaining the selected stock's hidden spectral and development parameters.
    recipe = {**recipe, "grain": False, "halation": False, "effects": {
        **recipe["effects"], "cameraDiffusionEnabled": False,
        "printDiffusionEnabled": False, "scannerEnabled": False}}
    good = engine.render(chart, **recipe)
    bad = engine.render(chart, **{**recipe, "effects": {**recipe["effects"], **NEGATIVE}})
    luma = good[32, 32::64] @ np.array([0.2126, 0.7152, 0.0722])
    bad_luma = bad[32, 32::64] @ np.array([0.2126, 0.7152, 0.0722])
    assert np.isfinite(good).all() and np.isfinite(bad).all()
    assert np.all(np.diff(luma) > 0) and luma[-1] - luma[0] > 0.1, luma
    assert np.min(np.diff(bad_luma)) < -0.02, bad_luma
    assert np.max(np.abs(good - bad)) > 0.1
    print(f"PASS native Portra-to-Velvia: fixed={luma.round(5).tolist()}, stale={bad_luma.round(5).tolist()}", flush=True)


if not installed:
    catalog.register()
    compositor.register()
try:
    with TemporaryDirectory() as folder, (
        patch.object(engine, "backend_error", return_value="") if "--no-metal" in sys.argv else nullcontext()
    ):
        recipe = check_node(Path(folder))
        if "--no-metal" not in sys.argv:
            check_render(recipe)
finally:
    if not installed:
        compositor.unregister()
        catalog.unregister()
