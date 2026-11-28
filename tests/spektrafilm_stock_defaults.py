# SPDX-License-Identifier: GPL-3.0-or-later
"""Accept Film recommendations, personal overrides and silver B&W paths in an installed ZIP.

blender -b --offline-mode --python-exit-code 1 -P tests/spektrafilm_stock_defaults.py
Add -- --pixels for actual Vulkan/Metal renders. This never records or replaces a baseline.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import bpy
import numpy as np
from bl_ext.user_default.raw_photo_editor import film_node, film_user_library, film_user_settings
from bl_ext.user_default.raw_photo_editor import spektrafilm_engine as engine
from bl_ext.user_default.raw_photo_editor.film_effects import BY_KEY, DIR_MATRIX_PARAMETERS, FAMILIES, GROUPS


BW_NEGATIVES = (
    "kodak_doublex", "ilford_hp5_plus", "ilford_fp4_plus", "ilford_delta_3200",
    "kodak_tmax_100", "fujifilm_acros_100_ii", "kodak_trix400_d76_8min",
)
BW_POSITIVE = "kodak_trix"
BW_PAPERS = ("kodak_2302", "ilford_mgrc_deluxe_grade2")
GRAIN_MODELS = ("Preview", "Production", "Grain Synthesis")
OLD_CUSTOM_DIR = {
    "dirGammaSameLayerRgb": [.125, .25, .375],
    "dirGammaRToGb": [.125, .25],
    "dirGammaGToRb": [.375, .5],
    "dirGammaBToRg": [.625, .75],
}


def equal(actual, expected, label):
    if isinstance(expected, str) or expected is None:
        assert actual == expected, (label, actual, expected)
    else:
        np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-6, err_msg=label)


def recommended(node, stock, choices=None):
    """Check the public recipe's field mapping, including every editable effect."""
    choices = choices or {}
    record = engine.stock_defaults(stock)
    settings = film_node.recipe(node)
    assert node.film == stock
    assert node.process == ("Negative only" if record["type"] == "positive" else "Full Film Sim"), stock
    assert node.paper == (record["target_print"] or ""), (stock, node.paper, record["target_print"])
    assert not node.scanNegativeInvert and not node.preset_name, stock
    for key in ("grain", "halation"):
        assert getattr(node, key) == settings[key] == choices.get(key, False), (stock, key)
    assert node.grain_model == choices.get("grain_model", "Preview"), stock
    assert settings["grain_model"] == GRAIN_MODELS.index(node.grain_model), stock
    assert settings["exposure"] == settings["print_exposure"] == 0.0, stock
    assert settings["parameters"]["process"] == (1 if record["type"] == "positive" else 0), stock
    assert settings["parameters"]["scanNegativeInvert"] is False, stock
    for key, effect in BY_KEY.items():
        expected = record["parameters"].get(key, effect.default)
        if type(effect.default) is int:
            expected = FAMILIES[expected]
        expected = choices.get(key, expected)
        equal(getattr(node, key), expected, f"{stock}: node {key}")
        native = FAMILIES.index(expected) if isinstance(expected, str) else expected
        equal(settings["effects"][key], native, f"{stock}: recipe {key}")
    for key, expected in record["parameters"].items():
        engine.validate_parameter(key, expected)
        actual = settings["effects"] if key in BY_KEY else settings["parameters"]
        equal(actual[key], expected, f"{stock}: calibration {key}")
    assert "grainBlurDyeCloudsUm" not in settings["parameters"], (stock, settings["parameters"])
    return settings


def edit_every_effect(node):
    # Pollute every visible group and hidden calibration before the next Film
    # selection. This catches partial resets which appear correct on clean nodes.
    for key, effect in BY_KEY.items():
        current = getattr(node, key)
        if isinstance(effect.default, tuple):
            setattr(node, key, [effect.minimum + (effect.maximum - effect.minimum) * .73] * len(current))
        elif isinstance(effect.default, bool):
            setattr(node, key, not current)
        elif isinstance(current, str):
            setattr(node, key, FAMILIES[(FAMILIES.index(current) + 1) % len(FAMILIES)])
        else:
            setattr(node, key, effect.minimum + (effect.maximum - effect.minimum) * .73)
    node.grain, node.halation, node.grain_model = True, True, "Production"
    node.process, node.scanNegativeInvert = "Print only", True
    node.preset_name = "Deliberate personal edits"
    node.preset_film = node.film
    node.use_stock_calibration = False
    native = json.loads(node.preset_parameters or "{}")
    native["grainUniformity"] = [.2, .3, .4]
    # A native-only field outside the stock metadata must also be reset.
    native["grainBlurDyeCloudsUm"] = .25
    node.preset_parameters = json.dumps(native)


