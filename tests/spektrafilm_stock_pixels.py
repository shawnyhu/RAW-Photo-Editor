"""Record stock-pair output with the previous installed ZIP, then compare on the same GPU.

blender -b --offline-mode --python-exit-code 1 -P tests/spektrafilm_stock_pixels.py -- record reference.npz
blender -b --offline-mode --python-exit-code 1 -P tests/spektrafilm_stock_pixels.py -- compare reference.npz
"""
import sys
from pathlib import Path

import bpy
import numpy as np
from bl_ext.user_default.raw_photo_editor import film_node, spektrafilm_engine as engine


def chart():
    rng = np.random.default_rng(78412)
    image = rng.uniform(.0001, 1.5, (24, 32, 3)).astype(np.float32)
    image[0] = np.geomspace(.001, 4., 32)[:, None]
    return image


def stock_pairs(node, image, films, papers):
    for film in films:
        if hasattr(film_node, "apply_stock_defaults"):
            # Recreate the beta.4/5 recipe explicitly. Selection in beta.6
            # intentionally loads a new full recommendation; saved recipes do not.
            pointer = node.as_pointer()
            film_node._applying_user_preset.add(pointer)
            try:
                for key in ("preset_parameters", "preset_name", "preset_film", "use_stock_calibration",
                            "grain", "grain_model", "halation", "scanNegativeInvert", *film_node.BY_KEY):
                    node.property_unset(key)
                node.film = film
            finally:
                film_node._applying_user_preset.discard(pointer)
        else:
            node.film = film
        for paper in papers:
            node.paper = paper
            node.process = "Full Film Sim"
            yield engine.render(image, **film_node.recipe(node))


def run(mode, path):
    if mode == "record-if-available" and (error := engine.backend_error()):
        print("Previous renderer unavailable on this runner:", error)
        return
    films, papers = engine.stocks()
    print(f"Installed engine: {engine.__file__}; {len(films)} films / {len(papers)} prints", flush=True)
    tree = bpy.data.node_groups.new("Stock pair render comparison", "CompositorNodeTree")
    node = tree.nodes.new(film_node.FILM_TYPE)
    if mode in {"record", "record-if-available"}:
        assert (len(films), len(papers)) == (20, 8), "Record the beta.4 baseline, not the expanded library"
        assert not Path(path).exists(), "Never overwrite a recorded baseline"
        pixels = list(stock_pairs(node, chart(), films, papers))
        assert np.isfinite(pixels).all()
        np.savez_compressed(path, films=films, papers=papers, pixels=np.stack(pixels))
        print("PASS recorded 160 original pairs on the native GPU", flush=True)
    elif mode == "compare":
        with np.load(path) as baseline:
            old_films, old_papers = baseline["films"].tolist(), baseline["papers"].tolist()
            assert (len(old_films), len(old_papers)) == (20, 8), "Expected the original beta.4 stock list"
            assert (len(films), len(papers)) in {(56, 12), (64, 14)}, "Expected the expanded stock list"
            assert list(films[:len(old_films)]) == old_films
            assert list(papers[:len(old_papers)]) == old_papers
            expected = baseline["pixels"]
            assert expected.shape == (160, *chart().shape) and np.isfinite(expected).all()
            for i, pixels in enumerate(stock_pairs(node, chart(), old_films, old_papers)):
                np.testing.assert_array_equal(pixels, expected[i], err_msg=f"{node.film} / {node.paper}")
                if (i + 1) % 40 == 0:
                    print(f"Compared {i + 1}/160 original pairs", flush=True)
            print(f"PASS {len(old_films) * len(old_papers)} original pairs, exact GPU pixels and indices", flush=True)
            # Exercise both sides of the new filter table, including all-new pairs.
            # A missing or misindexed filter can collapse the chart to black or white.
            count = 0
            for group, media in ((films[len(old_films):56], papers[:12]), (old_films, papers[len(old_papers):12])):
                for pixels in stock_pairs(node, chart(), group, media):
                    assert np.isfinite(pixels).all(), (node.film, node.paper)
                    assert np.ptp(pixels[0]) > .01, (node.film, node.paper, "flat gray ramp")
                    count += 1
                    if count % 64 == 0:
                        print(f"Rendered {count}/512 additional pairs", flush=True)
            print(f"PASS {count} additional combinations rendered finite, non-flat charts", flush=True)
    else:
        raise ValueError("Expected record or compare")


run(*sys.argv[sys.argv.index("--") + 1:])
