"""Verify appended film stocks with the installed extension or a source checkout.

blender -b --offline-mode --python-exit-code 1 -P tests/spektrafilm_added_calibration.py
Add -- --pixels to render charts on Metal or Vulkan.
"""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import bpy
import numpy as np

installed = "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons
if "--installed" in sys.argv:
    assert installed, "Run against an enabled installed ZIP"
if installed:
    from bl_ext.user_default.raw_photo_editor import catalog, compositor, film_node, film_user_settings, spektrafilm_engine as engine
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from blender_raw import catalog, compositor, film_node, film_user_settings, spektrafilm_engine as engine


NEGATIVE = "cinestill_800t"
SLIDE = "fujifilm_provia_400x"
PRINT = "fujifilm_eterna_cp_3513di"
DIR_KEYS = ("dirGammaSameLayerRgb", "dirGammaRToGb", "dirGammaGToRb", "dirGammaBToRg")
# Independently transcribed from The Dyeworks 2.2.3 couplers.toml:
# negative cine daylight, negative cine tungsten, and negative still defaults.
# These catch a generator that accidentally uses Negs' different groups.
DYEWORKS_DIR = {
    "kodak_vision2_50d": ((.5456, .5434, .3766), (.4263, .2781), (.2051, .5015), (.295, .2449)),
    "kodak_vision2_100t": ((.4601, .4433, .4414), (.4313, .3157), (.3145, .389), (.2423, .2506)),
    "kodak_vision2_500t": ((.4601, .4433, .4414), (.4313, .3157), (.3145, .389), (.2423, .2506)),
    "kodak_ultramax_800": ((.5159, .5934, .2829), (.4032, .2488), (.2227, .434), (.1829, .1799)),
}


def close(actual, expected):
    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-6)


def check_stocks(tree, node):
    films, papers = engine.stocks()
    assert (len(films), len(papers)) == (64, 14), (len(films), len(papers))
    assert len(set(films)) == len(films) and len(set(papers)) == len(papers)
    assert NEGATIVE in films[20:] and SLIDE in films[20:] and PRINT in papers[8:]
    assert all(engine.added_stock_calibration(film) is None for film in films[:20])
    for film in films[20:]:
        calibration = engine.added_stock_calibration(film)
        assert calibration is not None, film
        node.film = film
        recipe = film_node.recipe(node)
        assert node.process == ("Negative only" if calibration.reversal else "Full Film Sim"), film
        assert not node.scanNegativeInvert
        for key, value in calibration.parameters.items():
            engine.validate_parameter(key, value)
            target = recipe["effects"] if key in recipe["effects"] else recipe["parameters"]
            close(target[key], value)

    for film, groups in DYEWORKS_DIR.items():
        node.film = film
        calibration = engine.added_stock_calibration(film)
        recipe = film_node.recipe(node)
        for key, expected in zip(DIR_KEYS, groups):
            close(calibration.parameters[key], expected)
            close(getattr(node, key), expected)
            close(recipe["effects"][key], expected)
    # Independent Negs source values and strong/no anti-halation presets.
    node.film = SLIDE
    assert node.process == "Negative only"
    close(node.dirGammaSameLayerRgb, (.1903, .0855, .2202))
    close(node.dirGammaRToGb, (.1204, .1296))
    close(node.dirGammaGToRb, (.0412, .0726))
    close(node.dirGammaBToRg, (.0474, .2045))
    close(node.halationStrength, (.015, .005, 0))

    # Old preset grain must yield to the new stock. A deliberately stored
    # override for this same stock still takes precedence over its default.
    node.preset_film = films[0]
    node.preset_parameters = json.dumps({"grainUniformity": [.25, .3, .35]})
    node.film = NEGATIVE
    assert node.process == "Full Film Sim"
    close(node.halationStrength, (.3, .1, .015))
    parameters = film_node.recipe(node)["parameters"]
    close(parameters["grainUniformity"], (.93, .97, .99))
    close(parameters["grainParticleScaleLayers"], (1, .3, .25))
    close(parameters["grainDensityMin"], (.03, .03, .03))
    areas = node.grainParticleAreaUm2 * np.asarray(parameters["grainParticleScale"])
    close(areas, (.51828355, .77606561, 5.09774039))
    # Editing the selected stock may override its recommendation until the
    # next explicit Film choice or Restore action.
    stored = json.loads(node.preset_parameters)
    stored["grainUniformity"] = [.25, .3, .35]
    node.preset_parameters = json.dumps(stored)
    node.preset_film = NEGATIVE
    close(film_node.recipe(node)["parameters"]["grainUniformity"], (.25, .3, .35))

    node.halationStrength[1] = .23
    node.grainAmount = .67
    node.grainParticleAreaUm2 = .31
    node.paper = PRINT
    close(node.halationStrength, (.3, .23, .015))
    copy = tree.nodes.new(film_node.FILM_TYPE)
    film_user_settings.apply(copy, film_user_settings.snapshot(node))
    assert copy.paper == PRINT and film_node.recipe(copy) == film_node.recipe(node)
    copy.film = SLIDE
    close(copy.halationStrength, (.015, .005, 0))
    close(copy.grainAmount, 1.)
    close(copy.grainParticleAreaUm2, (.09479919 + 1.593925 + .25421191) / 3)
    close(node.grainParticleAreaUm2, .31)
    close(node.halationStrength, (.3, .23, .015))
    copy.process = "Print only"
    copy.scanNegativeInvert = True
    copy.film = NEGATIVE
    assert copy.process == "Full Film Sim" and not copy.scanNegativeInvert
    assert copy.paper == engine.stock_defaults(NEGATIVE)["target_print"]
    print(f"PASS {len(films) - 20} new films, {len(papers) - 8} new prints, stock and preset controls", flush=True)


