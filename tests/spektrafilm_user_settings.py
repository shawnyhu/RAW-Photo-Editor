"""Apply a saved Lite look to fresh nodes with both preview states, including old DIR calibration."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from unittest.mock import patch

import bpy

installed = "bl_ext.user_default.raw_photo_editor" in bpy.context.preferences.addons
if installed:
    from bl_ext.user_default.raw_photo_editor import catalog, compositor, film_node, film_preview, film_user_settings, spektrafilm_engine
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from blender_raw import catalog, compositor, film_node, film_preview, film_user_settings, spektrafilm_engine


def run():
    tree = bpy.data.node_groups.new("Shared Lite preset regression", "CompositorNodeTree")
    source = tree.nodes.new(film_node.FILM_TYPE)
    source.film = "fujifilm_velvia_100"
    source.paper = "kodak_2383"
    source.process = "Print only"
    source.grain = True
    source.grain_model = "Grain Synthesis"
    source.scanNegativeInvert = True
    source.cameraDiffusionFamily = "CineBloom"
    source.cameraDiffusionBloomSize = 2.25
    # Emulate a pre-recommendation node: inherited matrices were unset RNA.
    # New nodes now write an entire stock recipe when they are created.
    for key in (*film_node.DIR_MATRIX_PARAMETERS, "grainParticleAreaUm2"):
        source.property_unset(key)
    source.use_stock_calibration = False
    source.preset_film = source.film
    source.preset_parameters = json.dumps({"filmGamma": 1.2, "dirGammaRToGb": [0.07, 0.02],
                                          "grainParticleAreaUm2": 0.65})
    source.dirGammaBToRg = (0.03, 0.04)
    source.preset_name = "Old Macintosh file"
    settings = json.loads(json.dumps(film_user_settings.snapshot(source)))
    assert settings["parameters"] == {"filmGamma": 1.2, "dirGammaRToGb": [0.07, 0.02]}
    assert all(key not in str(settings) for key in ("live_preview", "instance_uid", "preset_name", "status"))
    expected = film_node.recipe(source)
    for live in (False, True):
        target = tree.nodes.new(film_node.FILM_TYPE)
        developer = tree.nodes.new(compositor.DEVELOPER_TYPE)
        tree.links.new(developer.outputs["Image"], target.inputs["Image"])
        developer.auto_exposure = "GLOBAL"
        developer.exposure = 1.25
        target.live_preview = live
        target.preset_name = "Prior look"
        target.status = "Original status"
        target.show_dir = True
        target.film = "kodak_portra_800"
        target.dirGammaSameLayerRgb = (0.5, 0.2, 0.1)
        target.preset_parameters = json.dumps({"rgbToRawMethod": 2})
        original_uid = target.instance_uid
        prior_scan = bpy.data.images.new("Cached target scan", width=3, height=2)
        film_node.apply_result(target, prior_scan)
        with patch.object(film_preview, "request") as request:
            film_user_settings.apply(target, settings)
            request.assert_called_once_with(target)
        assert film_node.recipe(target) == expected
        assert target.live_preview == live and target.instance_uid == original_uid
        assert target.show_dir and target.status == "Original status" and target.preset_name == ""
        assert target.node_tree.nodes["Film Result"].image == prior_scan
        assert target.inputs["Image"].links[0].from_node == developer
        assert developer.auto_exposure == "GLOBAL" and developer.exposure == 1.25
        assert target.is_property_set("dirGammaBToRg") and not target.is_property_set("dirGammaRToGb")
        assert not target.is_property_set("dirGammaSameLayerRgb")
        assert film_user_settings.snapshot(target) == settings
        for section, key, value in (
            ("controls", "live_preview", not live), ("controls", "dirGammaBToRg", [0.1]),
            ("controls", "paper", "unavailable stock"), ("controls", "grain_model", "unknown"),
            ("parameters", "rawTemperature", 4500), ("parameters", "filmGamma", float("nan")),
        ):
            invalid = deepcopy(settings)
            invalid[section][key] = value
            try:
                film_user_settings.apply(target, invalid)
            except ValueError:
                pass
            else:
                raise AssertionError((section, key))
            assert film_user_settings.snapshot(target) == settings and target.live_preview == live
        target.film = "kodak_portra_800"
        assert target.use_stock_calibration and target.is_property_set("dirGammaBToRg")
        assert target.grain and target.grain_model == "Grain Synthesis" and target.process == "Full Film Sim"
        target.film = source.film
        expected_dir = spektrafilm_engine.stock_defaults(source.film)["parameters"]["dirGammaRToGb"]
        assert all(abs(a - b) < 1e-6 for a, b in zip(target.dirGammaRToGb, expected_dir))
        assert list(target.dirGammaRToGb) != list(source.dirGammaRToGb)
        assert not target.cameraDiffusionEnabled and target.cameraDiffusionFamily == "CineBloom"
        assert target.grain and target.grain_model == "Grain Synthesis"
        film_user_settings.apply(target, settings)
        assert film_node.recipe(target) == expected and target.live_preview == live

    # Blender rounds values (e.g. the upper glare limit of 0.2) to float32.
    # Every slider endpoint should be valid both when saving and when applying.
    for bound in ("minimum", "maximum"):
        for key, effect in film_node.BY_KEY.items():
            value = getattr(effect, bound)
            if isinstance(effect.default, tuple):
                value = [value] * len(effect.default)
            elif isinstance(effect.default, bool):
                value = bool(value)
            elif isinstance(effect.default, int):
                value = film_node.FAMILIES[int(value)]
            setattr(source, key, value)
        film_user_settings.apply(target, film_user_settings.snapshot(source))
        assert film_node.recipe(target) == film_node.recipe(source)
    source.film = "Unrecognized free-text stock"
    try:
        film_user_settings.snapshot(source)
    except ValueError as error:
        assert "stock" in str(error)
    else:
        raise AssertionError("Saving an unusable stock name should fail")
    print("PASS Lite user settings, hidden native parameters, DIR inheritance, validation and live preview")


if not installed:
    catalog.register()
    compositor.register()
try:
    # Exercise real RNA and the shipped stock/parameter tables on machines without a GPU.
    with patch.object(spektrafilm_engine, "backend_error", return_value=""):
        run()
finally:
    if not installed:
        compositor.unregister()
        catalog.unregister()
