"""Blender integration for standalone presets, including partial and invalid recipes."""
import json
from contextlib import nullcontext
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import bpy
import numpy as np

installed = "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons
if installed:
    from bl_ext.user_default.raw_photo_editor import catalog, compositor, film_node, spektrafilm_engine
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from blender_raw import catalog, compositor, film_node, spektrafilm_engine


def run(folder):
    tree = bpy.data.node_groups.new("Preset roundtrip", "CompositorNodeTree")
    tree.use_fake_user = True
    node = tree.nodes.new(film_node.FILM_TYPE)
    filepath = folder / "standalone.sfpreset"
    settings = {
        "bools": {"rawLensCorrection": False, "scannerEnabled": True, "halationEnabled": True},
        "ints": {"film": 3, "paper": 6, "rgbToRawMethod": 0, "outputColorSpace": 17, "grainModel": 1},
        "doubles": {"filmExposureEv": -0.25, "printExposureEv": 1.5, "filmGamma": 1.2, "grainParticleAreaUm2": 1},
        "vectors": {"dirGammaBToRg": [0.168, 0.226], "halationStrength": [0.05, 0.015, 0]},
    }

    def write(settings):
        filepath.write_text(json.dumps({"version": 1, "settings": settings}))

    write(settings)
    warnings = film_node.load_preset(node, filepath)
    assert warnings and "Raw Developer" in warnings[0]
    recipe = film_node.recipe(node)
    assert node.film == "kodak_portra_800" and node.paper == "kodak_2383"
    assert node.exposure == node.print_exposure == 0 and node.scannerEnabled and node.halation
    assert node.grain_model == "Production" and recipe["grain_model"] == 1
    assert recipe["parameters"] == {"rgbToRawMethod": 0, "filmGamma": 1.2,
                                     "process": 0, "scanNegativeInvert": False}
    np.testing.assert_allclose(recipe["effects"]["dirGammaBToRg"], settings["vectors"]["dirGammaBToRg"])
    assert recipe["effects"]["grainParticleAreaUm2"] == 1
    np.testing.assert_allclose(recipe["effects"]["halationStrength"], settings["vectors"]["halationStrength"])
    assert not spektrafilm_engine.HOST_PARAMETERS.intersection(recipe["parameters"])
    for bad in ({"ints": {"rgbToRawMethod": -1}}, {"ints": {"film": 1000}},
                {"ints": {"outputRole": 1}}, {"ints": {"printTiming": 1}},
                {"vectors": {"halationStrength": [0, 1]}}, {"ints": {"grainEnabled": 1}},
                {"doubles": {"filmGamma": float("nan")}}, {"ints": {"noSuchField": 0}}):
        write(bad)
        try:
            film_node.load_preset(node, filepath)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
        assert film_node.recipe(node) == recipe

    node.exposure, node.print_exposure = 0.5, -0.5
    node.grain_model = "Grain Synthesis"
    node.halationStrength = (0.55, 0.2, 0.15)
    recipe = film_node.recipe(node)
    assert node.exposure == node.print_exposure == recipe["exposure"] == recipe["print_exposure"] == 0.0
    assert recipe["grain_model"] == 2
    blend = str(folder / "roundtrip.blend")
    tree_name, node_name = tree.name, node.name
    filepath.unlink()
    assert bpy.ops.wm.save_as_mainfile(filepath=blend) == {"FINISHED"}
    assert bpy.ops.wm.open_mainfile(filepath=blend) == {"FINISHED"}
    node = bpy.data.node_groups[tree_name].nodes[node_name]
    assert film_node.recipe(node) == recipe
    if "--no-metal" not in sys.argv:
        x = np.linspace(0.02, 1.0, 64, dtype=np.float32)
        rgb = np.broadcast_to(np.stack([x, x[::-1], x**2], axis=-1), (48, 64, 3)).copy()
        result = spektrafilm_engine.render(rgb, **recipe)
        assert np.isfinite(result).all()
        legacy = {**recipe, "exposure": 1.25, "print_exposure": -0.75,
                  "parameters": {**recipe["parameters"], "filmExposureEv": 2, "printExposureEv": -1}}
        np.testing.assert_array_equal(spektrafilm_engine.render(rgb, **legacy), result)
        alternative = {**recipe, "parameters": {**recipe["parameters"], "rgbToRawMethod": 2}}
        assert np.max(np.abs(result - spektrafilm_engine.render(rgb, **alternative))) > 0.005

    # In version 0.14 these values only existed in the native preset payload.
    for key in ("halationStrength", "grainParticleAreaUm2"):
        node.property_unset(key)
    old = {**recipe["parameters"], "grainParticleAreaUm2": 0.66, "halationStrength": [0.6, 0.4, 0.2]}
    node.preset_parameters = json.dumps(old)
    assert abs(node.grainParticleAreaUm2 - 0.66) < 1e-6
    np.testing.assert_allclose(node.halationStrength, old["halationStrength"])
    node.halationStrength[1] = 0.8
    np.testing.assert_allclose(film_node.recipe(node)["effects"]["halationStrength"], [0.6, 0.8, 0.2])

    write({"bools": {"grainEnabled": True}})
    assert bpy.ops.blender_raw.import_spektrafilm_preset(tree_name=tree_name, node_name=node_name,
                                                        filepath=str(filepath)) == {"FINISHED"}
    assert node.grain and not node.scannerEnabled and not node.halation
    assert node.grain_model == "Preview"
    assert node.exposure == 0 and not node.film
    assert film_node.recipe(node)["parameters"] == {"process": 0, "scanNegativeInvert": False}
    print("PASS preset import, validation, numeric method 0, manual edits, standalone node and .blend roundtrip")


if not installed:
    catalog.register()
    compositor.register()
try:
    # Headless CI can still test native parameter tables and Blender persistence.
    with TemporaryDirectory() as folder, (
        patch.object(spektrafilm_engine, "backend_error", return_value="") if "--no-metal" in sys.argv else nullcontext()
    ):
        run(Path(folder))
finally:
    if not installed:
        compositor.unregister()
        catalog.unregister()
