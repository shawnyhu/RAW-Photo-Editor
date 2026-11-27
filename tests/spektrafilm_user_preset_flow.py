"""Run write and use in separate Blender processes against the same temporary library."""
from pathlib import Path
import sys
from unittest.mock import patch

import bpy

from bl_ext.user_default.raw_photo_editor import film_node, film_user_library, native_effects, spektrafilm_engine


def run(stage):
    scene = bpy.context.scene
    effect = native_effects.create_effect(scene, film_node.FILM_TYPE)
    node = native_effects.effect_node(effect)
    target = dict(tree_name=node.id_data.name, node_name=node.name)
    ops = bpy.ops.blender_raw
    assert "import_spektrafilm_preset" not in dir(ops)
    if stage == "write":
        assert not film_user_library.list_presets()
        node.film, node.paper = "kodak_portra_800", "kodak_2383"
        node.grain, node.grainAmount = True, .375
        node.halationStrength = (.125, .25, .5)
        node.live_preview = False
        assert ops.save_film_preset(**target, preset_name="柔光 Portra") == {"FINISHED"}
        (_, name), = film_user_library.list_presets()
        assert name == "柔光 Portra" and not node.live_preview
        return

    assert stage == "use"
    (preset_id, name), = film_user_library.list_presets()
    assert name == "柔光 Portra"
    untouched = native_effects.effect_node(native_effects.create_effect(scene, film_node.FILM_TYPE))
    assert untouched != node
    untouched_values = film_node.recipe(untouched)
    assert ops.rename_film_preset(preset_id=preset_id, preset_name="旅行") == {"FINISHED"}
    assert film_user_library.list_presets() == [(preset_id, "旅行")]
    for live in (True, False):
        node.live_preview, node.grainAmount = live, 1.
        assert ops.apply_film_preset(**target, preset_id=preset_id) == {"FINISHED"}
        assert (node.film, node.paper, node.grain, node.grainAmount) == ("kodak_portra_800", "kodak_2383", True, .375)
        assert tuple(node.halationStrength) == (.125, .25, .5)
        assert node.live_preview == live and film_node.recipe(untouched) == untouched_values
    assert ops.delete_film_preset(preset_id=preset_id) == {"FINISHED"}
    assert not film_user_library.list_presets() and node.grainAmount == .375


stage, directory = sys.argv[sys.argv.index("--") + 1:]
with patch.object(film_user_library, "_directory", return_value=Path(directory)), \
     patch.object(spektrafilm_engine, "backend_error", return_value=""):
    run(stage)
print("PASS installed Spektrafilm Lite user preset library", stage)
