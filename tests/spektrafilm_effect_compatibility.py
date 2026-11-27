"""Freeze beta.5 color grain/halation outputs before extending grain storage to four layers.

Use the installed beta.5 for `-- record baseline.npz`, then the installed candidate
on the same GPU for `-- compare baseline.npz`. Never overwrites a baseline.
"""
import json
from pathlib import Path
import sys

import bpy
import numpy as np
from bl_ext.user_default.raw_photo_editor import film_node, spektrafilm_engine as engine


def chart():
    pixels = np.random.default_rng(90145).uniform(.005, .9, (32, 48, 3)).astype(np.float32)
    pixels[:8] = np.geomspace(.003, 4, 48)[None, :, None]
    pixels[16:24, 16:32] = 8
    return pixels


def record(path):
    assert not path.exists(), "Never overwrite a recorded baseline"
    films, papers = engine.stocks()
    assert (len(films), len(papers)) == (56, 12), "Record with beta.5 only"
    tree = bpy.data.node_groups.new("Legacy color physical effects", "CompositorNodeTree")
    node = tree.nodes.new(film_node.FILM_TYPE)
    recipes, pixels = [], []
    for index, film in enumerate(films):
        node.film = film
        node.paper = papers[index % len(papers)]
        node.grain = node.halation = True
        node.grainAmount = .65
        for model in film_node.GRAIN_MODELS:
            node.grain_model = model
            recipe = film_node.recipe(node)
            recipes.append(recipe)
            pixels.append(engine.render(chart(), **recipe))
        print(f"Recorded {len(recipes)}/168 color effect recipes", flush=True)
    assert np.isfinite(pixels).all()
    np.savez_compressed(path, pixels=np.stack(pixels), recipes=json.dumps(recipes))
    print("PASS recorded 168 beta.5 color grain and halation recipes", flush=True)


def compare(path):
    with np.load(path) as reference:
        recipes = json.loads(str(reference["recipes"]))
        assert len(recipes) == 168
        for index, recipe in enumerate(recipes):
            pixels = engine.render(chart(), **recipe)
            np.testing.assert_array_equal(pixels, reference["pixels"][index],
                                          err_msg=f"{recipe['film']}, grain model {recipe['grain_model']}")
            if (index + 1) % 24 == 0:
                print(f"Compared {index + 1}/168 color effects", flush=True)
    print("PASS 168 color grain and halation recipes are pixel-exact to beta.5", flush=True)


mode, filename = sys.argv[sys.argv.index("--") + 1:]
print("Installed engine:", engine.__file__, flush=True)
assert not engine.backend_error(), engine.backend_error()
{"record": record, "compare": compare}[mode](Path(filename).resolve())