def check_catalog_and_selection(tree):
    films, papers = engine.stocks()
    assert (len(films), len(papers)) == (64, 14), (len(films), len(papers))
    assert len(set(films)) == len(films) and len(set(papers)) == len(papers)
    assert set(films[56:]) == {*BW_NEGATIVES, BW_POSITIVE}, films[56:]
    assert tuple(papers[12:]) == BW_PAPERS, papers[12:]
    node = tree.nodes.new(film_node.FILM_TYPE)
    recommended(node, engine.default_film())
    original_uid, original_group = node.instance_uid, node.node_tree
    for index, stock in enumerate(films):
        node.film = films[(index + 1) % len(films)]
        edit_every_effect(node)
        choices = {
            "grain": bool(index & 1), "halation": bool(index & 2),
            "cameraDiffusionEnabled": bool(index & 4), "scannerEnabled": bool(index & 8),
            "printDiffusionEnabled": bool(index & 16),
            "grain_model": GRAIN_MODELS[index % len(GRAIN_MODELS)],
            "cameraDiffusionFamily": FAMILIES[index % len(FAMILIES)],
            "printDiffusionFamily": FAMILIES[(index + 1) % len(FAMILIES)],
        }
        for key, value in choices.items():
            setattr(node, key, value)
        node.live_preview = index % 2 == 0
        live = node.live_preview
        node.film = stock
        recommended(node, stock, choices)
        assert node.live_preview == live and node.instance_uid == original_uid and node.node_tree == original_group

        choices = engine.compatible_papers(stock)
        assert isinstance(choices, tuple)
        expected = BW_PAPERS if stock in BW_NEGATIVES else () if stock == BW_POSITIVE else papers[:12]
        assert choices == expected, (stock, choices, expected)
        target = engine.stock_defaults(stock)["target_print"]
        if target:
            assert target in choices, (stock, target, choices)
        wrong_paper = papers[0] if stock in (*BW_NEGATIVES, BW_POSITIVE) else BW_PAPERS[0]
        try:
            engine.validate_stock_pair(stock, wrong_paper, 0)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Uncalibrated pairing accepted: {stock} / {wrong_paper}")
        # Direct scans do not consume the remembered Print selection.
        engine.validate_stock_pair(stock, wrong_paper, 1)

        edit_every_effect(node)
        assert bpy.ops.blender_raw.restore_film_defaults(tree_name=tree.name, node_name=node.name) == {"FINISHED"}
        recommended(node, stock)
        assert node.live_preview == live and node.instance_uid == original_uid and node.node_tree == original_group
    print("PASS 64 Film selections preserve effect choices and load stock parameters; Film Defaults restores all controls", flush=True)
    return node


class LayoutProbe:
    """Record controls requested by the actual effects draw function."""
    def __init__(self):
        self.properties = []
        self.labels = []

    def prop(self, node, key, **kwargs):
        self.properties.append(key)

    def label(self, **kwargs):
        self.labels.append(kwargs.get("text", ""))

    def row(self, **kwargs):
        return self

    column = row

    def panel_prop(self, *args, **kwargs):
        return self, self


def check_dir_visibility(node):
    node.film = "kodak_portra_400"
    probe = LayoutProbe()
    node._draw_effects(probe, native_panels=True)
    assert not ({effect.key for effect in GROUPS["dir"]} | {"show_dir"}).intersection(probe.properties)
    assert not any("DIR" in label for label in probe.labels), probe.labels
    for effect in GROUPS["dir"]:
        assert node.bl_rna.properties[effect.key].is_hidden, effect.key
    print("PASS DIR controls remain hidden RNA and are absent from the effects UI", flush=True)