def check_persistence():
    tree = bpy.data.node_groups.new("Additional stock persistence", "CompositorNodeTree")
    tree.use_fake_user = True
    films, _papers = engine.stocks()
    expected = {}
    for index, film in enumerate(films[20:]):
        node = tree.nodes.new(film_node.FILM_TYPE)
        node.name = film
        node.film = film
        papers = engine.compatible_papers(film)
        node.paper = papers[index % len(papers)] if papers else ""
        node.grainAmount = .67
        node.dirGammaGToRb[0] *= .75
        expected[node.name] = film_user_settings.snapshot(node), film_node.recipe(node)
    name = tree.name
    with TemporaryDirectory(prefix="added-stock-roundtrip-") as folder:
        blend = str(Path(folder) / "stocks.blend")
        assert bpy.ops.wm.save_as_mainfile(filepath=blend) == {"FINISHED"}
        assert bpy.ops.wm.open_mainfile(filepath=blend) == {"FINISHED"}
        tree = bpy.data.node_groups[name]
        for node_name, (settings, recipe) in expected.items():
            node = tree.nodes[node_name]
            assert film_user_settings.snapshot(node) == settings, node_name
            assert film_node.recipe(node) == recipe, node_name
    print(f"PASS {len(expected)} added stocks retain print, calibration and edits after save/reopen", flush=True)


def check_pixels(node):
    image = np.empty((48, 64, 3), dtype=np.float32)
    image[:] = np.geomspace(.005, 2., 64)[:, None]
    image[16:32, :, 1] *= .32
    image[32:, :, 2] *= .17
    film_node.reset_recipe(node)
    outputs = []
    for film, paper in ((NEGATIVE, "kodak_2383"), (NEGATIVE, PRINT), (SLIDE, PRINT)):
        node.film = film
        node.paper = paper
        pixels = engine.render(image, **film_node.recipe(node))
        assert np.isfinite(pixels).all(), (film, paper)
        assert pixels[8, -1].mean() - pixels[8, 0].mean() > .025, (film, paper)
        outputs.append(pixels)
    assert np.max(np.abs(outputs[0] - outputs[1])) > 1e-3, PRINT
    assert np.max(np.abs(outputs[1] - outputs[2])) > 1e-3, SLIDE
    node.film = NEGATIVE
    node.grain = True
    node.grain_model = "Production"
    stock_grain = engine.render(image, **film_node.recipe(node))
    node.grainParticleAreaUm2 = .015
    fine_grain = engine.render(image, **film_node.recipe(node))
    assert np.isfinite(stock_grain).all() and np.isfinite(fine_grain).all()
    assert np.max(np.abs(stock_grain - fine_grain)) > 1e-5
    print("PASS negative, reversal, print and production grain renders on the native GPU", flush=True)


if not installed:
    catalog.register()
    compositor.register()
try:
    tree = bpy.data.node_groups.new("Additional stock calibration", "CompositorNodeTree")
    with patch.object(engine, "backend_error", return_value=""):
        node = tree.nodes.new(film_node.FILM_TYPE)
        check_stocks(tree, node)
    if "--pixels" in sys.argv:
        check_pixels(node)
    with patch.object(engine, "backend_error", return_value=""):
        check_persistence()
finally:
    if not installed:
        compositor.unregister()
        catalog.unregister()
