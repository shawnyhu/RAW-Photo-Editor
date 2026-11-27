"""Freeze beta.5 recipes/pixels, then check the same recipes with a newer installed ZIP.

Use separate installed profiles on the same GPU:
blender -b --offline-mode --python-exit-code 1 -P tests/spektrafilm_color_compatibility.py -- record baseline.npz
blender -b --offline-mode --python-exit-code 1 -P tests/spektrafilm_color_compatibility.py -- compare baseline.npz
"""
import json
from pathlib import Path
import sys

import bpy
import numpy as np
from bl_ext.user_default.raw_photo_editor import film_node, film_user_library, film_user_settings
from bl_ext.user_default.raw_photo_editor import spektrafilm_engine as engine


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def chart():
    rng = np.random.default_rng(78412)
    image = rng.uniform(.0001, 1.5, (24, 32, 3)).astype(np.float32)
    image[0] = np.geomspace(.001, 4., 32)[:, None]
    return image


def record(path):
    assert not path.exists(), "Never overwrite an existing baseline"
    films, papers = engine.stocks()
    assert (len(films), len(papers)) == (56, 12), "Record with the installed beta.5"
    tree = bpy.data.node_groups.new("Beta5 saved film recipes", "CompositorNodeTree")
    tree.use_fake_user = True
    pairs, pixels, nodes = [], [], {}
    image = chart()
    for film in films:
        node = tree.nodes.new(film_node.FILM_TYPE)
        node.name = film
        node.film = film
        for paper in papers:
            node.paper = paper
            node.process = "Full Film Sim"
            recipe = film_node.recipe(node)
            pairs.append(recipe)
            pixels.append(engine.render(image, **recipe))
        # Preserve both inferred stock defaults and explicit edits from beta.5.
        node.grainAmount = .37
        node.dirGammaRToGb[0] *= .85
        nodes[node.name] = dict(recipe=film_node.recipe(node), settings=film_user_settings.snapshot(node))
        print(f"Recorded {len(pairs)}/672 beta.5 pairs", flush=True)
    for key, name in getattr(film_user_library, "BUILTIN_PRESETS", {}).items():
        node = tree.nodes.new(film_node.FILM_TYPE)
        node.name = "Applied built-in: " + name
        film_user_settings.apply(node, film_user_library.load(key))
        nodes[node.name] = dict(recipe=film_node.recipe(node), settings=film_user_settings.snapshot(node))
    assert np.isfinite(pixels).all()
    np.savez_compressed(path, films=films, papers=papers, pixels=np.stack(pixels),
                        recipes=canonical(pairs), nodes=canonical(nodes), tree=tree.name)
    assert bpy.ops.wm.save_as_mainfile(filepath=str(path.with_suffix(".blend"))) == {"FINISHED"}
    print("PASS recorded 672 beta.5 GPU pairs and saved original nodes", flush=True)


def compare(path):
    with np.load(path) as baseline:
        films, papers = engine.stocks()
        assert list(films[:56]) == baseline["films"].tolist()
        assert list(papers[:12]) == baseline["papers"].tolist()
        recipes = json.loads(str(baseline["recipes"]))
        assert len(recipes) == 672 and baseline["pixels"].shape == (672, *chart().shape)
        image = chart()
        for index, recipe in enumerate(recipes):
            actual = engine.render(image, **recipe)
            np.testing.assert_array_equal(actual, baseline["pixels"][index],
                                          err_msg=f"{recipe['film']} / {recipe['paper']}")
            if (index + 1) % 48 == 0:
                print(f"Compared {index + 1}/672 fixed beta.5 recipes", flush=True)
        expected = json.loads(str(baseline["nodes"]))
        tree_name = str(baseline["tree"])
    assert bpy.ops.wm.open_mainfile(filepath=str(path.with_suffix(".blend"))) == {"FINISHED"}
    tree = bpy.data.node_groups[tree_name]
    for name, values in expected.items():
        node = tree.nodes[name]
        assert canonical(film_node.recipe(node)) == canonical(values["recipe"]), name
        assert canonical(film_user_settings.snapshot(node)) == canonical(values["settings"]), name
    print(f"PASS 672 exact beta.5 GPU pairs; {len(expected)} saved nodes including applied built-ins unchanged", flush=True)


mode, filename = sys.argv[sys.argv.index("--") + 1:]
print("Installed engine:", engine.__file__, flush=True)
if mode == "record-if-available" and (error := engine.backend_error()):
    print("SKIP actual GPU pixels: previous renderer unavailable:", error, flush=True)
    sys.exit(0)
assert not engine.backend_error(), engine.backend_error()
{"record": record, "record-if-available": record, "compare": compare}[mode](Path(filename).resolve())