def check_old_personal_recipe(tree, node):
    node.film = "kodak_portra_800"
    old = film_user_settings.snapshot(node)
    # Old personal recipes could carry inherited DIR matrices in their native
    # parameters, plus a manual amount in their controls. Preserve both forms.
    for key in DIR_MATRIX_PARAMETERS:
        old["controls"].pop(key, None)
    old["controls"].update(paper="kodak_2383", grain=True, grainAmount=.375,
                           dirCouplersAmount=.625, cameraDiffusionEnabled=True,
                           scannerEnabled=True, halation=True)
    old["parameters"].update(deepcopy(OLD_CUSTOM_DIR), grainUniformity=[.5, .625, .75])
    old.update(calibration_film="kodak_portra_800", use_stock_calibration=False)
    other = tree.nodes.new(film_node.FILM_TYPE)
    untouched = film_user_settings.snapshot(other)
    with TemporaryDirectory(prefix="old-personal-film-recipe-") as folder, \
            patch.object(film_user_library, "_directory", return_value=Path(folder)):
        preset_id = "17" * 16
        path = Path(folder) / (preset_id + ".json")
        path.write_text(json.dumps({"version": 1, "name": "KODAK Portra 800", "settings": old}), encoding="utf-8")
        original_bytes = path.read_bytes()
        settings = film_user_library.load(preset_id)
        for live in (False, True):
            node.film = BW_NEGATIVES[0]
            node.live_preview = live
            film_user_settings.apply(node, settings)
            assert film_user_settings.snapshot(node) == old
            assert node.live_preview == live and film_user_settings.snapshot(other) == untouched
            for key, value in OLD_CUSTOM_DIR.items():
                equal(film_node.recipe(node)["effects"][key], value, "old personal " + key)
            assert node.paper == "kodak_2383" and node.grain and node.scannerEnabled
            film_node.apply_stock_defaults(node)
            recommended(node, "kodak_portra_800")
            assert node.live_preview == live
        assert path.read_bytes() == original_bytes, "Restoring a node must not rewrite the user's preset"
    print("PASS version-1 personal recipe restores custom DIR, full effects and Live Preview; Restore leaves its file intact", flush=True)


def chart():
    image = np.empty((32, 48, 3), dtype=np.float32)
    image[:] = np.geomspace(.002, 3., 48)[:, None]
    image[8:16, :, 1:] *= .15
    image[16:24, :, ::2] *= .15
    image[24:, :, :2] *= .15
    return image


def render_checked(node, image, *, gray_tolerance=None):
    pixels = engine.render(image, **film_node.recipe(node))
    assert pixels.shape == image.shape and np.isfinite(pixels).all(), (node.film, node.paper)
    assert np.ptp(pixels[0].mean(axis=-1)) > .01, (node.film, node.paper, "flat gray ramp")
    if gray_tolerance is not None:
        error = float(np.max(np.ptp(pixels, axis=-1)))
        assert error <= gray_tolerance, (node.film, node.paper, node.grain_model, "RGB neutrality", error)
    return pixels


def check_pixels(node):
    error = engine.backend_error()
    assert not error, f"Actual native GPU required for --pixels: {error}"
    image = chart()
    # Compare switching to Verita against setting up the same look from its defaults.
    node.grain, node.grain_model = True, "Production"
    node.cameraDiffusionEnabled, node.cameraDiffusionFamily = True, "Black Pro-Mist"
    node.grainParticleAreaUm2, node.cameraDiffusionStrength = 2., 1.5
    result = bpy.ops.blender_raw.choose_film_stock(tree_name=node.id_data.name, node_name=node.name,
                                                 kind="film", stock="kodak_verita_200d")
    assert result == {"FINISHED"} and node.film == "kodak_verita_200d"
    switched = render_checked(node, image)
    film_node.apply_stock_defaults(node)
    clean = render_checked(node, image)
    node.grain, node.grain_model = True, "Production"
    node.cameraDiffusionEnabled, node.cameraDiffusionFamily = True, "Black Pro-Mist"
    np.testing.assert_array_equal(switched, render_checked(node, image))
    assert np.max(np.abs(switched - clean)) > 1e-5
    print("PASS switched Verita 200D pixels match Production grain and Black Pro-Mist on stock parameters", flush=True)

    films, _papers = engine.stocks()
    for index, stock in enumerate(films, 1):
        film_node.apply_stock_defaults(node, stock)
        render_checked(node, image, gray_tolerance=1e-5 if stock in (*BW_NEGATIVES, BW_POSITIVE) else None)
        if index % 16 == 0:
            print(f"Rendered {index}/64 stock recommendations", flush=True)
    print("PASS 64 recommended recipes render finite pixels with a non-flat gray ramp on the native GPU", flush=True)

    count = 0
    for stock in BW_NEGATIVES:
        film_node.apply_stock_defaults(node, stock)
        for paper in BW_PAPERS:
            node.paper = paper
            render_checked(node, image, gray_tolerance=1e-5)
            count += 1
    film_node.apply_stock_defaults(node, BW_POSITIVE)
    assert node.process == "Negative only"
    render_checked(node, image, gray_tolerance=1e-5)
    count += 1
    assert count == 15
    print("PASS 15 calibrated B&W paths: 7 negatives x 2 prints and 1 direct reversal scan; RGB error <= 1e-5", flush=True)

    count = 0
    for stock in (*BW_NEGATIVES, BW_POSITIVE):
        film_node.apply_stock_defaults(node, stock)
        clean = render_checked(node, image, gray_tolerance=1e-5)
        node.grain, node.grainAmount = True, .65
        for model in GRAIN_MODELS:
            node.grain_model = model
            grain = render_checked(node, image, gray_tolerance=5e-5)
            assert np.max(np.abs(grain - clean)) > 1e-7, (stock, model, "grain had no effect")
            count += 1
        print(f"Rendered B&W grain models: {stock}", flush=True)
    print(f"PASS {count} B&W grain renders, including printing and reversal; RGB error <= 5e-5", flush=True)


def check_persistence():
    tree = bpy.data.node_groups.new("Recommended film persistence", "CompositorNodeTree")
    tree.use_fake_user = True
    expected = {}
    films, _papers = engine.stocks()
    for index, stock in enumerate(films):
        node = tree.nodes.new(film_node.FILM_TYPE)
        node.name = stock
        node.film = stock
        choices = engine.compatible_papers(stock)
        if choices:
            node.paper = choices[(index + 1) % len(choices)]
        node.grainAmount = .375
        node.dirCouplersAmount = .625
        node.dirGammaGToRb = (.125, .25)
        node.live_preview = index % 2 == 0
        parameters = json.loads(node.preset_parameters or "{}")
        parameters["grainUniformity"] = [.5, .625, .75]
        node.preset_parameters = json.dumps(parameters)
        node.preset_film = stock
        expected[node.name] = film_user_settings.snapshot(node), film_node.recipe(node), node.live_preview
    name = tree.name
    with TemporaryDirectory(prefix="recommended-stock-roundtrip-") as folder:
        path = str(Path(folder) / "all-stocks.blend")
        assert bpy.ops.wm.save_as_mainfile(filepath=path) == {"FINISHED"}
        assert bpy.ops.wm.open_mainfile(filepath=path) == {"FINISHED"}
        tree = bpy.data.node_groups[name]
        for node_name, (settings, recipe, live) in expected.items():
            node = tree.nodes[node_name]
            assert film_user_settings.snapshot(node) == settings, node_name
            assert film_node.recipe(node) == recipe, node_name
            assert node.live_preview == live, node_name
    print("PASS 64 stocks retain personal Print, effect, hidden DIR/calibration and Live Preview edits after save/reopen", flush=True)


print(f"Installed stock-default acceptance: {engine.__file__}", flush=True)
assert "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons, "Enable the installed extension first"
with patch.object(engine, "backend_error", return_value=""):
    tree = bpy.data.node_groups.new("Film recommendation acceptance", "CompositorNodeTree")
    node = check_catalog_and_selection(tree)
    check_dir_visibility(node)
    check_old_personal_recipe(tree, node)
if "--pixels" in sys.argv:
    check_pixels(node)
with patch.object(engine, "backend_error", return_value=""):
    check_persistence()
print("PASS installed Film recommendation acceptance" + (" with GPU pixels" if "--pixels" in sys.argv else " (pixels not requested)"), flush=True)
